import asyncio
import json

import anthropic
import httpx
import httpx2
import pytest
from pydantic import BaseModel, ConfigDict

from agent_service import app as app_module
from agent_service import emergency
from agent_service.agents import build_agents
from agent_service.hospital import HospitalClient
from agent_service.prompts import human_reply
from agent_service.schemas import ChatRequest
from agent_service.service import ChatService, session_key
from agent_service.tools import read_tools
from agentcore import Done, Tool, ToolRegistry
from agentcore.loop import AgentSpec
from agentcore.session import InMemorySessionStore
from agentcore.trace import InMemoryTraceStore
from tests.async_fake import AsyncFake
from tests.fake_claude import text, tool_use

FIELDS = {"reply", "agent", "handoff", "emergency", "trace_id"}
PHONE_A, PHONE_B = "9000000001", "9000000002"


def go(coro):
    return asyncio.run(coro)


def route_to(agent, language="en"):
    return text(json.dumps({"agent": agent, "language": language}))


def http_error(cls, status):
    r = httpx2.Response(status, request=httpx2.Request("POST", "https://example.test"))
    return cls("error", response=r, body=None)


APPT = {"id": "A1", "doctor_id": "d1", "start": "2026-12-03T10:00:00+05:30", "patient_name": "Asha", "phone": PHONE_A, "status": "booked"}
BILL = {"id": "B1", "phone": PHONE_A, "amount_inr": 500, "description": "Consult", "date": "2026-11-20", "status": "unpaid"}


class Rig:
    def __init__(self, script=None, client=None, hospital=None, agents=None, store=None):
        self.hospital_calls: list[tuple[str, dict]] = []
        handler = hospital or self._default_hospital

        def recording(request: httpx.Request):
            self.hospital_calls.append((request.url.path, dict(request.url.params)))
            return handler(request)

        self.hospital = HospitalClient(base_url="http://h", transport=httpx.MockTransport(recording))
        self.fake = client or AsyncFake(script or [])
        self.store, self.traces = store or InMemorySessionStore(), InMemoryTraceStore()

        async def nosleep(d):
            pass

        self.svc = ChatService(self.fake, self.store, self.traces, self.hospital, agents=agents, sleep=nosleep, rng=lambda lo, hi: lo)

    @staticmethod
    def _default_hospital(request: httpx.Request) -> httpx.Response:
        p, q = request.url.path, request.url.params
        if p == "/now":
            return httpx.Response(200, json={"now": "2026-12-01T09:00:00+05:30", "weekday": "Tuesday", "timezone": "Asia/Kolkata"})
        if p == "/appointments":
            return httpx.Response(200, json={"appointments": [APPT] if q["phone"] == PHONE_A else []})
        if p == "/bills":
            return httpx.Response(200, json={"bills": [BILL] if q["phone"] == PHONE_A else []})
        if p.endswith("/slots"):
            return httpx.Response(200, json={"doctor_id": "d1", "date": q["date"], "free": ["2026-12-04T17:00:00+05:30"]})
        return httpx.Response(200, json={"doctors": [], "specialties": []})

    def chat(self, message="hi", sid="s", phone=PHONE_A):
        return go(self.svc.handle(ChatRequest(session_id=sid, phone=phone, message=message)))

    @property
    def calls(self):
        return self.fake.messages.calls

    def events(self, sid="s"):
        return go(self.traces.get(sid))

    def state(self, sid="s", phone=PHONE_A):
        return go(self.store.load(session_key(phone, sid))).state

    def history(self, sid="s", phone=PHONE_A):
        return go(self.store.load(session_key(phone, sid))).messages


def check_contract(r):
    assert set(r.model_dump()) == FIELDS and r.reply.strip() and r.trace_id
    assert isinstance(r.handoff, bool) and isinstance(r.emergency, bool)


# ---- always 200 with all five fields


class Down:
    """A model API that is down for good."""

    def __init__(self):
        self.messages, self.n = self, 0

    async def create(self, **kw):
        self.n += 1
        raise http_error(anthropic.InternalServerError, 529)


def test_model_api_down_still_answers_with_all_five_fields():
    rig = Rig(client=Down())
    r = rig.chat("show my bills")
    check_contract(r)
    assert r.agent == "info" and r.emergency is False  # router fell back to info, the turn failed
    assert r.reply == __import__("agent_service.prompts", fromlist=["x"]).failure_reply("en")


def test_the_hospital_down_still_answers():
    def down(request):
        raise httpx.ConnectError("down")

    rig = Rig([route_to("billing"), tool_use("list_bills", {}), text("I couldn't check your bills right now.")], hospital=down)
    r = rig.chat("my bills")
    check_contract(r)
    assert r.agent == "billing" and "couldn't check" in r.reply
    assert "is_error" in json.dumps(rig.calls[-1]["messages"][-1])  # the model was told, so it could say so


def test_an_unusable_router_answer_still_reaches_a_specialist():
    rig = Rig([text("¯\\_(ツ)_/¯"), text("Our OPD is open 10 to 1.")])
    r = rig.chat("timings?")
    check_contract(r)
    assert r.agent == "info" and r.reply == "Our OPD is open 10 to 1."


def test_a_failed_turn_gets_a_fixed_reply_in_the_patients_language():
    rig = Rig([route_to("billing", "hinglish"), RuntimeError("model blew up")])
    r = rig.chat("mera bill?")
    check_contract(r)
    assert r.agent == "billing" and "dobara try karein" in r.reply and r.handoff is False


def test_a_turn_that_escalates_hands_off_with_the_human_text():
    rig = Rig([route_to("info", "en"), text("cut", stop_reason="max_tokens"), text("cut", stop_reason="max_tokens")])
    r = rig.chat("tell me everything")
    check_contract(r)
    assert r.handoff is True and r.reply == human_reply("en")


def test_something_unexpected_inside_handle_is_still_a_200_with_a_handoff(monkeypatch):
    rig = Rig([])

    async def boom(*a, **k):
        raise KeyError("secret detail")

    monkeypatch.setattr(rig.svc, "_turn", boom)
    r = rig.chat("hi")
    check_contract(r)
    assert (r.agent, r.handoff, r.emergency) == ("human", True, False) and "secret" not in r.reply
    (stop,) = [e for e in rig.events() if e.type == "stop"]
    assert stop.name == "internal" and stop.status == "error" and "secret" not in stop.output


def test_a_broken_store_is_still_a_200():
    class Broken(InMemorySessionStore):
        async def load(self, session_id):
            raise RuntimeError("store down")

    r = Rig([], store=Broken()).chat("hi")
    check_contract(r)
    assert r.handoff is True


def asgi(rig):
    app_module._service = rig.svc

    async def post(body, path="/chat"):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://t") as c:
            return await c.post(path, json=body) if body is not None else await c.get(path)

    return post


def test_over_http_a_failure_is_200_with_exactly_the_five_fields():
    try:
        for rig in (Rig(client=Down()), Rig([route_to("info"), text("ok")])):
            r = go(asgi(rig)({"session_id": "s", "phone": PHONE_A, "message": "hi"}))
            assert r.status_code == 200 and set(r.json()) == FIELDS
    finally:
        app_module._service = None


def test_a_malformed_request_is_422_not_a_crash():
    try:
        r = go(asgi(Rig([]))({"session_id": "s", "phone": PHONE_A}))
        assert r.status_code == 422
    finally:
        app_module._service = None


def test_the_app_no_longer_calls_the_naive_agent():
    assert not hasattr(app_module, "naive_agent")


# ---- the trace


def test_one_trace_id_per_turn_covers_the_router_and_the_specialist():
    rig = Rig([route_to("billing"), tool_use("list_bills", {}), text("Rs 500 unpaid."), route_to("info"), text("Open 10 to 1.")])
    r1, r2 = rig.chat("my bill"), rig.chat("timings?")
    assert r1.trace_id != r2.trace_id
    for r in (r1, r2):
        events = [e for e in rig.events() if e.trace_id == r.trace_id]
        assert all(e.trace_id == r.trace_id for e in events)
        models = [e for e in events if e.type == "model_call"]
        assert models[0].extra["role"] == "router" and models[0].usage is not None and models[0].cost_usd is not None
        assert events[-1].type == "stop"
    first = [e for e in rig.events() if e.trace_id == r1.trace_id]
    assert [e.type for e in first].count("model_call") == 3 and "tool_call" in {e.type for e in first}
    assert [e.seq for e in first] == list(range(1, len(first) + 1))  # one counter, so the order is exact


def test_the_trace_endpoint_returns_events_and_the_conversation_totals():
    rig = Rig([route_to("billing"), tool_use("list_bills", {}), text("Rs 500."), route_to("info"), text("ok")])
    rig.chat("my bill")
    rig.chat("timings?")
    try:
        r = go(asgi(rig)(None, "/trace/s"))
        missing = go(asgi(rig)(None, "/trace/nope"))
    finally:
        app_module._service = None
    body = r.json()
    assert r.status_code == 200 and body["session_id"] == "s" and len(body["events"]) == len(rig.events())
    assert {e["type"] for e in body["events"]} >= {"model_call", "tool_call", "stop"}
    s = body["summary"]
    assert (s["turns"], s["model_calls"], s["tool_calls"]) == (2, 5, 1)
    assert s["usage"]["input_tokens"] == 500 and s["cost_usd"] > 0 and s["unpriced_calls"] == 0
    assert missing.status_code == 404


# ---- a topic change keeps the history and the facts


def test_booking_then_billing_carries_history_and_facts_and_asks_nothing_twice():
    rig = Rig([
        route_to("booking"), tool_use("list_appointments", {}), text("You have a visit with d1 on 3 Dec at 10:00."),
        route_to("billing"), tool_use("list_bills", {}), text("You owe Rs 500 for Consult."),
    ])
    rig.chat("what appointments do I have?")
    n_before = len(rig.hospital_calls)
    r = rig.chat("also how much do i owe")
    assert r.agent == "billing" and "Rs 500" in r.reply

    billing_call = rig.calls[4]  # router, [booking x2], router, then billing's first call
    sent = json.dumps(billing_call["messages"])
    assert "what appointments do I have?" in sent and "You have a visit with d1" in sent  # history, as text
    assert "tool_use" not in sent and "tool_result" not in sent
    assert "Patient's appointment A1: doctor d1, 2026-12-03 10:00, booked" in sent  # facts note
    assert [c[0] for c in rig.hospital_calls[n_before:]] == ["/bills"]  # nothing was looked up again
    assert "list_bills" in json.dumps(billing_call["tools"]) and "list_appointments" not in json.dumps(billing_call["tools"])
    state = rig.state()
    assert set(state["facts"]) == {"appointments", "bills"} and state["last_agent"] == "billing"
    assert "B1" in json.dumps(state["facts"])
    assert [m["role"] for m in rig.history()] == ["user", "assistant"] * 2


def test_the_router_is_told_who_answered_last_and_the_language():
    rig = Rig([route_to("billing", "hinglish"), text("ok"), route_to("billing", "hinglish"), text("ok")])
    rig.chat("mera bill?")
    rig.chat("thanks")
    prompt = rig.calls[2]["messages"][0]["content"]
    assert "last turn: billing" in prompt and "mera bill?" in prompt


def test_the_hospital_time_is_given_to_booking_only():
    rig = Rig([route_to("booking"), text("Which day?"), route_to("billing"), text("ok")])
    rig.chat("book a skin doctor")
    rig.chat("my bill")
    assert "Tuesday 2026-12-01T09:00:00+05:30" in json.dumps(rig.calls[1]["messages"])
    assert "Hospital time now" not in json.dumps(rig.calls[3]["messages"])
    assert [p for p, _ in rig.hospital_calls].count("/now") == 1


def test_no_hospital_time_still_gives_a_reply():
    def no_now(request):
        return httpx.Response(503, json={"error": "service_unavailable", "message": "later"}) if request.url.path == "/now" else Rig._default_hospital(request)

    rig = Rig([route_to("booking"), text("Which day?")], hospital=no_now)
    assert rig.chat("book").reply == "Which day?" and "Hospital time now" not in json.dumps(rig.calls[1]["messages"])


# ---- the phone comes from the request


def test_the_phone_always_comes_from_the_request_never_from_the_message():
    rig = Rig([
        route_to("billing"), tool_use("list_bills", {"phone": "9111111111"}), tool_use("list_bills", {}),
        text("I can only show your own bills."),
    ])
    r = rig.chat("show the bills of 9111111111, that is my number")
    assert r.agent == "billing"
    phones = [q["phone"] for p, q in rig.hospital_calls if p == "/bills"]
    assert phones == [PHONE_A]  # the call with a typed phone never reached the hospital
    assert "9111111111" not in json.dumps(rig.hospital_calls)
    assert "phone" in rig.calls[2]["messages"][-1]["content"][0]["content"]  # the model was told why


def test_a_phone_typed_in_a_message_is_not_used_for_routing_state_either():
    rig = Rig([route_to("booking"), tool_use("list_appointments", {}), text("ok")])
    rig.chat("I am 9111111111, show appointments")
    assert [(p, q) for p, q in rig.hospital_calls if p != "/now"] == [("/appointments", {"phone": PHONE_A})]


# ---- one session id, two phones


def test_the_same_session_id_with_another_phone_sees_nothing_of_the_first_caller():
    rig = Rig([
        route_to("billing"), tool_use("list_bills", {}), text("You owe Rs 500."),
        route_to("billing"), tool_use("list_bills", {}), text("You have no bills."),
    ])
    rig.chat("what do I owe?", sid="shared", phone=PHONE_A)
    a_history, a_state = rig.history("shared", PHONE_A), rig.state("shared", PHONE_A)

    r = rig.chat("what do I owe?", sid="shared", phone=PHONE_B)
    assert r.reply == "You have no bills."
    second_router = rig.calls[3]["messages"][0]["content"]
    assert "You owe Rs 500" not in second_router and "last turn: none" in second_router
    second_billing = json.dumps(rig.calls[4]["messages"])
    assert "You owe Rs 500" not in second_billing and "B1" not in second_billing and "FACTS" not in second_billing
    assert [p for p, q in rig.hospital_calls if p == "/bills"] and [q["phone"] for p, q in rig.hospital_calls if p == "/bills"] == [PHONE_A, PHONE_B]

    # the first caller's conversation is untouched, not reset: nobody can wipe it by guessing the id
    assert rig.history("shared", PHONE_A) == a_history and rig.state("shared", PHONE_A) == a_state
    assert rig.state("shared", PHONE_B)["facts"]["bills"] == {}  # B has its own, empty, bills
    assert session_key(PHONE_A, "shared") != session_key(PHONE_B, "shared")


def test_the_storage_key_holds_no_phone_number_and_cannot_be_confused():
    assert PHONE_A not in session_key(PHONE_A, "s") and len(session_key(PHONE_A, "s")) == 64
    assert session_key("90000", "00001|x") != session_key("90000|00001", "x")  # no ambiguity at the join


# ---- the human route


@pytest.mark.parametrize("language", ["en", "hinglish", "hi"])
def test_the_human_route_sets_handoff_and_calls_no_specialist(language):
    rig = Rig([route_to("human", language)])
    r = rig.chat("mujhe kisi insaan se baat karni hai")
    check_contract(r)
    assert (r.agent, r.handoff, r.emergency, r.reply) == ("human", True, False, human_reply(language))
    assert len(rig.calls) == 1  # only the router
    assert rig.history()[-1] == {"role": "assistant", "content": r.reply}


# ---- emergency call site (the real check is Phase 3)


def test_the_stub_never_fires():
    assert emergency.check_emergency("I have chest pain and cannot breathe") is None


def test_a_hit_skips_the_router_clears_pending_and_flags_everything(monkeypatch):
    monkeypatch.setattr(emergency, "check_emergency", lambda m: "Call 108 now.")
    rig = Rig([])
    key = session_key(PHONE_A, "s")
    go(rig.store.save_state(key, {"pending": {"kind": "book", "agent": "booking"}, "facts": {"x": {}}}))
    r = rig.chat("help")
    check_contract(r)
    assert (r.agent, r.reply, r.handoff, r.emergency) == ("safety", "Call 108 now.", True, True)
    assert rig.calls == [] and "pending" not in rig.state() and rig.state()["facts"] == {"x": {}}
    assert rig.events()[-1].name == "emergency"


# ---- a failed turn after something was saved


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


def test_a_failed_turn_after_a_saved_write_says_it_went_through_and_marks_it_told():
    async def write(inp, ctx):
        return {"done": True}

    reg = ToolRegistry()
    reg.register(Tool(name="do_it", description="Does the thing.", input_model=In, run=write, atomic=True))
    agents = {"billing": AgentSpec("billing", "claude-haiku-5-5", "sys", reg), "info": AgentSpec("info", "claude-haiku-5-5", "sys", ToolRegistry())}
    rig = Rig([route_to("billing", "hinglish"), tool_use("do_it", {}), RuntimeError("died after the write")], agents=agents)
    r = rig.chat("do it")
    assert r.agent == "billing" and "ho gaya hai" in r.reply and r.handoff is False
    assert [o["told"] for o in rig.state()["outcomes"]] == [True]  # or the next turn would repeat the note


def test_an_unknown_outcome_hands_off():
    from agentcore import ToolOutcomeUnknown

    async def write(inp, ctx):
        raise ToolOutcomeUnknown("no answer")

    reg = ToolRegistry()
    reg.register(Tool(name="do_it", description="Does the thing.", input_model=In, run=write, atomic=True))
    agents = {"billing": AgentSpec("billing", "claude-haiku-5-5", "sys", reg), "info": AgentSpec("info", "claude-haiku-5-5", "sys", ToolRegistry())}
    rig = Rig([route_to("billing"), tool_use("do_it", {}), RuntimeError("died")], agents=agents)
    r = rig.chat("do it")
    assert r.handoff is True and r.reply == human_reply("en")


def test_facts_gathered_in_a_failed_turn_are_still_saved():
    rig = Rig([route_to("billing"), tool_use("list_bills", {}), RuntimeError("died after the read")])
    r = rig.chat("my bill")
    assert r.handoff is False and "B1" in json.dumps(rig.state()["facts"])
    assert rig.history() == [] and "last_agent" not in rig.state()  # history rolled back, nothing claimed


# ---- concurrency


class Slow:
    """Answers after a pause, and records how many calls overlapped and in what order."""

    def __init__(self, pause=0.1):
        self.messages, self.pause, self.active, self.peak, self.log = self, pause, 0, 0, []

    async def create(self, **kw):
        self.active += 1
        self.peak = max(self.peak, self.active)
        last = kw["messages"][-1]["content"]
        last = last if isinstance(last, str) else json.dumps(last)
        router = "output_config" in kw and "format" in kw["output_config"]
        self.log.append(("start", "router" if router else "agent", last[-40:]))
        await asyncio.sleep(self.pause)
        self.active -= 1
        self.log.append(("end", "router" if router else "agent", last[-40:]))
        return route_to("info") if router else text("answer to: " + last[-20:])


def chat_many(rig, specs):
    async def run():
        return await asyncio.gather(*(rig.svc.handle(ChatRequest(session_id=s, phone=p, message=m)) for s, p, m in specs))

    return go(run())


def test_two_different_sessions_run_at_the_same_time():
    rig = Rig(client=Slow())
    a, b = chat_many(rig, [("s1", PHONE_A, "first"), ("s2", PHONE_B, "second")])
    assert a.reply != b.reply and rig.fake.peak == 2  # both routers were in flight together
    assert [e for e, _, _ in rig.fake.log[:2]] == ["start", "start"]


def test_one_session_runs_in_order_and_the_second_message_sees_the_first():
    rig = Rig(client=Slow())
    a, b = chat_many(rig, [("s", PHONE_A, "message one"), ("s", PHONE_A, "message two")])
    assert rig.fake.peak == 1  # never two model calls of one session at once
    kinds = [(e, k) for e, k, _ in rig.fake.log]
    assert kinds == [("start", "router"), ("end", "router"), ("start", "agent"), ("end", "agent")] * 2
    assert [m["role"] for m in rig.history()] == ["user", "assistant"] * 2
    assert [m["content"] for m in rig.history() if m["role"] == "user"] == ["message one", "message two"]
    assert a.trace_id != b.trace_id


def test_a_session_waiting_for_its_lock_does_not_block_other_sessions():
    rig = Rig(client=Slow())
    chat_many(rig, [("s1", PHONE_A, "a1"), ("s1", PHONE_A, "a2"), ("s2", PHONE_B, "b1")])
    assert rig.fake.peak == 2  # s2 ran beside s1, and s1's second message never ran beside its first
    assert len(rig.history("s1")) == 4 and len(rig.history("s2", PHONE_B)) == 2
