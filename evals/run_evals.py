"""Eval runner: plays scripted patient chats against the agent and checks what actually happened.

    python -m evals.run_evals                 # all scenarios, once, no chaos
    python -m evals.run_evals --runs 3 --chaos
    python -m evals.run_evals --only book_new_patient,emergency_hinglish
    python -m evals.run_evals --scenarios-dir path/to/other/scenarios

Both services must be running (mock hospital on HOSPITAL_URL, agent on AGENT_URL).
Each scenario resets the hospital, sets chaos, sends the turns, then runs the checks
against the hospital's real state and the agent's responses. Results are saved to
evals/results/.

Checks (see context.md for the full list):
  appointment_count     phone, status (default booked), equals | min | max
  appointment_exists    phone, doctor_in, date, after, before, time, status
  appointment_status    id, status, date, before
  no_appointment        phone, doctor_id, start
  payment_link_exists   bill_id
  no_payment_link       bill_id
  payment_link_count    bill_id, equals | min | max      (added for our own scenarios; the given checks are unchanged)
  response_field        turn (index | "any" | "last" | "all"), field, equals | in (a list)
  reply_contains_any    turn, values   (case-insensitive)
  reply_not_contains    turn, values   (case-insensitive)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import yaml

HOSPITAL = os.getenv("HOSPITAL_URL", "http://localhost:8001")
AGENT = os.getenv("AGENT_URL", "http://localhost:8000")
HERE = Path(__file__).parent


def load_scenarios(only: set[str] | None, folder: Path) -> list[dict]:
    out = []
    for path in sorted(folder.glob("*.yaml")):
        sc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not only or sc["id"] in only:
            out.append(sc)
    return out


def _turns(responses: list[dict], which) -> list[dict]:
    if which == "any" or which == "all":
        return responses
    if which == "last":
        return responses[-1:]
    return [responses[int(which)]] if int(which) < len(responses) else []


def _hm(start: str) -> str:
    return start[11:16]


def run_check(check: dict, state: dict, responses: list[dict]) -> str | None:
    """Return None if the check passes, or a short reason if it fails."""
    kind = check["type"]
    appts = state["appointments"]

    if kind == "appointment_count":
        status = check.get("status", "booked")
        n = sum(1 for a in appts if a["phone"] == check["phone"] and a["status"] == status)
        if "equals" in check and n != check["equals"]:
            return f"expected {check['equals']} {status} appointment(s) for {check['phone']}, found {n}"
        if "min" in check and n < check["min"]:
            return f"expected at least {check['min']} {status} appointment(s), found {n}"
        if "max" in check and n > check["max"]:
            return f"expected at most {check['max']} {status} appointment(s), found {n}"
        return None

    if kind == "appointment_exists":
        status = check.get("status", "booked")
        for a in appts:
            if a["phone"] != check["phone"] or a["status"] != status:
                continue
            if "doctor_in" in check and a["doctor_id"] not in check["doctor_in"]:
                continue
            if "date" in check and a["start"][:10] != check["date"]:
                continue
            if "time" in check and _hm(a["start"]) != check["time"]:
                continue
            if "after" in check and _hm(a["start"]) < check["after"]:
                continue
            if "before" in check and _hm(a["start"]) >= check["before"]:
                continue
            return None
        return f"no {status} appointment for {check['phone']} matching {({k: v for k, v in check.items() if k not in ('type', 'phone')})}"

    if kind == "appointment_status":
        a = next((x for x in appts if x["id"] == check["id"]), None)
        if not a:
            return f"appointment {check['id']} missing"
        if "status" in check and a["status"] != check["status"]:
            return f"{check['id']} is {a['status']}, expected {check['status']}"
        if "date" in check and a["start"][:10] != check["date"]:
            return f"{check['id']} is on {a['start'][:10]}, expected {check['date']}"
        if "before" in check and _hm(a["start"]) >= check["before"]:
            return f"{check['id']} starts {_hm(a['start'])}, expected before {check['before']}"
        return None

    if kind == "no_appointment":
        for a in appts:
            if a["phone"] == check["phone"] and a["status"] == "booked" and a["doctor_id"] == check["doctor_id"] \
                    and a["start"].startswith(check["start"]):
                return f"unexpected booking {a['id']} at {a['start']}"
        return None

    if kind == "payment_link_exists":
        if any(link["bill_id"] == check["bill_id"] for link in state["payment_links"]):
            return None
        return f"no payment link created for bill {check['bill_id']}"

    if kind == "no_payment_link":
        if any(link["bill_id"] == check["bill_id"] for link in state["payment_links"]):
            return f"a payment link was created for bill {check['bill_id']}"
        return None

    if kind == "payment_link_count":
        n = sum(1 for link in state["payment_links"] if link["bill_id"] == check["bill_id"])
        if "equals" in check and n != check["equals"]:
            return f"expected {check['equals']} payment link(s) for bill {check['bill_id']}, found {n}"
        if "min" in check and n < check["min"]:
            return f"expected at least {check['min']} payment link(s) for bill {check['bill_id']}, found {n}"
        if "max" in check and n > check["max"]:
            return f"expected at most {check['max']} payment link(s) for bill {check['bill_id']}, found {n}"
        return None

    if kind == "response_field":
        picked = _turns(responses, check.get("turn", "any"))
        values = [r.get(check["field"]) for r in picked]
        allowed = check["in"] if "in" in check else [check["equals"]]
        ok = all(v in allowed for v in values) if check.get("turn") == "all" else any(v in allowed for v in values)
        want = allowed[0] if len(allowed) == 1 else f"one of {allowed}"
        return None if picked and ok else f"{check['field']} was {values}, expected {want} on turn {check.get('turn', 'any')}"

    if kind == "reply_contains_any":
        picked = _turns(responses, check.get("turn", "any"))
        text = " ".join(r.get("reply", "") for r in picked).lower()
        if any(v.lower() in text for v in check["values"]):
            return None
        return f"reply on turn {check.get('turn', 'any')} has none of {check['values']}"

    if kind == "reply_not_contains":
        picked = _turns(responses, check.get("turn", "all"))
        text = " ".join(r.get("reply", "") for r in picked).lower()
        hits = [v for v in check["values"] if v.lower() in text]
        return f"reply contains {hits}" if hits else None

    return f"unknown check type {kind}"


def run_scenario(sc: dict, chaos_on: bool, client: httpx.Client) -> dict:
    client.post(f"{HOSPITAL}/_admin/reset").raise_for_status()
    chaos = client.get(f"{HOSPITAL}/_admin/chaos/defaults").json() if chaos_on else {}
    chaos.update(sc.get("chaos", {}))
    client.post(f"{HOSPITAL}/_admin/chaos", json=chaos).raise_for_status()

    session_id = f"{sc['id']}-{uuid.uuid4().hex[:8]}"
    responses, errors = [], []
    started = time.perf_counter()
    for msg in sc["turns"]:
        try:
            r = client.post(f"{AGENT}/chat", json={"session_id": session_id, "phone": sc["phone"], "message": msg})
            if r.status_code != 200:
                errors.append(f"/chat returned {r.status_code}: {r.text[:200]}")
                responses.append({"reply": "", "error": r.status_code})
                continue
            responses.append(r.json())
        except httpx.HTTPError as e:
            errors.append(f"/chat failed: {e!r}")
            responses.append({"reply": "", "error": str(e)})
    seconds = time.perf_counter() - started

    state = client.get(f"{HOSPITAL}/_admin/state").json()
    failures = [f for c in sc["checks"] if (f := run_check(c, state, responses))]
    failures += errors
    return {
        "id": sc["id"], "passed": not failures, "failures": failures, "seconds": round(seconds, 1),
        "session_id": session_id,
        "transcript": [{"patient": m, "agent": r} for m, r in zip(sc["turns"], responses)],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--chaos", action="store_true", help="turn on the hospital's default chaos settings")
    ap.add_argument("--only", help="comma-separated scenario ids")
    ap.add_argument("--scenarios-dir", type=Path, default=HERE / "scenarios",
                    help="folder of scenario .yaml files (default: evals/scenarios)")
    args = ap.parse_args()

    scenarios = load_scenarios(set(args.only.split(",")) if args.only else None, args.scenarios_dir)
    if not scenarios:
        print("No scenarios found.")
        return 1

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    summary = []
    with httpx.Client(timeout=180) as client:
        for run in range(1, args.runs + 1):
            print(f"\n=== Run {run}/{args.runs} ({'chaos' if args.chaos else 'calm'}) ===")
            results = []
            for sc in scenarios:
                res = run_scenario(sc, args.chaos, client)
                results.append(res)
                mark = "PASS" if res["passed"] else "FAIL"
                print(f"  {mark}  {res['id']:<32} {res['seconds']:>6}s")
                for f in res["failures"]:
                    print(f"        - {f}")
            passed = sum(r["passed"] for r in results)
            rate = passed / len(results)
            summary.append({"run": run, "passed": passed, "total": len(results), "pass_rate": round(rate, 3)})
            print(f"  Pass rate: {passed}/{len(results)} ({rate:.0%})")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (out_dir / f"run-{stamp}-{run}.json").write_text(
                json.dumps({"chaos": args.chaos, "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.runs > 1:
        print("\n=== Summary ===")
        for s in summary:
            print(f"  Run {s['run']}: {s['passed']}/{s['total']} ({s['pass_rate']:.0%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
