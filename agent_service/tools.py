"""The four read tools. Hospital knowledge lives here, not in agentcore.

Own data: no input model has a `phone` field (extra="forbid" makes a model-supplied one a
validation error). The service puts the verified phone in `ctx.data["phone"]`.

Facts: each tool also records the ids it returned in `ctx.data["facts"]` (a per-turn dict the
service owns), so a later agent can use them after a text-only handoff (design-log 2.4).
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from agentcore import Tool, ToolContext, ToolFailed, ToolRegistry
from agent_service.hospital import HospitalClient


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FindDoctorsIn(_In):
    specialty: str | None = Field(None, description="e.g. Dermatology. Leave out to list every doctor.")


class FreeSlotsIn(_In):
    doctor_id: str = Field(description="Doctor id from find_doctors, e.g. d1")
    date: dt.date = Field(description="Day to check, YYYY-MM-DD")


class NoIn(_In):
    pass


def _phone(ctx: ToolContext) -> str:
    phone = ctx.data.get("phone")
    if not phone:
        raise ToolFailed("No verified patient on this session, so personal records can't be read.")
    return phone


def _remember(ctx: ToolContext, kind: str, items: dict) -> None:
    facts = ctx.data.get("facts")
    if facts is not None:
        facts.setdefault(kind, {}).update(items)


def read_tools(client: HospitalClient) -> ToolRegistry:
    async def find_doctors(inp: FindDoctorsIn, ctx: ToolContext):
        out = await client.get("/doctors", specialty=inp.specialty)
        _remember(ctx, "doctors", {d["id"]: d for d in out["doctors"]})
        return out

    async def free_slots(inp: FreeSlotsIn, ctx: ToolContext):
        out = await client.get(f"/doctors/{inp.doctor_id}/slots", date=inp.date.isoformat())
        _remember(ctx, "slots", {f"{inp.doctor_id}@{out['date']}": out["free"]})
        return out

    async def list_appointments(inp: NoIn, ctx: ToolContext):
        out = await client.get("/appointments", phone=_phone(ctx))
        appts = [{k: v for k, v in a.items() if k != "phone"} for a in out["appointments"]]
        _remember(ctx, "appointments", {a["id"]: a for a in appts})
        return {"appointments": appts}

    async def list_bills(inp: NoIn, ctx: ToolContext):
        out = await client.get("/bills", phone=_phone(ctx))
        bills = [{k: v for k, v in b.items() if k != "phone"} for b in out["bills"]]
        _remember(ctx, "bills", {b["id"]: b for b in bills})
        return {"bills": bills}

    # Reads are safe to repeat, so the default retry policy (503 and timeout) applies.
    reg = ToolRegistry()
    for name, desc, model, fn in [
        ("find_doctors", "List doctors and their specialties, optionally for one specialty.", FindDoctorsIn, find_doctors),
        ("free_slots", "List the free appointment slots of one doctor on one day.", FreeSlotsIn, free_slots),
        ("list_appointments", "List this patient's own appointments (past and cancelled included).", NoIn, list_appointments),
        ("list_bills", "List this patient's own bills and whether each is paid.", NoIn, list_bills),
    ]:
        reg.register(Tool(name=name, description=desc, input_model=model, run=fn, idempotent=True, tags=frozenset({"read"})))
    return reg
