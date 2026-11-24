# City Care front-desk agent

A WhatsApp assistant for a hospital: it books, reschedules and cancels appointments, sends payment links, and answers general questions, in English, Hindi and Hinglish. It is built on our own agent runtime (`agentcore/`) with no agent framework: Python 3.11+, FastAPI, pydantic and the `anthropic` SDK only.

- **How it works and why:** [DESIGN.md](DESIGN.md) (read this first). The message flow, with diagrams, is `docs/flow.md`; every decision, alternative and failure is in `docs/design-log.md`.
- **What the evals show:** [EVALS.md](EVALS.md): three chaos runs, every failing case, the hand read of replies, and a ledger of every real eval run.
- **The assignment and the hospital:** `Assignment.md`, `context.md`, `hospital_info.md`.

## What's here

| Path | What it is |
|---|---|
| `agentcore/` | The reusable runtime: loop, tool registry, hooks, atomic tool executor, session store, traces. Imports no hospital code (a test checks) |
| `agent_service/` | The FastAPI app (`POST /chat`, `GET /trace/{session_id}`), the router, the three agents (booking, billing, info), the tools, prompts, the confirmation gate and the hospital rules |
| `mock_hospital/` | The fake hospital system with a chaos mode. Read-only: we do not change it. Agents never call its `/_admin` endpoints; only the evals and tests do |
| `evals/` | The runner, the 10 given scenarios (unchanged) and ours (`11_` to `14_`); `evals/reported/` holds the three result files reported in EVALS.md |
| `tests/` | 575 tests, with `fake_claude.py`, a scripted Claude client. They pass with no API key |
| `tools/` | `run_eval_with_budget.py` (an eval run with a hard cost stop), `check_yes_layer.py` and `check_router_format.py` (small real-API checks), `smoke_real.py`, `pack_sessions.py` |
| `docs/` | `design-log.md` and `flow.md` |
| `claude-sessions/` | The Claude Code session exports this was built with |

## Setup

Python 3.11 or newer. From a fresh clone, with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
cp .env.example .env     # then put your API key in .env (PowerShell: Copy-Item .env.example .env)
```

Or with pip: `python -m venv .venv`, `.venv/bin/pip install -r requirements.txt` (Windows: `.venv\Scripts\pip`), activate it, and drop the `uv run` prefix from the commands below.

The agent service reads `.env`. The eval runner and the mock hospital read plain environment variables, so for evals set the key in your terminal as well:

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
```

Never commit `.env` (it is in `.gitignore`).

## Run it

Terminal 1, the hospital system (calm, or flaky and slow with chaos on):

```bash
uv run uvicorn mock_hospital.app:app --port 8001
CHAOS=1 uv run uvicorn mock_hospital.app:app --port 8001     # chaos mode. PowerShell: $env:CHAOS="1"; uv run uvicorn ...
```

Terminal 2, the agent (default hospital URL `http://localhost:8001`, override with `HOSPITAL_URL`):

```bash
uv run uvicorn agent_service.app:app --port 8000
```

Try it. The `phone` is who the patient is; a number typed in a message is never trusted:

```bash
curl -s localhost:8000/chat -H "content-type: application/json" -d '{"session_id":"t1","phone":"9876543210","message":"What do I owe?"}'
curl -s localhost:8000/trace/t1        # every model call, tool call, hook decision, time, tokens and cost of that session
```

A reply has five fields: `reply`, `agent`, `handoff`, `emergency`, `trace_id`. `/chat` always returns 200.

## Evals

Two ways. Both need the API key and cost about a cent per run of all 14 scenarios.

**The simple way**: one command starts both servers, runs every scenario once, prints pass or fail with the reasons, time per turn and cost, and stops the servers. It has a hard cost stop and it deletes `traces/traces.jsonl` first, so the cost it adds up is that run's only:

```bash
uv run python tools/run_eval_with_budget.py --limit 0.50 --chaos     # drop --chaos for a calm run; add --only id1,id2 for some scenarios
```

**By hand**, with both services running (the runner resets the hospital and sets chaos itself):

```bash
uv run python -m evals.run_evals                         # every scenario, once
uv run python -m evals.run_evals --runs 3 --chaos        # three chaos runs
uv run python -m evals.run_evals --only emergency_hinglish
```

Results go to `evals/results/` (gitignored). The three reported runs are in `evals/reported/`. The scenarios are `evals/scenarios/`: files `01_` to `10_` are the given ones, `11_` to `14_` are ours. Scenario 14 uses our own check type, `payment_link_count`, and fails with "unknown check type" on an unmodified runner (see EVALS.md).

## Tests

```bash
uv run pytest
```

575 tests, no API key needed: the model is a scripted fake (`tests/fake_claude.py`) and several tests run against the real mock hospital app in process. Unset `ANTHROPIC_API_KEY` first if you want to be sure.

## Claude Code sessions

This was written with Claude Code only. Each session was exported to `claude-sessions/` in order, and at hand-in `uv run python tools/pack_sessions.py` adds `claude-sessions/transcripts.zip` with the full transcripts (it also refuses to pack if it finds an API key in the repo).
