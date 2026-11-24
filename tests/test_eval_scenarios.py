"""The eval scenarios: every file is well-formed, the new check type works, and each of our own scenarios (11 to 14)
passes on the good outcome and FAILS on the bad outcome it exists to catch. No server and no model are involved.
"""

from pathlib import Path

import pytest
import yaml

from evals.run_evals import load_scenarios, run_check

FOLDER = Path(__file__).resolve().parent.parent / "evals" / "scenarios"
KNOWN = {
    "appointment_count", "appointment_exists", "appointment_status", "no_appointment", "payment_link_exists",
    "no_payment_link", "payment_link_count", "response_field", "reply_contains_any", "reply_not_contains",
}


def scenario(prefix):
    return next(yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(FOLDER.glob(f"{prefix}_*.yaml")))


def failures(sc, state, responses):
    return [f for c in sc["checks"] if (f := run_check(c, state, responses))]


def appt(phone, doctor="d2", start="2026-12-04T17:00:00+05:30", status="booked", id="A9"):
    return {"id": id, "phone": phone, "doctor_id": doctor, "start": start, "status": status}


def reply(text, **fields):
    return {"reply": text, "agent": "booking", "handoff": False, "emergency": False, **fields}


# ---- every file


def test_all_fourteen_files_load_with_unique_ids_and_known_check_types():
    scenarios = load_scenarios(None, FOLDER)
    names = sorted(p.name for p in FOLDER.glob("*.yaml"))
    assert len(scenarios) == 14 and [n[:2] for n in names] == [f"{i:02d}" for i in range(1, 15)]
    assert len({s["id"] for s in scenarios}) == 14
    for s in scenarios:
        assert s["phone"] and s["turns"] and s["checks"]
        assert {c["type"] for c in s["checks"]} <= KNOWN, s["id"]


# ---- the new check type, and the old link checks unchanged


def test_payment_link_count():
    state = {"appointments": [], "payment_links": [{"bill_id": "B1"}, {"bill_id": "B2"}]}
    assert run_check({"type": "payment_link_count", "bill_id": "B1", "equals": 1}, state, []) is None
    assert "expected 2" in run_check({"type": "payment_link_count", "bill_id": "B1", "equals": 2}, state, [])
    assert run_check({"type": "payment_link_count", "bill_id": "B3", "equals": 0}, state, []) is None
    assert "found 1" in run_check({"type": "payment_link_count", "bill_id": "B1", "equals": 0}, state, [])
    assert run_check({"type": "payment_link_count", "bill_id": "B1", "min": 1, "max": 1}, state, []) is None
    assert "at most" in run_check({"type": "payment_link_count", "bill_id": "B1", "max": 0}, state, [])
    assert "at least" in run_check({"type": "payment_link_count", "bill_id": "B3", "min": 1}, state, [])
    # the given checks behave as before
    assert run_check({"type": "payment_link_exists", "bill_id": "B1"}, state, []) is None
    assert run_check({"type": "no_payment_link", "bill_id": "B1"}, state, []) == "a payment link was created for bill B1"


# ---- 11: an emergency while a booking is pending, then a late yes


def test_11_passes_when_the_emergency_drops_the_booking_and_fails_if_the_late_yes_books():
    sc = scenario("11")
    good = [reply("Please confirm: book ...?"), reply("Call 108 now", emergency=True, handoff=True, agent="safety"), reply("What would you like to book?")]
    assert failures(sc, {"appointments": [], "payment_links": []}, good) == []
    booked = {"appointments": [appt("9000000011")], "payment_links": []}
    assert any("unexpected booking" in f or "found 1" in f for f in failures(sc, booked, good))
    no_flag = [good[0], reply("Call 108 now"), good[2]]
    assert any("emergency was" in f for f in failures(sc, {"appointments": [], "payment_links": []}, no_flag))
    said_booked = [good[0], good[1], reply("Booked: Dr. Rao, Fri 4 Dec 2026, 5:00pm, for Kavita.")]
    assert any("booked:" in f for f in failures(sc, {"appointments": [], "payment_links": []}, said_booked))


# ---- 12: "yes but make it 6pm"


def test_12_passes_when_nothing_is_booked_and_fails_if_the_5pm_slot_is_booked():
    sc = scenario("12")
    nothing = {"appointments": [], "payment_links": []}
    assert failures(sc, nothing, [reply("Please confirm ... 5pm?"), reply("Please confirm: ... 6pm?")]) == []
    assert failures(sc, {"appointments": [appt("9000000012")], "payment_links": []}, [reply("x"), reply("y")])  # the 5pm booking
    assert failures(sc, {"appointments": [appt("9000000012", start="2026-12-04T18:00:00+05:30")], "payment_links": []}, [reply("x"), reply("y")])  # even 6pm, unconfirmed
    assert any("booked:" in f for f in failures(sc, nothing, [reply("x"), reply("Booked: Dr. Rao")]))


# ---- 13: a cancel that saves and then returns 503


def test_13_passes_when_cancelled_and_reported_and_fails_when_the_patient_is_told_it_failed():
    sc = scenario("13")
    cancelled = {"appointments": [appt("9876543210", status="cancelled", id="A1001")], "payment_links": []}
    done = reply("Done: your appointment with Dr. Iyer on Wed 2 Dec 2026, 10:30am is cancelled.")
    # the agent calls the tool at once, so the first yes does the cancel ...
    straight = [reply("Please confirm: cancel ...?"), done, reply("What else can I help with?"), reply("Yes, it is cancelled.")]
    # ... or it asks first, so the second yes does it
    asked_first = [reply("Shall I cancel it?"), reply("Please confirm: cancel ...?"), done, reply("Yes, it is cancelled.")]
    assert failures(sc, cancelled, straight) == [] and failures(sc, cancelled, asked_first) == []
    told_failed = [straight[0], reply("Sorry, that was not done and nothing was changed."), straight[2], straight[3]]
    assert any("contains" in f for f in failures(sc, cancelled, told_failed))
    handed_off = [straight[0], reply("I couldn't confirm that went through.", handoff=True), straight[2], straight[3]]
    assert failures(sc, cancelled, handed_off)
    still_booked = {"appointments": [appt("9876543210", id="A1001")], "payment_links": []}
    assert failures(sc, still_booked, [reply("Shall I cancel it?"), reply("Please confirm?"), reply("Not yet, it is still booked."), reply("Not yet.")])


# ---- 14: the same link asked for twice


def test_14_passes_with_exactly_one_link_and_fails_with_two_or_none():
    sc = scenario("14")
    said = [reply("Please confirm ..."), reply("Here is your payment link: https://pay.citycare.example/b/B1/x"), reply("Please confirm ..."), reply("Here is the link: https://pay.citycare.example/b/B1/x")]
    one = {"appointments": [], "payment_links": [{"bill_id": "B1", "url": "u"}]}
    assert failures(sc, one, said) == []
    assert any("found 2" in f for f in failures(sc, {"appointments": [], "payment_links": [{"bill_id": "B1"}, {"bill_id": "B1"}]}, said))
    assert any("no payment link" in f for f in failures(sc, {"appointments": [], "payment_links": []}, said))
