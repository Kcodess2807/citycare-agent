"""The agent loop: one turn of one agent, from the user's message to a reply, a pause, or a failure.

Built on `client.messages.create` and nothing else. The loop never raises: whatever goes wrong
comes back as a `LoopResult` with status "failed" and a reason code the caller can act on.

What a turn does (docs/flow.md, section 2):
    lock the session -> load it -> settle any interrupted write -> call the model, run its tool
    calls through hooks and the executor, repeat -> commit the history (done / paused) or drop it
    (failed). The session state is saved along the way, so it survives a failed turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic

from agentcore.config import RetryKind, RuntimeConfig
from agentcore.executor import Executor
from agentcore.hooks import (
    Allow,
    Block,
    HookContext,
    HookRunner,
    ModelReply,
    ModelRequest,
    Pause,
    ToolCallEvent,
    ToolDoneEvent,
)
from agentcore.results import Done, Failed, ToolResult, Unknown
from agentcore.retry import with_retries
from agentcore.session import SessionError, SessionStore
from agentcore.tools import ToolContext, ToolRegistry, tool_result_block
from agentcore.trace import TokenUsage, Tracer, TraceStore, cost_usd

OUTCOMES_KEPT = 20  # told outcomes kept in the session state

HistoryTransform = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


# ---------------------------------------------------------------- what the caller gives and gets


@dataclass(frozen=True)
class AgentSpec:
    """A model, a prompt and a set of tools. What the agent is for is the caller's business."""

    name: str
    model: str
    system: str
    tools: ToolRegistry
    max_tokens: int = 4096
    effort: str | None = None  # output_config.effort. Thinking itself is left at the model's default.


@dataclass
class LoopResult:
    status: Literal["done", "paused", "failed"]
    trace_id: str
    text: str = ""  # the reply (done), or what the model said after a pause
    reason_code: str = ""  # failed only: step_limit, budget, model_error, truncated, refused, unhandled_stop, ...
    reason: str = ""  # failed only: a short factual line for logs, not for the end user
    escalate: bool = False  # the caller should hand this to a person
    pending: Any = None  # paused only: the payload of the Pause, stored in session state under "pending"
    outcomes: list[dict[str, Any]] = field(default_factory=list)  # atomic tool outcomes recorded in this turn
    steps: int = 0


class _Fail(Exception):
    def __init__(self, code: str, reason: str, escalate: bool = False):
        super().__init__(reason)
        self.code, self.reason, self.escalate = code, reason, escalate


@dataclass
class _Run:
    """Everything one turn works on. Thrown away at the end."""

    agent: AgentSpec
    session_id: str
    data: Mapping[str, Any]
    state: dict[str, Any]
    messages: list[dict[str, Any]]
    transform: HistoryTransform | None
    tracer: Tracer
    ctx: HookContext
    remaining: Callable[[], float]
    outcomes_before: int = 0
    steps: int = 0
    escalate: bool = False
    pause: Pause | None = None
    pause_step: int = 0
    pause_strikes: int = 0
    calls: Counter = field(default_factory=Counter)


# ---------------------------------------------------------------- model errors


def _classify_model_error(e: Exception) -> RetryKind | None:
    """Retry rate limits, overload, server errors and connection trouble. Never a bad request."""
    if isinstance(e, anthropic.APIStatusError):
        return "retryable" if e.status_code in (408, 409, 429) or e.status_code >= 500 else None
    if isinstance(e, (anthropic.APIConnectionError, asyncio.TimeoutError)):
        return "retryable"
    return None


def _retry_after(e: Exception) -> float | None:
    if isinstance(e, anthropic.APIStatusError):
        try:
            return float(e.response.headers.get("retry-after", ""))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- the loop


class AgentLoop:
    def __init__(
        self,
        client: Any,  # an AsyncAnthropic, or anything with an async messages.create
        store: SessionStore,
        traces: TraceStore,
        *,
        hooks: HookRunner | None = None,
        executor: Executor | None = None,
        config: RuntimeConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[float, float], float] = random.uniform,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config or RuntimeConfig()
        self._client, self._store, self._traces = client, store, traces
        self.hooks = hooks or HookRunner(self.config.hook_timeout_s)
        self.executor = executor or Executor(self.config, store, sleep, rng)
        self._sleep, self._rng, self._clock = sleep, rng, clock

    async def run_turn(
        self,
        agent: AgentSpec,
        session_id: str,
        user_message: str,
        data: Mapping[str, Any] | None = None,
        *,
        lock_held: bool = False,
        history_transform: HistoryTransform | None = None,
    ) -> LoopResult:
        """`lock_held`: the caller already holds the session lock (it is not re-entrant), so do not take it.

        `history_transform`: applied to the turn's messages just before they are committed, and only then.
        The model sees the full messages during the turn; the store gets the transformed list.
        """
        tracer = Tracer(self._traces, session_id, field_limit=self.config.trace_field_chars)
        result: LoopResult | None = None
        try:
            async with contextlib.nullcontext() if lock_held else self._store.lock(session_id):
                session = await self._store.load(session_id)
                deadline = self._clock() + self.config.turn_budget_s
                data = data or {}
                run = _Run(
                    agent=agent,
                    session_id=session_id,
                    data=data,
                    state=session.state,
                    messages=session.messages,
                    transform=history_transform,
                    tracer=tracer,
                    ctx=HookContext.for_turn(session_id, tracer.trace_id, data, session.state),
                    remaining=lambda: deadline - self._clock(),
                )
                try:
                    result = await self._drive(run, user_message)
                except _Fail as f:
                    result = self._failed(run, f.code, f.reason, f.escalate)
                except Exception as e:  # the loop never raises
                    result = self._failed(run, "internal", f"Internal error ({type(e).__name__}).")
        except Exception as e:  # the lock, or loading the session, broke
            result = LoopResult("failed", tracer.trace_id, reason_code="internal", reason=f"Internal error ({type(e).__name__}).")
        try:
            await tracer.record("stop", result.status, status=result.reason_code or "ok", output=result.reason or result.text)
        except Exception:
            pass
        return result

    def _failed(self, run: _Run, code: str, reason: str, escalate: bool) -> LoopResult:
        outcomes = [dict(o) for o in run.state.get("outcomes", [])[run.outcomes_before :]]
        return LoopResult(
            "failed", run.tracer.trace_id, reason_code=code, reason=reason, escalate=escalate or run.escalate,
            outcomes=outcomes, steps=run.steps,
        )

    # ------------------------------------------------------------ one turn

    async def _drive(self, run: _Run, user_message: str) -> LoopResult:
        agent, state = run.agent, run.state
        run.outcomes_before = len(state.get("outcomes", []))
        await self.executor.recover_inflight(agent.tools, ToolContext(run.session_id, run.data), state=state, tracer=run.tracer)
        run.messages.append({"role": "user", "content": self._user_content(state, user_message)})

        max_tokens_retried = False
        tokens = agent.max_tokens
        while True:
            if run.steps >= self.config.step_limit:
                raise _Fail("step_limit", f"No reply after {self.config.step_limit} model calls.")
            if run.remaining() <= 0:
                raise _Fail("budget", "The time budget for this turn ran out.")
            run.steps += 1

            request = self._request(agent, run.messages, tokens)
            await self._check("before_model", run, ModelRequest(run.steps, agent.model, request))
            response = await self._call_model(run, request)
            await self._check("after_model", run, ModelReply(run.steps, agent.model, response))

            stop = response.stop_reason
            if stop == "max_tokens":
                # Never use a cut-off reply or a cut-off tool call: drop it and try once with more room.
                bigger = min(tokens * 2, self.config.max_tokens_retry_cap)
                if max_tokens_retried or bigger <= tokens:
                    raise _Fail("truncated", "The reply was cut off at the token limit, even after a retry.", escalate=True)
                max_tokens_retried, tokens = True, bigger
                continue
            blocks = [b.model_dump(mode="json", exclude_none=True) for b in response.content]
            if stop in ("end_turn", "stop_sequence"):
                return await self._finish(run, blocks)
            if stop == "tool_use":
                run.messages.append({"role": "assistant", "content": blocks})
                run.messages.append({"role": "user", "content": await self._run_tools(run, blocks)})
            elif stop == "pause_turn":
                run.messages.append({"role": "assistant", "content": blocks})
            elif stop == "refusal":
                raise _Fail("refused", "The model refused to answer.", escalate=True)
            else:  # including "model_context_window_exceeded": fail closed
                raise _Fail("unhandled_stop", f"Unhandled stop reason: {stop!r}.", escalate=True)

    def _user_content(self, state: dict[str, Any], user_message: str) -> Any:
        untold = [o for o in state.get("outcomes", []) if not o.get("told")]
        if not untold:
            return user_message
        lines = "\n".join(f"- {o['tool']}: {o['status']} - {o['summary']}" for o in untold)
        note = f"(System note, not from the user. Earlier actions you have not been told about yet:\n{lines})"
        return [{"type": "text", "text": note}, {"type": "text", "text": user_message}]

    @staticmethod
    def _request(agent: AgentSpec, messages: list[dict[str, Any]], max_tokens: int) -> dict[str, Any]:
        request: dict[str, Any] = {"model": agent.model, "max_tokens": max_tokens, "messages": list(messages)}
        if agent.system:
            # One breakpoint after the system prompt caches the tool list and the prompt together.
            request["system"] = [{"type": "text", "text": agent.system, "cache_control": {"type": "ephemeral"}}]
        tools = agent.tools.schemas()
        if tools:
            request["tools"] = tools
        if agent.effort:
            request["output_config"] = {"effort": agent.effort}
        return request

    async def _check(self, point: Any, run: _Run, event: Any) -> None:
        decision = await self.hooks.run(point, run.ctx, event, run.tracer)
        if isinstance(decision, (Block, Pause)):  # the runner already turned a Pause here into a Block
            raise _Fail("hook_blocked", decision.reason)

    async def _call_model(self, run: _Run, request: dict[str, Any]) -> Any:
        agent = run.agent

        async def attempt(n: int) -> Any:
            timeout = min(self.config.model_timeout_s, run.remaining())
            if timeout <= 0:
                raise _Fail("budget", "The time budget for this turn ran out.")
            async with run.tracer.span(
                "model_call", agent.model, attempt=n,
                input={"step": run.steps, "max_tokens": request["max_tokens"], "messages": len(request["messages"]),
                       "last": request["messages"][-1]},
            ) as sp:
                response = await asyncio.wait_for(self._client.messages.create(**request), timeout)
                sp.usage = TokenUsage.from_sdk(response.usage)
                sp.cost_usd = cost_usd(agent.model, sp.usage)  # priced by the model we asked for
                sp.output = [b.type for b in response.content]
                sp.extra = {"stop_reason": response.stop_reason, "step": run.steps}
                return response

        try:
            return await with_retries(
                attempt, self.config.model_retry, _classify_model_error,
                sleep=self._sleep, rng=self._rng, can_wait=lambda d: run.remaining() > d, min_delay=_retry_after,
            )
        except _Fail:
            raise
        except Exception as e:
            code = getattr(e, "status_code", None)
            raise _Fail("model_error", f"The model call failed ({type(e).__name__}{f' {code}' if code else ''}).") from e

    # ------------------------------------------------------------ tool calls

    async def _run_tools(self, run: _Run, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One tool_result for every tool_use, in order, whatever happens."""
        results: list[dict[str, Any]] = []
        struck = False
        for block in (b for b in blocks if b.get("type") == "tool_use"):
            try:
                if run.pause is not None and run.pause_step != run.steps:
                    # The turn is paused: only text is allowed. A second attempt fails the turn.
                    if not struck:
                        run.pause_strikes += 1
                        struck = True
                        if run.pause_strikes >= 2:
                            raise _Fail("paused_misuse", "The model kept calling tools after the turn was paused.", escalate=True)
                    result: ToolResult = Failed("Not run: the turn is paused. Reply with text only.")
                elif run.pause is not None:
                    result = Failed("Skipped: an earlier call in this step paused the turn.")
                else:
                    result = await self._one_call(run, block)
            except _Fail:
                raise
            except Exception as e:  # a bug must not leave a tool_use without its result
                result = Failed(f"The call could not be completed ({type(e).__name__}).")
            results.append(tool_result_block(block["id"], result))
        return results

    async def _one_call(self, run: _Run, block: dict[str, Any]) -> ToolResult:
        name, raw = block["name"], block.get("input")
        parsed = run.agent.tools.parse(name, raw)
        if isinstance(parsed, Failed):
            return parsed  # unknown tool or invalid input: the model reads the message and tries again
        tool, inp = parsed

        key = (name, json.dumps(raw, sort_keys=True, default=str))
        run.calls[key] += 1
        if run.calls[key] >= self.config.identical_call_limit:
            return Failed(f"Not run: this exact call was already made {run.calls[key] - 1} times this turn.")

        decision = await self.hooks.run("before_tool", run.ctx, ToolCallEvent(tool, block["id"], inp), run.tracer)
        if isinstance(decision, Block):
            return Failed(decision.reason)
        if isinstance(decision, Pause):
            self._hold_pause(run, decision)
            return Failed(decision.reason)

        result = await self.executor.execute(
            tool, inp, ToolContext(run.session_id, run.data), state=run.state, remaining_s=run.remaining, tracer=run.tracer
        )
        if isinstance(result, Unknown) or (isinstance(result, Failed) and result.escalate):
            run.escalate = True
        after = await self.hooks.run("after_tool", run.ctx, ToolDoneEvent(tool, block["id"], inp, result), run.tracer)
        if isinstance(after, (Block, Pause)):
            return Failed(after.reason)
        return result

    def _hold_pause(self, run: _Run, pause: Pause) -> None:
        """Remember the pause for the end of the turn. Nothing is saved yet: see `_finish`."""
        try:
            json.dumps(pause.payload)
        except (TypeError, ValueError) as e:
            raise _Fail("internal", f"The paused action is not JSON-serialisable: {e}") from e
        run.pause, run.pause_step = pause, run.steps

    # ------------------------------------------------------------ ending a turn

    async def _finish(self, run: _Run, blocks: list[dict[str, Any]]) -> LoopResult:
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if not text:
            raise _Fail("empty_reply", "The model ended its turn without any text.")
        run.messages.append({"role": "assistant", "content": blocks})
        state, sid = run.state, run.session_id

        # Everything recorded so far is now in the stored history or has been reported to the model.
        new_outcomes = [dict(o) for o in state.get("outcomes", [])[run.outcomes_before :]]
        for outcome in state.get("outcomes", []):
            outcome["told"] = True
        if "outcomes" in state:
            state["outcomes"] = state["outcomes"][-OUTCOMES_KEPT:]

        if run.pause is None:
            await self._commit(run)
            try:
                await self._store.save_state(sid, state)
            except Exception:  # worst case the next turn repeats a note it already has
                pass
            return LoopResult("done", run.tracer.trace_id, text=text, escalate=run.escalate, outcomes=new_outcomes, steps=run.steps)

        # Paused. The pending action is stored only together with the history that holds its read-back:
        # a reply that never reached the user must not leave something behind that a later "yes" could confirm.
        missing = object()
        before = state.get("pending", missing)
        state["pending"] = run.pause.payload
        try:
            await self._store.save_state(sid, state)
        except Exception as e:
            self._put_back(state, before, missing)
            raise _Fail("internal", f"The paused action could not be stored ({type(e).__name__}).") from e
        try:
            await self._commit(run)
        except _Fail:
            self._put_back(state, before, missing)
            try:
                await self._store.save_state(sid, state)
            except Exception:
                pass
            raise
        return LoopResult(
            "paused", run.tracer.trace_id, text=text, escalate=run.escalate, pending=run.pause.payload,
            outcomes=new_outcomes, steps=run.steps,
        )

    async def _commit(self, run: _Run) -> None:
        messages = run.messages
        if run.transform:
            try:
                messages = run.transform(messages)
            except Exception as e:  # a broken transform must fail the turn, not store something half-changed
                raise _Fail("history_invalid", f"The conversation could not be prepared for storage ({type(e).__name__}).") from e
        try:
            await self._store.commit_history(run.session_id, messages)
        except SessionError as e:
            raise _Fail("history_invalid", f"The conversation could not be stored: {e}") from e

    @staticmethod
    def _put_back(state: dict[str, Any], before: Any, missing: Any) -> None:
        if before is missing:
            state.pop("pending", None)
        else:
            state["pending"] = before
