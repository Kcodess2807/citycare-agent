# citycare-agent: how we work + rules for this project

this is a hiring take-home. read `context.md` and `Assignment.md` before anything else. when in doubt, the hard rules below win over everything else in this file.

## 01. hard rules (break any of these = submission fails)
we stop being reviewed the moment one of these happens, however good the rest is. never do any of them, and tell me immediately if a requested change would.

- **api key:** never put the key anywhere in the repo or git history (code, .env committed, docs, prompts, exports, commits). `.env` stays in `.gitignore`. don't paste it into prompts either. if it shows up in an export, the only allowed edit is removing the key.
- **mock_hospital/ is read-only:** don't change it or how it behaves. read it, never edit it.
- **the 10 given scenarios are read-only:** don't edit them. i may add new check types to `evals/run_evals.py` for my own scenarios, but never change how existing checks work. my own scenarios go in `evals/scenarios/` as `11_...yaml` and up (at least 3).
- **no agent frameworks:** no langchain, langgraph, crewai, llamaindex, pydantic ai, openai agents sdk, autogen, claude agent sdk. no sdk tool runner helper either. stack is only python 3.11+, fastapi, pydantic, the `anthropic` sdk. the loop is ours, built on `client.messages.create`.
- **agentcore/ must not import hospital code:** no hospital imports, no hospital concepts (doctors, bills, appointments, phone rules) inside it. agents, tools, prompts and hospital rules live outside (e.g. `agent_service/`). they will check this.
- **agents must never touch `/_admin/...`** on the mock. evals and tests only.
- **pytest must pass with no api key set.** use `tests/fake_claude.py`. check this before every commit.
- **claude code only:** no cursor, copilot, chatgpt, codex or any other ai coding tool. everything must be written through claude code with the assignment key.
- **sessions must be complete:** every claude code session exported to `claude-sessions/` before clearing/closing. code that is in commits but in none of the sessions counts as not written by me. always start claude code from the repo root. don't edit exports or transcripts.
- **repo must be openable:** public, or private with **Fire-Dolphin** invited as collaborator.
- **freeze after submission:** no commits, pushes or force-pushes after i reply to the email or after the deadline, whichever comes first. a late reply or a reply without the repo link counts as no submission.
- **eval numbers must be honest:** they rerun them. every claim in `DESIGN.md` and `EVALS.md` has to match what they find. report all 3 runs, explain every failing case, never cherry-pick.
- **no tuning to the 10 given scenarios:** they also run unseen scenarios with their own copy of the mock and runner. build for the rules, not the tests.

### safety rules that must be enforced in code (hooks or tools), not just prompts
- own data only: the `phone` in the request is who the patient is. never trust a number typed in a message. the mock doesn't check ownership, so we must.
- confirm before changing anything: book, reschedule, cancel and payment link all need a read-back (doctor/day/date/time, or bill + amount) and a clear yes first.
- 4-hour rule: reschedule/cancel inside 4 hours of the slot goes to staff (`handoff: true`). use the mock's `/now` clock (fixed tue 1 dec 2026 09:00 ist), never the machine clock.
- emergency check runs before the router on every message, in english, hindi and hinglish. on emergency: stop everything, say call 108 / nearest er (24 hrs), `emergency: true`, flag staff, no medical advice.
- no medical advice, ever.
- info agent answers only from `hospital_info.md`, says it doesn't know otherwise and offers staff.
- handoff when patient asks for a person, is clearly upset, or the agent fails twice in a row on the same request.
- never say something happened when it didn't. never book twice: use `idempotency_key`, and after a 503 on a write, check the real state before answering (writes can be saved but still return 503).
- reply in the patient's language (english/hindi/hinglish), short whatsapp-sized messages.
- `/chat` contract fields stay as they are: `reply`, `agent`, `handoff`, `emergency`, `trace_id`. expose trace at `GET /trace/{session_id}`.

### required runtime pieces (agentcore)
tool registry (pydantic input models, validation, tool errors visible to the model) / own agent loop with step limit, timeouts, retries + backoff, every stop reason handled (max_tokens, refusal etc.) / hooks around each tool call and model call (allow, block with reason, pause for confirm) / full trace (inputs, outputs, time, tokens, cost) / session store behind a small interface (in-memory ok, redis-swappable).

### models
- router: `claude-haiku-5-5` (required).
- specialists: `claude-sonnet-5-5` or `claude-haiku-5-5`, justified with numbers in `DESIGN.md`.
- opus is not for the agents. in claude code only for a short, hard planning step.
- these models changed recently: sonnet/opus 5.5 reject forced `tool_choice` of `any`/`tool`, and thinking is on by default. check current docs instead of relying on memory.

## 02. budget ($30 total, for everything)
$30 covers claude code + the agents + eval runs. wasting it is judged, spending it all is fine.

| bucket | target |
|---|---|
| claude code (1-2 days) | ~$13 per active day, $13-26 max |
| agents while building | $2-4 |
| eval runs (3 required) | $2-6 |

- claude code runs on sonnet (`/model sonnet`). opus only for a short, hard planning step.
- run `/cost` at the end of every phase and tell me the number against the budget. warn me if we're pacing over.
- test against `fake_claude.py` and the mock first, real api calls only when needed. don't run the full 3x chaos evals until the agent is stable.
- prompt caching (stable system prompt + tool list first) is worth it for cost.
- `/clear` between unrelated tasks, but only after exporting (see 04).
- if the credit runs out, email them (they can top up once).

## how we will be working!!
- different phases as mentioned: 0 understand,  1 agentcore runtime,  2 router + agents + handoffs, 3 safety hooks, 4 chaos hardening, 5 evals + docs
- before each phase: discuss the design with me first, don't write code
  until i say go or execute that
- quiz gate: before starting a new/next phase, quiz me (~10 questions, one at a time,
  include "what happens if..." failure cases), don't start unless i score 80% or above
- if i fail the quiz or score below 80%, re-quiz only the weak topics, and that quiz should be **5 questions only** on those topics. repeat until the goal is achieved
- keep `docs/design-log.md` updated: each decision, alternatives, why, and
  what failed. keep `docs/flow.md` updated: how a message moves through the
  system, with a mermaid diagram or anything so that i can have some idea how things are flowing around
- update both docs in the same commit as the code they describe.

## 03. commit pattern
they read the git history. "fix", "update", "wip" or one big commit at the end all count against me.
- small commits, pushed as we go, after each working piece.
- before every commit: run `pytest` (must pass with no api key), then **give me a short summary of what changed and why, and wait for my ok. only then commit.** never commit on your own. this applies to every commit, small fixes included.
- run `pytest` on its own, never piped into `tail`/`grep` (a pipe hides the exit code). never commit or push on a failing run.
- message says what changed and why, e.g. "add idempotency key to booking writes so a retry can't double-book".
- imperative mood, capitalised subject, ~50 chars, no trailing punctuation. add a body only if it gives useful info (wrap at 72).
- docs (`design-log.md`, `flow.md`) go in the same commit as the code they describe.
- never commit secrets. check no key in the diff before committing.
- don't create branches. push to `main` right after every commit (standing instruction, no need to ask).
- no commits or pushes after i've sent the submission email.

## 04. session export reminder
they read my sessions, and code missing from sessions counts as not mine.
- before i `/clear`, close, or switch sessions: **remind me to export first**, and don't let it slide.
- `/export claude-sessions/01-planning.txt`, `02-agentcore-loop.txt`, etc., numbered in order, one file per session.
- also remind me at the end of each phase and when a session is getting long or about to be compacted.
- at the end: `uv run python tools/pack_sessions.py` (makes `claude-sessions/transcripts.zip` and checks for api keys), then commit and push.
- start claude code from the repo root every time. next day use `claude --continue`.

## final hand-in checklist (repo must contain)
- `agentcore/` + agents, `README.md` updated so they can run my version
- `CLAUDE.md` (+ any other files guiding claude code)
- `DESIGN.md` (1-2 pages): how agentcore works and why, model vs code decisions, how each failure is handled, model per agent with cost/speed numbers, eval results + failing cases, what changes for phone calls, what's missing before real patients, what i skipped and why
- `EVALS.md`: pass rate of each of the 3 chaos runs + why each failing case fails
- `evals/reported/`: the 3 result files that `EVALS.md` reports
- `evals/scenarios/`: the 10 given (unchanged) + at least 3 of mine
- `claude-sessions/`: one export per session + `transcripts.zip`
- email with repo link + 5-10 line summary (what works, what i skipped and why, pass rate of each of the 3 chaos runs, hours spent) before the deadline
