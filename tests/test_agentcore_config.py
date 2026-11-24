import pytest
from pydantic import ValidationError

from agentcore import (
    Done,
    Failed,
    RetryPolicy,
    RuntimeConfig,
    ToolFailed,
    ToolOutcomeUnknown,
    ToolRetryable,
    Unknown,
)


def test_defaults_match_the_design_log():
    c = RuntimeConfig()
    assert (c.step_limit, c.turn_budget_s, c.model_timeout_s) == (8, 90.0, 60.0)
    assert c.model_retry.max_retries == 2 and c.model_retry.backoff_base_s == 1.0
    assert c.model_retry.backoff_cap_s == 8.0
    assert c.atomic_reserve_s == 30.0 and c.session_ttl_s == 1800.0
    assert c.history_cap_messages == 40 and c.trace_max_sessions == 1000


def test_config_is_frozen():
    with pytest.raises(ValidationError):
        RuntimeConfig().step_limit = 99


@pytest.mark.parametrize("bad", [{"step_limit": 0}, {"turn_budget_s": 0}, {"history_cap_messages": 1}])
def test_config_rejects_nonsense(bad):
    with pytest.raises(ValidationError):
        RuntimeConfig(**bad)


def test_retry_policy_rejects_unknown_kind_and_negative_retries():
    with pytest.raises(ValidationError):
        RetryPolicy(on=frozenset({"failed"}))
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=-1)


def test_error_kinds_line_up_with_retry_kinds():
    assert ToolRetryable.kind == "retryable" and ToolOutcomeUnknown.kind == "unknown"
    assert ToolFailed.kind == "failed"  # never retryable: RetryPolicy has no such kind
    assert ToolFailed("nope").message == "nope"


def test_result_statuses():
    assert [r.status for r in (Done(1), Failed("x"), Unknown("y"))] == ["done", "failed", "unknown"]
