"""A 409 on a confirmed booking offers the next free slot with the same doctor as a new read-back and pending action.

Runs on the real mock hospital with its race scripted, like test_confirm.py.
"""

import asyncio
import datetime as dt
import json

import httpx
import pytest

from agent_service import prompts
from agent_service.gate import Gate
from agent_service.hospital import HospitalClient
from tests.fake_claude import text, tool_use
from tests.test_confirm import ADMIN, BOOK, DR_RAO, LATER, PHONE, START, Rig, fresh, mine, route_to  # noqa: F401
from tests.test_write_tools import script


def lost_race_rig(monkeypatch):
    rig = Rig()
    rig.propose_booking()
    script(monkeypatch, race=[True])  # someone else takes the slot between the read-back and the yes
    return rig


def test_a_409_offers_the_next_free_slot_with_the_same_doctor_as_a_full_read_back(monkeypatch):
    rig = lost_race_rig(monkeypatch)
    old_key = rig.state()["pending"]["key"]
    r = rig.say("yes")
    assert "not booked" in r.reply and "Booked:" not in r.reply and r.handoff is False
    assert DR_RAO in r.reply and "Fri 4 Dec 2026, 5:30pm" in r.reply and "Asha" in r.reply and "Reply yes to confirm" in r.reply
    pending = rig.state()["pending"]
    assert pending["tool"] == "book_appointment" and pending["params"]["doctor_id"] == "d2"
    assert pending["params"]["start"].startswith("2026-12-04T17:30") and pending["params"]["patient_name"] == "Asha"
    assert pending["key"] != old_key and pending["readback"] in r.reply
    assert mine() == [] and len(rig.wire.posts("/appointments")) == 1  # the offer itself wrote nothing


def test_a_yes_to_the_offer_books_that_slot_once(monkeypatch):
    rig = lost_race_rig(monkeypatch)
    rig.say("yes")
    r = rig.say("yes")
    assert r.reply.startswith("Booked:") and "5:30pm" in r.reply
    assert [a["start"] for a in mine()] == [LATER] and len(rig.wire.posts("/appointments")) == 2
    keys = {json.loads(b)["idempotency_key"] for _, _, b in rig.wire.posts("/appointments")}
    assert len(keys) == 2  # a new key for a new action


def test_anything_but_a_yes_to_the_offer_spends_it_and_writes_nothing(monkeypatch):
    rig = lost_race_rig(monkeypatch)
    rig.say("yes")
    rig.say("no thanks", route_to("booking"), text("Okay."))
    assert "pending" not in rig.state() and mine() == []


def test_the_offer_is_in_the_patients_language(monkeypatch):
    rig = Rig()
    rig.say("book karo", route_to("booking", "hinglish"), tool_use("book_appointment", BOOK), text("ok"))
    script(monkeypatch, race=[True])
    r = rig.say("haan")
    assert "Woh time abhi kisi aur ne le liya" in r.reply and "Confirm karein" in r.reply


def test_no_free_slot_to_offer_gives_the_plain_message_and_no_pending(monkeypatch):
    rig = lost_race_rig(monkeypatch)

    async def none(self, *a, **k):
        return None

    monkeypatch.setattr(Gate, "next_free_slot", none)
    r = rig.say("yes")
    assert "not booked" in r.reply and "Please confirm" not in r.reply and "pending" not in rig.state()


def test_a_reschedule_that_loses_the_race_offers_nothing(monkeypatch):
    rig = Rig()
    rig.say("move it", route_to("booking"), tool_use("reschedule_appointment", {"appointment_id": "A1001", "new_start": START}), text("ok"))
    script(monkeypatch, race=[True])
    r = rig.say("yes")
    assert "not moved" in r.reply and "pending" not in rig.state()


def test_a_failure_that_is_not_a_taken_slot_offers_nothing(monkeypatch):
    rig = Rig()
    rig.propose_booking()
    script(monkeypatch, before=[False, True, True, True])  # the gate read passes, then the write and both retries are turned away
    r = rig.say("yes")
    assert "nothing was changed" in r.reply and "pending" not in rig.state()


# ---- the search itself


def clinic(slots_by_day):
    def handler(request: httpx.Request) -> httpx.Response:
        day = request.url.params["date"]
        return httpx.Response(200, json={"doctor_id": "d1", "date": day, "free": slots_by_day.get(day, [])})

    gate = Gate(HospitalClient(base_url="http://h", transport=httpx.MockTransport(handler)), None, None)

    async def nosleep(d):
        pass

    gate._sleep, gate._rng = nosleep, lambda lo, hi: lo
    return gate


def run(gate, after):
    return asyncio.run(gate.next_free_slot("d1", dt.datetime.fromisoformat(after)))


def test_the_search_takes_the_first_later_slot_that_day():
    gate = clinic({"2026-12-04": ["2026-12-04T16:30:00+05:30", "2026-12-04T17:00:00+05:30", "2026-12-04T18:00:00+05:30", "2026-12-04T17:30:00+05:30"]})
    assert run(gate, "2026-12-04T17:00:00+05:30") == dt.datetime.fromisoformat("2026-12-04T17:30:00+05:30")  # not the same slot, not an earlier one


def test_the_search_moves_to_the_next_day_and_gives_up_after_a_week():
    gate = clinic({"2026-12-05": ["2026-12-05T10:00:00+05:30"]})
    assert run(gate, "2026-12-04T18:00:00+05:30") == dt.datetime.fromisoformat("2026-12-05T10:00:00+05:30")
    assert run(clinic({}), "2026-12-04T18:00:00+05:30") is None
    assert run(clinic({"2026-12-12": ["2026-12-12T10:00:00+05:30"]}), "2026-12-04T18:00:00+05:30") is None  # 8 days out: past the window


def test_the_search_gives_none_when_the_hospital_cannot_say():
    def down(request):
        return httpx.Response(503, json={"error": "service_unavailable", "message": "down"})

    gate = Gate(HospitalClient(base_url="http://h", transport=httpx.MockTransport(down)), None, None)

    async def nosleep(d):
        pass

    gate._sleep, gate._rng = nosleep, lambda lo, hi: lo
    assert run(gate, "2026-12-04T17:00:00+05:30") is None


# ---- the prompts


@pytest.mark.parametrize("prompt", [prompts.BOOKING, prompts.BILLING])
def test_write_prompts_say_do_not_ask_and_give_an_example(prompt):
    assert 'Do not ask "shall I' in prompt and "Example:" in prompt and "then write one short line" in prompt


def test_booking_picks_the_earliest_when_the_choice_is_open_and_billing_still_asks_which_bill():
    assert "pick the earliest match" in prompts.BOOKING and "Do not ask which one" in prompts.BOOKING
    assert "pick the earliest" not in prompts.BILLING and "several unpaid bills" in prompts.BILLING
