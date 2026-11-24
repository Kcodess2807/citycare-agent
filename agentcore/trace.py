"""Full trace of a turn: every model call, tool call, hook decision and state save, with time, tokens and cost.

A `Tracer` belongs to one turn. It writes events to a `TraceStore`, which is separate from the
session store so traces outlive the session.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

EventType = Literal["model_call", "tool_call", "hook_decision", "state_saved", "stop"]

# The key must never reach a trace. Scrubbed on the way in, as a second line of defence.
_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


# ---------------------------------------------------------------- tokens and cost


class TokenUsage(BaseModel):
    input_tokens: int = 0  # uncached input only, as the API reports it
    output_tokens: int = 0  # includes thinking tokens
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0  # informational, already inside output_tokens

    @classmethod
    def from_sdk(cls, usage: Any) -> TokenUsage:
        """Read an SDK usage object. Fields the SDK leaves out or sets to None count as 0."""
        details = getattr(usage, "output_tokens_details", None)
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            thinking_tokens=getattr(details, "thinking_tokens", 0) or 0,
        )


class Price(BaseModel):
    """US dollars per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float


# Input and output prices are from context.md (models and prices table).
# Cache prices are NOT in context.md:
#   - cache_read for sonnet and opus comes from the claude-api skill's model table.
#   - cache_read for haiku (ASSUMED 0.1x input) and every cache_write (ASSUMED 1.25x input,
#     5 minute cache) are assumptions. Check them on the pricing page before quoting costs.
# Haiku 5.5 doubles above 100K prompt tokens: ignored, our prompts are far below that.
PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.10, cache_write=2.50),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20, cache_write=5.00),
}


def cost_usd(model: str, usage: TokenUsage, prices: dict[str, Price] = PRICES) -> float | None:
    """Cost of one call, or None for a model we have no price for. We never guess a price."""
    p = prices.get(model)
    if p is None:
        return None
    return (
        usage.input_tokens * p.input
        + usage.output_tokens * p.output
        + usage.cache_read_tokens * p.cache_read
        + usage.cache_write_tokens * p.cache_write
    ) / 1_000_000


# ---------------------------------------------------------------- events


def clip(value: Any, limit: int) -> str:
    """Turn a value into a short string for the trace: JSON, key scrubbed, cut at `limit` characters."""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    text = _KEY_PATTERN.sub("[redacted]", text)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated {len(text) - limit} chars]"


class TraceEvent(BaseModel):
    trace_id: str
    session_id: str
    seq: int
    type: EventType
    name: str = ""  # model id, tool name or hook name
    ts: float  # unix time at the start of the event
    duration_ms: float = 0.0
    status: str = "ok"  # "ok", "error", or a result status such as "blocked"
    attempt: int = 1
    input: str = ""
    output: str = ""
    usage: TokenUsage | None = None
    cost_usd: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- stores


class TraceStore(Protocol):
    async def append(self, event: TraceEvent) -> None: ...

    async def get(self, session_id: str) -> list[TraceEvent]: ...


class InMemoryTraceStore:
    """Events per session in memory, least recently used sessions dropped past `max_sessions`.

    With `path` set, every event is also appended to that JSONL file. The file is never trimmed
    and holds personal data, so keep it out of git. `get` falls back to it for sessions evicted
    from memory or from before a restart.
    """

    def __init__(self, max_sessions: int = 1000, path: Path | None = None):
        self._sessions: OrderedDict[str, list[TraceEvent]] = OrderedDict()
        self._max = max_sessions
        self._path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    async def append(self, event: TraceEvent) -> None:
        events = self._sessions.setdefault(event.session_id, [])
        events.append(event)
        self._sessions.move_to_end(event.session_id)
        while len(self._sessions) > self._max:
            self._sessions.popitem(last=False)
        if self._path is not None:
            # TODO: blocking write inside async code, a few hundred bytes. Use a thread or a queue if it shows up in latency.
            with self._path.open("a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")

    async def get(self, session_id: str) -> list[TraceEvent]:
        if session_id in self._sessions:
            return list(self._sessions[session_id])
        if self._path is None or not self._path.exists():
            return []
        found = []
        with self._path.open(encoding="utf-8") as f:
            for line in f:
                if session_id in line:  # cheap pre-filter, the exact check is below
                    event = TraceEvent.model_validate_json(line)
                    if event.session_id == session_id:
                        found.append(event)
        return found


# ---------------------------------------------------------------- totals


class TraceSummary(BaseModel):
    """Totals over a list of events, normally every turn of one session."""

    turns: int = 0  # distinct trace ids
    model_calls: int = 0
    tool_calls: int = 0
    errors: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0  # only calls we have a price for
    unpriced_calls: int = 0  # model calls with tokens but no price, so cost_usd is a lower bound if > 0
    duration_ms: float = 0.0  # sum of model and tool call durations


def summarize(events: list[TraceEvent]) -> TraceSummary:
    total = TraceSummary(turns=len({e.trace_id for e in events}))
    for e in events:
        total.errors += e.status == "error"
        if e.type == "tool_call":
            total.tool_calls += 1
        elif e.type == "model_call":
            total.model_calls += 1
        else:
            continue
        total.duration_ms += e.duration_ms
        if e.usage is not None:
            for field in TokenUsage.model_fields:
                setattr(total.usage, field, getattr(total.usage, field) + getattr(e.usage, field))
            if e.cost_usd is None:
                total.unpriced_calls += 1
        if e.cost_usd is not None:
            total.cost_usd += e.cost_usd
    return total


# ---------------------------------------------------------------- tracer


class Span:
    """One timed event. Use `async with tracer.span(...) as sp`, set sp.output / sp.status / sp.usage inside."""

    def __init__(self, tracer: Tracer, type: EventType, name: str, input: Any, attempt: int):
        self._tracer = tracer
        self.type, self.name, self.attempt = type, name, attempt
        self.input: Any = input
        self.output: Any = ""
        self.status = "ok"
        self.usage: TokenUsage | None = None
        self.cost_usd: float | None = None
        self.extra: dict[str, Any] = {}

    async def __aenter__(self) -> Span:
        self._ts = time.time()
        self._t0 = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.status = "error"
            self.output = self.output or f"{exc_type.__name__}: {exc}"
        await self._tracer.record(
            self.type,
            self.name,
            ts=self._ts,
            duration_ms=(time.monotonic() - self._t0) * 1000,
            status=self.status,
            attempt=self.attempt,
            input=self.input,
            output=self.output,
            usage=self.usage,
            cost_usd=self.cost_usd,
            extra=self.extra,
        )
        return False  # never swallow the exception


class Tracer:
    def __init__(self, store: TraceStore, session_id: str, trace_id: str | None = None, field_limit: int = 2000):
        self.store = store
        self.session_id = session_id
        self.trace_id = trace_id or uuid.uuid4().hex
        self._limit = field_limit
        self._seq = 0

    async def record(self, type: EventType, name: str = "", **fields: Any) -> TraceEvent:
        fields.setdefault("ts", time.time())
        for key in ("input", "output"):
            if key in fields:
                fields[key] = clip(fields[key], self._limit)
        self._seq += 1
        event = TraceEvent(trace_id=self.trace_id, session_id=self.session_id, seq=self._seq, type=type, name=name, **fields)
        await self.store.append(event)
        return event

    def span(self, type: EventType, name: str = "", input: Any = "", attempt: int = 1) -> Span:
        return Span(self, type, name, input, attempt)
