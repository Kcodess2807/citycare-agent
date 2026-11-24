"""Run the eval runner against both services with a hard cost stop, then report. Run by hand; it spends money.

    uv run python tools/run_eval_with_budget.py --limit 0.75                 # one calm run
    uv run python tools/run_eval_with_budget.py --limit 3 --runs 1 --chaos   # any runner flags pass through

What it does: starts the mock hospital (8001) and the agent service (8000), runs `python -m evals.run_evals`,
and while it runs adds up `cost_usd` from `traces/traces.jsonl` every 2 seconds. Past the limit it kills the
runner. Afterwards it prints, from the result file and the traces: pass or fail per scenario with the reasons,
time per turn (median and worst), cost per conversation, and the cache reads. It always stops both servers.

Time per turn is the server's own, from the trace (first event start to last event end of one trace id), so it
leaves out the HTTP hop. The result file stays in evals/results/ (gitignored). It prints no key and no phone number.
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
TRACES = REPO / "traces" / "traces.jsonl"
RESULTS = REPO / "evals" / "results"
HOSPITAL, AGENT = ("127.0.0.1", 8001), ("127.0.0.1", 8000)


def port_busy(addr: tuple[str, int]) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(addr) == 0


def start(module: str, port: int, log: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--port", str(port), "--log-level", "warning"],
        cwd=REPO, stdout=log.open("w"), stderr=subprocess.STDOUT,
    )


def wait_up(url: str, proc: subprocess.Popen, log: Path, seconds: float = 30) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if proc.poll() is not None:
            raise SystemExit(f"server for {url} exited early:\n{log.read_text(errors='replace')[-800:]}")
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.5)
    raise SystemExit(f"{url} did not come up in {seconds}s")


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def events() -> list[dict]:
    if not TRACES.exists():
        return []
    out = []
    for line in TRACES.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass  # a half-written last line
    return out


def spent() -> float:
    return sum(e.get("cost_usd") or 0.0 for e in events())


def turn_seconds(evs: list[dict]) -> list[float]:
    by_turn: dict[str, list[dict]] = {}
    for e in evs:
        by_turn.setdefault(e["trace_id"], []).append(e)
    return [max(e["ts"] + e["duration_ms"] / 1000 for e in t) - min(e["ts"] for e in t) for t in by_turn.values()]


def report(result_file: Path, limit: float, killed: bool) -> None:
    run = json.loads(result_file.read_text(encoding="utf-8"))
    evs = events()
    print(f"\n=== Report for {result_file.name} ({'chaos' if run['chaos'] else 'calm'}) ===")
    all_turns: list[float] = []
    for r in run["results"]:
        mine = [e for e in evs if e["session_id"] == r["session_id"]]
        turns = turn_seconds(mine)
        all_turns += turns
        cost = sum(e.get("cost_usd") or 0.0 for e in mine)
        cache_r = sum((e.get("usage") or {}).get("cache_read_tokens", 0) for e in mine)
        cache_w = sum((e.get("usage") or {}).get("cache_write_tokens", 0) for e in mine)
        calls = sum(e["type"] == "model_call" for e in mine)
        t = f"median {statistics.median(turns):.1f}s worst {max(turns):.1f}s" if turns else "no turns"
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']:<34} {len(turns)} turns, {calls} model calls, {t}, ${cost:.4f}, cache_read {cache_r}, cache_write {cache_w}")
        for f in r["failures"]:
            print(f"        - {f}")
        for i, step in enumerate(r["transcript"]):
            a = step["agent"]
            print(f"        turn {i}: agent={a.get('agent')} handoff={a.get('handoff')} emergency={a.get('emergency')}")
    passed = sum(r["passed"] for r in run["results"])
    print(f"\nPass rate: {passed}/{len(run['results'])}")
    if all_turns:
        print(f"Time per turn (server side, {len(all_turns)} turns): median {statistics.median(all_turns):.1f}s, worst {max(all_turns):.1f}s")
    by_role: dict[str, dict[str, float]] = {}
    for e in evs:
        if e["type"] != "model_call":
            continue
        role = "router" if (e.get("extra") or {}).get("role") == "router" else "specialist"
        u = e.get("usage") or {}
        d = by_role.setdefault(role, {"calls": 0, "input": 0, "output": 0, "thinking": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0})
        d["calls"] += 1
        d["input"] += u.get("input_tokens", 0)
        d["output"] += u.get("output_tokens", 0)
        d["thinking"] += u.get("thinking_tokens", 0)
        d["cache_read"] += u.get("cache_read_tokens", 0)
        d["cache_write"] += u.get("cache_write_tokens", 0)
        d["cost"] += e.get("cost_usd") or 0.0
    for role, d in by_role.items():
        print(f"{role:<10} calls={int(d['calls'])} in={int(d['input'])} out={int(d['output'])} thinking={int(d['thinking'])} "
              f"cache_read={int(d['cache_read'])} cache_write={int(d['cache_write'])} cost=${d['cost']:.4f}")
    total_cache = sum(d["cache_read"] for d in by_role.values())
    print(f"cache_read above zero: {'YES' if total_cache > 0 else 'NO'} ({int(total_cache)} tokens)")
    unpriced = sum(1 for e in evs if e["type"] == "model_call" and e.get("usage") and e.get("cost_usd") is None)
    print(f"Total cost from traces: ${spent():.4f} of ${limit:.2f} limit" + (f" (unpriced calls: {unpriced})" if unpriced else ""))
    if killed:
        print(f"STOPPED EARLY: the cost limit was reached, so the run is incomplete.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=float, required=True, help="hard cost stop in USD")
    args, runner_args = ap.parse_known_args()

    for name, addr in (("hospital", HOSPITAL), ("agent", AGENT)):
        if port_busy(addr):
            raise SystemExit(f"Port {addr[1]} ({name}) is already in use. Stop that server first; I won't touch it.")

    logs = Path(tempfile.mkdtemp(prefix="citycare-eval-"))
    if TRACES.exists():
        TRACES.unlink()  # a clean file, so the cost I add up is this run's only
    procs: list[subprocess.Popen | None] = [None, None]
    killed = False
    started = time.time()
    try:
        procs[0] = start("mock_hospital.app:app", 8001, logs / "hospital.log")
        wait_up("http://127.0.0.1:8001/now", procs[0], logs / "hospital.log")
        procs[1] = start("agent_service.app:app", 8000, logs / "agent.log")
        wait_up("http://127.0.0.1:8000/health", procs[1], logs / "agent.log")
        print("Both servers are up. Running the evals.", flush=True)

        runner = subprocess.Popen([sys.executable, "-m", "evals.run_evals", *runner_args], cwd=REPO)
        while runner.poll() is None:
            time.sleep(2)
            if spent() > args.limit:
                killed = True
                runner.kill()
                print(f"\nHARD STOP: ${spent():.4f} passed the ${args.limit:.2f} limit. Runner killed.", flush=True)
                break
        runner.wait()
        files = sorted(f for f in RESULTS.glob("run-*.json") if f.stat().st_mtime >= started - 1)
        if files:
            for f in files:
                report(f, args.limit, killed)
        else:
            print("No result file was written.")
        return 1 if killed else runner.returncode or 0
    finally:
        for p in procs:
            stop(p)
        busy = [a[1] for a in (HOSPITAL, AGENT) if port_busy(a)]
        print(f"\nServers stopped. Ports still busy: {busy or 'none'}. Server logs: {logs}")


if __name__ == "__main__":
    sys.exit(main())
