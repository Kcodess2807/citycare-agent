# 00 - zed agent thread (assignment understanding + claude.md rules)

session type: zed agent (not claude code), planning only, no code written.
date: 2026-11-24
project: D:\PROJECTS\citycare-agent

## 1. goal of this thread
- understand the take-home assignment (agentcore + city care hospital whatsapp front desk)
- update the existing `Claude.md` with the project rules so a successful submission is possible

## 2. files read
- `Assignment.md`: full brief, rules, hand-in list, what fails a submission, grading weights
- `context.md`: product, /chat contract, mock hospital api, chaos mode, 9 hospital rules, models and cost, evals, seed data
- `README.md`: setup, running, evals, tests, claude code sessions, hand-in table
- `Claude.md` (existing): phases 0-5, design-before-code, quiz gate, docs, pytest + commit

## 3. what the assignment is (short)
- build `agentcore/`: a small reusable agent runtime with no hospital code in it
  - tool registry (pydantic input, validation, tool errors visible to model)
  - own agent loop on `client.messages.create` (step limit, timeouts, retries + backoff, all stop reasons)
  - hooks around tool calls and model calls (allow / block with reason / pause for confirm)
  - full trace (inputs, outputs, time, tokens, cost)
  - session store behind a small interface (in-memory ok, redis-swappable)
- build agents on top (outside agentcore, e.g. `agent_service/`)
  - router on `claude-haiku-5-5` -> booking | billing | info | human
  - booking agent, billing agent, info agent (only from `hospital_info.md`), handoffs with context carried over
- safety in code (hooks), not just prompts
  - own data only (phone in request is identity)
  - confirm before any change (read-back + clear yes)
  - emergency check before the router on every message (english/hindi/hinglish)
  - 4-hour rule for reschedule/cancel -> handoff to staff
- bad day (chaos mode): slow, 10% 503s, races, saved-but-503 writes. never say "booked" unless it is, never book twice
- prove it: 10 given scenarios x 3 chaos runs + at least 3 own scenarios, pytest passes with no key, `GET /trace/{session_id}`
- docs: `DESIGN.md` (1-2 pages), `EVALS.md`, `evals/reported/`, `claude-sessions/`
- limits: 48 hours from key email, $30 budget, python 3.11+, fastapi, pydantic, anthropic sdk, claude code only

## 4. what fails a submission (from Assignment.md)
- api key anywhere in repo or git history
- changed `mock_hospital/` or the 10 given scenarios, or how an existing eval check works
- used an agent framework or the sdk tool runner
- `agentcore/` imports hospital code
- sessions missing, or real part of the code not in them
- pytest fails with no api key set
- repo can't be opened (private without Fire-Dolphin invited)
- commit or push after replying or after the deadline
- eval numbers don't hold up on rerun and can't be explained

grading weights: runtime 8, loop 6, safety in code 7, bad day 6, router + handoffs 5, evals + traces 4, DESIGN.md 2

## 5. how we agreed to work (phases)
0 understand, 1 agentcore runtime, 2 router + agents + handoffs, 3 safety hooks, 4 chaos hardening, 5 evals + docs
- discuss design before each phase, no code until i say go
- quiz gate before each next phase: ~10 questions, one at a time, includes "what happens if..." failure cases, need 80%+
- if i fail or score below 80%: re-quiz only the weak topics, 5 questions only, repeat until passed
- keep `docs/design-log.md` (decisions, alternatives, why, what failed) and `docs/flow.md` (how a message moves, mermaid) updated in the same commit as the code

## 6. the 6 changes requested for Claude.md
1. hard rules section: scenarios where the submission fails (frameworks, agentcore reading hospital code, modifying mock/scenarios, key leaks, etc.), plus any missed from the two files
2. budget for the whole thing
3. commit pattern: give me a short summary first, then only commit after my ok
4. remind me to export the claude session before i clear anything
5. if i fail a quiz, the re-quiz on the weak topic is 5 questions only
6. keep my wording where it works, all lowercase, fix typos, show the full proposed file before saving

## 7. things i added beyond the request (for review)
- no other ai coding tools (cursor, copilot, chatgpt, codex)
- agents must never call `/_admin/...`
- use mock `/now` clock (fixed tue 1 dec 2026 09:00 ist), never machine clock
- no tuning to the 10 given scenarios, they run unseen ones
- `/chat` contract fields stay unchanged, trace at `GET /trace/{session_id}`
- freeze after submission email, late reply or no repo link = no submission
- final hand-in checklist
- model notes: sonnet/opus 5.5 reject forced `tool_choice` any/tool, thinking on by default
- suggestion: rename `Claude.md` to `CLAUDE.md` (assignment uses uppercase, github is case-sensitive)

## 8. budget decided
- total $30 for claude code + agents + evals
- claude code ~$13 per active day ($13-26), agents while building $2-4, eval runs (3) $2-6
- sonnet in claude code, opus only for short hard planning
- run `/cost` end of each phase, warn if pacing over
- test on `fake_claude.py` + mock first, full 3x chaos evals only when agent is stable

## 9. commit pattern decided
- small commits, pushed as we go
- pytest (no key) first, then short summary of what changed and why, wait for my ok, then commit
- message: what + why, imperative, capitalised, ~50 chars, no trailing punctuation, body only if useful
- docs in the same commit as code
- no secrets, no branches or pushes unless i say, nothing after the submission email

## 10. session export rule decided
- remind me to `/export claude-sessions/NN-name.txt` before any `/clear`, close, or session switch
- also at the end of each phase and when a session is long or about to compact
- at the end: `uv run python tools/pack_sessions.py`, then commit and push

## 11. status at end of this thread
- full proposed `Claude.md` was shown and approved ("yeah save it")
- the zed agent had no write tool, so `Claude.md` and this file had to be saved manually by the user
- next step: paste the approved Claude.md into the repo (rename to `CLAUDE.md`), then start phase 0 (understand) with claude code from the repo root
- remember: export the first claude code session as `claude-sessions/01-planning.txt` before any `/clear`

note: this thread was in zed's agent, not claude code. i can't confirm yet whether the reviewers will count it as a claude code session. mention it in the design doc / email if relevant.
