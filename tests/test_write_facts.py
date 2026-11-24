"""Every finished write leaves its result in the facts note, so a later "is it booked?" is not left to guess.

The model is scripted; what is checked is what the NEXT turn's request contains, from the real mock hospital.
"""

import pytest

from agent_service import prompts
from agent_service.facts import KEEP_PER_KIND, facts_note, merge_facts, write_facts
from agentcore import Done, Failed, Unknown
from tests.fake_claude import text, tool_use
from tests.test_confirm import ADMIN, BOOK, DR_RAO, PHONE, START, Rig, fresh, route_to  # noqa: F401
from tests.test_write_tools import script

TRUE = "Finished by the system, true (the system told the patient): "


def ask(rig, message="is it booked?"):
    """A follow-up turn. Returns the note and message the specialist model was given."""
    rig.say(message, route_to("booking"), text("ok"))
    return rig.fake.messages.calls[-1]["messages"][-1]["content"]


def test_a_finished_booking_is_in_the_next_turns_note_with_the_doctor_the_time_and_the_id():
    rig = Rig()
    rig.propose_booking()
    assert rig.say("yes").reply.startswith("Booked:")
    seen = ask(rig)
    assert TRUE + "booking DONE: appointment A1004 with " + DR_RAO in seen and "2026-12-04 17:00" in seen and "Asha" in seen
    assert "Patient's appointment A1004" in seen  # the appointment line is current too


def test_a_finished_cancellation_and_a_move_are_recorded():
    rig = Rig()
    rig.say("cancel", route_to("booking"), tool_use("cancel_appointment", {"appointment_id": "A1001"}), text("ok"))
    rig.say("yes")
    seen = ask(rig, "is it cancelled?")
    assert TRUE + "cancellation DONE: appointment A1001" in seen and "cancelled" in seen
    assert "A1001: doctor" in seen and ", cancelled" in seen  # the appointment fact shows the new status


def test_a_move_is_recorded_from_and_to():
    free = ADMIN.get("/doctors/d3/slots", params={"date": "2026-12-04"}).json()["free"][0]  # a real free slot of A1001's own doctor
    rig = Rig()
    rig.say("move", route_to("booking"), tool_use("reschedule_appointment", {"appointment_id": "A1001", "new_start": free}), text("ok"))
    assert rig.say("yes").reply.startswith("Done:")
    seen = ask(rig)
    assert "reschedule DONE: appointment A1001" in seen and f"from 2026-12-02 10:30 to {free[:10]} {free[11:16]}" in seen


def test_a_payment_link_is_recorded_with_the_bill_and_the_link():
    rig = Rig()
    rig.say("link", route_to("billing"), tool_use("create_payment_link", {"bill_id": "B1"}), text("ok"))
    rig.say("yes")
    rig.say("was it sent?", route_to("billing"), text("ok"))
    seen = rig.fake.messages.calls[-1]["messages"][-1]["content"]
    assert "payment link DONE: bill B1" in seen and "https://pay.citycare.example/b/B1/" in seen


def test_a_failed_write_is_recorded_as_not_done(monkeypatch):
    rig = Rig()
    rig.say("cancel", route_to("booking"), tool_use("cancel_appointment", {"appointment_id": "A1001"}), text("ok"))
    script(monkeypatch, before=[False, False, True])  # the gate's reads pass, the cancel itself is turned away before saving
    assert "nothing was changed" in rig.say("yes").reply
    seen = ask(rig)
    assert "cancellation NOT DONE: it failed and nothing was changed" in seen and "cancellation DONE" not in seen


def test_a_lost_race_is_recorded_as_not_done_because_the_slot_was_taken(monkeypatch):
    rig = Rig()
    rig.propose_booking()
    script(monkeypatch, race=[True])
    rig.say("yes")
    assert "booking NOT DONE: it failed and nothing was changed (the slot was taken)" in ask(rig)


def test_an_unknown_result_tells_the_model_to_say_neither():
    rig = Rig()
    rig.propose_booking()
    rig.wire.timeout_on = lambda request: request.url.path == "/appointments"
    assert rig.say("yes").handoff is True
    rig.wire.timeout_on = None
    seen = ask(rig)
    assert "booking UNKNOWN" in seen and "Do not say it was done and do not say it was not" in seen


def test_a_409_on_the_patients_own_booking_is_recorded_as_not_new():
    rig = Rig()
    rig.propose_booking()
    ADMIN.post("/appointments", json={"doctor_id": "d2", "start": START, "patient_name": "Asha", "phone": PHONE})
    assert "already have this appointment" in rig.say("yes").reply
    seen = ask(rig)
    assert "booking NOT NEW: the patient already holds this appointment" in seen and "NOT DONE" not in seen


def test_a_turn_with_no_write_adds_nothing():
    rig = Rig()
    rig.say("hi", route_to("booking"), text("Hello"))
    assert "writes" not in (rig.state().get("facts") or {})
    assert TRUE not in ask(rig)


# ---- the pieces


def test_write_facts_for_each_status():
    pending = {"key": "abcdef123456", "display": {"doctor": "Dr. Rao", "when": "2026-12-04T17:00:00+05:30", "patient": "Asha"}}
    done = write_facts("book_appointment", pending, Done({"id": "A9", "start": "2026-12-04T17:00:00+05:30"}))
    assert done["writes"] == {"book_appointment:abcdef12": "booking DONE: appointment A9 with Dr. Rao at 2026-12-04 17:00 for Asha"}
    assert done["appointments"]["A9"]["id"] == "A9"
    assert "NOT DONE" in write_facts("book_appointment", pending, Failed("x"))["writes"]["book_appointment:abcdef12"]
    assert "UNKNOWN" in write_facts("book_appointment", pending, Unknown("x"))["writes"]["book_appointment:abcdef12"]
    assert "appointments" not in write_facts("create_payment_link", pending, Done({"bill_id": "B1", "url": "u"}))


def test_the_note_lists_finished_writes_first_and_only_the_last_ten_are_kept():
    facts = {}
    for i in range(KEEP_PER_KIND + 3):
        facts = merge_facts(facts, {"writes": {f"k{i}": f"booking DONE: n{i}"}})
    assert list(facts["writes"]) == [f"k{i}" for i in range(3, KEEP_PER_KIND + 3)]
    note = facts_note({"writes": {"a": "booking DONE: x"}, "doctors": {"d1": {"name": "Dr. A", "specialty": "S"}}})
    assert note.index("booking DONE: x") < note.index("Doctors:")


@pytest.mark.parametrize("prompt", [prompts.BOOKING, prompts.BILLING])
def test_status_questions_are_answered_from_a_lookup_and_the_finished_lines(prompt):
    assert "call list_" in prompt and "finished by the system" in prompt and "Never contradict a finished line" in prompt


def test_booking_prompt_says_a_missing_slot_does_not_mean_not_booked():
    assert "A time missing from free_slots does not mean it was not booked" in prompts.BOOKING
