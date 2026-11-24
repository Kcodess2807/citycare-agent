"""Tool registry: what a tool is, how its input is validated, and how it is shown to the model.

A tool is a spec. The registry only stores specs, builds the API schema and validates input.
Running a tool (retries, verify, timeouts) is the executor's job, and what a tool means is the
service's job. A bad call from the model never raises: it becomes a `Failed` result whose text
the model can read and act on.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from agentcore.config import RetryPolicy
from agentcore.errors import ToolError
from agentcore.results import Done, Failed, ToolResult

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # the API's rule for tool names


class ToolSpecError(ValueError):
    """The tool is defined wrongly. Raised at registration, so it shows up at start-up."""


@dataclass(frozen=True)
class ToolContext:
    """What a tool receives besides its input. `data` is opaque: the service fills it per turn."""

    session_id: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    run: Callable[[Any, ToolContext], Awaitable[Any]]  # ONE attempt. Returns a JSON-serialisable value or raises a ToolError
    verify: Callable[[Any, ToolContext, ToolError | None], Awaitable[ToolResult]] | None = None
    retry: RetryPolicy = RetryPolicy()  # only used if idempotent
    idempotent: bool = False  # True: running it again with the same input is safe
    atomic: bool = False  # True: run + retries + verify is one unit that is never cut off
    reserve_s: float | None = None  # budget an atomic tool needs before it may start. None = the config default
    timeout_s: float | None = None  # one attempt. None = the config default
    tags: frozenset[str] = frozenset()  # opaque to the runtime, hooks may read them

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ToolSpecError(f"tool name {self.name!r} must match {_NAME.pattern}")
        if not self.description.strip():
            raise ToolSpecError(f"tool {self.name!r} needs a description, the model reads it")
        if self.input_model.model_config.get("extra") != "forbid":
            # Without this, a field the model invents is dropped without a word.
            raise ToolSpecError(f"tool {self.name!r}: input model must set model_config = ConfigDict(extra='forbid')")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}  # insertion order is the order the model sees

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ToolSpecError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """The `tools` list for the API. Same order every time, so the prompt cache keeps hitting."""
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_model.model_json_schema()}
            for t in self._tools.values()
        ]

    def parse(self, name: str, raw_input: Any) -> tuple[Tool, BaseModel] | Failed:
        """Look up the tool and validate the model's input. Any problem comes back as a Failed for the model to read."""
        tool = self._tools.get(name)
        if tool is None:
            return Failed(f"Unknown tool '{name}'. Available tools: {', '.join(self._tools) or 'none'}.")
        if not isinstance(raw_input, dict):
            return Failed(f"Invalid input for '{name}': expected a JSON object.")
        try:
            return tool, tool.input_model.model_validate(raw_input)
        except ValidationError as e:
            return Failed(f"Invalid input for '{name}': " + "; ".join(_describe(err) for err in e.errors()) + ".")


def _describe(err: Mapping[str, Any]) -> str:
    # Field path and message only. The offending value is left out: it could be long or private.
    where = ".".join(str(p) for p in err["loc"]) or "input"
    return f"{where}: {err['msg']}"


def tool_result_block(tool_use_id: str, result: ToolResult) -> dict[str, Any]:
    """A `tool_result` content block for the API. Only Done is a success."""
    if isinstance(result, Done):
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": json.dumps(result.value, default=str, ensure_ascii=False),
        }
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": result.reason, "is_error": True}
