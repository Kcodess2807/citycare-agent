import asyncio

import pytest

from agentcore.session import HistoryError, InMemorySessionStore, SessionError, check_history, trim_history


def run(coro):
    return asyncio.run(coro)


def user(text):
    return {"role": "user", "content": text}


def call(*ids):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": i, "name": "t", "input": {}} for i in ids]}


def result(*ids):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": "ok"} for i in ids]}


def reply(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


GOOD = [user("hi"), call("a", "b"), result("a", "b"), reply("done")]


# ---- the pairing check


def test_good_history_passes_and_empty_passes():
    check_history(GOOD)
    check_history([])


@pytest.mark.parametrize(
    "bad",
    [
        [user("hi"), call("a")],  # call at the end, no result message
        [user("hi"), call("a"), user("again")],  # next message is not a result
        [user("hi"), call("a", "b"), result("a")],  # a result is missing
        [user("hi"), call("a"), result("a", "z")],  # a result for a call that was never made
        [user("hi"), result("a")],  # result with nothing before it
        [reply("hi")],  # starts with the assistant
        [{"role": "system", "content": "x"}],  # role not allowed
        [user("hi"), {"role": "assistant"}],  # no content
    ],
)
def test_bad_histories_are_rejected(bad):
    with pytest.raises(HistoryError):
        check_history(bad)


# ---- trimming


def _turn(n):
    return [user(f"q{n}"), call(f"c{n}"), result(f"c{n}"), reply(f"a{n}")]


def test_trim_cuts_at_a_turn_start_and_keeps_pairs_whole():
    history = [m for n in range(6) for m in _turn(n)]  # 24 messages
    trimmed = trim_history(history, 10)
    assert trimmed[0] == user("q4") and len(trimmed) == 8
    check_history(trimmed)


def test_trim_never_splits_one_long_turn():
    long_turn = [user("q"), call("a"), result("a"), call("b"), result("b"), call("c"), result("c"), reply("x")]
    assert trim_history(long_turn, 3) == long_turn


def test_trim_leaves_short_histories_alone():
    assert trim_history(GOOD, 40) == GOOD


# ---- the store


def test_unknown_session_is_empty():
    s = run(InMemorySessionStore().load("nope"))
    assert (s.session_id, s.messages, s.state) == ("nope", [], {})


def test_history_and_state_round_trip_and_load_returns_a_copy():
    async def go():
        store = InMemorySessionStore()
        await store.commit_history("s", GOOD)
        await store.save_state("s", {"pending": {"k": [1, 2]}})
        first = await store.load("s")
        first.messages.append(user("scribble"))
        first.state["pending"]["k"].append(3)
        return await store.load("s")

    s = run(go())
    assert s.messages == GOOD and s.state == {"pending": {"k": [1, 2]}}


def test_failed_commit_leaves_history_unchanged_and_state_untouched():
    async def go():
        store = InMemorySessionStore()
        await store.commit_history("s", [user("hi"), reply("hello")])
        await store.save_state("s", {"inflight": {"tool": "t"}})
        with pytest.raises(HistoryError):
            await store.commit_history("s", [user("hi"), call("a")])  # dangling tool call
        return await store.load("s")

    s = run(go())
    assert s.messages == [user("hi"), reply("hello")]
    assert s.state == {"inflight": {"tool": "t"}}


def test_state_save_does_not_touch_history_and_the_reverse():
    async def go():
        store = InMemorySessionStore()
        await store.commit_history("s", GOOD)
        await store.save_state("s", {"a": 1})
        await store.commit_history("s", GOOD + [user("more")])
        return await store.load("s")

    s = run(go())
    assert s.state == {"a": 1} and len(s.messages) == 5


def test_non_json_payload_fails_at_save_time():
    async def go():
        store = InMemorySessionStore()
        with pytest.raises(SessionError):
            await store.save_state("s", {"pending": object()})
        with pytest.raises(SessionError):
            await store.commit_history("s", [user("hi"), {"role": "assistant", "content": {1, 2}}])

    run(go())


def test_commit_trims_to_the_cap():
    async def go():
        store = InMemorySessionStore(history_cap=10)
        await store.commit_history("s", [m for n in range(6) for m in _turn(n)])
        return await store.load("s")

    s = run(go())
    assert len(s.messages) == 8 and s.messages[0] == user("q4")


def test_idle_sessions_expire_and_writes_keep_them_alive():
    now = [0.0]

    async def go():
        store = InMemorySessionStore(ttl_s=100, clock=lambda: now[0])
        await store.commit_history("s", GOOD)
        now[0] = 90
        await store.save_state("s", {"x": 1})  # activity at t=90
        now[0] = 180  # 90s idle: alive
        alive = await store.load("s")
        now[0] = 191  # 101s idle: expired
        expired = await store.load("s")
        return alive, expired

    alive, expired = run(go())
    assert alive.messages == GOOD and alive.state == {"x": 1}
    assert expired.messages == [] and expired.state == {}


def test_purge_removes_expired_sessions_and_idle_locks():
    now = [0.0]

    async def go():
        store = InMemorySessionStore(ttl_s=10, clock=lambda: now[0])
        await store.commit_history("old", GOOD)
        store.lock("old")
        now[0] = 5
        await store.commit_history("new", GOOD)
        now[0] = 12
        return store.purge_expired(), store

    gone, store = run(go())
    assert gone == 1 and "old" not in store._data and "old" not in store._locks and "new" in store._data


def test_lock_serialises_one_session_but_not_two():
    log = []

    async def turn(store, sid, name):
        async with store.lock(sid):
            log.append(f"{name}-start")
            await asyncio.sleep(0.02)
            log.append(f"{name}-end")

    async def go():
        store = InMemorySessionStore()
        await asyncio.gather(turn(store, "s", "a"), turn(store, "s", "b"))
        same = list(log)
        log.clear()
        await asyncio.gather(turn(store, "s", "a"), turn(store, "t", "b"))
        return same, list(log)

    same, different = run(go())
    assert same == ["a-start", "a-end", "b-start", "b-end"]
    assert different[:2] == ["a-start", "b-start"]  # different sessions overlap
