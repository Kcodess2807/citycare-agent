import asyncio

import pytest
from pydantic import BaseModel, ConfigDict

from agentcore import Done, Failed, RetryPolicy, ToolContext, ToolFailed, ToolOutcomeUnknown, ToolRetryable, Unknown
from agentcore.executor import Executor
from agentcore.retry import backoff_delay, with_retries
from agentcore.session import InMemorySessionStore
from agentcore.tools import Tool, ToolRegistry
from agentcore.trace import InMemoryTraceStore, Tracer

CTX = ToolContext(session_id="s")


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = 1


def go(coro):
    return asyncio.run(coro)


def tool(run, verify=None, **kw):
    return Tool(name=kw.pop("name", "t"), description="d", input_model=In, run=run, verify=verify, **kw)


def make(store=None):
    """An executor whose backoff sleeps are recorded, not waited, and whose jitter always takes the maximum."""
    sleeps = []

    async def sleep(d):
        sleeps.append(d)

    return Executor(store=store, sleep=sleep, rng=lambda lo, hi: hi), sleeps


def scripted(script, seen=None):
    async def run(inp, ctx):
        if seen is not None:
            seen.append(inp)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return run


# ---------------------------------------------------------------- the seven required cases


def test_retry_then_success_with_the_same_input_object():
    seen = []
    ex, sleeps = make()
    t = tool(scripted([ToolRetryable("503"), ToolRetryable("503"), {"id": 1}], seen), idempotent=True)
    inp = In()
    assert go(ex.execute(t, inp, CTX, state={})) == Done({"id": 1})
    assert len(seen) == 3 and all(s is inp for s in seen)  # the very same object, so a stored key can't change
    assert sleeps == [0.5, 1.0]  # base 0.5, doubling, jitter pinned to its maximum


def test_saved_then_503_where_verify_returns_done():
    backend, state = [], {}

    async def run(inp, ctx):
        backend.append("saved")
        raise ToolRetryable("503 after the write was saved")

    async def verify(inp, ctx, err):
        return Done({"found": list(backend)}) if backend else Failed("not there")

    store = InMemorySessionStore()
    ex, _ = make(store)
    result = go(ex.execute(tool(run, verify, atomic=True), In(), CTX, state=state))
    assert result == Done({"found": ["saved"]})
    assert [o["status"] for o in state["outcomes"]] == ["done"] and "inflight" not in state
    assert go(store.load("s")).state["outcomes"][0]["status"] == "done"


def test_non_idempotent_503_goes_straight_to_verify_with_no_retry():
    calls = {"run": 0, "verify": 0}
    errors = []

    async def run(inp, ctx):
        calls["run"] += 1
        raise ToolRetryable("503")

    async def verify(inp, ctx, err):
        calls["verify"] += 1
        errors.append(err)
        return Failed("not done")

    ex, sleeps = make()
    t = tool(run, verify, atomic=True, idempotent=False)  # the default retry policy would allow 2 retries
    assert go(ex.execute(t, In(), CTX, state={})) == Failed("not done")
    assert calls == {"run": 1, "verify": 1} and sleeps == []
    assert isinstance(errors[0], ToolRetryable)  # verify is told what went wrong


def test_timeout_with_no_verify_returns_unknown():
    async def slow(inp, ctx):
        await asyncio.sleep(5)

    ex, _ = make()
    result = go(ex.execute(tool(slow, atomic=True, timeout_s=0.05), In(), CTX, state={}))
    assert isinstance(result, Unknown) and "No answer" in result.reason


def test_definite_failure_does_no_retry_and_no_verify():
    calls = {"run": 0, "verify": 0}

    async def run(inp, ctx):
        calls["run"] += 1
        raise ToolFailed("that is no longer available")

    async def verify(inp, ctx, err):
        calls["verify"] += 1
        return Done(1)

    ex, sleeps = make()
    result = go(ex.execute(tool(run, verify, atomic=True, idempotent=True), In(), CTX, state={}))
    assert result == Failed("that is no longer available")
    assert calls == {"run": 1, "verify": 0} and sleeps == []


def test_budget_below_reserve_means_the_write_never_starts():
    ran, state = [], {}

    async def run(inp, ctx):
        ran.append(1)

    store = InMemorySessionStore()
    ex, _ = make(store)  # the default reserve is 30s
    result = go(ex.execute(tool(run, atomic=True), In(), CTX, state=state, remaining_s=lambda: 10.0))
    assert isinstance(result, Failed) and result.escalate and "Nothing was changed" in result.reason
    assert ran == [] and state == {}  # never ran, and no state was written
    assert go(store.load("s")).state == {}


def test_a_cancel_mid_write_still_finishes_write_and_verify():
    async def scenario():
        started, gate = asyncio.Event(), asyncio.Event()
        backend, verified, state = [], [], {}

        async def run(inp, ctx):
            started.set()
            await gate.wait()
            backend.append("written")
            raise ToolRetryable("503 after the write was saved")

        async def verify(inp, ctx, err):
            verified.append(1)
            return Done({"found": list(backend)})

        store = InMemorySessionStore()
        ex, _ = make(store)
        task = asyncio.create_task(ex.execute(tool(run, verify, atomic=True), In(), CTX, state=state))
        await started.wait()
        task.cancel()  # the request is abandoned while the write is in flight
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend == []  # the write had not finished when we cancelled
        gate.set()
        await ex.drain()
        return backend, verified, state, await store.load("s")

    backend, verified, state, stored = go(scenario())
    assert backend == ["written"] and verified == [1]
    assert [o["status"] for o in state["outcomes"]] == ["done"] and "inflight" not in state
    assert stored.state["outcomes"][0]["status"] == "done" and "inflight" not in stored.state


# ---------------------------------------------------------------- state around a write


def test_state_is_saved_before_the_write_and_after_it_and_the_trace_shows_both():
    async def scenario():
        store, traces = InMemorySessionStore(), InMemoryTraceStore()
        seen_inside = []

        async def run(inp, ctx):
            seen_inside.append((await store.load("s")).state.get("inflight"))
            return {"ok": True}

        ex, _ = make(store)
        state = {}
        await ex.execute(tool(run, atomic=True, name="w"), In(n=7), CTX, state=state, tracer=Tracer(traces, "s"))
        return seen_inside, state, await store.load("s"), await traces.get("s")

    seen_inside, state, stored, events = go(scenario())
    assert seen_inside[0]["tool"] == "w" and seen_inside[0]["input"] == {"n": 7}  # on disk before the write ran
    assert "inflight" not in stored.state
    outcome = stored.state["outcomes"][0]
    assert outcome["tool"] == "w" and outcome["status"] == "done" and outcome["told"] is False
    saves = [e.name for e in events if e.type == "state_saved"]
    assert saves == ["before", "after"]


def test_if_the_first_save_fails_the_write_never_runs():
    class BrokenStore(InMemorySessionStore):
        async def save_state(self, session_id, state):
            raise OSError("disk full")

    ran, state = [], {}

    async def run(inp, ctx):
        ran.append(1)

    ex, _ = make(BrokenStore())
    result = go(ex.execute(tool(run, atomic=True), In(), CTX, state=state))
    assert isinstance(result, Failed) and result.escalate and ran == [] and "inflight" not in state


def test_a_failed_save_after_the_write_does_not_lose_the_result():
    class FlakyStore(InMemorySessionStore):
        calls = 0

        async def save_state(self, session_id, state):
            FlakyStore.calls += 1
            if FlakyStore.calls == 2:
                raise OSError("gone")
            await super().save_state(session_id, state)

    async def run(inp, ctx):
        return {"ok": 1}

    state = {}
    ex, _ = make(FlakyStore())
    assert go(ex.execute(tool(run, atomic=True), In(), CTX, state=state)) == Done({"ok": 1})
    assert state["outcomes"][0]["status"] == "done"  # still in the live state


# ---------------------------------------------------------------- more of the order


def test_retries_used_up_then_verify_decides():
    n = {"run": 0}

    async def run(inp, ctx):
        n["run"] += 1
        raise ToolRetryable("503")

    async def verify(inp, ctx, err):
        return Done("it is there")

    ex, sleeps = make()
    assert go(ex.execute(tool(run, verify, atomic=True, idempotent=True), In(), CTX, state={})) == Done("it is there")
    assert n["run"] == 3 and sleeps == [0.5, 1.0]  # 1 try + 2 retries


def test_a_kind_the_policy_does_not_allow_is_not_retried():
    n = {"run": 0}

    async def run(inp, ctx):
        n["run"] += 1
        raise ToolOutcomeUnknown("timed out")

    ex, sleeps = make()
    t = tool(run, idempotent=True, retry=RetryPolicy(on=frozenset({"retryable"})))
    assert isinstance(go(ex.execute(t, In(), CTX, state={})), Failed)  # a read, no verify: plain failure
    assert n["run"] == 1 and sleeps == []


def test_a_bug_in_the_tool_is_not_retried_but_is_verified():
    n = {"run": 0, "verify": 0}

    async def run(inp, ctx):
        n["run"] += 1
        raise KeyError("oops")

    async def verify(inp, ctx, err):
        n["verify"] += 1
        assert "KeyError" in err.message
        return Failed("nothing happened")

    ex, _ = make()
    assert go(ex.execute(tool(run, verify, atomic=True, idempotent=True), In(), CTX, state={})) == Failed("nothing happened")
    assert n == {"run": 1, "verify": 1}


def test_verify_is_retried_and_its_failures_become_unknown():
    seen = {"retried": 0}

    async def flaky_verify(inp, ctx, err):
        seen["retried"] += 1
        if seen["retried"] < 3:
            raise ToolRetryable("503 on the read")
        return Done("found")

    async def run(inp, ctx):
        raise ToolRetryable("503")

    ex, _ = make()
    assert go(ex.execute(tool(run, flaky_verify, atomic=True), In(), CTX, state={})) == Done("found")

    async def always_down(inp, ctx, err):
        raise ToolRetryable("still 503")

    result = go(ex.execute(tool(run, always_down, atomic=True), In(), CTX, state={}))
    assert isinstance(result, Unknown) and "Could not check" in result.reason

    async def garbage(inp, ctx, err):
        return "not a result"

    assert isinstance(go(ex.execute(tool(run, garbage, atomic=True), In(), CTX, state={})), Unknown)


def test_a_timeout_on_a_read_is_a_plain_failure_not_unknown():
    async def slow(inp, ctx):
        await asyncio.sleep(5)

    ex, _ = make()
    result = go(ex.execute(tool(slow, timeout_s=0.05), In(), CTX, state={}))
    assert isinstance(result, Failed) and not result.escalate  # a read can't have changed anything


def test_non_atomic_retries_stop_when_there_is_no_time_to_wait():
    n = {"run": 0}

    async def run(inp, ctx):
        n["run"] += 1
        raise ToolRetryable("503")

    ex, sleeps = make()
    t = tool(run, idempotent=True)
    result = go(ex.execute(t, In(), CTX, state={}, remaining_s=lambda: 0.1))  # the first wait would be 0.5s
    assert isinstance(result, Failed) and n["run"] == 1 and sleeps == []


def test_non_atomic_with_no_time_left_does_not_start():
    async def run(inp, ctx):
        raise AssertionError("must not run")

    ex, _ = make()
    result = go(ex.execute(tool(run), In(), CTX, state={}, remaining_s=lambda: 0.0))
    assert isinstance(result, Failed) and result.escalate


def test_the_trace_numbers_each_attempt():
    async def scenario():
        traces = InMemoryTraceStore()
        ex, _ = make()
        t = tool(scripted([ToolRetryable("503"), {"ok": 1}]), idempotent=True)
        await ex.execute(t, In(), CTX, state={}, tracer=Tracer(traces, "s"))
        return await traces.get("s")

    events = go(scenario())
    assert [(e.type, e.attempt, e.status) for e in events] == [("tool_call", 1, "error"), ("tool_call", 2, "ok")]


# ---------------------------------------------------------------- recovering an interrupted write


def _registry(verify):
    async def run(inp, ctx):
        raise AssertionError("recovery must never run the write again")

    reg = ToolRegistry()
    reg.register(tool(run, verify, atomic=True, name="w"))
    return reg


def test_recover_inflight_verifies_records_and_clears():
    async def verify(inp, ctx, err):
        assert inp.n == 5 and "Interrupted" in err.message
        return Done({"found": True})

    async def scenario():
        store = InMemorySessionStore()
        state = {"inflight": {"tool": "w", "input": {"n": 5}, "started_at": 0}}
        ex, _ = make(store)
        result = await ex.recover_inflight(_registry(verify), CTX, state=state)
        return result, state, await store.load("s")

    result, state, stored = go(scenario())
    assert result == Done({"found": True}) and "inflight" not in state
    assert stored.state["outcomes"][0] == {**stored.state["outcomes"][0], "tool": "w", "status": "done", "told": False}


def test_recover_inflight_with_nothing_interrupted_does_nothing():
    ex, _ = make()
    assert go(ex.recover_inflight(_registry(None), CTX, state={})) is None


@pytest.mark.parametrize("inflight", [{"tool": "gone", "input": {}}, {"tool": "w", "input": {"n": "bad"}}])
def test_recover_inflight_that_cannot_be_checked_is_unknown(inflight):
    async def verify(inp, ctx, err):
        return Done(1)

    state = {"inflight": inflight}
    ex, _ = make()
    assert isinstance(go(ex.recover_inflight(_registry(verify), CTX, state=state)), Unknown)
    assert "inflight" not in state and state["outcomes"][0]["status"] == "unknown"


# ---------------------------------------------------------------- the retry helper itself


def test_backoff_is_full_jitter_up_to_a_cap():
    p = RetryPolicy(backoff_base_s=1.0, backoff_cap_s=8.0)
    top = lambda lo, hi: hi  # noqa: E731
    bottom = lambda lo, hi: lo  # noqa: E731
    assert [backoff_delay(p, n, top) for n in range(6)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
    assert backoff_delay(p, 3, bottom) == 0.0


def test_with_retries_raises_a_non_retryable_error_at_once_and_never_swallows_cancellation():
    async def definite(n):
        raise ToolFailed("no")

    async def cancelled(n):
        raise asyncio.CancelledError()

    classify = lambda e: None if isinstance(e, ToolFailed) else "retryable"  # noqa: E731
    with pytest.raises(ToolFailed):
        go(with_retries(definite, RetryPolicy(), classify))
    with pytest.raises(asyncio.CancelledError):
        go(with_retries(cancelled, RetryPolicy(), classify))
