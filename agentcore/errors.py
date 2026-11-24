"""What a tool's `run` may raise.

The executor uses the kind to decide between stopping, retrying and verifying.
The message is shown to the model, so keep it short and factual.
"""

from __future__ import annotations

from typing import ClassVar


class ToolError(Exception):
    kind: ClassVar[str]

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ToolFailed(ToolError):
    """A definite failure: the attempt was refused and nothing changed. Never retried, never verified."""

    kind = "failed"


class ToolRetryable(ToolError):
    """The attempt failed but the outcome is not certain (for example a 503). May be retried."""

    kind = "retryable"


class ToolOutcomeUnknown(ToolError):
    """No answer came back (timeout, dropped connection). Whether it took effect is unknown."""

    kind = "unknown"
