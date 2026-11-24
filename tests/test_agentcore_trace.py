import asyncio

import pytest

from agentcore.trace import InMemoryTraceStore, TokenUsage, Tracer, clip, cost_usd, summarize
from tests.fake_claude import text


def run(coro):
    return asyncio.run(coro)


def test_cost_known_model():
    u = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert cost_usd("claude-sonnet-5-5", u) == pytest.approx(2.0 + 10.0 + 0.10 + 2.50)
    assert cost_usd("claude-haiku-5-5", TokenUsage(input_tokens=1000, output_tokens=200)) == pytest.approx(0.0002)


def test_cost_unknown_model_is_none_not_a_guess():
    assert cost_usd("fake-model", TokenUsage(input_tokens=10)) is None


def test_usage_from_sdk_object_handles_none_fields():
    u = TokenUsage.from_sdk(text("hi").usage)  # fake usage: cache fields are None, no thinking details
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.thinking_tokens) == (100, 20, 0, 0, 0)


def test_clip_truncates_and_scrubs_key():
    assert clip("x" * 50, 10) == "x" * 10 + "...[truncated 40 chars]"
    fake_key = "sk" + "-ant-" + "api03-" + "A1b2" * 8  # built at runtime: no key-shaped literal in the repo
    clipped = clip({"k": fake_key}, 500)
    assert fake_key not in clipped and "[redacted]" in clipped
    assert clip({"a": 1}, 50) == '{"a": 1}'


def test_span_records_duration_and_numbers_events():
    async def go():
        store = InMemoryTraceStore()
        tr = Tracer(store, "s1")
        async with tr.span("tool_call", "lookup", input={"q": 1}) as sp:
            await asyncio.sleep(0.01)
            sp.output = {"ok": True}
        await tr.record("stop", "done")
        return await store.get("s1")

    first, second = run(go())
    assert (first.seq, second.seq) == (1, 2) and first.trace_id == second.trace_id
    assert first.duration_ms >= 5 and first.status == "ok" and first.output == '{"ok": true}'


def test_span_marks_error_and_reraises():
    async def go():
        store = InMemoryTraceStore()
        with pytest.raises(ValueError):
            async with Tracer(store, "s1").span("tool_call", "t") as sp:
                raise ValueError("boom")
        return await store.get("s1")

    (event,) = run(go())
    assert event.status == "error" and "boom" in event.output


def test_lru_drops_least_recently_used_session():
    async def go():
        store = InMemoryTraceStore(max_sessions=2)
        for sid in ("a", "b"):
            await Tracer(store, sid).record("stop")
        await Tracer(store, "a").record("stop")  # touch a, so b is now the oldest
        await Tracer(store, "c").record("stop")
        return [len(await store.get(s)) for s in ("a", "b", "c")]

    assert run(go()) == [2, 0, 1]


def test_jsonl_survives_a_new_store_and_eviction(tmp_path):
    path = tmp_path / "traces" / "t.jsonl"

    async def go():
        first = InMemoryTraceStore(max_sessions=1, path=path)
        await Tracer(first, "s1").record("stop", "one")
        await Tracer(first, "s2").record("stop", "two")  # evicts s1 from memory
        evicted = await first.get("s1")
        restarted = await InMemoryTraceStore(path=path).get("s2")
        return evicted, restarted, await InMemoryTraceStore(path=path).get("nope")

    evicted, restarted, missing = run(go())
    assert [e.name for e in evicted] == ["one"] and [e.name for e in restarted] == ["two"] and missing == []


def test_long_fields_are_clipped_by_the_tracer():
    async def go():
        store = InMemoryTraceStore()
        await Tracer(store, "s", field_limit=100).record("tool_call", "t", input="y" * 500)
        return (await store.get("s"))[0]

    assert "truncated 400 chars" in run(go()).input


def test_summary_totals_all_turns_of_a_session():
    async def go():
        store = InMemoryTraceStore()
        for turn in range(2):  # two turns = two tracers, same session
            tr = Tracer(store, "s1")
            u = TokenUsage(input_tokens=100, output_tokens=50, cache_read_tokens=10, thinking_tokens=20)
            await tr.record("model_call", "claude-haiku-5-5", usage=u, cost_usd=cost_usd("claude-haiku-5-5", u), duration_ms=100)
            await tr.record("tool_call", "lookup", duration_ms=40)
            await tr.record("stop", "done")
        u = TokenUsage(input_tokens=7, output_tokens=3)
        await Tracer(store, "s1").record("model_call", "fake-model", usage=u, cost_usd=None, status="error", duration_ms=5)
        await Tracer(store, "other").record("model_call", "claude-haiku-5-5", usage=TokenUsage(input_tokens=999))
        return summarize(await store.get("s1"))

    s = run(go())
    assert (s.turns, s.model_calls, s.tool_calls, s.errors) == (3, 3, 2, 1)
    assert (s.usage.input_tokens, s.usage.output_tokens, s.usage.cache_read_tokens, s.usage.thinking_tokens) == (207, 103, 20, 40)
    assert s.cost_usd == pytest.approx(2 * cost_usd("claude-haiku-5-5", TokenUsage(input_tokens=100, output_tokens=50, cache_read_tokens=10)))
    assert s.unpriced_calls == 1 and s.duration_ms == pytest.approx(285)


def test_summary_of_nothing_is_zero():
    s = summarize([])
    assert (s.turns, s.cost_usd, s.usage.output_tokens) == (0, 0.0, 0)
