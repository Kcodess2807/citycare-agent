# DESIGN

The City Care front-desk agent: a WhatsApp assistant for booking, billing and hospital questions. `agentcore/` is a reusable runtime that knows nothing about hospitals; `agent_service/` holds the agents, tools, prompts and hospital rules. Numbers come from [EVALS.md](EVALS.md) (three chaos runs on commit `21b4690`). The full decision record is `docs/design-log.md`, the message flow `docs/flow.md`.

## 1. How agentcore works, and why

- **Our own loop on `client.messages.create`**, async, SDK retries off. Per turn: 8 model calls at most, a 90 s budget, a 60 s model timeout, 2 retries with jittered backoff that honours `retry-after`. Every stop reason is handled: `max_tokens` discards the reply (no half sentence, no cut-off tool call), retries once with double the limit, then hands off; a refusal hands off.
- **Tool registry with pydantic input models** that forbid extra fields, so the model cannot invent a `phone`, a key or a `confirmed` flag. A bad call comes back as an error the model can read.
- **Hooks around every model and tool call**: `Allow`, `Block(reason)` or `Pause(payload)`. A hook that crashes or times out counts as a Block. A pause ends the turn and stores a payload agentcore never reads.
- **One executor owns the order of a write**: attempt, retry with the same input if idempotent, `verify` against the real state, end as `Done`, `Failed` or `Unknown`. A write is shielded from cancellation and the turn budget; state is saved before and after it while history rolls back with a failed turn, so a saved booking is never lost or repeated.
- **Session store behind an interface** (in-memory, Redis-swappable, one lock per session) and **full traces** (inputs, outputs, time, tokens, cost; JSONL; `GET /trace/{session_id}`).
- **Why:** a 503 after a save, a timeout or a lost race needs the same handling for every tool, so the runtime owns the order and a tool only says how to try once and how to check. Hospital rules live in hooks outside agentcore; a test fails if `agentcore/` imports hospital code or uses hospital words.

## 2. What the model decides, and what code decides

| The model decides | Code decides (`agent_service/`) |
|---|---|
| Which agent (router), the language, short WhatsApp wording | **Emergency**: word lists (English, Hindi, Hinglish) run before the router; the router's flag is a second layer. Fixed 108 reply, no advice |
| Which tool and arguments; the earliest slot when the patient leaves it open | **Whose data**: the phone comes from the request; every id is checked against the patient's own records, same refusal for someone else's id and a missing one |
| Answers from `hospital_info.md` | **Confirmation**: a write tool never runs on the model's call. Code pauses, builds the read-back from the records, and on a yes runs the stored action with the stored key after checking again |
| | **4-hour rule** from the hospital's `/now`; handoffs and their wording; every "Booked" or "cancelled" text, built only from a finished result |

A guarded second yes layer lets a model *recognise* agreement with a read-back that code built. It never authorises or chooses anything, fails closed, and never sees a message with a digit, time word, "?" or process word (design-log 3.9). Still prompt-only: no medical advice, and the info agent using only the document.

## 3. How each failure is handled

| Failure | What happens |
|---|---|
| Model 429, 5xx, timeout | retried; if all fail, a fixed reply and a strike. Two failed turns in a row hand off |
| Booking saved, then 503 | retried with the same key, then `verify` finds it: booked once |
| Cancel or reschedule saved, then 503 | never repeated; `verify` reads the real state |
| Payment link timeout | `Unknown`: "could not confirm, don't retry", handoff; an existing link is returned, never remade |
| Slot lost (409) | "not booked"; if the patient already holds it, say so; else offer the next free slot as a new read-back (bookings only) |
| Clock unreachable, or inside 4 hours | nothing changes; fixed staff text, `handoff=true` |
| Crash mid-write | the next turn verifies the stored call before the model runs |
| Anything unexpected | `/chat` still returns 200 with all five fields and a handoff |

## 4. Model per agent, with numbers

Router, specialists and the yes layer all run `claude-haiku-5-5`, effort `low`.

| Role | Calls | Median per call | Cost per call |
|---|---|---|---|
| Router | 77 | 1.12 s | $0.00004 |
| Specialists | 204 | 0.94 s | $0.00016 |
| Second yes layer | 6 | 1.11 s | $0.00004 |

A run of 14 conversations cost $0.0114 to $0.0125 (about $0.0009 each); thinking is 45% of specialist output. Specialists on sonnet, tried once on the same chaos setup: 10/10 against haiku's 9/10 on the given ten, median 4.7 s against 4.6 s per turn, $0.1115 against $0.0075 (about 15 times). The one haiku miss was the router, which stayed on haiku in both, so sonnet bought no measured gain. Haiku stays. One run each, so this is a first comparison.

## 5. Eval results, and what still fails

Three valid chaos runs, none re-run, nothing changed between them: **13/14, 14/14, 14/14** (29/30 on the given 10, 12/12 on ours). Median turn 5.5, 5.1 and 4.4 s; worst 13.6, 17.9 and 18.5 s. **The one failure** (run 1, `reschedule_own`): the mock's injected race took the new slot, the code said "was not moved" and changed nothing; reschedule has no next-slot offer. A hand read of all 114 replies found no false claim of a change. Still weak: one model-written post-emergency reply, a slot list that dropped 7:30pm, a router that can mis-route an ambiguous question, and a runner whose keyword checks accept "not booked". Scenario 14 uses our own check type and fails with `unknown check type` on an unmodified runner. `pytest`: 575 tests, no API key.

## 6. What changes for phone calls

Speech gives noisy text, so names, dates and numbers are read back aloud and confirmed more carefully, and the strict yes must tolerate recognition errors. A 4.4 to 5.5 s turn is too slow for voice: it needs streaming, a spoken acknowledgement while tools run, parallel reads and a faster router. Caller ID is weaker than a verified WhatsApp number, so identity needs a second factor. A payment link is sent by SMS. Handoff becomes a warm transfer. A dropped call mid-write is covered by verify-before-model, but a pending confirmation must survive silence. Hindi and Hinglish need speech models that handle code-mixing.

## 7. What is missing before real patients

A real identity and ownership check at the hospital (the mock has none, so ours is the only one). Durable shared sessions and a real staff queue (a handoff is a flag and a trace event today). Privacy work: traces hold phone numbers and chat text. Clinical review of the emergency lists and wording, and proof that the router's emergency flag fires on the real API (0 times in our runs). Evals on unseen scenarios, adversarial and load tests, monitoring, cost and rate limits. The hospital's real idempotency behaviour for cancel and reschedule.

## 8. What we skipped, and why

Voice, UI, a real database and deployment (the brief says not to). A Redis store (the interface is ready). A next-slot offer after a lost reschedule race (left out on purpose, logged as a known limit). Parallel tool calls (tools run in the model's order; we did not show reads dominate a turn). Negation handling in the emergency detector, on purpose: a false alarm costs one reply with 108, a miss costs a patient. A fuller model comparison, since the cost bought no visible gain. Per-request strike tracking: we count consecutive failed turns, which errs toward a person.
