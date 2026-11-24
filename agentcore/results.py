"""The one result every tool call ends with: Done, Failed or Unknown."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass(frozen=True)
class Done:
    """The tool did what was asked. `value` is the JSON-serialisable output."""

    value: Any
    status: ClassVar[str] = "done"


@dataclass(frozen=True)
class Failed:
    """A definite failure: nothing changed. `reason` is shown to the model."""

    reason: str
    status: ClassVar[str] = "failed"


@dataclass(frozen=True)
class Unknown:
    """Could not tell whether it took effect, and could not find out. The caller must escalate."""

    reason: str
    status: ClassVar[str] = "unknown"


ToolResult = Done | Failed | Unknown
