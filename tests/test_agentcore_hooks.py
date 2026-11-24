import asyncio
import json

import pytest

from pydantic import BaseModel, ConfigDict

from agentcore import Allow, Block, Done, HookContext, HookRunner, Pause, Tool
from agentcore.hooks import ModelRequest, ToolCallEvent
from agentcore.trace import InMemoryTraceStore, Tracer


def run(coro):
    return asyncio.run(coro)


def ctx(**kw):
    return HookContext(session_id="s", turn_id="t", data=kw.get("data", {}), state={})


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _noop(inp, c):
    return None


def call_event(name="x", tags=()):
    t = Tool(name=name, description="d", input_model=In, run=_noop, tags=frozenset(tags))
    return ToolCallEvent(tool=t, tool_use_id="u1", input=In())


def decide(runner, point="before_tool", event=None, c=None, tracer=None):
    return run(runner.run(point, c or ctx(), event if event is not None else call_event(), tracer))


def test_no_hooks_means_allow():
    assert decide(HookRunner()) == Allow()


def test_all_allow_means_allow_and_order_is_registration_order():
    seen = []
    r = HookRunner()
    for n in "abc":
        r.add("before_tool", lambda c, e, n=n: seen.append(n) or Allow(), name=n)
    assert decide(r) == Allow() and seen == ["a", "b", "c"]


def test_first_block_wins_and_later_hooks_do_not_run():
    seen = []
    r = HookRunner()
    r.add("before_tool", lambda c, e: seen.append("first") or Allow(), name="first")
    r.add("before_tool", lambda c, e: Block("not allowed: reason from the hook"), name="gate")
    r.add("before_tool", lambda c, e: seen.append("never") or Allow(), name="late")
    d = decide(r)
    assert d == Block("not allowed: reason from the hook", by="gate") and seen == ["first"]


def test_reason_is_passed_on_verbatim():
    r = HookRunner()
    r.add("before_tool", lambda c, e: Block("Any wording the service likes."))
    assert decide(r).reason == "Any wording the service likes."


def test_pause_keeps_its_payload_and_names_the_hook():
    r = HookRunner()
    r.add("before_tool", lambda c, e: Pause("ask first", payload={"k": [1]}), name="confirm")
    d = decide(r)
    assert d == Pause("ask first", payload={"k": [1]}, by="confirm")
    json.dumps(d.payload)


def test_async_hooks_work_too():
    async def gate(c, e):
        await asyncio.sleep(0)
        return Block("async no")

    r = HookRunner()
    r.add("before_tool", gate)
    assert decide(r) == Block("async no", by="gate")


# ---- fail closed


def test_a_hook_that_raises_blocks():
    def boom(c, e):
        raise RuntimeError("secret detail")

    r = HookRunner()
    r.add("before_tool", boom, name="boom")
    d = decide(r)
    assert isinstance(d, Block) and "boom" in d.reason and "RuntimeError" in d.reason
    assert "secret detail" not in d.reason  # the message of the exception stays out of what the model sees


def test_a_hook_that_returns_nothing_blocks():
    r = HookRunner()
    r.add("before_tool", lambda c, e: None, name="forgot")
    assert isinstance(decide(r), Block)


def test_a_hook_that_hangs_blocks_after_the_timeout():
    async def hang(c, e):
        await asyncio.sleep(5)
        return Allow()

    r = HookRunner(timeout_s=0.05)
    r.add("before_tool", hang, name="slow")
    d = decide(r)
    assert isinstance(d, Block) and "timed out" in d.reason


def test_pause_is_only_valid_before_a_tool():
    for point in ("before_model", "after_model", "after_tool"):
        r = HookRunner()
        r.add(point, lambda c, e: Pause("nope"), name="p")
        d = decide(r, point=point, event=ModelRequest(step=1, model="m", request={}))
        assert isinstance(d, Block) and "cannot pause" in d.reason


def test_points_are_separate():
    r = HookRunner()
    r.add("after_tool", lambda c, e: Block("only after"))
    assert decide(r, point="before_tool") == Allow()


# ---- policy lives outside, using the scratch space

def test_a_policy_hook_can_count_per_turn_using_scratch_and_tags():
    def one_marked_call_per_turn(c, e):
        if "marked" not in e.tool.tags:
            return Allow()
        if c.scratch.get("marked_calls", 0) >= 1:
            return Block("One at a time, please.")
        c.scratch["marked_calls"] = 1
        return Allow()

    r = HookRunner()
    r.add("before_tool", one_marked_call_per_turn)
    turn = ctx()
    assert decide(r, event=call_event(tags={"marked"}), c=turn) == Allow()
    assert decide(r, event=call_event(tags=set()), c=turn) == Allow()  # unmarked tools are not counted
    assert isinstance(decide(r, event=call_event(tags={"marked"}), c=turn), Block)
    assert decide(r, event=call_event(tags={"marked"}), c=ctx()) == Allow()  # a new turn starts clean


# ---- trace

def test_every_decision_is_traced_allow_included():
    async def go():
        store = InMemoryTraceStore()
        r = HookRunner()
        r.add("before_tool", lambda c, e: Allow(), name="a")
        r.add("before_tool", lambda c, e: Block("why not"), name="b")
        r.add("before_tool", lambda c, e: Allow(), name="never")
        await r.run("before_tool", ctx(), call_event(), Tracer(store, "s"))
        return await store.get("s")

    events = run(go())
    assert [(e.type, e.name, e.status) for e in events] == [("hook_decision", "a", "allow"), ("hook_decision", "b", "block")]
    assert events[1].output == "why not" and events[1].extra == {"point": "before_tool"}


# ---- state is live, not a snapshot

def test_hooks_see_state_changes_made_after_the_context_was_built():
    """Confirm flow: code marks the pending action confirmed, then runs the stored call in the same turn."""
    state = {"pending": {"status": "awaiting"}}
    turn = HookContext.for_turn("s", "t", {}, state)

    def needs_confirmed(c, e):
        return Allow() if c.state.get("pending", {}).get("status") == "confirmed" else Block("not confirmed yet")

    r = HookRunner()
    r.add("before_tool", needs_confirmed)
    assert isinstance(decide(r, c=turn), Block)
    state["pending"]["status"] = "confirmed"  # what the service does before running the stored call
    assert decide(r, c=turn) == Allow()
    state["pending"] = {"status": "awaiting"}  # replaced wholesale: also seen
    assert isinstance(decide(r, c=turn), Block)


def test_hooks_cannot_write_to_the_state_through_the_view():
    turn = HookContext.for_turn("s", "t", {}, {"a": 1})
    with pytest.raises(TypeError):
        turn.state["a"] = 2
