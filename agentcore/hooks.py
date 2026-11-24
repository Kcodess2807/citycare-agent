"""Hooks: code that runs around every model call and tool call and can allow it, block it, or pause the turn.

The runtime only provides the mechanism. What a hook checks, and the wording of its reason, is
decided by whoever registers it. The reason is passed on to the model as is.

Rules of the runner:
- hooks run in registration order and the first one that does not Allow decides;
- it fails closed: a hook that raises, times out, or returns nothing counts as a Block;
- Pause is only valid at `before_tool`. Anywhere else it is turned into a Block;
- every decision, Allow included, is written to the trace.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel

from agentcore.results import ToolResult
from agentcore.tools import Tool
from agentcore.trace import Tracer

HookPoint = Literal["before_model", "after_model", "before_tool", "after_tool"]
_PAUSE_ALLOWED = {"before_tool"}


# ---------------------------------------------------------------- what a hook returns


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Block:
    """Do not go ahead. `reason` is shown to the model (tool hooks) or ends the turn (model hooks)."""

    reason: str
    by: str = ""  # name of the hook, filled in by the runner


@dataclass(frozen=True)
class Pause:
    """Do not go ahead now, and end the turn. `payload` is stored untouched in the session state.

    It must be JSON-serialisable. The runtime never reads it: what it means is up to the hooks
    and code that created it.
    """

    reason: str
    payload: Any = None
    by: str = ""


HookResult = Allow | Block | Pause


# ---------------------------------------------------------------- what a hook receives


@dataclass(frozen=True)
class HookContext:
    session_id: str
    turn_id: str
    data: Mapping[str, Any]  # the same opaque per-turn data tools get
    # A LIVE, read-only view of the turn's state, not a copy. When code changes the turn's state
    # (for example marks a pending action as confirmed) and then runs a tool in the same turn, the
    # hooks see the change. Build it with `for_turn` so it is wrapped correctly. Only the top
    # level is protected: nested values are the turn's own objects, hooks must not change them.
    state: Mapping[str, Any]
    scratch: dict[str, Any] = field(default_factory=dict)  # shared by every hook of this turn, thrown away after it

    @classmethod
    def for_turn(cls, session_id: str, turn_id: str, data: Mapping[str, Any], state: dict[str, Any]) -> HookContext:
        """`state` is the turn's live state dict. Keep one HookContext for the whole turn."""
        return cls(session_id, turn_id, data, MappingProxyType(state))


@dataclass(frozen=True)
class ModelRequest:  # before_model
    step: int
    model: str
    request: Mapping[str, Any]


@dataclass(frozen=True)
class ModelReply:  # after_model
    step: int
    model: str
    message: Any  # the SDK's Message


@dataclass(frozen=True)
class ToolCallEvent:  # before_tool, input already validated
    tool: Tool
    tool_use_id: str
    input: BaseModel


@dataclass(frozen=True)
class ToolDoneEvent:  # after_tool. Block replaces the result with a Failed(reason)
    tool: Tool
    tool_use_id: str
    input: BaseModel
    result: ToolResult


Hook = Callable[[HookContext, Any], HookResult | Awaitable[HookResult]]


# ---------------------------------------------------------------- the runner


class HookRunner:
    def __init__(self, timeout_s: float = 10.0):
        self._timeout = timeout_s
        self._hooks: dict[str, list[tuple[str, Hook]]] = {}

    def add(self, point: HookPoint, hook: Hook, name: str | None = None) -> None:
        self._hooks.setdefault(point, []).append((name or getattr(hook, "__name__", "hook"), hook))

    def has(self, point: HookPoint, name: str) -> bool:
        """Is a hook with this name registered at this point? Lets a caller refuse to start without one."""
        return any(n == name for n, _ in self._hooks.get(point, []))

    async def run(self, point: HookPoint, ctx: HookContext, event: Any, tracer: Tracer | None = None) -> HookResult:
        for name, hook in self._hooks.get(point, []):
            t0 = time.perf_counter()
            result = await self._call(point, name, hook, ctx, event)
            if tracer is not None:
                kind = type(result).__name__.lower()
                await tracer.record(
                    "hook_decision",
                    name,
                    duration_ms=(time.perf_counter() - t0) * 1000,
                    status=kind,
                    output=getattr(result, "reason", ""),
                    extra={"point": point},
                )
            if not isinstance(result, Allow):
                return result
        return Allow()

    async def _call(self, point: str, name: str, hook: Hook, ctx: HookContext, event: Any) -> HookResult:
        try:
            result = hook(ctx, event)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, self._timeout)
        except asyncio.TimeoutError:
            return Block(f"Check '{name}' timed out.", by=name)
        except Exception as e:  # a broken check must never let a call through
            return Block(f"Check '{name}' failed to run ({type(e).__name__}).", by=name)
        if isinstance(result, Pause):
            if point not in _PAUSE_ALLOWED:
                return Block(f"Check '{name}' paused at {point}, which cannot pause.", by=name)
            return dataclasses.replace(result, by=name)
        if isinstance(result, Block):
            return dataclasses.replace(result, by=name)
        if isinstance(result, Allow):
            return result
        return Block(f"Check '{name}' returned nothing usable.", by=name)
