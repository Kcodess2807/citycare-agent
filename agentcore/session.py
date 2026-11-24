"""Session store: the conversation history and a free-form state dict, saved separately.

They are separate on purpose. A failed turn rolls back the history, but the state (a pending
action, an in-flight write, recorded outcomes) must survive it. See docs/design-log.md, 1.7.

Everything stored is JSON: what goes in and what comes out is a JSON round trip. That keeps
the in-memory store behaving like a Redis one would, and makes a non-serialisable payload fail
at save time instead of in production.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any, AsyncContextManager, Protocol

from pydantic import BaseModel, Field


class SessionError(ValueError):
    """Something that can't be stored."""


class HistoryError(SessionError):
    """The message list is not a valid conversation. Nothing was stored."""


class Session(BaseModel):
    session_id: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- history rules


def _ids(message: dict[str, Any], block_type: str) -> list[str]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    key = "id" if block_type == "tool_use" else "tool_use_id"
    return [b[key] for b in content if isinstance(b, dict) and b.get("type") == block_type and key in b]


def check_history(messages: list[dict[str, Any]]) -> None:
    """Raise HistoryError unless every tool call has its result in the next message, and nothing else is loose.

    The API rejects a conversation with a tool call that has no result, so a history that fails
    this check must never be stored.
    """
    if messages and messages[0].get("role") != "user":
        raise HistoryError("history must start with a user message")
    for i, m in enumerate(messages):
        role = m.get("role")
        if role not in ("user", "assistant") or "content" not in m:
            raise HistoryError(f"message {i}: needs a role of user or assistant, and content")
        if role == "assistant":
            calls = _ids(m, "tool_use")
            if calls:
                nxt = messages[i + 1] if i + 1 < len(messages) else None
                if nxt is None or nxt.get("role") != "user":
                    raise HistoryError(f"message {i}: tool call with no result message after it")
                if sorted(_ids(nxt, "tool_result")) != sorted(calls):
                    raise HistoryError(f"message {i}: results do not match the tool calls (missing or extra)")
        else:
            results = _ids(m, "tool_result")
            if results:
                prev = messages[i - 1] if i > 0 else None
                if prev is None or prev.get("role") != "assistant" or sorted(_ids(prev, "tool_use")) != sorted(results):
                    raise HistoryError(f"message {i}: tool result with no matching tool call before it")


def trim_history(messages: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """Drop the oldest messages down to about `cap`, cutting only where a turn starts.

    A turn starts at a user message that is not a tool result, so a tool call is never separated
    from its result. If no such point exists within the cap (one very long turn), keep everything.
    """
    if len(messages) <= cap:
        return messages
    for i in range(len(messages) - cap, len(messages)):
        if messages[i].get("role") == "user" and not _ids(messages[i], "tool_result"):
            return messages[i:]
    return messages


def _roundtrip(value: Any, what: str) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as e:
        raise SessionError(f"{what} is not JSON-serialisable: {e}") from e


# ---------------------------------------------------------------- store


class SessionStore(Protocol):
    def lock(self, session_id: str) -> AsyncContextManager[Any]:
        """Hold this for a whole turn so two messages of one session never interleave."""
        ...

    async def load(self, session_id: str) -> Session:
        """A copy of the session, empty if unknown or expired. Changing it changes nothing until saved."""
        ...

    async def save_state(self, session_id: str, state: dict[str, Any]) -> None: ...

    async def commit_history(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Validate, trim and replace the history in one step. On HistoryError the stored history is unchanged."""
        ...

    async def delete(self, session_id: str) -> None: ...


class InMemorySessionStore:
    """Sessions in a dict. Idle sessions expire after `ttl_s`, checked when they are loaded or purged."""

    def __init__(self, ttl_s: float = 1800.0, history_cap: int = 40, clock: Callable[[], float] = time.monotonic):
        self._ttl, self._cap, self._clock = ttl_s, history_cap, clock
        self._data: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _live(self, session_id: str) -> dict[str, Any] | None:
        entry = self._data.get(session_id)
        if entry is not None and self._clock() - entry["last"] > self._ttl:
            del self._data[session_id]
            return None
        return entry

    def _entry(self, session_id: str) -> dict[str, Any]:
        entry = self._live(session_id)
        if entry is None:
            entry = self._data[session_id] = {"messages": [], "state": {}, "last": self._clock()}
        return entry

    async def load(self, session_id: str) -> Session:
        entry = self._live(session_id)
        if entry is None:
            return Session(session_id=session_id)
        return Session(session_id=session_id, messages=_roundtrip(entry["messages"], "history"), state=_roundtrip(entry["state"], "state"))

    async def save_state(self, session_id: str, state: dict[str, Any]) -> None:
        clean = _roundtrip(state, "state")
        entry = self._entry(session_id)
        entry["state"], entry["last"] = clean, self._clock()

    async def commit_history(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        clean = _roundtrip(messages, "history")
        check_history(clean)
        entry = self._entry(session_id)
        entry["messages"], entry["last"] = trim_history(clean, self._cap), self._clock()

    async def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)
        self._locks.pop(session_id, None)

    def purge_expired(self) -> int:
        """Drop every expired session, and the lock of each unless a turn is holding it. Returns how many."""
        gone = [sid for sid in list(self._data) if self._live(sid) is None]
        for sid in gone:
            lock = self._locks.get(sid)
            if lock is not None and not lock.locked():
                del self._locks[sid]
        return len(gone)
