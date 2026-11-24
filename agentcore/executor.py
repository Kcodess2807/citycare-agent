"""Runs one tool call from start to one definite result.

The order, for every call (docs/design-log.md, 1.6):
    attempt -> a definite failure stops here
            -> an uncertain failure is retried, with the SAME input, if the tool is idempotent
            -> then the tool's `verify` looks at the real state
            -> the result is exactly one of Done, Failed or Unknown.

An `atomic` tool runs as one unit that nothing can cut in the middle: not the turn budget, not a
cancelled request. Its state is saved before it starts and after it ends, so a turn that fails
later still leaves a record of what happened.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from agentcore.config import RetryKind, RuntimeConfig
from agentcore.errors import ToolError, ToolFailed, ToolOutcomeUnknown
from agentcore.results import Done, Failed, ToolResult, Unknown
from agentcore.retry import with_retries
from agentcore.session import SessionStore
from agentcore.tools import Tool, ToolContext, ToolRegistry
from agentcore.trace import Tracer, clip


def _classify(e: Exception) -> RetryKind | None:
    """Which failures a retry policy may retry. A definite failure or a bug in the tool never is."""
    if isinstance(e, ToolFailed):
        return None
    if isinstance(e, ToolError):
        return e.kind  # type: ignore[return-value]  # "retryable" or "unknown"
    return None


class Executor:
    def __init__(
        self,
        config: RuntimeConfig | None = None,
        store: SessionStore | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[float, float], float] = random.uniform,
    ):
        self.config = config or RuntimeConfig()
        self._store, self._sleep, self._rng = store, sleep, rng
        self._background: set[asyncio.Task[Any]] = set()

    async def drain(self) -> None:
        """Wait for atomic units that outlived a cancelled caller. For shutdown and tests."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    # ------------------------------------------------------------ entry points

    async def execute(
        self,
        tool: Tool,
        inp: BaseModel,
        ctx: ToolContext,
        *,
        state: dict[str, Any],
        remaining_s: Callable[[], float] | None = None,
        tracer: Tracer | None = None,
    ) -> ToolResult:
        """`state` is the turn's live state dict. `remaining_s` says how much of the turn budget is left."""
        remaining = remaining_s or (lambda: float("inf"))
        if not tool.atomic:
            if remaining() <= 0:
                return Failed("Out of time before this could start.", escalate=True)
            return await self._attempts(tool, inp, ctx, remaining, tracer)

        reserve = tool.reserve_s if tool.reserve_s is not None else self.config.atomic_reserve_s
        if remaining() < reserve:
            return Failed("Not started: not enough time left to finish it safely. Nothing was changed.", escalate=True)
        unit = asyncio.ensure_future(self._unit(tool, inp, ctx, state, tracer))
        self._background.add(unit)
        unit.add_done_callback(self._background.discard)
        # If the caller is cancelled here, only the wait is cancelled. The unit runs on to its end.
        return await asyncio.shield(unit)

    async def recover_inflight(
        self, registry: ToolRegistry, ctx: ToolContext, *, state: dict[str, Any], tracer: Tracer | None = None
    ) -> ToolResult | None:
        """At the start of a turn: if a unit was interrupted before its result was saved, find out what happened."""
        inflight = state.get("inflight")
        if not inflight:
            return None
        tool = registry.get(inflight.get("tool", ""))
        interrupted = ToolOutcomeUnknown("Interrupted before the result was recorded.")
        if tool is None or tool.verify is None:
            result: ToolResult = Unknown(interrupted.message)
        else:
            try:
                inp = tool.input_model.model_validate(inflight.get("input", {}))
                result = await self._settle(tool, inp, ctx, interrupted, tracer)
            except Exception as e:  # stored input no longer valid, or anything else: do not guess
                result = Unknown(f"Could not check what happened ({type(e).__name__}).")
        await self._record(state, ctx, tracer, inflight.get("tool", "?"), result)
        return result

    # ------------------------------------------------------------ the atomic unit

    async def _unit(self, tool: Tool, inp: BaseModel, ctx: ToolContext, state: dict[str, Any], tracer: Tracer | None) -> ToolResult:
        state["inflight"] = {"tool": tool.name, "input": inp.model_dump(mode="json"), "started_at": time.time()}
        try:
            await self._save(state, ctx, tracer, "before", tool.name)
        except Exception as e:  # not saved, so do not start: we could not say afterwards what was in flight
            state.pop("inflight", None)
            return Failed(f"Not started: could not record it first ({type(e).__name__}). Nothing was changed.", escalate=True)
        result = await self._attempts(tool, inp, ctx, None, tracer)
        await self._record(state, ctx, tracer, tool.name, result)
        return result

    async def _record(self, state: dict[str, Any], ctx: ToolContext, tracer: Tracer | None, tool_name: str, result: ToolResult) -> None:
        state.pop("inflight", None)
        summary = clip(result.value, 500) if isinstance(result, Done) else result.reason
        state.setdefault("outcomes", []).append(
            {"tool": tool_name, "status": result.status, "summary": summary, "told": False, "at": time.time()}
        )
        try:
            await self._save(state, ctx, tracer, "after", tool_name)
        except Exception:  # the outcome stays in the live state, and the trace shows the failed save
            pass

    async def _save(self, state: dict[str, Any], ctx: ToolContext, tracer: Tracer | None, when: str, tool_name: str) -> None:
        try:
            if self._store is not None:
                await self._store.save_state(ctx.session_id, state)
        except Exception as e:
            if tracer is not None:
                await tracer.record("state_saved", when, status="error", output=f"{type(e).__name__}: {e}", extra={"tool": tool_name})
            raise
        if tracer is not None:
            await tracer.record("state_saved", when, extra={"tool": tool_name})

    # ------------------------------------------------------------ attempts, then verify

    async def _attempts(
        self, tool: Tool, inp: BaseModel, ctx: ToolContext, remaining: Callable[[], float] | None, tracer: Tracer | None
    ) -> ToolResult:
        # A tool that is not idempotent is never run twice. It goes straight to verify.
        policy = tool.retry if tool.idempotent else tool.retry.model_copy(update={"max_retries": 0})

        async def attempt(n: int) -> Any:
            return await self._call(tool, tool.name, tool.run(inp, ctx), inp, tracer, n)

        try:
            value = await with_retries(
                attempt,
                policy,
                _classify,
                sleep=self._sleep,
                rng=self._rng,
                can_wait=(lambda delay: remaining() > delay) if remaining is not None else None,
            )
            return Done(value)
        except ToolFailed as e:
            return Failed(e.message)
        except ToolError as e:
            return await self._settle(tool, inp, ctx, e, tracer)
        except Exception as e:  # a bug in the tool: whether it took effect is unknown
            return await self._settle(tool, inp, ctx, ToolOutcomeUnknown(f"The tool raised {type(e).__name__}."), tracer)

    async def _settle(self, tool: Tool, inp: BaseModel, ctx: ToolContext, last_error: ToolError, tracer: Tracer | None) -> ToolResult:
        """The attempts ended without a clear answer. Ask the tool's verify what really happened."""
        if tool.verify is None:
            # Something atomic may have changed. Something that only reads cannot have.
            return Unknown(last_error.message) if tool.atomic else Failed(last_error.message)

        async def check(n: int) -> Any:
            return await self._call(tool, f"{tool.name}:verify", tool.verify(inp, ctx, last_error), inp, tracer, n)

        try:
            result = await with_retries(check, tool.retry, _classify, sleep=self._sleep, rng=self._rng)
        except ToolError as e:
            return Unknown(f"Could not check whether it took effect: {e.message}")
        except Exception as e:
            return Unknown(f"Could not check whether it took effect ({type(e).__name__}).")
        if isinstance(result, (Done, Failed, Unknown)):
            return result
        return Unknown("The check returned nothing usable.")

    async def _call(self, tool: Tool, name: str, coro: Awaitable[Any], inp: BaseModel, tracer: Tracer | None, attempt: int) -> Any:
        timeout = tool.timeout_s if tool.timeout_s is not None else self.config.tool_attempt_timeout_s
        if tracer is None:
            return await self._timed(coro, timeout)
        async with tracer.span("tool_call", name, input=inp.model_dump(mode="json"), attempt=attempt) as sp:
            value = await self._timed(coro, timeout)
            sp.output = value.value if isinstance(value, Done) else value
            if isinstance(value, (Failed, Unknown)):
                sp.status = value.status
            return value

    @staticmethod
    async def _timed(coro: Awaitable[Any], timeout: float) -> Any:
        try:
            return await asyncio.wait_for(coro, timeout)
        except asyncio.TimeoutError:
            raise ToolOutcomeUnknown(f"No answer after {timeout:g}s.") from None
