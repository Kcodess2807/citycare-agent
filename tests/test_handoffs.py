"""Commit 4a: the 4-hour rule, contact_staff, two strikes and the router's emergency flag. The model is scripted.

The 4-hour tests use a small fake hospital so the appointment start can be set to the minute. One test
uses the real mock (A1002, today 12:00, three hours away).
"""

import asyncio
import json

import anthropic
import httpx
import pytest

from agent_service import emergency
from agent_service.agents import build_agents
from agent_service.hospital import HospitalClient
from agent_service.prompts import STAFF_REPLIES, human_reply, staff_reply
from agent_service.router import SCHEMA
from agent_service.schemas import ChatRequest
from agent_service.service import ChatService, session_key
from agent_service.tools import read_tools
from agentcore.session import InMemorySessionStore
from agentcore.trace import InMemoryTraceStore
from tests.async_fake import AsyncFake
from tests.fake_claude import text, tool_use
from tests.test_confirm import ADMIN, OTHER, Rig as MockRig, fresh, mine, route_to  # noqa: F401  (fresh is an autouse fixture)
from tests.test_service_chat import Rig as PlainRig, http_error

PHONE = "9000000001"
NOW = "2026-12-01T09:00:00+05:30"
NEW = "2026-12-04T17:00:00+05:30"
LIES = "Done! It is all cancelled and moved."  # what a model might say. None of these tests may let it through


def go(coro):
    return asyncio.run(coro)


class Clinic:
    """A fake hospital with ONE appointment for PHONE, A1, starting at `start`. Records every call."""

    def __init__(self, start, now_down=False):
        self.start, self.now_down, self.calls = start, now_down, []
        self.client = HospitalClient(base_url="http://h", transport=httpx.MockTransport(self))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        self.calls.append((request.method, p))
        if p == "/now":
            if self.now_down:
                return httpx.Response(503, json={"error": "service_unavailable", "message": "down"})
            return httpx.Response(200, json={"now": NOW, "weekday": "Tuesday", "timezone": "Asia/Kolkata"})
        if p == "/doctors":
            return httpx.Response(200, json={"doctors": [{"id": "d1", "name": "Dr. Iyer", "specialty": "Dermatology"}]})
        appt = {"id": "A1", "doctor_id": "d1", "start": self.start, "patient_name": "Asha", "phone": PHONE, "status": "booked"}
        if request.method == "GET" and p == "/appointments":
            return httpx.Response(200, json={"appointments": [appt]})
        return httpx.Response(200, json={**appt, "status": "cancelled"})  # only reached if a write got through

    @property
    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def service(clinic, script):
    async def nosleep(d):
        pass

    fake = AsyncFake(script)
    svc = ChatService(fake, InMemorySessionStore(), InMemoryTraceStore(), clinic.client, sleep=nosleep, rng=lambda lo, hi: lo)
    return svc, fake


def say(svc, message, phone=PHONE, sid="s"):
    return go(svc.handle(ChatRequest(session_id=sid, phone=phone, message=message)))


CANCEL = ("cancel_appointment", {"appointment_id": "A1"})
RESCHEDULE = ("reschedule_appointment", {"appointment_id": "A1", "new_start": NEW})


# ---- the 4-hour rule


@pytest.mark.parametrize("tool,params", [CANCEL, RESCHEDULE])
def test_cancel_or_reschedule_inside_4_hours_changes_nothing_and_sets_handoff(tool, params):
    clinic = Clinic("2026-12-01T12:59:00+05:30")  # 3h59m away
    svc, _ = service(clinic, [route_to("booking"), tool_use(tool, params), text(LIES)])
    r = say(svc, "please do it")
    assert (r.handoff, r.emergency, r.agent) == (True, False, "booking")
    assert r.reply == staff_reply("soon", "en") and "Nothing was changed" in r.reply
    assert clinic.posts == []
    assert "pending" not in go(svc.store.load(session_key(PHONE, "s"))).state  # nothing waits for a yes


def test_the_same_rule_on_the_real_mock_for_a_three_hour_appointment():
    rig = MockRig()
    r = rig.say("cancel it", route_to("booking"), tool_use("cancel_appointment", {"appointment_id": "A1002"}), text(LIES), phone=OTHER)
    assert r.handoff is True and r.reply == staff_reply("soon", "en")
    assert rig.wire.posts() == []
    assert next(a for a in ADMIN.get("/appointments", params={"phone": OTHER}).json()["appointments"] if a["id"] == "A1002")["status"] == "booked"


@pytest.mark.parametrize("tool,params", [CANCEL, RESCHEDULE])
def test_exactly_4_hours_is_allowed(tool, params):
    clinic = Clinic("2026-12-01T13:00:00+05:30")
    svc, _ = service(clinic, [route_to("booking"), tool_use(tool, params), text("ok")])
    r = say(svc, "please do it")
    assert r.handoff is False and "Please confirm" in r.reply  # the read-back, like any other write
    assert go(svc.store.load(session_key(PHONE, "s"))).state["pending"]["tool"] == tool
    assert clinic.posts == []  # and it still waits for the yes


def test_a_passed_start_goes_to_staff():
    clinic = Clinic("2026-12-01T08:00:00+05:30")  # an hour ago
    svc, _ = service(clinic, [route_to("booking"), tool_use(*CANCEL), text(LIES)])
    r = say(svc, "cancel it")
    assert r.handoff is True and r.reply == staff_reply("soon", "en") and clinic.posts == []


def test_when_now_cannot_be_read_nothing_changes_and_it_hands_off():
    clinic = Clinic("2026-12-05T10:00:00+05:30", now_down=True)  # days away, but we cannot tell what time it is
    svc, _ = service(clinic, [route_to("booking"), tool_use(*CANCEL), text(LIES)])
    r = say(svc, "cancel it")
    assert r.handoff is True and r.reply == staff_reply("clock", "en") and "couldn't check the time" in r.reply
    assert clinic.posts == [] and sum(1 for c in clinic.calls if c == ("GET", "/now")) == 1 + 3  # the facts note once, then the gate: tried, retried twice, gave up


def test_a_booking_is_not_held_to_the_rule():
    clinic = Clinic("2026-12-01T09:30:00+05:30")
    book = ("book_appointment", {"doctor_id": "d1", "start": NEW, "patient_name": "Asha"})
    svc, _ = service(clinic, [route_to("booking"), tool_use(*book), text("ok")])
    r = say(svc, "book it")
    assert r.handoff is False and "Please confirm" in r.reply and clinic.calls.count(("GET", "/now")) == 1  # only the facts note read it, the gate did not


def test_if_the_appointment_moves_inside_4_hours_between_the_read_back_and_the_yes_nothing_runs():
    clinic = Clinic("2026-12-02T10:00:00+05:30")
    svc, fake = service(clinic, [route_to("booking"), tool_use(*CANCEL), text("ok")])
    assert "Please confirm" in say(svc, "cancel it").reply
    clinic.start = "2026-12-01T10:00:00+05:30"  # now an hour away
    r = say(svc, "yes")
    assert r.handoff is True and r.reply == staff_reply("soon", "en") and clinic.posts == []


def test_the_staff_texts_exist_in_three_languages_and_never_claim_a_change():
    for by_language in STAFF_REPLIES.values():
        assert set(by_language) == {"en", "hinglish", "hi"} and len(set(by_language.values())) == 3
        assert all("020-4000-1234" in t for t in by_language.values())
    assert staff_reply("soon", "hi") == STAFF_REPLIES["soon"]["hi"] and staff_reply("clock", None) == STAFF_REPLIES["clock"]["en"]
    assert staff_reply("asked", "hinglish") == human_reply("hinglish")  # any other kind is the plain handoff text


# ---- contact_staff


def test_contact_staff_sets_handoff_and_the_reply_is_the_fixed_text_not_the_models():
    rig = PlainRig([route_to("info"), tool_use("contact_staff", {"reason": "wants a person"}), text("I have told the doctor personally!")])
    r = rig.chat("I want to speak to someone")
    assert r.handoff is True and r.agent == "info" and r.reply == human_reply("en")
    assert any(e.type == "stop" and e.name == "handoff:asked" and "wants a person" in e.output for e in rig.events())


def test_contact_staff_replies_in_the_patients_language():
    rig = PlainRig([route_to("billing", "hinglish"), tool_use("contact_staff", {"reason": "upset"}), text("ok")])
    assert rig.chat("mujhe kisi se baat karni hai").reply == human_reply("hinglish")


def test_an_ordinary_turn_does_not_hand_off():
    rig = PlainRig([route_to("info"), text("We are open 9 to 8.")])
    r = rig.chat("when are you open")
    assert r.handoff is False and r.reply == "We are open 9 to 8."


# ---- two strikes


class Flaky:
    """The scripted model, until `down` is set: then every call is a 529."""

    def __init__(self, script):
        self.inner, self.down, self.messages = AsyncFake(script), False, self

    async def create(self, **kw):
        if self.down:
            raise http_error(anthropic.InternalServerError, 529)
        return await self.inner.messages.create(**kw)


def strikes(rig):
    return rig.state().get("strikes")


def test_two_failed_turns_in_a_row_hand_off_and_a_good_turn_resets_the_count():
    flaky = Flaky([route_to("info"), text("We are open 9 to 8.")])
    rig = PlainRig(client=flaky)
    flaky.down = True
    first = rig.chat("when are you open")
    assert first.handoff is False and strikes(rig) == 1
    flaky.down = False
    assert rig.chat("when are you open").handoff is False and strikes(rig) == 0  # a good turn resets
    flaky.down = True
    assert rig.chat("when are you open").handoff is False and strikes(rig) == 1  # one strike again, not two
    second = rig.chat("hello?")
    assert second.handoff is True and second.reply == human_reply("en") and strikes(rig) == 0  # handed off, count starts over
    assert any(e.type == "stop" and e.name == "handoff:strikes" for e in rig.events())


def test_an_unusable_router_answer_is_a_strike_even_when_the_specialist_then_answers():
    rig = PlainRig([text("not json"), text("We are open 9 to 8."), text("still not json"), text("Same answer.")])
    first = rig.chat("when are you open")
    assert first.handoff is False and first.reply == "We are open 9 to 8." and strikes(rig) == 1
    second = rig.chat("and on sunday?")
    assert second.handoff is True and second.reply == human_reply("en")


def test_a_handoff_that_already_happened_does_not_leave_a_strike_behind():
    rig = PlainRig([route_to("human")])
    assert rig.chat("give me a person").handoff is True and strikes(rig) == 0


# ---- the router's emergency flag


def test_the_router_flag_takes_the_emergency_path_and_clears_pending():
    rig = MockRig()
    rig.propose_booking()
    assert "pending" in rig.state()
    msg = "mujhe chakkar aa raha hai aur sab dhundhla dikh raha hai"
    assert not emergency.is_emergency(msg)  # the word lists miss it, which is what the second layer is for
    calls = rig.model_calls
    flagged = text(json.dumps({"agent": "info", "language": "hinglish", "emergency_suspected": True}))
    r = rig.say(msg, text('{"decision": "other"}'), flagged)  # a pending action: the yes layer looks first, then the router
    assert (r.agent, r.emergency, r.handoff) == ("safety", True, True)
    assert r.reply == emergency.reply_for("hinglish") and "108" in r.reply
    assert "pending" not in rig.state() and rig.model_calls == calls + 2  # only the yes layer and the router ran: no specialist, no tool
    assert rig.wire.posts() == [] and mine() == []
    rig.say("yes", route_to("booking"), text("What should I book?"))  # nothing is left to confirm
    assert rig.wire.posts() == []


def test_the_flag_is_required_by_the_schema_and_defaults_to_no_if_missing():
    assert "emergency_suspected" in SCHEMA["required"] and SCHEMA["properties"]["emergency_suspected"] == {"type": "boolean"}
    rig = PlainRig([route_to("info"), text("We are open 9 to 8.")])  # route_to sends no flag: read as "no"
    r = rig.chat("when are you open")
    assert r.emergency is False and r.agent == "info"


def test_the_emergency_reply_is_in_the_routers_language():
    assert len({emergency.reply_for(l) for l in ("hi", "en", "hinglish")}) == 3 and emergency.reply_for(None) == emergency.reply_for("en")
