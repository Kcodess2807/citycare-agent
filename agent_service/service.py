"""One /chat turn: emergency check -> router -> specialist (or a fixed human reply) -> a ChatResponse.

`handle` never raises and always returns all five contract fields, so /chat is always HTTP 200.
Everything it does for one patient runs under that patient's session lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_service import confirm, emergency
from agent_service.agents import build_agents
from agent_service.facts import facts_note, merge_facts, text_only, with_note
from agent_service.gate import Checked, Gate, Refused
from agent_service.hospital import HospitalClient
from agent_service.prompts import done_but_no_reply, failure_reply, human_reply, staff_reply
from agent_service.router import Route, route as pick_route
from agent_service.schemas import ChatRequest, ChatResponse
from agent_service.tools import read_tools
from agent_service.write_tools import write_tools
from agentcore import Done, Failed, HookRunner, RuntimeConfig, ToolContext, Unknown
from agentcore.loop import AgentLoop, AgentSpec, LoopResult
from agentcore.session import InMemorySessionStore, SessionStore
from agentcore.trace import InMemoryTraceStore, Tracer, TraceStore


def session_key(phone: str, session_id: str) -> str:
    """Where a conversation is stored. The phone is part of the key.

    One session id used by two phones is two conversations: the second caller gets an empty one and
    cannot see, change or wipe the first caller's history, facts or pending action. Hashed, so a
    phone number never appears in a storage key or a log line.
    """
    return hashlib.sha256(json.dumps([phone, session_id]).encode()).hexdigest()


class ChatService:
    def __init__(
        self,
        client: Any,  # an AsyncAnthropic, or anything with an async messages.create
        store: SessionStore,
        traces: TraceStore,
        hospital: HospitalClient,
        *,
        agents: dict[str, AgentSpec] | None = None,
        config: RuntimeConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[float, float], float] = random.uniform,
    ):
        self.config = config or RuntimeConfig()
        self.client, self.store, self.traces, self.hospital = client, store, traces, hospital
        self.writes = write_tools(hospital)
        self.gate = Gate(hospital, sleep, rng)
        hooks = HookRunner(self.config.hook_timeout_s)
        self.gate.install(hooks)  # before the agents: build_agents refuses write tools without it
        self.agents = agents if agents is not None else build_agents(read_tools(hospital), self.writes, hooks=hooks)
        self.loop = AgentLoop(client, store, traces, hooks=hooks, config=self.config, sleep=sleep, rng=rng)
        self._sleep, self._rng = sleep, rng

    async def handle(self, req: ChatRequest) -> ChatResponse:
        tracer = Tracer(self.traces, req.session_id, field_limit=self.config.trace_field_chars)
        try:
            return await self._turn(req, tracer)
        except Exception as e:  # the last resort: whatever broke, the patient gets a person
            await self._stop(tracer, "internal", f"Unexpected {type(e).__name__}.", status="error")
            return ChatResponse(reply=human_reply("en"), agent="human", handoff=True, emergency=False, trace_id=tracer.trace_id)

    # ------------------------------------------------------------ one turn

    async def _turn(self, req: ChatRequest, tracer: Tracer) -> ChatResponse:
        key = session_key(req.phone, req.session_id)
        async with self.store.lock(key):
            session = await self.store.load(key)
            state = session.state

            reply = emergency.check_emergency(req.message)  # before the router, before anything
            if reply:
                return await self._emergency(key, session.messages, state, req.message, reply, tracer)

            # Whatever the patient says next, a pending action is spent: it is confirmed now or never.
            pending = state.pop("pending", None)
            if pending is not None:
                await self.store.save_state(key, state)
                if isinstance(pending, dict) and confirm.is_yes(req.message, pending.get("tool")):
                    return await self._confirmed(key, session.messages, state, pending, req, tracer)

            route = await pick_route(
                self.client, tracer, req.message,
                history=session.messages, last_agent=state.get("last_agent"), last_language=state.get("language"),
                pending=state.get("pending"), config=self.config, sleep=self._sleep, rng=self._rng,
            )

            if route.emergency:  # the router's second look, for what the word lists miss. Same path as the code check
                return await self._emergency(key, session.messages, state, req.message, emergency.reply_for(route.language), tracer)

            if route.agent == "human":
                reply = human_reply(route.language)
                await self._remember(key, session.messages, req.message, reply)
                await self._save(key, route, ran=None, strikes=0)
                await self._stop(tracer, "human", reply)
                return self._response(reply, "human", tracer, handoff=True)

            agent = self.agents[route.agent]
            # The phone is the request's, never the message's. `links` and `facts` are per-session records that
            # tools write into during the turn and `_save` stores after it, failed turns included.
            data = {
                "phone": req.phone, "facts": {}, "links": dict(state.get("links") or {}),
                "language": route.language, "agent": route.agent, "gate": {}, "handoff": {},
            }
            now = await self._hospital_time() if route.agent == "booking" else None
            note = facts_note(state.get("facts"), now)
            result = await self.loop.run_turn(
                agent, key, with_note(req.message, note), data,
                lock_held=True, history_transform=self._history(data), tracer=tracer,
            )
            return await self._answer(key, route, agent, result, data, tracer, int(state.get("strikes") or 0))

    async def _emergency(
        self, key: str, messages: list[dict[str, Any]], state: dict[str, Any], message: str, reply: str, tracer: Tracer
    ) -> ChatResponse:
        """Stop everything: a pending write is dropped, nothing runs, the fixed reply goes out and staff are flagged."""
        state.pop("pending", None)
        state["strikes"] = 0
        await self.store.save_state(key, state)
        await self._remember(key, messages, message, reply)
        await self._stop(tracer, "emergency", reply)
        return self._response(reply, "safety", tracer, handoff=True, emergency=True)

    async def _answer(
        self, key: str, route: Route, agent: AgentSpec, result: LoopResult, data: dict, tracer: Tracer, strikes_before: int = 0
    ) -> ChatResponse:
        ok = result.status != "failed"
        told = False
        if ok:
            # A paused turn is a write waiting for a yes: the patient reads the code's read-back, not the model's words.
            reply = result.pending["readback"] if result.status == "paused" else result.text
            handoff = result.escalate
        else:
            reply, handoff = self._failure(result, route.language)
            told = bool(result.outcomes)  # the patient now hears about what was saved
        kind = data["handoff"].get("kind")
        if kind:  # the 4-hour rule, an unreachable clock or contact_staff: code sets the handoff and the words
            reply, handoff = staff_reply(kind, route.language), True
            await self._stop(tracer, f"handoff:{kind}", data["handoff"].get("reason") or reply)
        # Two strikes (2.8): a turn that failed, or whose router answer was unusable, counts. A good turn resets.
        strikes = strikes_before + 1 if (not ok or route.fallback) else 0
        if strikes >= 2:
            handoff, strikes = True, 0
            if not (kind or told):  # told: the reply already says something was saved, keep it
                reply = human_reply(route.language)
            await self._stop(tracer, "handoff:strikes", "Two turns in a row failed.")
        await self._save(key, route, ran=agent.name if ok else None, facts=data["facts"], links=data["links"], mark_told=told, strikes=strikes)
        return self._response(reply, agent.name, tracer, handoff=handoff)

    @staticmethod
    def _history(data: dict[str, Any]):
        """Text-only history, with the code's read-back in place of the model's words when a write paused."""

        def transform(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out = text_only(messages)
            shown = data["gate"].get("readback")
            if shown and out and out[-1]["role"] == "assistant":
                out[-1] = {"role": "assistant", "content": shown}
            return out

        return transform

    async def _confirmed(
        self, key: str, messages: list[dict[str, Any]], state: dict[str, Any], pending: dict[str, Any],
        req: ChatRequest, tracer: Tracer,
    ) -> ChatResponse:
        """A strict yes: code runs the stored action itself and the reply comes from the real result (design-log 3.2)."""
        language = pending.get("language") or state.get("language") or "en"
        agent = pending.get("agent") or "booking"
        data = {
            "phone": req.phone, "facts": {}, "links": dict(state.get("links") or {}),
            "language": language, "agent": agent, "gate": {}, "handoff": {}, "idempotency_key": pending.get("key"),
        }
        handoff = False
        tool = self.writes.get(str(pending.get("tool")))
        try:
            inp = tool.input_model.model_validate(pending.get("params")) if tool else None
        except Exception:
            inp = None
        if tool is None or inp is None:  # a stored action we cannot read: nothing runs, a person looks
            reply, handoff = confirm.unknown_reply(language), True
        else:
            # Fetched again: the patient said yes to what the records said then. If they say something else now, stop.
            checked = await self.gate.check(tool.name, inp, data)
            if isinstance(checked, Refused) and checked.handoff:  # the slot moved inside 4 hours since the read-back
                reply, handoff = staff_reply(checked.handoff, language), True
            elif not isinstance(checked, Checked) or checked.display != pending.get("display"):
                reply = confirm.changed_reply(language)
            else:
                result = await self.loop.executor.execute(tool, inp, ToolContext(key, data), state=state, tracer=tracer)
                if isinstance(result, Done):
                    reply = confirm.done_reply(tool.name, pending["display"], result.value, language)
                elif isinstance(result, Unknown):
                    reply, handoff = confirm.unknown_reply(language), True
                else:
                    reply = confirm.failed_reply(tool.name, result.reason, language)
                    handoff = isinstance(result, Failed) and result.escalate
        state["links"] = data["links"]
        state["strikes"] = 0
        for outcome in state.get("outcomes", []):
            outcome["told"] = True  # the patient is told by this reply, so the model need not repeat it next turn
        await self.store.save_state(key, state)
        await self._remember(key, messages, req.message, reply)
        await self._stop(tracer, "confirmed", reply)
        return self._response(reply, agent, tracer, handoff=handoff)

    def _failure(self, result: LoopResult, language: str) -> tuple[str, bool]:
        statuses = {o.get("status") for o in result.outcomes}
        if "unknown" in statuses:  # we don't know what was saved: a person has to look
            return human_reply(language), True
        if "done" in statuses:
            return done_but_no_reply(language), result.escalate
        if result.escalate:
            return human_reply(language), True
        return failure_reply(language), False

    # ------------------------------------------------------------ small helpers

    async def _hospital_time(self) -> dict[str, Any] | None:
        try:
            return await self.hospital.now()
        except Exception:  # without it the booking agent has no date and will say so
            return None

    async def _remember(self, key: str, messages: list[dict[str, Any]], user_text: str, reply: str) -> None:
        """Add an exchange that did not go through a specialist to the stored history."""
        try:
            await self.store.commit_history(
                key, [*messages, {"role": "user", "content": with_note(user_text, "")}, {"role": "assistant", "content": reply}]
            )
        except Exception:  # losing one exchange is better than losing the reply
            pass

    async def _save(
        self, key: str, route: Route, *, ran: str | None, facts: dict | None = None, links: dict | None = None,
        mark_told: bool = False, strikes: int | None = None,
    ) -> None:
        """Save what the turn learned. Runs after a failed turn too, so ids and outcomes are not lost."""
        try:
            state = (await self.store.load(key)).state  # fresh: the loop saved its own changes
            if facts:
                state["facts"] = merge_facts(state.get("facts"), facts)
            if links is not None:
                state["links"] = links
            if ran:
                state["last_agent"] = ran
            if not route.fallback:
                state["language"] = route.language
            if mark_told:
                for o in state.get("outcomes", []):
                    o["told"] = True
            if strikes is not None:
                state["strikes"] = strikes
            await self.store.save_state(key, state)
        except Exception:
            pass

    @staticmethod
    async def _stop(tracer: Tracer, name: str, output: str, status: str = "ok") -> None:
        try:
            await tracer.record("stop", name, status=status, output=output)
        except Exception:
            pass

    @staticmethod
    def _response(reply: str, agent: str, tracer: Tracer, *, handoff: bool = False, emergency: bool = False) -> ChatResponse:
        return ChatResponse(reply=reply, agent=agent, handoff=handoff, emergency=emergency, trace_id=tracer.trace_id)


def build_service() -> ChatService:
    """The real wiring: the Anthropic client (the loop owns retries), in-memory sessions, JSONL traces."""
    import anthropic

    hospital = HospitalClient()
    traces = InMemoryTraceStore(path=Path("traces") / "traces.jsonl")
    return ChatService(anthropic.AsyncAnthropic(max_retries=0), InMemorySessionStore(), traces, hospital)
