import asyncio

import httpx
import pytest

from agentcore import Done, Failed, ToolContext, ToolFailed, ToolOutcomeUnknown, ToolRetryable
from agentcore.executor import Executor
from agent_service.hospital import HospitalClient
from agent_service.tools import read_tools


def go(coro):
    return asyncio.run(coro)


def client(handler):
    return HospitalClient(base_url="http://hospital", transport=httpx.MockTransport(handler))


def ctx(**data):
    return ToolContext(session_id="s", data={"facts": {}, **data})


def call(reg, name, raw, c):
    tool, inp = reg.parse(name, raw)
    return go(tool.run(inp, c))


APPT = {"id": "A1", "doctor_id": "d1", "start": "2026-12-03T10:00:00+05:30", "patient_name": "Asha", "phone": "9000000001", "status": "booked"}
BILL = {"id": "B1", "phone": "9000000001", "amount_inr": 500, "description": "Consult", "date": "2026-11-20", "status": "unpaid"}


def hospital(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    if p == "/doctors":
        return httpx.Response(200, json={"doctors": [{"id": "d1", "name": "Dr. A", "specialty": "Dermatology"}], "specialties": ["Dermatology"]})
    if p.endswith("/slots"):
        return httpx.Response(200, json={"doctor_id": "d1", "date": request.url.params["date"], "free": ["2026-12-04T10:00:00+05:30"]})
    if p == "/appointments":
        return httpx.Response(200, json={"appointments": [a for a in [APPT] if a["phone"] == request.url.params["phone"]]})
    if p == "/bills":
        return httpx.Response(200, json={"bills": [b for b in [BILL] if b["phone"] == request.url.params["phone"]]})
    return httpx.Response(404, json={"error": "nope", "message": "no such path"})


REG = read_tools(client(hospital))


# ---- own data: the phone comes from the service, never from the model


@pytest.mark.parametrize("name", ["list_appointments", "list_bills"])
def test_model_cannot_supply_a_phone(name):
    out = REG.parse(name, {"phone": "9999999999"})
    assert isinstance(out, Failed) and "phone" in out.reason


@pytest.mark.parametrize("name", ["list_appointments", "list_bills"])
def test_no_verified_phone_means_no_read(name):
    with pytest.raises(ToolFailed):
        call(REG, name, {}, ctx())


def test_reads_use_the_session_phone_and_hide_it_from_the_model():
    out = call(REG, "list_appointments", {}, ctx(phone="9000000001"))
    assert [a["id"] for a in out["appointments"]] == ["A1"] and "phone" not in out["appointments"][0]
    assert call(REG, "list_appointments", {}, ctx(phone="9111111111")) == {"appointments": []}
    assert "phone" not in call(REG, "list_bills", {}, ctx(phone="9000000001"))["bills"][0]


def test_only_read_tools_and_contact_staff_exist():
    assert REG.names() == ["find_doctors", "free_slots", "list_appointments", "list_bills", "contact_staff"]
    assert all("read" in REG.get(n).tags and not REG.get(n).atomic for n in REG.names() if n != "contact_staff")
    assert REG.get("contact_staff").tags == {"handoff"} and not REG.get("contact_staff").atomic


# ---- inputs


def test_free_slots_validates_the_date():
    assert isinstance(REG.parse("free_slots", {"doctor_id": "d1", "date": "next friday"}), Failed)
    assert isinstance(REG.parse("free_slots", {"date": "2026-12-04"}), Failed)


def test_free_slots_returns_the_free_list():
    out = call(REG, "free_slots", {"doctor_id": "d1", "date": "2026-12-04"}, ctx())
    assert out["free"] == ["2026-12-04T10:00:00+05:30"]


# ---- facts for the next agent


def test_facts_record_the_ids_each_tool_returned():
    c = ctx(phone="9000000001")
    call(REG, "find_doctors", {"specialty": "Dermatology"}, c)
    call(REG, "free_slots", {"doctor_id": "d1", "date": "2026-12-04"}, c)
    call(REG, "list_appointments", {}, c)
    call(REG, "list_bills", {}, c)
    f = c.data["facts"]
    assert set(f) == {"doctors", "slots", "appointments", "bills"}
    assert "d1" in f["doctors"] and "A1" in f["appointments"] and "B1" in f["bills"]
    assert f["slots"]["d1@2026-12-04"] == ["2026-12-04T10:00:00+05:30"]
    assert "phone" not in f["appointments"]["A1"]


def test_a_tool_still_works_without_a_facts_dict():
    assert call(REG, "find_doctors", {}, ToolContext(session_id="s"))["doctors"]


# ---- hospital client: every outcome is a value or a typed error


def err(status, body=None):
    return client(lambda r: httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="boom"))


def get(c, path="/doctors"):
    return go(c.get(path))


def test_503_is_retryable():
    with pytest.raises(ToolRetryable):
        get(err(503, {"error": "service_unavailable", "message": "later"}))


def test_4xx_is_a_definite_failure_with_the_hospitals_words():
    with pytest.raises(ToolFailed, match="doctor_not_found: No doctor"):
        get(err(404, {"error": "doctor_not_found", "message": "No doctor with id x"}))


def test_a_non_json_error_body_still_gives_a_failure():
    with pytest.raises(ToolFailed, match="HTTP 400"):
        get(err(400))


def test_timeout_and_dropped_connection_are_unknown():
    def timeout(request):
        raise httpx.ReadTimeout("slow")

    def dropped(request):
        raise httpx.ConnectError("down")

    for h in (timeout, dropped):
        with pytest.raises(ToolOutcomeUnknown):
            get(client(h))


def test_none_params_are_not_sent():
    seen = []
    c = client(lambda r: (seen.append(str(r.url)), httpx.Response(200, json={}))[1])
    go(c.get("/doctors", specialty=None))
    assert seen == ["http://hospital/doctors"]


# ---- through the real executor: a read survives a 503


def test_executor_retries_a_read_after_503():
    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "service_unavailable", "message": "later"})
        return hospital(request)

    async def nosleep(d):
        pass

    tool, inp = read_tools(client(flaky)).parse("find_doctors", {})
    result = go(Executor(sleep=nosleep).execute(tool, inp, ctx(), state={}))
    assert isinstance(result, Done) and len(calls) == 2


def test_executor_gives_failed_not_unknown_when_a_read_never_answers():
    def down(request):
        raise httpx.ReadTimeout("slow")

    async def nosleep(d):
        pass

    tool, inp = read_tools(client(down)).parse("find_doctors", {})
    assert isinstance(go(Executor(sleep=nosleep).execute(tool, inp, ctx(), state={})), Failed)
