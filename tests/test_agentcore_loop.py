import asyncio

import anthropic
import httpx2
import pytest
from anthropic.types import ThinkingBlock, ToolUseBlock
from pydantic import BaseModel, ConfigDict

from agentcore import (
    Allow,
    Block,
    Done,
    HookRunner,
    Pause,
    RuntimeConfig,
    Tool,
    ToolFailed,
    ToolOutcomeUnknown,
    ToolRegistry,
)
from agentcore.loop import AgentLoop, AgentSpec
from agentcore.session import InMemorySessionStore, check_history
from agentcore.trace import InMemoryTraceStore, summarize
from tests.async_fake import AsyncFake
from tests.fake_claude import _message, text, tool_use, tool_uses

HAIKU = "claude-haiku-5-5"


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = 0


def go(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- building blocks


def make_tool(name, run=None, **kw):
    ran = []

    async def default(inp, ctx):
        ran.append(inp)
        return {"ok": name}

    async def counted(inp, ctx):
        ran.append(inp)
        return await run(inp, ctx)

    t = Tool(name=name, description=f"The {name} tool.", input_model=In, run=counted if run else default, **kw)
    object.__setattr__(t, "ran", ran)  # the spec is frozen, this is only for the tests to look at
    return t


def registry(*tools):
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


class Rig:
    def __init__(self, script, tools=(), hooks=None, config=None, delays=None, rng=lambda lo, hi: lo, clock=None, store=None, client=None):
        self.sleeps = []

        async def sleep(d):
            self.sleeps.append(d)

        self.fake = client or AsyncFake(script, delays)
        self.store = store or InMemorySessionStore()
        self.traces = InMemoryTraceStore()
        kw = {"clock": clock} if clock else {}
        self.loop = AgentLoop(self.fake, self.store, self.traces, hooks=hooks, config=config, sleep=sleep, rng=rng, **kw)
        self.tools = list(tools)
        self.agent = AgentSpec("a", HAIKU, "You are a test agent.", registry(*tools), max_tokens=1024, effort="low")

    def turn(self, message="hi", sid="s", data=None):
        return go(self.loop.run_turn(self.agent, sid, message, data))

    @property
    def calls(self):
        return self.fake.messages.calls

    def stored(self, sid="s"):
        return go(self.store.load(sid))


def http_error(cls, status, **headers):
    response = httpx2.Response(status, headers=headers, request=httpx2.Request("POST", "https://example.test"))
    return cls("error", response=response, body=None)


def tool_result_blocks(call):
    """The tool_result blocks in the last message of a recorded request."""
    return call["messages"][-1]["content"]


# ---------------------------------------------------------------- stop reasons


def test_end_turn_returns_the_text_and_commits_history():
    rig = Rig([text("hello")])
    r = rig.turn("hi")
    assert (r.status, r.text, r.steps, r.escalate) == ("done", "hello", 1, False)
    assert rig.stored().messages == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]


def test_tool_use_runs_the_tool_and_loops_back_with_its_result():
    t = make_tool("look", run=lambda inp, ctx: _value({"v": inp.n}))
    rig = Rig([tool_use("look", {"n": 4}), text("it is 4")], [t])
    r = rig.turn()
    assert (r.status, r.text, r.steps) == ("done", "it is 4", 2)
    (block,) = tool_result_blocks(rig.calls[1])
    assert block["type"] == "tool_result" and block["content"] == '{"v": 4}' and "is_error" not in block
    assert block["tool_use_id"] == rig.calls[1]["messages"][-2]["content"][0]["id"]


async def _async_value(v):
    return v


def _value(v):
    return _async_value(v)


def test_max_tokens_is_discarded_then_retried_once_at_a_larger_cap():
    rig = Rig([text("a cut-off sent", stop_reason="max_tokens"), text("the whole answer")])
    r = rig.turn()
    assert (r.status, r.text) == ("done", "the whole answer")
    assert [c["max_tokens"] for c in rig.calls] == [1024, 2048]
    assert "cut-off" not in str(rig.stored().messages)  # the partial reply is in neither history nor result


def test_max_tokens_twice_escalates_and_stores_nothing():
    rig = Rig([text("part", stop_reason="max_tokens"), text("part", stop_reason="max_tokens"), text("never reached")])
    r = rig.turn()
    assert (r.status, r.reason_code, r.escalate, r.text) == ("failed", "truncated", True, "")
    assert len(rig.calls) == 2 and rig.stored().messages == []


def test_max_tokens_at_the_ceiling_escalates_without_a_pointless_retry():
    rig = Rig([text("part", stop_reason="max_tokens")], config=RuntimeConfig(max_tokens_retry_cap=1024))
    r = rig.turn()
    assert (r.reason_code, r.escalate) == ("truncated", True) and len(rig.calls) == 1


def test_a_tool_call_cut_off_by_max_tokens_never_runs():
    t = make_tool("w")
    cut = tool_use("w", {"n": 1}).model_copy(update={"stop_reason": "max_tokens"})
    rig = Rig([cut, text("fine")], [t])
    assert rig.turn().status == "done" and t.ran == []


def test_refusal_fails_and_escalates():
    rig = Rig([text("", stop_reason="refusal")])
    r = rig.turn()
    assert (r.status, r.reason_code, r.escalate) == ("failed", "refused", True) and rig.stored().messages == []


def test_pause_turn_continues_from_the_partial_assistant_message():
    rig = Rig([text("working on it", stop_reason="pause_turn"), text("all done")])
    r = rig.turn()
    assert (r.status, r.text, r.steps) == ("done", "all done", 2)
    last = rig.calls[1]["messages"][-1]
    assert last["role"] == "assistant" and last["content"][0]["text"] == "working on it"


@pytest.mark.parametrize(
    "reply",
    [
        text("x", stop_reason="model_context_window_exceeded"),
        text("x").model_copy(update={"stop_reason": "a_reason_from_the_future"}),  # model_copy skips validation
        text("x", stop_reason=None),
    ],
)
def test_an_unknown_stop_reason_fails_closed_and_escalates(reply):
    r = Rig([reply]).turn()
    assert (r.status, r.reason_code, r.escalate) == ("failed", "unhandled_stop", True)


def test_a_turn_that_ends_with_no_text_fails():
    r = Rig([_message([], "end_turn")]).turn()
    assert (r.status, r.reason_code) == ("failed", "empty_reply")


def test_step_limit():
    t = make_tool("w")
    rig = Rig([tool_use("w", {"n": i}) for i in range(20)], [t])  # a different input each time
    r = rig.turn()
    assert (r.status, r.reason_code, r.steps) == ("failed", "step_limit", 8)
    assert len(rig.calls) == 8 and rig.stored().messages == []


# ---------------------------------------------------------------- model errors and retries


def test_429_is_retried_with_backoff_and_retry_after_is_honoured():
    rig = Rig([http_error(anthropic.RateLimitError, 429, **{"retry-after": "7"}), text("ok")])
    assert rig.turn().status == "done"
    assert rig.sleeps == [7.0]  # the backoff alone (0..1s) would have been shorter


def test_without_retry_after_the_backoff_doubles_up_to_the_cap():
    errs = [http_error(anthropic.RateLimitError, 429) for _ in range(2)]
    rig = Rig([*errs, text("ok")], rng=lambda lo, hi: hi)
    assert rig.turn().status == "done" and rig.sleeps == [1.0, 2.0]


def test_529_overloaded_is_retried():
    rig = Rig([http_error(anthropic.OverloadedError, 529), text("ok")])
    assert rig.turn().status == "done" and len(rig.calls) == 2 and len(rig.sleeps) == 1


def test_a_connection_error_is_retried():
    rig = Rig([anthropic.APIConnectionError(request=httpx2.Request("POST", "https://example.test")), text("ok")])
    assert rig.turn().status == "done" and len(rig.calls) == 2


def test_400_is_not_retried():
    rig = Rig([http_error(anthropic.BadRequestError, 400), text("never")])
    r = rig.turn()
    assert (r.status, r.reason_code) == ("failed", "model_error") and len(rig.calls) == 1 and rig.sleeps == []


def test_the_model_call_gives_up_after_its_retries():
    rig = Rig([http_error(anthropic.RateLimitError, 429) for _ in range(5)])
    r = rig.turn()
    assert (r.status, r.reason_code) == ("failed", "model_error") and len(rig.calls) == 3  # 1 try + 2 retries
    assert len(rig.sleeps) == 2 and "429" in r.reason


def test_a_retry_after_longer_than_the_time_left_is_not_waited_for():
    rig = Rig([http_error(anthropic.RateLimitError, 429, **{"retry-after": "60"}), text("never")], config=RuntimeConfig(turn_budget_s=5))
    r = rig.turn()
    assert r.reason_code == "model_error" and rig.sleeps == [] and len(rig.calls) == 1


def test_a_model_call_that_takes_too_long_is_timed_out_and_retried():
    rig = Rig([text("ok")], delays=[1.0, 0], config=RuntimeConfig(model_timeout_s=0.05))
    assert rig.turn().status == "done" and len(rig.sleeps) == 1


# ---------------------------------------------------------------- tool calls


def test_a_tool_error_is_returned_to_the_model_as_a_result():
    async def refuse(inp, ctx):
        raise ToolFailed("that option does not exist")

    rig = Rig([tool_use("w", {"n": 1}), text("sorry, not possible")], [make_tool("w", run=refuse)])
    r = rig.turn()
    (block,) = tool_result_blocks(rig.calls[1])
    assert r.status == "done" and block["is_error"] is True and block["content"] == "that option does not exist"


def test_bad_input_and_unknown_tools_are_returned_to_the_model():
    rig = Rig([tool_uses([("w", {"n": "x"}), ("nope", {})]), text("ok")], [make_tool("w")])
    r = rig.turn()
    first, second = tool_result_blocks(rig.calls[1])
    assert r.status == "done" and first["is_error"] and first["content"].startswith("Invalid input for 'w'")
    assert second["is_error"] and "Unknown tool 'nope'" in second["content"] and "w" in second["content"]


def hook_by_name(runner):
    ok, blk, conf = make_tool("ok"), make_tool("blk"), make_tool("conf")

    def gate(ctx, event):
        return {"blk": Block("blocked by the test hook"), "conf": Pause("waiting for a yes", payload={"k": 1})}.get(event.tool.name, Allow())

    runner.add("before_tool", gate)
    return ok, blk, conf


def test_every_tool_use_gets_a_tool_result_blocked_and_skipped_included():
    runner = HookRunner()
    ok, blk, conf = hook_by_name(runner)
    script = [tool_uses([("ok", {}), ("blk", {}), ("conf", {}), ("ok", {"n": 1})]), text("Shall I go ahead?")]
    rig = Rig(script, [ok, blk, conf], hooks=runner)
    r = rig.turn()

    uses = [b for b in rig.calls[1]["messages"][-2]["content"] if b["type"] == "tool_use"]
    results = tool_result_blocks(rig.calls[1])
    assert [b["tool_use_id"] for b in results] == [u["id"] for u in uses]  # one result per call, same order
    assert results[0]["content"] == '{"ok": "ok"}' and "is_error" not in results[0]
    assert results[1]["content"] == "blocked by the test hook" and results[1]["is_error"]
    assert results[2]["content"] == "waiting for a yes" and results[2]["is_error"]  # the wording is the hook's
    assert results[3]["content"].startswith("Skipped") and results[3]["is_error"]
    assert (len(ok.ran), blk.ran, conf.ran) == (1, [], [])  # only the allowed call ran
    assert r.status == "paused"
    check_history(rig.stored().messages)  # and the stored history is valid


def test_pause_ends_the_turn_and_saves_the_payload():
    runner = HookRunner()
    ok, blk, conf = hook_by_name(runner)
    rig = Rig([tool_use("conf", {}), text("Please confirm: X. Yes?")], [ok, blk, conf], hooks=runner)
    r = rig.turn()
    assert (r.status, r.text, r.pending, r.escalate) == ("paused", "Please confirm: X. Yes?", {"k": 1}, False)
    assert rig.stored().state["pending"] == {"k": 1} and conf.ran == []
    assert rig.stored().messages[-1]["content"][0]["text"] == "Please confirm: X. Yes?"  # the read-back is in the history


def test_a_paused_action_is_not_kept_if_the_turn_then_fails():
    """The read-back never reached the user, so a later 'yes' must not be able to confirm it."""
    runner = HookRunner()
    ok, blk, conf = hook_by_name(runner)
    rig = Rig([tool_use("conf", {}), http_error(anthropic.BadRequestError, 400)], [ok, blk, conf], hooks=runner)
    go(rig.store.save_state("s", {"pending": {"old": True}}))
    assert rig.turn().status == "failed"
    assert rig.stored().state["pending"] == {"old": True}  # the earlier value is untouched, the new one never stored
    assert rig.stored().messages == []


def test_after_a_pause_only_text_is_allowed():
    runner = HookRunner()
    ok, blk, conf = hook_by_name(runner)
    rig = Rig([tool_use("conf", {}), tool_use("ok", {}), text("Please confirm.")], [ok, blk, conf], hooks=runner)
    r = rig.turn()
    (block,) = tool_result_blocks(rig.calls[2])
    assert r.status == "paused" and block["content"].startswith("Not run: the turn is paused") and ok.ran == []

    rig = Rig([tool_use("conf", {}), tool_use("ok", {}), tool_use("ok", {"n": 2}), text("x")], [ok, blk, conf], hooks=runner)
    r = rig.turn()
    assert (r.status, r.reason_code, r.escalate) == ("failed", "paused_misuse", True)


def test_a_payload_that_is_not_json_fails_the_turn_instead_of_being_lost():
    runner = HookRunner()
    runner.add("before_tool", lambda c, e: Pause("wait", payload={"bad": object()}))
    rig = Rig([tool_use("w", {}), text("never")], [make_tool("w")], hooks=runner)
    r = rig.turn()
    assert (r.status, r.reason_code) == ("failed", "internal") and "JSON" in r.reason


def test_the_same_call_is_blocked_the_third_time():
    t = make_tool("w")
    rig = Rig([tool_use("w", {"n": 1}), tool_use("w", {"n": 1}), tool_use("w", {"n": 1}), text("stopped")], [t])
    r = rig.turn()
    (third,) = tool_result_blocks(rig.calls[3])
    assert r.status == "done" and len(t.ran) == 2 and third["is_error"] and "already made 2 times" in third["content"]


def test_an_unknown_outcome_escalates_even_when_the_model_replies():
    async def lost(inp, ctx):
        raise ToolOutcomeUnknown("no answer")

    t = make_tool("w", run=lost, atomic=True)  # a write with no verify
    rig = Rig([tool_use("w", {}), text("I could not confirm that.")], [t])
    r = rig.turn()
    (block,) = tool_result_blocks(rig.calls[1])
    assert r.status == "done" and r.escalate is True and block["is_error"] and block["content"] == "no answer"


def test_a_hook_that_blocks_the_model_call_fails_the_turn_with_its_reason():
    runner = HookRunner()
    runner.add("before_model", lambda c, e: Block("not today"))
    rig = Rig([text("never")], hooks=runner)
    r = rig.turn()
    assert (r.status, r.reason_code, r.reason) == ("failed", "hook_blocked", "not today") and rig.calls == []


def test_the_turn_budget_is_enforced():
    now = [0.0]

    async def slow(inp, ctx):
        now[0] += 11  # the tool "takes" 11 seconds of a 10 second budget
        return {}

    rig = Rig([tool_use("w", {}), text("never")], [make_tool("w", run=slow)], config=RuntimeConfig(turn_budget_s=10), clock=lambda: now[0])
    r = rig.turn()
    assert (r.status, r.reason_code) == ("failed", "budget") and len(rig.calls) == 1


# ---------------------------------------------------------------- failed turns, state, and what the model is told


def _write_tool():
    async def run(inp, ctx):
        return {"written": True}

    return make_tool("w", run=run, atomic=True)


def test_a_failed_turn_rolls_back_history_while_state_survives():
    rig = Rig([text("first reply"), tool_use("w", {"n": 1}), http_error(anthropic.BadRequestError, 400)], [_write_tool()])
    assert rig.turn("one").status == "done"
    before = rig.stored().messages

    r = rig.turn("two")
    assert r.status == "failed"
    assert rig.stored().messages == before  # history is exactly as it was before the failed turn
    (outcome,) = rig.stored().state["outcomes"]  # but the write is on record
    assert (outcome["tool"], outcome["status"], outcome["told"]) == ("w", "done", False)
    assert [o["status"] for o in r.outcomes] == ["done"]  # and the caller is told about it
    assert "inflight" not in rig.stored().state


def test_an_untold_outcome_reaches_the_model_on_the_next_turn_and_is_then_marked_told():
    rig = Rig([tool_use("w", {"n": 1}), http_error(anthropic.BadRequestError, 400), text("noted")], [_write_tool()])
    rig.turn("book it")
    r = rig.turn("did it work?")
    content = rig.calls[-1]["messages"][-1]["content"]
    assert r.status == "done" and content[0]["type"] == "text" and "w: done" in content[0]["text"] and content[1]["text"] == "did it work?"
    assert all(o["told"] for o in rig.stored().state["outcomes"])
    rig.fake.messages._inner.script.append(text("again"))
    rig.turn("and now?")
    assert rig.calls[-1]["messages"][-1]["content"] == "and now?"  # told once, not repeated


def test_an_interrupted_write_is_settled_before_the_model_runs():
    async def verify(inp, ctx, err):
        return Done({"found": True})

    async def never(inp, ctx):
        raise AssertionError("recovery must not run the write again")

    t = Tool(name="w", description="d", input_model=In, run=never, verify=verify, atomic=True)
    rig = Rig([text("ok")], [t])
    go(rig.store.save_state("s", {"inflight": {"tool": "w", "input": {"n": 3}, "started_at": 0}}))
    r = rig.turn()
    note = rig.calls[0]["messages"][-1]["content"][0]["text"]
    assert r.status == "done" and "w: done" in note and "inflight" not in rig.stored().state


# ---------------------------------------------------------------- the loop never raises


class Hang:
    class messages:  # noqa: N801
        @staticmethod
        async def create(**kw):
            await asyncio.sleep(30)


class BrokenCommit(InMemorySessionStore):
    async def commit_history(self, *a):
        raise RuntimeError("disk on fire")


class BrokenLoad(InMemorySessionStore):
    async def load(self, *a):
        raise RuntimeError("cannot read")


@pytest.mark.parametrize(
    "rig_kwargs",
    [
        {"script": [RuntimeError("the client blew up")]},
        {"script": [ValueError("so did this")]},
        {"script": [object()]},  # not a Message at all
        {"script": [text("x")], "store": BrokenCommit()},
        {"script": [text("x")], "store": BrokenLoad()},
    ],
    ids=["client RuntimeError", "client ValueError", "garbage response", "store commit fails", "store load fails"],
)
def test_the_loop_never_raises(rig_kwargs):
    r = Rig(**rig_kwargs).turn()
    assert r.status == "failed" and r.reason_code in ("model_error", "internal") and r.text == ""


def test_a_broken_tool_and_a_broken_hook_do_not_raise_either():
    async def boom(inp, ctx):
        raise KeyError("bug")

    runner = HookRunner()
    runner.add("after_tool", lambda c, e: 1 / 0)
    rig = Rig([tool_use("w", {}), text("carried on")], [make_tool("w", run=boom)], hooks=runner)
    assert rig.turn().status == "done"


def test_cancelling_a_turn_is_not_swallowed():
    async def scenario():
        rig = Rig([], client=Hang())
        task = asyncio.create_task(rig.loop.run_turn(rig.agent, "s", "hi"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    go(scenario())


# ---------------------------------------------------------------- what is sent, kept and traced


def test_the_request_has_the_expected_shape():
    t = make_tool("look")
    rig = Rig([text("hi")], [t])
    rig.turn()
    req = rig.calls[0]
    assert req["model"] == HAIKU and req["max_tokens"] == 1024 and req["output_config"] == {"effort": "low"}
    assert req["system"] == [{"type": "text", "text": "You are a test agent.", "cache_control": {"type": "ephemeral"}}]
    assert [x["name"] for x in req["tools"]] == ["look"] and req["messages"] == [{"role": "user", "content": "hi"}]
    assert not {"temperature", "top_p", "top_k", "thinking", "tool_choice"} & set(req)


def test_thinking_blocks_are_stored_and_sent_back_unchanged():
    reply = _message(
        [ThinkingBlock(type="thinking", thinking="hm", signature="sig"), ToolUseBlock(type="tool_use", id="t1", name="w", input={})],
        "tool_use",
    )
    rig = Rig([reply, text("done")], [make_tool("w")])
    rig.turn()
    assert rig.calls[1]["messages"][-2]["content"][0] == {"type": "thinking", "thinking": "hm", "signature": "sig"}


def test_the_trace_records_every_call_with_tokens_and_cost():
    t = make_tool("w")
    rig = Rig([tool_use("w", {}), text("done")], [t])
    r = rig.turn()
    events = go(rig.traces.get("s"))
    assert {e.trace_id for e in events} == {r.trace_id}
    models = [e for e in events if e.type == "model_call"]
    assert len(models) == 2 and models[0].usage.input_tokens == 100
    assert models[0].cost_usd == pytest.approx((100 * 0.10 + 20 * 0.50) / 1_000_000)  # priced by the model we asked for
    assert models[0].extra["stop_reason"] == "tool_use" and events[-1].type == "stop"
    total = summarize(events)
    assert (total.model_calls, total.tool_calls, total.turns) == (2, 1, 1)


def test_two_turns_of_one_session_never_interleave():
    async def scenario():
        rig = Rig([text("first"), text("second")], delays=[0.05, 0])
        a = asyncio.create_task(rig.loop.run_turn(rig.agent, "s", "one"))
        await asyncio.sleep(0.01)
        b = asyncio.create_task(rig.loop.run_turn(rig.agent, "s", "two"))
        await asyncio.gather(a, b)
        return rig

    rig = go(scenario())
    assert len(rig.calls[1]["messages"]) == 3  # the second turn saw the first turn's reply
    assert rig.stored().messages[-1]["content"][0]["text"] == "second"


# ---------------------------------------------------------------- caller holds the lock, history transform


def test_lock_held_runs_inside_a_lock_the_caller_already_holds():
    async def scenario():
        rig = Rig([text("blocked"), text("ok")])
        async with rig.store.lock("s"):
            # Without the keyword the turn waits for the lock we hold: it never finishes.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(rig.loop.run_turn(rig.agent, "s", "x"), 0.05)
            return await asyncio.wait_for(rig.loop.run_turn(rig.agent, "s", "x", lock_held=True), 1)

    assert go(scenario()).status == "done"


def test_lock_held_does_not_release_the_callers_lock():
    async def scenario():
        rig = Rig([text("ok")])
        async with rig.store.lock("s"):
            await asyncio.wait_for(rig.loop.run_turn(rig.agent, "s", "x", lock_held=True), 1)
            return rig.store.lock("s").locked()

    assert go(scenario()) is True


def text_only(messages):
    """What a service would pass: keep text, drop tool traffic and a marked note."""
    out = []
    for m in messages:
        blocks = [{"type": "text", "text": m["content"]}] if isinstance(m["content"], str) else m["content"]
        for b in blocks:  # a service-added note is a first line starting with [NOTE]
            if b["type"] == "text":
                b["text"] = "\n".join(l for l in b["text"].split("\n") if not l.startswith("[NOTE]")).strip()
        keep = [b for b in blocks if b["type"] == "text" and b["text"]]
        if keep:
            out.append({"role": m["role"], "content": keep})
    return out


def test_the_model_sees_everything_but_the_store_gets_the_transformed_history():
    rig = Rig([tool_use("look", {}), text("done")], tools=[make_tool("look")])
    r = go(rig.loop.run_turn(rig.agent, "s", "[NOTE] facts here\nbook me", history_transform=text_only))
    assert r.status == "done"
    assert "tool_use" in str(rig.calls[1]["messages"]) and "[NOTE] facts here" in str(rig.calls[0]["messages"])
    stored = rig.stored().messages
    assert [m["role"] for m in stored] == ["user", "assistant"] and stored[0]["content"][0]["text"] == "book me"
    assert "tool_use" not in str(stored) and "tool_result" not in str(stored) and "[NOTE]" not in str(stored)


def test_a_broken_transform_fails_the_turn_and_stores_nothing():
    def boom(messages):
        raise RuntimeError("secret detail")

    rig = Rig([text("first"), text("second")])
    assert rig.turn("one").status == "done"
    before = rig.stored().messages
    r = go(rig.loop.run_turn(rig.agent, "s", "two", history_transform=boom))
    assert (r.status, r.reason_code) == ("failed", "history_invalid")
    assert "secret detail" not in r.reason and rig.stored().messages == before


def test_a_transform_that_breaks_the_history_rules_is_rejected_by_the_store():
    rig = Rig([text("hi")])
    r = go(rig.loop.run_turn(rig.agent, "s", "x", history_transform=lambda m: m[1:]))  # starts with an assistant message
    assert (r.status, r.reason_code) == ("failed", "history_invalid") and rig.stored().messages == []


def test_a_paused_turn_also_stores_the_transformed_history():
    class Hold:
        async def __call__(self, ctx, event):
            return Pause("awaiting yes", {"k": 1})

    hooks = HookRunner()
    hooks.add("before_tool", Hold())
    rig = Rig([tool_use("look", {}), text("confirm?")], tools=[make_tool("look")], hooks=hooks)
    r = go(rig.loop.run_turn(rig.agent, "s", "[NOTE] n\nyes", history_transform=text_only))
    assert r.status == "paused" and rig.stored().state["pending"] == {"k": 1}
    assert "tool_use" not in str(rig.stored().messages) and "[NOTE]" not in str(rig.stored().messages)
