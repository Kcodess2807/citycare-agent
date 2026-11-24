"""Every tunable number of the runtime, in one place.

All values are provisional until measured (see docs/design-log.md, 1.4).
Nothing in the loop, executor or stores hard-codes a number: they read this.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Which failures a retry policy may retry. Matches the `kind` of the errors in errors.py.
RetryKind = Literal["retryable", "unknown"]


class RetryPolicy(BaseModel):
    """How many times to retry and how long to wait between attempts.

    Delay before retry n (n starts at 0) is a random value in [0, min(cap, base * 2**n)].
    """

    model_config = ConfigDict(frozen=True)

    max_retries: int = Field(2, ge=0, description="Extra attempts after the first one")
    backoff_base_s: float = Field(0.5, gt=0)
    backoff_cap_s: float = Field(8.0, gt=0)
    on: frozenset[RetryKind] = Field(
        frozenset({"retryable", "unknown"}),
        description="Failure kinds this policy is allowed to retry",
    )


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Model calls. The SDK's own retries are off: the loop retries with this policy.
    model_timeout_s: float = Field(60.0, gt=0)
    model_retry: RetryPolicy = RetryPolicy(max_retries=2, backoff_base_s=1.0, backoff_cap_s=8.0)

    # One turn (one message in, one reply out).
    step_limit: int = Field(8, ge=1, description="Model calls per agent per turn")
    turn_budget_s: float = Field(90.0, gt=0, description="Wall clock for the whole turn")
    identical_call_limit: int = Field(3, ge=2, description="The Nth identical tool call in a turn is blocked")
    max_tokens_retry_cap: int = Field(8192, ge=1, description="Ceiling when max_tokens is doubled after a truncation")

    # Tools.
    tool_attempt_timeout_s: float = Field(10.0, gt=0, description="Default for one attempt of one tool")
    atomic_reserve_s: float = Field(30.0, gt=0, description="Default budget an atomic tool needs before it may start")

    # Stores.
    session_ttl_s: float = Field(1800.0, gt=0)
    history_cap_messages: int = Field(40, ge=2)
    trace_max_sessions: int = Field(1000, ge=1)
    trace_field_chars: int = Field(2000, ge=100, description="Input and output of an event are cut to this many characters")
