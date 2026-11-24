"""The write tools against the REAL mock hospital app, with scripted faults.

The mock's chaos functions are replaced by scripts so each test says exactly which call fails and how.
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_service.agents import build_agents
from agent_service.hospital import HospitalClient
from agent_service.tools import read_tools
from agent_service.write_tools import WRITE_TOOL_NAMES, write_tools
from agentcore import Done, Failed, ToolContext, Unknown
from agentcore.executor import Executor
from mock_hospital import app as mock

PHONE = "9876543210"  # owns A1001 (Dr. Iyer, tomorrow 10:30) and B1
ADMIN = TestClient(mock.app)
START = "2026-12-04T17:00:00+05:30"  # Dr. Rao (d2) is free here


def go(coro):
    return asyncio.run(coro)


class Wire(httpx.AsyncBaseTransport):
    """The real mock app behind a recorder. `timeout_on` makes matching requests time out."""

    def __init__(self, timeout_on=None):
        self.inner = httpx.ASGITransport(app=mock.app)
        self.seen: list[tuple[str, str, str]] = []
        self.timeout_on = timeout_on

    async def handle_async_request(self, request):
        self.seen.append((request.method, request.url.path, request.content.decode()))
        if self.timeout_on and self.timeout_on(request):
            raise httpx.ReadTimeout("no answer", request=request)
        return await self.inner.handle_async_request(request)

    def posts(self, fragment=""):
        return [s for s in self.seen if s[0] == "POST" and fragment in s[1]]


@pytest.fixture(autouse=True)
def fresh():
    ADMIN.post("/_admin/reset")
    ADMIN.post("/_admin/chaos", json={})
    yield
    ADMIN.post("/_admin/chaos", json={})


def script(monkeypatch, *, before=(), after=(), race=()):
    """Each list is consumed one call at a time. before: True = 503 before saving. after: True = saved, then 503.
    race: True = someone else takes the slot. Past the end of a list, nothing goes wrong."""
    before, after, race = list(before), list(after), list(race)

    async def chaos_before():
        if before and before.pop(0):
            return mock._err(503, "service_unavailable", "The hospital system is temporarily unavailable. Try again.")

    monkeypatch.setattr(mock, "_chaos_before", chaos_before)
    monkeypatch.setattr(mock, "_fail_after_commit", lambda: bool(after and after.pop(0)))
    monkeypatch.setattr(mock, "_lose_race", lambda: bool(race and race.pop(0)))


class Rig:
    def __init__(self, wire=None, key="key-1", links=None):
        self.wire = wire or Wire()
        self.client = HospitalClient(base_url="http://hospital", transport=self.wire)
        self.reg = write_tools(self.client)

        async def nosleep(d):
            pass

        self.ex = Executor(sleep=nosleep, rng=lambda lo, hi: lo)
        self.data = {"phone": PHONE, "idempotency_key": key, "links": {} if links is None else links}

    def run(self, name, raw):
        tool, inp = self.reg.parse(name, raw)
        assert not isinstance(tool, Failed), (name, raw)
        return go(self.ex.execute(tool, inp, ToolContext("s", self.data), state={}))


def appointments(phone=PHONE):
    return ADMIN.get("/appointments", params={"phone": phone}).json()["appointments"]


BOOK = {"doctor_id": "d2", "start": START, "patient_name": "Asha"}


def booked_at(start=START):
    return [a for a in appointments() if a["start"] == start and a["status"] == "booked"]


# ---- the eight cases


def test_book_gets_a_503_that_did_not_save_retries_with_the_same_key_exactly_one_booking(monkeypatch):
    script(monkeypatch, before=[True])
    rig = Rig()
    assert isinstance(rig.run("book_appointment", BOOK), Done)
    keys = [json.loads(body)["idempotency_key"] for _, _, body in rig.wire.posts("/appointments")]
    assert keys == ["key-1", "key-1"]
    assert len(booked_at()) == 1


def test_book_saved_then_503_verify_finds_it_exactly_one_booking(monkeypatch):
    # First attempt is saved and answers 503. Both retries are turned away before the key is even looked at,
    # so only verify can tell what happened.
    script(monkeypatch, before=[False, True, True], after=[True])
    rig = Rig()
    result = rig.run("book_appointment", BOOK)
    assert isinstance(result, Done) and result.value["start"] == START
    assert len(rig.wire.posts("/appointments")) == 3  # first try + 2 retries
    assert any(m == "GET" and p == "/appointments" for m, p, _ in rig.wire.seen)  # verify looked
    assert len(booked_at()) == 1


def test_book_saved_then_503_the_retry_gets_the_same_booking_back_through_the_key(monkeypatch):
    script(monkeypatch, after=[True])
    rig = Rig()
    assert isinstance(rig.run("book_appointment", BOOK), Done)
    assert len(rig.wire.posts("/appointments")) == 2 and len(booked_at()) == 1


def test_book_gets_409_and_does_not_retry(monkeypatch):
    script(monkeypatch, race=[True])
    rig = Rig()
    result = rig.run("book_appointment", BOOK)
    assert isinstance(result, Failed) and "slot_taken" in result.reason
    assert len(rig.wire.posts("/appointments")) == 1 and booked_at() == []


def test_reschedule_saved_then_503_does_not_retry_and_verify_says_done(monkeypatch):
    new = go(Rig().client.get("/doctors/d3/slots", date="2026-12-04"))["free"][0]
    script(monkeypatch, after=[True])
    rig = Rig()
    result = rig.run("reschedule_appointment", {"appointment_id": "A1001", "new_start": new})
    assert isinstance(result, Done) and result.value["start"] == new
    assert len(rig.wire.posts("/reschedule")) == 1
    assert next(a for a in appointments() if a["id"] == "A1001")["start"] == new


def test_cancel_503_that_did_not_save_verify_says_still_booked_result_is_failed(monkeypatch):
    script(monkeypatch, before=[True])
    rig = Rig()
    result = rig.run("cancel_appointment", {"appointment_id": "A1001"})
    assert isinstance(result, Failed) and "still booked" in result.reason
    assert len(rig.wire.posts("/cancel")) == 1
    assert next(a for a in appointments() if a["id"] == "A1001")["status"] == "booked"


def test_cancel_saved_then_503_verify_says_done(monkeypatch):
    script(monkeypatch, after=[True])
    assert isinstance(Rig().run("cancel_appointment", {"appointment_id": "A1001"}), Done)


def test_payment_link_503_is_retried(monkeypatch):
    script(monkeypatch, before=[True])
    rig = Rig()
    result = rig.run("create_payment_link", {"bill_id": "B1"})
    assert isinstance(result, Done) and result.value["url"].startswith("https://pay.citycare.example/b/B1/")
    assert len(rig.wire.posts("/payment-link")) == 2
    assert len(mock.STATE.payment_links) == 1


def test_payment_link_timeout_is_unknown_and_the_bill_is_marked_in_doubt():
    rig = Rig(Wire(timeout_on=lambda r: r.url.path.endswith("/payment-link")))
    result = rig.run("create_payment_link", {"bill_id": "B1"})
    assert isinstance(result, Unknown)
    assert len(rig.wire.posts("/payment-link")) == 1  # not retried
    assert rig.data["links"]["B1"] == {"in_doubt": True}
    # Asking again does not call the hospital: a link may already exist, so a person has to look.
    again = rig.run("create_payment_link", {"bill_id": "B1"})
    assert isinstance(again, Unknown) and len(rig.wire.posts("/payment-link")) == 1


def test_a_second_link_request_for_the_same_bill_returns_the_cached_one():
    rig = Rig()
    first, second = rig.run("create_payment_link", {"bill_id": "B1"}), rig.run("create_payment_link", {"bill_id": "B1"})
    assert isinstance(first, Done) and isinstance(second, Done) and first.value == second.value
    assert len(rig.wire.posts("/payment-link")) == 1 and len(mock.STATE.payment_links) == 1


# ---- guards around the tools


def test_book_refuses_without_a_confirmed_key():
    rig = Rig(key=None)
    result = rig.run("book_appointment", BOOK)
    assert isinstance(result, Failed) and rig.wire.posts() == []


def test_link_refuses_without_a_place_to_remember_it():
    rig = Rig()
    rig.data.pop("links")
    assert isinstance(rig.run("create_payment_link", {"bill_id": "B1"}), Failed) and rig.wire.posts() == []


@pytest.mark.parametrize("name,raw", [
    ("book_appointment", {**BOOK, "phone": "9999999999"}),
    ("book_appointment", {**BOOK, "idempotency_key": "mine"}),
    ("book_appointment", {**BOOK, "start": "2026-12-04T17:00:00"}),  # no time zone
    ("cancel_appointment", {"appointment_id": "A1001", "phone": "9999999999"}),
])
def test_the_model_cannot_supply_a_phone_a_key_or_a_time_without_a_zone(name, raw):
    assert isinstance(write_tools(HospitalClient(base_url="http://h")).parse(name, raw), Failed)


def test_a_booking_never_carries_the_phone_back_to_the_model():
    result = Rig().run("book_appointment", BOOK)
    assert "phone" not in result.value


def test_all_four_are_atomic_writes():
    reg = write_tools(HospitalClient(base_url="http://h"))
    assert tuple(reg.names()) == WRITE_TOOL_NAMES
    assert all(reg.get(n).atomic and "write" in reg.get(n).tags for n in reg.names())


def test_no_agent_has_a_write_tool_yet():
    """The gate (confirmation, ownership, 4-hour rule) comes before any agent gets one."""
    agents = build_agents(read_tools(HospitalClient(base_url="http://h")))
    for spec in agents.values():
        assert not set(spec.tools.names()) & set(WRITE_TOOL_NAMES)
    import agent_service.agents as agents_module
    import agent_service.service as service_module

    assert not hasattr(agents_module, "write_tools") and not hasattr(service_module, "write_tools")
