"""The confirmation gate: no write runs on the model's say-so.

A write tool called by the model never runs. The `before_tool` hook checks it against the hospital's
own records (fetched here, in this turn), builds the read-back text from them, stores a pending action
and pauses. Only a strict yes on the next message makes the service run the stored action, after the
same check runs again (design-log 3.2, 3.4).

The check is also the ownership check for id-based writes: the id must be in the patient's own
appointments or bills. An id that is someone else's and an id that does not exist get the same
refusal, and a refusal never repeats a doctor, time or name.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from agent_service import confirm
from agent_service.hospital import HospitalClient
from agentcore import Allow, Block, HookContext, HookRunner, Pause, RetryPolicy, ToolError, ToolFailed
from agentcore.hooks import ToolCallEvent
from agentcore.retry import with_retries

NOTHING = "Nothing was changed."
NO_APPOINTMENT = f"No such appointment on this patient's record. {NOTHING}"  # same text for someone else's id and a made-up one
NO_BILL = f"No such bill on this patient's record. {NOTHING}"
NO_DOCTOR = f"No such doctor. {NOTHING}"
NOT_ACTIVE = f"That appointment is not active, so it can't be changed. {NOTHING}"
PAID = f"That bill is already paid. {NOTHING}"
CANT_CHECK = f"Could not check the record just now. {NOTHING} Try again shortly."
# The 4-hour rule (CLAUDE.md): inside 4 hours, or already started, only staff change it. Exactly 4 hours is allowed.
# The reply and the handoff are set by code from these kinds, never by the model (prompts.staff_reply).
GATE_HOOK = "confirm_gate"  # the name it is registered under, so build_agents can tell it is installed
SOON, CLOCK = "soon", "clock"
FOUR_HOURS = dt.timedelta(hours=4)
TOO_SOON = f"That appointment starts within 4 hours or has already started, so only staff can change it. The system tells the patient and passes the chat to staff. {NOTHING}"
NO_CLOCK = f"Could not check the hospital's clock, so nothing can be changed safely. The system passes the chat to staff. {NOTHING}"


@dataclass(frozen=True)
class Checked:
    display: dict[str, Any]  # what the patient is told, from the hospital's records
    readback: str


@dataclass(frozen=True)
class Refused:
    reason: str  # for the model. Nothing in it comes from the id the patient or the model gave
    lookup_failed: bool = False  # the hospital could not be asked, as opposed to "no"
    handoff: str | None = None  # SOON or CLOCK: the chat goes to staff with a fixed reply


def _retryable(e: Exception) -> str | None:
    return e.kind if isinstance(e, ToolError) and not isinstance(e, ToolFailed) else None


class Gate:
    def __init__(
        self,
        hospital: HospitalClient,
        sleep: Callable[[float], Awaitable[None]],
        rng: Callable[[float, float], float],
    ) -> None:
        self._h, self._sleep, self._rng = hospital, sleep, rng

    def install(self, hooks: HookRunner) -> None:
        hooks.add("before_tool", self.before_tool, GATE_HOOK)

    async def _get(self, path: str, **params: Any) -> Any:
        async def attempt(n: int) -> Any:
            return await self._h.get(path, **params)

        return await with_retries(attempt, RetryPolicy(max_retries=2), _retryable, sleep=self._sleep, rng=self._rng)

    async def check(self, tool: str, inp: BaseModel, data: Any) -> Checked | Refused:
        """Fetch what this write touches and say what it would do. Used when proposing and again before running."""
        try:
            result = await self._check(tool, inp, data)
        except ToolError:
            return Refused(CANT_CHECK, lookup_failed=True)
        if isinstance(result, Refused):
            return result
        return Checked(result, confirm.readback(tool, result, data.get("language")))

    async def _check(self, tool: str, inp: Any, data: Any) -> dict[str, Any] | Refused:
        phone = data.get("phone")
        if not phone:
            return Refused(CANT_CHECK, lookup_failed=True)
        doctors = {d["id"]: d["name"] for d in (await self._get("/doctors"))["doctors"]}

        if tool == "book_appointment":
            if inp.doctor_id not in doctors:
                return Refused(NO_DOCTOR)
            return {"doctor": doctors[inp.doctor_id], "when": inp.start.isoformat(), "patient": inp.patient_name}

        if tool == "create_payment_link":
            bill = next((b for b in (await self._get("/bills", phone=phone))["bills"] if b["id"] == inp.bill_id), None)
            if bill is None:
                return Refused(NO_BILL)
            if bill["status"] == "paid":
                return Refused(PAID)
            return {"what": bill["description"], "amount": bill["amount_inr"]}

        appt = next((a for a in (await self._get("/appointments", phone=phone))["appointments"] if a["id"] == inp.appointment_id), None)
        if appt is None:
            return Refused(NO_APPOINTMENT)
        if appt["status"] != "booked":
            return Refused(NOT_ACTIVE)
        try:  # the hospital's clock, never the machine's. Fixed at Tue 1 Dec 2026 09:00 IST in the mock
            now = dt.datetime.fromisoformat((await self._get("/now"))["now"])
            if dt.datetime.fromisoformat(appt["start"]) - now < FOUR_HOURS:  # also true for a start that has passed
                return Refused(TOO_SOON, handoff=SOON)
        except (ToolError, ValueError, KeyError, TypeError):
            return Refused(NO_CLOCK, lookup_failed=True, handoff=CLOCK)
        doctor = doctors.get(appt["doctor_id"], "")
        if tool == "cancel_appointment":
            return {"doctor": doctor, "when": appt["start"]}
        return {"doctor": doctor, "old": appt["start"], "new": inp.new_start.isoformat()}

    async def before_tool(self, ctx: HookContext, event: ToolCallEvent):
        """For a write tool: refuse, or store a pending action and pause. It never lets the call run."""
        if "write" not in event.tool.tags:
            return Allow()
        checked = await self.check(event.tool.name, event.input, ctx.data)
        if isinstance(checked, Refused):
            if checked.handoff:
                ctx.data["handoff"].setdefault("kind", checked.handoff)  # the service answers and hands off
            return Block(checked.reason)
        pending = {
            "tool": event.tool.name,
            "params": event.input.model_dump(mode="json"),
            "key": uuid.uuid4().hex,  # a fresh idempotency key per confirmation (design-log 0.5)
            "agent": ctx.data.get("agent", ""),
            "language": ctx.data.get("language", "en"),
            "display": checked.display,
            "readback": checked.readback,
        }
        ctx.data["gate"]["readback"] = checked.readback  # the service swaps it in for the model's own words
        return Pause("Waiting for the patient's yes. The system sends them the details itself, so write one short line.", payload=pending)
