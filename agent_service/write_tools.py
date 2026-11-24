"""The four write tools: book, reschedule, cancel, payment link. Built and tested, given to NO agent.

Nothing here decides whether a write is allowed. Who owns the id, the 4-hour rule and the patient's
yes are checked by hooks and by the confirmation code (design-log 3.x). A model must never be able to
reach these tools without that gate, so `agents.py` does not import this module (tested).

What each tool does when the answer is lost (design-log 0.1, 0.4, 0.5, 1.6):
- book: safe to repeat, because every attempt carries the same idempotency key. After the retries,
  verify looks for the booking in the patient's own list.
- reschedule, cancel: the mock has no key, so never repeated. verify reads the real state.
- payment link: a 503 is always "nothing created", so it is retried. A timeout is not: there is no way
  to look links up, so it ends Unknown (a handoff) and the bill is marked "in doubt".

Inputs have no phone and no key. The phone is `ctx.data["phone"]` and the booking key is
`ctx.data["idempotency_key"]`, both put there by code. Without a key, book refuses to run.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import Field, field_validator

from agentcore import (
    Done, Failed, RetryPolicy, Tool, ToolContext, ToolError, ToolFailed, ToolOutcomeUnknown, ToolRegistry, Unknown,
)
from agent_service.hospital import HospitalClient
from agent_service.tools import _In, _phone

WRITE_TOOL_NAMES = ("book_appointment", "reschedule_appointment", "cancel_appointment", "create_payment_link")


def _aware(v: dt.datetime) -> dt.datetime:
    if v.tzinfo is None:
        raise ValueError("needs a time zone, like +05:30")
    return v


class BookIn(_In):
    doctor_id: str = Field(description="Doctor id, e.g. d1")
    start: dt.datetime = Field(description="Slot start with time zone, e.g. 2026-12-04T17:00:00+05:30")
    patient_name: str = Field(min_length=1, description="Name for the booking")
    _tz = field_validator("start")(_aware)


class RescheduleIn(_In):
    appointment_id: str
    new_start: dt.datetime = Field(description="New slot start with time zone")
    _tz = field_validator("new_start")(_aware)


class CancelIn(_In):
    appointment_id: str


class LinkIn(_In):
    bill_id: str


def _same_time(a: str, b: dt.datetime) -> bool:
    try:
        return dt.datetime.fromisoformat(a) == b
    except ValueError:
        return False


def _clean(appt: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in appt.items() if k != "phone"}


def write_tools(client: HospitalClient) -> ToolRegistry:
    async def _mine(ctx: ToolContext) -> list[dict[str, Any]]:
        return (await client.get("/appointments", phone=_phone(ctx)))["appointments"]

    # ---- book

    async def book(inp: BookIn, ctx: ToolContext):
        key = ctx.data.get("idempotency_key")
        if not key:
            raise ToolFailed("Not booked: there is no confirmed action to run.")
        out = await client.post(
            "/appointments",
            {"doctor_id": inp.doctor_id, "start": inp.start.isoformat(), "patient_name": inp.patient_name,
             "phone": _phone(ctx), "idempotency_key": key},
        )
        return _clean(out)

    async def verify_book(inp: BookIn, ctx: ToolContext, err: ToolError | None):
        for a in await _mine(ctx):
            if a["doctor_id"] == inp.doctor_id and a["status"] == "booked" and _same_time(a["start"], inp.start):
                return Done(_clean(a))
        return Failed("The booking was not saved.")

    # ---- reschedule and cancel

    async def reschedule(inp: RescheduleIn, ctx: ToolContext):
        body = {"new_start": inp.new_start.isoformat()}
        return _clean(await client.post(f"/appointments/{inp.appointment_id}/reschedule", body))

    async def verify_reschedule(inp: RescheduleIn, ctx: ToolContext, err: ToolError | None):
        a = next((a for a in await _mine(ctx) if a["id"] == inp.appointment_id), None)
        if a is None:
            return Unknown("Could not find the appointment to check it.")
        if a["status"] == "booked" and _same_time(a["start"], inp.new_start):
            return Done(_clean(a))
        return Failed("The appointment was not changed.")

    async def cancel(inp: CancelIn, ctx: ToolContext):
        return _clean(await client.post(f"/appointments/{inp.appointment_id}/cancel"))

    async def verify_cancel(inp: CancelIn, ctx: ToolContext, err: ToolError | None):
        a = next((a for a in await _mine(ctx) if a["id"] == inp.appointment_id), None)
        if a is None:
            return Unknown("Could not find the appointment to check it.")
        if a["status"] == "cancelled":
            return Done(_clean(a))
        return Failed("The appointment is still booked.")

    # ---- payment link

    async def link(inp: LinkIn, ctx: ToolContext):
        links = ctx.data.get("links")  # {bill_id: {"url": ...} or {"in_doubt": True}}, kept per session by the service
        if links is None:
            raise ToolFailed("No link was made: there is no record to keep it in.")
        seen = links.get(inp.bill_id)
        if seen and seen.get("url"):
            return {"bill_id": inp.bill_id, "url": seen["url"]}
        if seen and seen.get("in_doubt"):
            raise ToolOutcomeUnknown("A link for this bill may already exist, so none was made.")
        try:
            out = await client.post(f"/bills/{inp.bill_id}/payment-link")
        except ToolOutcomeUnknown:
            links[inp.bill_id] = {"in_doubt": True}
            raise
        links[inp.bill_id] = {"url": out["url"]}
        return {"bill_id": out["bill_id"], "url": out["url"]}

    tags = frozenset({"write"})
    reg = ToolRegistry()
    # Only what is safe to repeat is idempotent. Not idempotent means: never run twice, go straight to verify.
    for tool in [
        Tool("book_appointment", "Book an appointment. Runs only after the patient confirmed.", BookIn, book,
             verify=verify_book, idempotent=True, atomic=True, tags=tags),
        Tool("reschedule_appointment", "Move one of the patient's appointments. Runs only after the patient confirmed.",
             RescheduleIn, reschedule, verify=verify_reschedule, atomic=True, tags=tags),
        Tool("cancel_appointment", "Cancel one of the patient's appointments. Runs only after the patient confirmed.",
             CancelIn, cancel, verify=verify_cancel, atomic=True, tags=tags),
        Tool("create_payment_link", "Make a payment link for one of the patient's unpaid bills. Runs only after the patient confirmed.",
             LinkIn, link, idempotent=True, atomic=True, retry=RetryPolicy(on=frozenset({"retryable"})), tags=tags),
    ]:
        reg.register(tool)
    return reg
