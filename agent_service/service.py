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

from agent_service import emergency
from agent_service.agents import build_agents
from agent_service.facts import facts_note, merge_facts, text_only, with_note
from agent_service.hospital import HospitalClient
from agent_service.prompts import done_but_no_reply, failure_reply, human_reply
from agent_service.router import Route, route as pick_route
from agent_service.schemas import ChatRequest, ChatResponse
from agent_service.tools import read_tools
from agentcore import RuntimeConfig
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
        self.agents = agents if agents is not None else build_agents(read_tools(hospital))
        self.loop = AgentLoop(client, store, traces, config=self.config, sleep=sleep, rng=rng)
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
                state.pop("pending", None)
                await self.store.save_state(key, state)
                await self._remember(key, session.messages, req.message, reply)
                await self._stop(tracer, "emergency", reply)
                return self._response(reply, "safety", tracer, handoff=True, emergency=True)

            route = await pick_route(
                self.client, tracer, req.message,
                history=session.messages, last_agent=state.get("last_agent"), last_language=state.get("language"),
                pending=state.get("pending"), config=self.config, sleep=self._sleep, rng=self._rng,
            )

            if route.agent == "human":
                reply = human_reply(route.language)
                await self._remember(key, session.messages, req.message, reply)
                await self._save(key, route, ran=None)
                await self._stop(tracer, "human", reply)
                return self._response(reply, "human", tracer, handoff=True)

            agent = self.agents[route.agent]
            data = {"phone": req.phone, "facts": {}}  # the phone is the request's, never the message's
            now = await self._hospital_time() if route.agent == "booking" else None
            note = facts_note(state.get("facts"), now)
            result = await self.loop.run_turn(
                agent, key, with_note(req.message, note), data,
                lock_held=True, history_transform=text_only, tracer=tracer,
            )
            return await self._answer(key, route, agent, result, data, tracer)

    async def _answer(self, key: str, route: Route, agent: AgentSpec, result: LoopResult, data: dict, tracer: Tracer) -> ChatResponse:
        ok = result.status != "failed"
        told = False
        if ok:
            reply, handoff = result.text, result.escalate
        else:
            reply, handoff = self._failure(result, route.language)
            told = bool(result.outcomes)  # the patient now hears about what was saved
        await self._save(key, route, ran=agent.name if ok else None, facts=data["facts"], mark_told=told)
        return self._response(reply, agent.name, tracer, handoff=handoff)

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
        self, key: str, route: Route, *, ran: str | None, facts: dict | None = None, mark_told: bool = False
    ) -> None:
        """Save what the turn learned. Runs after a failed turn too, so ids and outcomes are not lost."""
        try:
            state = (await self.store.load(key)).state  # fresh: the loop saved its own changes
            if facts:
                state["facts"] = merge_facts(state.get("facts"), facts)
            if ran:
                state["last_agent"] = ran
            if not route.fallback:
                state["language"] = route.language
            if mark_told:
                for o in state.get("outcomes", []):
                    o["told"] = True
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
