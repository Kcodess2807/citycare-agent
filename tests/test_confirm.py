"""The confirmation gate end to end: service + gate + write tools + the REAL mock hospital app.

The model is scripted. A "yes" never reaches a model: where a test says so, it checks the call count.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent_service.agents import build_agents
from agent_service.confirm import is_yes, when
from agent_service.hospital import HospitalClient
from agent_service.schemas import ChatRequest
from agent_service.service import ChatService, session_key
from agent_service.tools import _In, read_tools
from agentcore import Done, Tool
from agentcore.loop import AgentSpec
from agentcore.session import InMemorySessionStore
from agentcore.tools import ToolRegistry
from agentcore.trace import InMemoryTraceStore
from mock_hospital import app as mock
from mock_hospital import data as mock_data
from tests.async_fake import AsyncFake
from tests.fake_claude import text, tool_use
from tests.test_write_tools import Wire, script

PHONE = "9876543210"  # owns A1001 and bill B1
OTHER = "9123456780"  # owns A1002 (today 12:00) and B2, B3
START, LATER = "2026-12-04T17:00:00+05:30", "2026-12-04T17:30:00+05:30"
BOOK = {"doctor_id": "d2", "start": START, "patient_name": "Asha"}
ADMIN = TestClient(mock.app)
DR_RAO = next(d["name"] for d in mock_data.DOCTORS if d["id"] == "d2")


def go(coro):
    return asyncio.run(coro)


def route_to(agent, language="en"):
    return text(json.dumps({"agent": agent, "language": language}))


@pytest.fixture(autouse=True)
def fresh():
    ADMIN.post("/_admin/reset")
    ADMIN.post("/_admin/chaos", json={})
    yield
    ADMIN.post("/_admin/chaos", json={})


class Rig:
    """The real service, with write tools handed to the agents (the default wiring does not do that yet)."""

    def __init__(self, script_=None):
        self.wire = Wire()
        self.hospital = HospitalClient(base_url="http://hospital", transport=self.wire)
        self.fake = AsyncFake(script_ or [])
        self.store, self.traces = InMemorySessionStore(), InMemoryTraceStore()

        async def nosleep(d):
            pass

        self.svc = ChatService(self.fake, self.store, self.traces, self.hospital, sleep=nosleep, rng=lambda lo, hi: lo)
        self.svc.agents = build_agents(read_tools(self.hospital), self.svc.writes)

    def say(self, message, *replies, phone=PHONE, sid="s"):
        """Queue the model's replies for this message, then send it."""
        self.fake.messages.script.extend(replies)
        return go(self.svc.handle(ChatRequest(session_id=sid, phone=phone, message=message)))

    @property
    def model_calls(self):
        return len(self.fake.messages.calls)

    def state(self, phone=PHONE, sid="s"):
        return go(self.store.load(session_key(phone, sid))).state

    def history(self, phone=PHONE, sid="s"):
        return go(self.store.load(session_key(phone, sid))).messages

    def propose_booking(self, params=BOOK):
        return self.say("book Dr Rao friday 5pm", route_to("booking"), tool_use("book_appointment", params), text("One moment."))


def mine():
    return [a for a in ADMIN.get("/appointments", params={"phone": PHONE}).json()["appointments"] if a["id"] != "A1001"]


def tool_result_text(rig):
    """What the model was told about the tool call it just made."""
    block = rig.fake.messages.calls[-1]["messages"][-1]["content"][0]
    assert block["type"] == "tool_result"
    return block["content"]


# ---- pause: nothing is written, the reply is the code's read-back


def test_a_write_with_no_pending_action_pauses_and_the_reply_is_the_code_read_back():
    rig = Rig()
    r = rig.propose_booking()
    assert (r.agent, r.handoff, r.emergency) == ("booking", False, False)
    assert DR_RAO in r.reply and "Fri 4 Dec 2026, 5:00pm" in r.reply and "Asha" in r.reply and "yes" in r.reply
    assert "One moment" not in r.reply  # the model's own words are not what the patient reads
    assert mine() == [] and rig.wire.posts() == []  # nothing was written
    pending = rig.state()["pending"]
    assert pending["tool"] == "book_appointment" and pending["params"]["doctor_id"] == "d2" and pending["key"]
    assert rig.history()[-1] == {"role": "assistant", "content": r.reply}  # and the stored history says the same


def test_a_payment_link_read_back_names_the_bill_and_the_amount():
    rig = Rig()
    r = rig.say("send me the payment link", route_to("billing"), tool_use("create_payment_link", {"bill_id": "B1"}), text("ok"))
    assert "Consultation" in r.reply and "Rs 500" in r.reply and rig.wire.posts() == []


def test_cancel_and_reschedule_read_back_the_real_record_not_the_models_words():
    rig = Rig()
    r = rig.say("cancel my appointment", route_to("booking"), tool_use("cancel_appointment", {"appointment_id": "A1001"}), text("ok"))
    assert "cancel" in r.reply and "Wed 2 Dec 2026, 10:30am" in r.reply and rig.wire.posts() == []
    r = rig.say("move it", route_to("booking"), tool_use("reschedule_appointment", {"appointment_id": "A1001", "new_start": START}), text("ok"))
    assert "from Wed 2 Dec 2026, 10:30am to Fri 4 Dec 2026, 5:00pm" in r.reply and rig.wire.posts() == []


# ---- a clear yes: code runs the stored params


def test_a_clear_yes_makes_code_run_the_stored_params_once_and_says_booked_only_on_done():
    rig = Rig()
    rig.propose_booking()
    calls_before, key = rig.model_calls, rig.state()["pending"]["key"]
    r = rig.say("yes please book it")
    assert r.reply.startswith("Booked:") and DR_RAO in r.reply and "Fri 4 Dec 2026, 5:00pm" in r.reply
    assert (r.agent, r.handoff) == ("booking", False)
    assert rig.model_calls == calls_before  # no model was involved
    bookings = mine()
    assert len(bookings) == 1 and bookings[0]["start"] == START and bookings[0]["patient_name"] == "Asha"
    sent = json.loads(rig.wire.posts("/appointments")[0][2])
    assert len(rig.wire.posts("/appointments")) == 1 and sent["idempotency_key"] == key and sent["phone"] == PHONE
    assert "pending" not in rig.state()
    assert rig.history()[-1] == {"role": "assistant", "content": r.reply}


def test_the_payment_link_is_sent_in_the_reply_from_the_real_result():
    rig = Rig()
    rig.say("link please", route_to("billing"), tool_use("create_payment_link", {"bill_id": "B1"}), text("ok"))
    r = rig.say("haan kar do")
    assert "https://pay.citycare.example/b/B1/" in r.reply and len(mock.STATE.payment_links) == 1
    assert rig.state()["links"]["B1"]["url"] in r.reply


# ---- the yes vocabulary

PASS = [
    ("yes", None), ("Yes!", None), ("yess", None), ("YES PLEASE", None), ("ok", None), ("okay, go ahead", None),
    ("sure", None), ("confirm", None), ("yes please book it", "book_appointment"), ("yes, cancel it", "cancel_appointment"),
    ("yes send the link", "create_payment_link"), ("haan", None), ("haan ji", None), ("ji haan", None), ("haan kar do", None),
    ("theek hai", None), ("haan theek hai kar do", None), ("haan book kar do", "book_appointment"), ("bilkul",  None),
    ("हाँ", None), ("हां", None), ("ठीक है", None), ("जी हां", None), ("हां कर दो", None), ("हाँ बुक कर दो", "book_appointment"),
]
FAIL = [
    ("yes but make it 6pm", None), ("yes?", None), ("haan nahi", None), ("5", None), ("yes 6", None), ("12", None),
    ("no", None), ("nahi", None), ("नहीं", None), ("", None), ("   ", None), ("👍", None), ("please", None), ("book it", "book_appointment"),
    ("yes cancel it", "book_appointment"),  # an action word for another action
    ("yes please book it", "cancel_appointment"), ("yes but not tomorrow", None), ("haan lekin kal nahi", None),
    ("ok what is the fee", None), ("ठीक है लेकिन कल नहीं", None), ("hmm", None), ("yes and also cancel the other one", "book_appointment"),
    ("हाँ?", None), ("yes, my number is 9876543210", None),
]


@pytest.mark.parametrize("message,tool", PASS)
def test_yes_phrases_that_pass(message, tool):
    assert is_yes(message, tool), message


@pytest.mark.parametrize("message,tool", FAIL)
def test_phrases_that_are_not_a_yes(message, tool):
    assert not is_yes(message, tool), message


# ---- what a pending action survives


def test_a_non_yes_reply_clears_the_pending_action_and_nothing_is_written():
    rig = Rig()
    rig.propose_booking()
    r = rig.say("make it 6pm instead", route_to("booking"), text("Which doctor and day?"))
    assert r.reply == "Which doctor and day?" and "pending" not in rig.state()
    assert mine() == [] and rig.wire.posts() == []
    # and a yes afterwards finds nothing to run
    rig.say("yes", route_to("booking"), text("What should I book?"))
    assert mine() == [] and rig.wire.posts() == []


def test_yes_with_nothing_pending_writes_nothing():
    rig = Rig()
    r = rig.say("yes", route_to("booking"), text("What would you like me to book?"))
    assert r.reply == "What would you like me to book?"
    assert mine() == [] and rig.wire.posts() == []


def test_yes_sent_twice_writes_once():
    rig = Rig()
    rig.propose_booking()
    first = rig.say("yes")
    second = rig.say("yes", route_to("booking"), text("That is already booked."))
    assert first.reply.startswith("Booked:") and second.reply == "That is already booked."
    assert len(mine()) == 1 and len(rig.wire.posts("/appointments")) == 1


def test_the_model_calls_the_tool_again_with_different_params_a_new_read_back_and_nothing_written():
    rig = Rig()
    first = rig.propose_booking()
    first_key = rig.state()["pending"]["key"]
    r = rig.say("make it 5:30pm", route_to("booking"), tool_use("book_appointment", {**BOOK, "start": LATER}), text("ok"))
    assert "5:30pm" in r.reply and "5:00pm" not in r.reply and r.reply != first.reply
    pending = rig.state()["pending"]
    assert pending["params"]["start"].startswith("2026-12-04T17:30") and pending["key"] != first_key
    assert mine() == [] and rig.wire.posts() == []
    assert rig.say("yes").reply.startswith("Booked:") and [a["start"] for a in mine()] == [LATER]


def test_two_write_calls_in_one_turn_pause_once_and_nothing_runs():
    rig = Rig()
    rig.say("book", route_to("booking"), tool_use("book_appointment", BOOK), tool_use("book_appointment", {**BOOK, "start": LATER}),
            text("ok"), text("ok"))
    assert mine() == [] and rig.wire.posts() == []


def test_an_emergency_message_while_something_is_pending_clears_it_and_writes_nothing():
    rig = Rig()
    rig.propose_booking()
    calls_before = rig.model_calls
    r = rig.say("mujhe seene mein dard hai aur saans nahi aa rahi")
    assert (r.agent, r.emergency, r.handoff) == ("safety", True, True) and "108" in r.reply
    assert "pending" not in rig.state() and rig.model_calls == calls_before
    rig.say("yes", route_to("booking"), text("What should I book?"))  # nothing left to confirm
    assert mine() == [] and rig.wire.posts() == []


# ---- the reply comes from the real result


def test_an_unknown_result_gives_the_handoff_reply_and_never_says_booked():
    rig = Rig()
    rig.propose_booking()
    rig.wire.timeout_on = lambda request: request.url.path == "/appointments"  # the write AND the check both get no answer
    r = rig.say("yes")
    assert r.handoff is True and r.agent == "booking"
    assert "booked" not in r.reply.lower() and "staff" in r.reply and "don't try it again" in r.reply
    assert "pending" not in rig.state()  # spent: the patient is not asked to confirm it again


def test_a_409_result_says_it_was_not_booked(monkeypatch):
    rig = Rig()
    rig.propose_booking()
    script(monkeypatch, race=[True])
    r = rig.say("yes")
    assert "not booked" in r.reply and "Booked:" not in r.reply and r.handoff is False
    assert mine() == [] and len(rig.wire.posts("/appointments")) == 1  # one try, no retry


def test_a_failed_write_that_did_not_save_says_nothing_was_changed(monkeypatch):
    rig = Rig()
    rig.say("cancel", route_to("booking"), tool_use("cancel_appointment", {"appointment_id": "A1001"}), text("ok"))
    # The mock rolls its 503 dice on reads too: the gate's two reads pass, then the cancel itself gets a 503 before saving.
    script(monkeypatch, before=[False, False, True])
    r = rig.say("yes")
    assert "nothing was changed" in r.reply and "cancelled" not in r.reply.lower() and r.handoff is False
    assert next(a for a in ADMIN.get("/appointments", params={"phone": PHONE}).json()["appointments"] if a["id"] == "A1001")["status"] == "booked"


def test_a_saved_then_503_cancel_is_still_reported_as_done(monkeypatch):
    rig = Rig()
    rig.say("cancel", route_to("booking"), tool_use("cancel_appointment", {"appointment_id": "A1001"}), text("ok"))
    script(monkeypatch, after=[True])  # saved, then 503: verify finds it cancelled
    r = rig.say("yes")
    assert "is cancelled" in r.reply and len(rig.wire.posts("/cancel")) == 1


def test_if_the_records_changed_after_the_read_back_nothing_runs():
    rig = Rig()
    rig.say("cancel", route_to("booking"), tool_use("cancel_appointment", {"appointment_id": "A1001"}), text("ok"))
    ADMIN.post("/appointments/A1001/cancel")  # someone else cancelled it in between
    r = rig.say("yes")
    assert "Something changed" in r.reply and rig.wire.posts("/cancel") == []


# ---- hindi and hinglish replies


def test_read_back_and_result_follow_the_patients_language():
    rig = Rig()
    r = rig.say("friday shaam 5 baje book karo", route_to("booking", "hinglish"), tool_use("book_appointment", BOOK), text("ok"))
    assert "Confirm karein" in r.reply and "haan" in r.reply
    assert "Book ho gaya" in rig.say("haan").reply
    ADMIN.post("/_admin/reset")
    rig = Rig()
    r = rig.say("बुक करें", route_to("booking", "hi"), tool_use("book_appointment", BOOK), text("ok"))
    assert "कृपया पुष्टि करें" in r.reply
    assert "बुक हो गया" in rig.say("हाँ").reply


# ---- ids: one answer for someone else's and a missing one, and nothing echoed


@pytest.mark.parametrize("tool,raw", [
    ("cancel_appointment", {"appointment_id": "{id}"}),
    ("reschedule_appointment", {"appointment_id": "{id}", "new_start": START}),
    ("create_payment_link", {"bill_id": "{id}"}),
])
def test_a_blocked_id_says_the_same_thing_for_someone_elses_and_a_missing_one_and_echoes_nothing(tool, raw):
    foreign, missing = ("B2", "B999") if tool == "create_payment_link" else ("A1002", "A999")
    told = []
    for ident in (foreign, missing):
        rig = Rig()
        agent = "billing" if tool == "create_payment_link" else "booking"
        r = rig.say("do it", route_to(agent), tool_use(tool, {k: (ident if v == "{id}" else v) for k, v in raw.items()}), text("I can't."))
        told.append(tool_result_text(rig))
        assert "pending" not in rig.state() and rig.wire.posts() == []
        assert r.reply == "I can't."
        for secret in (ident, "Mehta", "Skin biopsy", "12:00", "Arjun", "1200"):
            assert secret not in told[-1], secret
    assert told[0] == told[1]  # the model cannot tell the two apart


def test_a_made_up_doctor_is_refused_without_naming_one():
    rig = Rig()
    rig.say("book", route_to("booking"), tool_use("book_appointment", {**BOOK, "doctor_id": "d99"}), text("sorry"))
    assert "d99" not in tool_result_text(rig) and mine() == [] and "pending" not in rig.state()


def test_when_the_record_cannot_be_checked_nothing_is_proposed():
    rig = Rig()
    rig.wire.timeout_on = lambda request: request.url.path == "/appointments"
    rig.say("cancel", route_to("booking"), tool_use("cancel_appointment", {"appointment_id": "A1001"}), text("sorry"))
    assert "Could not check" in tool_result_text(rig) and "pending" not in rig.state()


# ---- the payment link record: loaded and saved every turn, failed turns included


class _Touch(_In):
    pass


def _touch_agent(rig):
    async def touch(inp, ctx):
        ctx.data["links"]["B1"] = {"in_doubt": True}  # what a lost payment-link answer leaves behind
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(Tool("touch_links", "test only", _Touch, touch))
    rig.svc.agents["billing"] = AgentSpec(name="billing", model="claude-haiku-5-5", system="test", tools=reg)


def test_the_payment_link_record_is_loaded_and_saved_every_turn_failed_turns_included():
    rig = Rig()
    _touch_agent(rig)
    go(rig.store.save_state(session_key(PHONE, "s"), {"links": {"B9": {"url": "https://old"}}}))
    # a turn that fails after the tool ran (the model ends with no text)
    r = rig.say("bills", route_to("billing"), tool_use("touch_links", {}), text(""))
    assert r.agent == "billing" and "pending" not in rig.state()
    assert rig.state()["links"] == {"B9": {"url": "https://old"}, "B1": {"in_doubt": True}}  # kept B9, saved B1
    # and the next turn is loaded with both
    seen = {}

    async def look(inp, ctx):
        seen.update(ctx.data["links"])
        return {}

    rig.svc.agents["billing"].tools.register(Tool("look_links", "test only", _Touch, look))
    rig.say("again", route_to("billing"), tool_use("look_links", {}), text("done"))
    assert seen == {"B9": {"url": "https://old"}, "B1": {"in_doubt": True}}


def test_a_link_in_doubt_survives_to_the_next_request_and_creates_no_second_link():
    rig = Rig()
    rig.say("link", route_to("billing"), tool_use("create_payment_link", {"bill_id": "B1"}), text("ok"))
    rig.wire.timeout_on = lambda request: request.url.path.endswith("/payment-link")
    first = rig.say("yes")
    assert first.handoff is True and rig.state()["links"]["B1"] == {"in_doubt": True}
    rig.wire.timeout_on = None
    rig.say("send it again", route_to("billing"), tool_use("create_payment_link", {"bill_id": "B1"}), text("ok"))
    second = rig.say("yes")
    assert second.handoff is True and "booked" not in second.reply.lower()
    assert len(rig.wire.posts("/payment-link")) == 1 and mock.STATE.payment_links == []  # nothing was ever created


def test_a_stored_link_is_returned_without_calling_the_hospital():
    rig = Rig()
    go(rig.store.save_state(session_key(PHONE, "s"), {"links": {"B1": {"url": "https://pay.example/stored"}}}))
    rig.say("link", route_to("billing"), tool_use("create_payment_link", {"bill_id": "B1"}), text("ok"))
    assert "https://pay.example/stored" in rig.say("yes").reply
    assert rig.wire.posts("/payment-link") == []


def test_the_date_format_has_weekday_date_and_time():
    assert when("2026-12-04T17:00:00+05:30") == "Fri 4 Dec 2026, 5:00pm"
    assert when("2026-12-01T00:05:00+05:30") == "Tue 1 Dec 2026, 12:05am"
    assert when("not a date") == "not a date"
