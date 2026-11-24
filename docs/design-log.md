# Design log

## Status

- **Current phase:** 1 (agentcore). Commits 1 to 4 of 8 pushed, 5 written but not committed. Deadline for agentcore: 7 pm
- **Code written:** `agentcore/` config, error and result types, boundary test (commit 1); trace events, cost and trace store (commit 2); session store (commit 3); tool registry (commit 4); hooks (commit 5)
- **Decided:**
  - Phase 0 (0.1 to 0.5): payment links retry on 503 only and are cached per session; the eval runner uses one session per scenario per run; a timeout on a write is treated like saved-then-503; the booking key is fresh per confirmation and lives in the pending action
  - Phase 1 (1.1 to 1.10): async-only loop, `max_tokens` never sends partial text, effort instead of thinking switches, one atomic executor owns try, retry same key, verify, one result, state saved around writes while history rolls back, policies live in `agent_service` hooks, build order in 1.10
- **Open:**
  - Real values for timeouts and turn budget: provisional now, set from `tools/measure.py` (commit 8)
  - Does trimming history break the thinking-block check? Test with one real call (commit 8)
  - Phase 3: how code detects a clear yes, and what to do when the model's read-back differs from the stored payload

Each entry: the decision or question, the alternatives, why, and what failed.
Newest at the bottom. Entries are answers verified against the code, not memory.

---

## Phase 0: understanding the mock and the eval runner

### 0.1 Can a payment-link call save and still return 503? Where can a duplicate link come from?

**Answer (from `mock_hospital/app.py`):**
- `payment_link` calls `_chaos_before()` and nothing else. It never calls `_fail_after_commit()`.
- So a 503 on a payment link is always the pre-save kind (`error_rate`). Nothing was created, and retrying is safe.
- Only `book`, `reschedule` and `cancel` can be saved-then-503.

**Where a duplicate link can still come from:**
1. **A lost success.** The hospital creates the link and returns 200, but our client times out or the connection drops first. We think it failed, retry, and a second link exists. The mock creates a new random URL on every call and does no dedupe.
2. **The model or loop calling it twice.** Examples: the patient says "yes" twice, or the model repeats the tool call after a long turn.
3. **A patient retry across turns** after we told them it failed when it hadn't.

The eval check `payment_link_exists` only needs one link, so a duplicate would not fail an eval. It is still wrong for real patients.

**Decision:**
- A 503 on `payment_link` is retried with backoff, like a read.
- We keep a per-session record of `bill_id -> link` and return the existing link instead of creating another. This is the idempotency the mock doesn't give us.
- A client timeout on `payment_link` is treated as an unknown outcome, not as a failure. The hospital has no read endpoint for links (only `/_admin/state`, which agents must never call). The record above is the only guard. The fallback is to hand off, rather than create a second link.

**Alternatives considered:** retry blindly (can create a duplicate link). Never retry (turns every 503 into a failure). Both rejected.

### 0.2 Does `run_evals.py` use one session id per scenario or per run?

**Answer:** one per scenario, per run.
- `run_scenario()` builds `session_id = f"{sc['id']}-{uuid4().hex[:8]}"` once, then sends every turn of that scenario with it. The multi-turn conversation therefore shares one session.
- Each scenario in each run gets a fresh id. Run 2 of a scenario does not reuse run 1's session, and two scenarios never share one.
- The hospital is reset (`/_admin/reset`) at the start of every scenario, so state doesn't leak between scenarios either.
- The id is also in the result JSON (`session_id`), so a failing case can be looked up at `GET /trace/{session_id}`.

**Implications for us:**
- The session store is keyed by `session_id` and must keep the full history across turns of one scenario.
- Anything we cache per session (confirmations, payment links, idempotency keys) is isolated per scenario, which is what we want.
- Our process memory outlives the hospital reset. An idempotency key must not be derived from `phone` alone. Scenarios reuse phones, and the hospital forgets its keys on reset while we wouldn't. (How the key is actually made is decided in 0.5, which supersedes the first idea of deriving it from session plus details.)

### 0.3 Chaos latency vs. the eval runner's timeout

**Chaos latency (mock):**
- With chaos on, `_chaos_before()` sleeps `uniform(0.3, 2.0)` seconds before every read and write. The mean is about 1.15s.
- The sleep happens before the 503 coin flip, so errors are slow too.
- Calm mode has `latency_max_s = 0`, so there is no delay.
- `/now` has no chaos at all. It's instant and never fails.
- Scenarios can override latency in `chaos:`.

**Runner timeout (`run_evals.py`):**
- `httpx.Client(timeout=180)` is used for everything the runner does: `/chat` calls, `_admin/reset`, `_admin/chaos`, `_admin/state`.
- 180s applies per request, not per scenario. The runner waits up to 3 minutes for one `/chat` reply.
- On timeout the turn is recorded as an error (`/chat failed: ...`) and the scenario fails, even if the hospital state is correct.

**What this means for our design:**
- One `/chat` turn can make several hospital calls (about 1.15s each, plus retries and backoff) and several model calls. We need our own per-turn budget well under 180s. A target of 30-60s is a reasonable first guess. It is not measured yet.
- Our hospital client timeout must be above the mock's 2.0s maximum. Use about 5-10s. A timeout below 2s turns healthy slow calls into failures.
- For writes, a client timeout is the same problem as saved-then-503. We don't know whether it committed. The mock doesn't cancel the handler when the client disconnects, so the write probably did commit. Treat timeout and 503 on `book`/`reschedule`/`cancel` the same way: check real state (`list_appointments` for the patient's own phone) before replying, and never retry blind.
- The agent must make a single failure visible within the runner's patience. It must never loop silently until the runner gives up.
- The `/chat` handler must not block the event loop. The naive agent calls sync `httpx` inside an `async def`. With chaos latency this serialises concurrent chats.

**Open:** the right values for hospital client timeout, retry count, backoff and per-turn budget. Decide in the Phase 1 design discussion.

---

### 0.4 Can we look up existing payment links before creating one? (refines 0.1)

**Checked in `mock_hospital/app.py` and `data.py`: no.**
- The only place links are listed is `GET /_admin/state`, which agents must never call.
- `GET /bills?phone=` returns the bill dicts only (`id, phone, amount_inr, description, date, status`). There is no link field.
- Creating a link does not change the bill. It stays `unpaid`.
- So "check first, create only if missing" is impossible for our code. The only memory of links we can have is our own.

**Decision:**
1. **503 on `payment_link`:** retry with backoff. Safe, because the mock never saves before a 503 on this endpoint (see 0.1).
2. **Timeout or dropped connection on `payment_link`:** never retried automatically. We can't tell whether a link was created. We tell the patient we couldn't confirm it and hand off to staff (`handoff: true`). We also mark that bill "link in doubt" for the session, so a later "send it again" goes to staff and doesn't silently create a second link.
3. **Success:** store `bill_id -> url` in the session. A second request for the same bill (the model calling the tool twice, or the patient asking again) returns the stored link and does not call the hospital.
4. **Ownership:** `bill_id` must be one of the bills `list_bills(session phone)` returned. The mock will make a link for anyone's bill.
5. **Already-paid bills:** don't offer a link. The mock would return 400 `already_paid`.

**Why:** a duplicate link is low harm (an extra unpaid URL for the same bill), but we can't verify, and rule 9 says never claim something we don't know. Stopping and handing off is the conservative choice. The cost is that a rare timeout, which should be uncommon with a 5-10s client timeout against a 2s maximum latency, becomes a handoff. If the eval runs show this firing often, revisit with one retry on timeout and accept the duplicate.

**Rejected:** retry on timeout (can create a duplicate we can't see). Check-before-create (no endpoint exists).

---

### 0.5 Booking idempotency key: derived from session + details, or fresh at confirmation?

Only `POST /appointments` honours `idempotency_key`. Two ways to make one:

- **A. Derived:** `key = hash(session_id, doctor_id, start, patient phone)`. The same booking always gives the same key. No stored state needed.
- **B. Fresh at confirmation:** when the patient says a clear yes to a specific read-back, code makes a pending action `{type, params, key=uuid4(), state}` and stores it in the session. The write tool runs only the stored params with the stored key. A retry reuses that key. The pending action ends on success or a definitive failure.

**Important mock detail:** in `book()`, the idempotency lookup runs first and returns `STATE.appointments[...]` for that key with 200. It does not check status. A key whose appointment was later cancelled returns the cancelled appointment, not a new booking.

| Scenario | A. Derived | B. Fresh + pending action |
|---|---|---|
| Retry after 503, saved or not | Same key, same booking. Works | Same key (stored), same booking. Works |
| Patient says "yes" twice | Second call returns the same booking. Works | Pending action is already resolved, so the second "yes" has nothing to act on. Works |
| Timeout on the write | Same key on retry. Works | Same key kept in the in-doubt pending action. Works |
| Book, cancel, book the same slot again | **Breaks.** The second book reuses the key and the mock returns the cancelled appointment with 200. The agent could say "booked" while the real status is cancelled. That is the rule 9 failure | Works. A new confirmation gives a new key, so the mock books a new appointment |
| Slot lost to a race (409), patient picks another slot | New slot gives a new key. Works | New confirmation gives a new key. Works |
| Model changes `patient_name` spelling or start format on a retry | Key changes if name or format is in the hash, so a duplicate is possible, but the slot is taken by our own booking so the retry gets 409 and we verify. Needs normalising | The model can't change anything. The write runs the stored params, not the model's |
| Session store loses the pending state (restart) | Key can be recomputed. Works | Key is lost. A retry gets a new key, but our own booking holds the slot, so we get 409 `slot_taken`. We then check `list_appointments` and find it. Works, via the verify step |
| Hospital resets but we don't (eval runner) | Safe only if `session_id` is in the hash, and the runner makes a new session per scenario | Safe, since the key is a fresh uuid each time |
| Confirmation gate (rule 2) | Separate mechanism, must be built anyway | Same object: the pending action is both the read-back the patient said yes to and the key |
| Extra code | Smallest | A pending-action lifecycle: create, expire, replace when the patient changes their mind, clear when finished |

**Decision: B (fresh key, stored in a pending action).**

**Why:**
1. A has a real correctness hole. Book, cancel and rebook the same slot is something patients do, and A turns it into a false "booked". Fixing A needs a counter or nonce in the hash, and then A is B with extra steps.
2. We need a pending action anyway to enforce "confirm before changing anything" in code (rule 2). Making it carry the key means one object answers both "did the patient agree to exactly this?" and "is this a retry of that same thing?".
3. Because the write runs the stored params, a prompt injection or a confused model can't change what gets written after the patient said yes.
4. The restart weakness is covered by the rule we need anyway: after a 503 or timeout on a write, read the real state before answering.

**Cost we accept:** more state to manage (expiry, replacement, cleanup) and tests for each transition. The store must keep pending actions behind the session-store interface so a Redis swap keeps working.

**Reschedule and cancel:** the mock gives no idempotency for them, so there is no key to send, but they use the same pending action for confirmation. For an unknown outcome we verify against real state:
- Cancel: if the appointment is now `cancelled`, it worked. A second cancel would only return `not_active`.
- Reschedule: if the appointment's `start` equals the requested time, it worked. A blind retry would return 409 `slot_taken` because our own booking holds the slot.

**Open:** how code detects "a clear yes" (a `confirm` tool the model must call, or code matching the patient's reply to the pending action). Decide in the Phase 3 design.

---

## Phase 1: agentcore design (no code yet)

All numbers are provisional until measured (1.4). Every one lives in a config object, none is hard-coded in the loop.

### 1.1 One async loop, a sync fake
- The loop is async only: `await client.messages.create(...)` on `AsyncAnthropic`, `httpx.AsyncClient` for the hospital. SDK `max_retries=0`, we own retries.
- `tests/fake_claude.py` is provided and sync. We do not edit it. A small test-side adapter (`AsyncFake`) wraps a `FakeClaude` and exposes `async create(**kw)` that calls the sync one. It keeps `.messages.calls`, and scripted exceptions still raise.
- Rejected: `if inspect.isawaitable(r)` inside the loop. Production code would bend to a test helper.
- Backoff sleeps go through an injectable `sleep`, so retry tests don't wait.

### 1.2 `max_tokens` never sends partial text (supersedes the first draft)
- On `stop_reason == "max_tokens"`: discard the response (never append it to history, never show it). Retry that step once with double `max_tokens` (capped at 8192). If it truncates again, fail the turn with a handoff.
- A truncated `tool_use` is discarded the same way, so a cut-off call never runs.

### 1.3 Thinking and effort (checked against the docs, 2026-11)
- Thinking tokens count inside `max_tokens` and are billed as output tokens, even when `display` is omitted. So `max_tokens=1024` was too low. New caps: router 1024, specialists 4096. They are ceilings, not targets. Short WhatsApp replies come from the prompt.
- Sonnet 5.5: `thinking: disabled` returns 400 (`between_tools` is the off switch, effort `high` or below). Haiku 5.5: adaptive by default, `disabled` allowed at `high` or below. Neither accepts non-default `temperature`/`top_p`. We send none.
- The docs say prompt text like "don't think" is unreliable on these models. Effort is the lever. Changing effort mid-conversation invalidates the prompt cache, so effort is fixed per agent.
- Decision: do not pass `thinking`. Set `output_config.effort` per agent: router `low`, specialists start at `low` and are raised only if measurement shows a quality gap. Trace records `usage.output_tokens_details.thinking_tokens` so we can see what thinking costs.
- Thinking blocks must go back to the API unchanged inside a tool loop. **Open risk:** the history-editing check (trimming old messages, stripping blocks) may reject edited history. Verify with one real call early in Phase 1. Fallback: trim only at turn boundaries.

### 1.4 Latency numbers and how we measure
- The earlier "10-20s" (Sonnet with thinking) and "3-6s" (no thinking) were two unmeasured guesses. Provisional config: model call timeout 60s, turn budget 90s (runner waits 180s), step limit 8, model retries 2 (3 attempts), backoff `min(8s, 1s*2^n)` with full jitter and `retry-after` honoured, hospital timeout 6s.
- Measure: every trace event has duration and tokens. A small `tools/measure.py` (Phase 1 end) sends about 5 canned conversations through the real loop against the mock with chaos on, for each candidate model/effort, and prints p50/p95 per model call, per tool call and per turn, plus cost. Estimated cost under $0.30.
- After that, timeouts become about 2-3x the measured p95, and the turn budget is set from the p95 of a full booking turn.

### 1.5 Hook results and pause/resume
- Three named hook results, each its own type: `Allow`, `Block(reason)`, `Pause(reason, payload)`. The loop handles Block and Pause through shared code (the call is not run and still gets a `tool_result`), but Pause is its own type because it also ends the turn as `status="paused"`.
- **`payload` is opaque.** The runtime stores it untouched in `session.state["pending"]`, it must be JSON-serialisable, and the runtime never reads a field of it. What it contains (params, read-back facts, idempotency key, expiry, replace/clear rules) is defined by `agent_service`, so agentcore imports no hospital types.
- After a Pause the model gets a `tool_result` ("Not executed: awaiting patient confirmation") for that call and "skipped" for the others in the step. It then writes the read-back in the patient's language. After a pause only text is allowed. A further tool call is blocked again, and a second one fails the turn to handoff.
- **History invariant:** an assistant `tool_use` message and its user `tool_result` message are appended together, and every `tool_use` id always gets a result (success, error, blocked, skipped, timeout). History is built on a copy and committed at the end of the turn after a pairing check.
- **On "yes":** code runs the stored payload through `execute_tool(name, input)`, which uses the same registry, hooks and trace. The result goes back to the model as plain text context, so no invented `tool_use` is added to history. How code recognises the yes is Phase 3.

### 1.6 The atomic unit: who owns the order (merges the old 1.6 and 1.7)
Ownership is split on purpose. **The registry executor owns the order. The tool supplies two small functions and a policy.** Hospital knowledge stays in the tool, order and retry logic stay in agentcore.

A tool declares: `run(input, ctx)` (one attempt), optional `verify(input, ctx, last_error)`, `retry` (max retries, backoff, which failures to retry), `idempotent`, `atomic`, `reserve_s`, and opaque `tags`. A failing `run` raises one of three agentcore errors: `ToolFailed` (definite: nothing changed, e.g. 409), `Retryable` (the attempt failed, outcome not certain, e.g. 503) or `Unknown` (timeout or dropped connection).

The executor, for every call:
1. Attempt `run`.
2. `ToolFailed`: stop. The result is a definite failure. No verify, because nothing changed.
3. `Retryable` or `Unknown`: if the tool is `idempotent`, the policy allows that failure kind and attempts remain, back off and run again **with the same input object**. For `book` that input holds the stored key, so a repeat can only return the same booking.
4. Retries used up, or the tool is not idempotent: call `verify`, if the tool has one. A tool with no `verify` (reads) returns a definite "unavailable".
5. The result is exactly one of `Done(value)`, `Failed(reason)` or `Unknown`. `Unknown` only happens if verify itself fails, or the tool has no verify and the outcome is uncertain. It becomes an error `tool_result` and the loop asks for a handoff.

Examples:
- `book`: idempotent, retries 503 and timeout with the same key, then verifies via `list_appointments`.
- `reschedule`, `cancel`: not idempotent, so no retry. A 503 goes straight to verify.
- `payment_link`: idempotent for 503 only, a timeout is not retried and there is no verify, so a timeout ends as `Unknown` and a handoff (0.4).

`atomic=True` makes steps 1 to 5 one shielded unit (`asyncio.shield`): turn-budget expiry, a client disconnect or a cancel can't stop it between write and verify. The budget is checked only before an atomic call starts. It starts only if the remaining budget is at least its `reserve_s` (about 30s for writes: 3 attempts x 6s + backoff + verify 6s). Otherwise nothing is changed and the patient gets a handoff.

Retries for model calls use the same `with_retries` helper. The hospital client has no retries of its own: 6s timeout and typed errors only.

### 1.7 State is saved before and after a write, history is not
- Session `state` and `history` are saved separately. A failed turn rolls back **history only**.
- Around every atomic call the runtime saves state twice. Before: `state["inflight"] = {tool, input, started_at}`. After: it clears `inflight` and appends to `state["outcomes"]` the tool name, status (`done`/`failed`/`unknown`) and the tool's result summary, flagged `told=false`. Each save writes a `state_saved` trace event.
- If a turn then fails and history rolls back, the outcome is still stored. `LoopResult.outcomes` exposes it so the service can tell the patient honestly ("your booking went through, I couldn't finish the message"). On the next turn, code adds every `told=false` outcome to the context as plain text and marks it told, so the model never rebooks something that already happened.
- If `inflight` is still set at the start of a turn (crash or cancel mid-write), the runtime calls that tool's `verify` with the stored input first, records the outcome, and only then lets the model run.

### 1.8 What is policy and stays out of agentcore
- agentcore holds mechanisms only: hooks, `atomic`, `idempotent`, retry, `verify`, opaque `tags` and opaque `payload`. It never interprets a tag.
- **One write per turn** is a `before_tool` hook in `agent_service` that counts calls whose tags contain `write` and blocks the second ("one change at a time, confirm each"). Ownership, the 4-hour rule, confirmation and the emergency check are hooks in `agent_service` too.
- A test asserts that nothing under `agentcore/` imports `agent_service` or `mock_hospital`, and that none of its source mentions hospital words.
- Tools in a step run sequentially in the model's order. Parallel reads would save about 1.2s each under chaos. Revisit if the measured turn p95 is dominated by reads.

### 1.9 Traces outlive sessions
- `TraceStore` is its own interface, separate from `SessionStore`. Sessions expire after 30 min idle. Traces don't expire with them.
- Default: in-memory, LRU capped at 1000 sessions, plus an append-only JSONL file under `traces/` (gitignored, it holds phone numbers) so a failing eval case can still be explained after a restart.
- `GET /trace/{session_id}` reads from the `TraceStore`.

### 1.10 Build order, one commit per piece
Each commit: tests pass with no API key, `design-log.md` and `flow.md` updated in the same commit, and I stop for the user's ok before committing.

| # | Commit | Contains |
|---|---|---|
| 1 | Add agentcore config and error types | `RuntimeConfig` (all numbers), `ToolFailed`/`Retryable`/`Unknown`, result types, the import-boundary test |
| 2 | Add trace events and trace store | event types, cost table, in-memory LRU + JSONL `TraceStore` |
| 3 | Add session store with split state and history | interface, in-memory store, TTL, per-session lock, atomic history commit with pairing check |
| 4 | Add tool registry with pydantic validation | `Tool` spec, JSON schema, validation errors returned to the model |
| 5 | Add hook results and hook runner | `Allow`/`Block`/`Pause`, ordering, fail-closed on a crashing hook |
| 6 | Add atomic tool executor with retry and verify | `with_retries`, the 1.6 order, shield, `reserve_s`, write-ahead state, inflight recovery |
| 7 | Add agent loop with every stop reason | step limit, budget, `max_tokens` retry, refusal, pause, rollback; plus the `AsyncFake` test adapter |
| 8 | Add measure script and thinking-history check | `tools/measure.py`; needs the user's ok before any real API spend |

### Notes carried forward to Phase 3
- The model's read-back may differ from the stored payload (wrong doctor, time or amount). The patient says yes to what they read, and the code runs what is stored. Options: code renders the canonical facts and an `after_model` hook checks the reply contains them, or code sends the read-back itself. Decide in Phase 3.
- How code recognises a clear yes (a `confirm` tool, or matching the reply to the pending action).

### 1.11 Build log

**Two rules for the whole build (set by the user):**
- Strings inside agentcore are generic too: no domain words in `tool_result` text or anywhere else in the package. Wording comes from the hook's `reason` or the tool's own message. The boundary test scans all agentcore source for domain words (code, comments, docstrings, strings), not only imports.
- agentcore must be done by 7 pm. If behind, cut in this order and log each cut here as skipped: (1) inflight recovery at turn start, (2) history trimming, (3) the trace LRU cap.

**Commit 1: config and error types.**
- `RuntimeConfig` (frozen pydantic) holds every number from 1.4 and 1.6. `RetryPolicy` is its own model so a tool can carry its own. Delay before retry n is random in `[0, min(cap, base * 2**n)]`.
- The hospital client's 6s timeout is not in this config. It belongs to `agent_service`. agentcore only has a default per-attempt tool timeout (10s).
- Names changed from the design text: errors are `ToolFailed`, `ToolRetryable`, `ToolOutcomeUnknown` (a bare `Unknown` would clash with the `Unknown` result). Results stay `Done`, `Failed`, `Unknown`.
- The boundary test's forbidden list includes `handoff` and `staff`, so the loop result will say `escalate`, not `handoff`. The service maps `escalate` to the `/chat` field `handoff`.
- The list leaves out `cancel` and `cancelled`, so `asyncio.CancelledError` and similar words in commit 6 don't trip it. Matching is whole words only, so `billed` is fine too.
- Checked: a temporary file with a forbidden word and an `agent_service` import makes the boundary tests fail.

**Commit 2: trace events and trace store.**
- `TraceEvent` is one flat model: trace id, session id, sequence number, type (`model_call`, `tool_call`, `hook_decision`, `state_saved`, `stop`), name, start time, duration, status, attempt, input, output, token usage and cost. Input and output are strings cut to 2000 characters (config `trace_field_chars`).
- A `Tracer` belongs to one turn and numbers its events. `async with tracer.span(...)` times a block, marks it `error` if it raises, and re-raises.
- Cost comes from a price table for the three 5.5 models. Input and output prices are from `context.md`. Cache prices are not in `context.md`: cache read for sonnet and opus is from the claude-api skill's table, and haiku cache read (0.1x input) and every cache write (1.25x input) are assumptions, marked as such in a comment in the source. An unknown model gives `None`, never a guessed price. The loop must pass the model name it requested, because the fake client reports `fake-model`.
- Key safety: anything that looks like `sk-ant-...` is replaced by `[redacted]` before an event is stored. This is a second line of defence, since the key is never meant to reach a trace.
- `InMemoryTraceStore`: sessions kept in least-recently-used order, capped at `trace_max_sessions`. With a path it also appends every event to one JSONL file (`traces/` is now in `.gitignore`), and `get` reads that file for a session that was evicted or from before a restart. One file, not one per session, because the session id comes from the request and must never become a file name.
- The JSONL write is a blocking call inside async code (marked `TODO:` in the source). Revisit if it shows up in measured latency.
- The LRU cap is on the 7 pm cut list, but it is about 4 lines, so it stays.
- `summarize(events)` totals a session over all its turns: turns (distinct trace ids), model and tool call counts, errors, token usage (including thinking tokens), cost and summed call time. Calls with tokens but no price are counted in `unpriced_calls`, so a nonzero value means the cost is a lower bound.

**Commit 3: session store.**
- A `Session` is `{session_id, messages, state}`. The store has `load`, `save_state`, `commit_history`, `delete` and a per-session `lock`. State and history are saved by different calls, so a failed history commit leaves the state exactly as it was saved (tested).
- **Everything stored is JSON.** Input and output both go through a JSON round trip. So `load` returns a copy (changing it changes nothing until saved), and a payload that is not JSON fails at save time with `SessionError`, as the opaque `pending` payload must. This is what a Redis store would do too.
- **History is plain dicts, not SDK objects.** The loop (commit 7) will turn response blocks into dicts before appending.
- **The pairing check** (`check_history`) rejects a history where a tool call has no result in the next message, a result has no call before it, the ids don't match (missing or extra), the first message is not a user message, or a role/content is invalid. `commit_history` runs it first and stores nothing on failure.
- **Trimming** (`trim_history`) runs at commit time, down to `history_cap_messages` (40). It cuts only at the start of a turn (a user message that is not a tool result), so a call is never separated from its result. One very long turn is kept whole rather than split. This is cut-list item 2: it was about 10 lines, so it stays. **Still open:** whether trimming breaks the thinking-block check, to be tested with the real API in commit 8.
- **TTL:** 30 minutes idle, counted from the last write (`save_state` or `commit_history`; `load` does not refresh it). Expiry is checked lazily on access, plus `purge_expired()`. When a session expires its state goes with it, so a pending confirmation older than 30 minutes is gone.
- The idle clock is `time.monotonic` and injectable. It is machine time on purpose: it measures idleness, not hospital time.
- Locks: one `asyncio.Lock` per session id, kept until `delete` or a purge of an expired session. A lock held by a running turn is never dropped.
- Bug caught by the tests: `purge_expired` deleted from the dict it was iterating over.
- **For DESIGN.md (what is missing before real patients):** the per-session lock is in-process (`asyncio.Lock`). With a Redis session store and several service instances, two instances could handle two messages of one session at once. A shared lock (for example a Redis lock with an expiry) or sticky routing by session id is needed first. The same applies to the in-memory trace store, which is per process.

**Commit 4: tool registry.**
- A `Tool` is a frozen spec: name, description, pydantic input model, `run` (one attempt), optional `verify`, `retry`, `idempotent`, `atomic`, `reserve_s`, `timeout_s`, opaque `tags`. `ToolContext` carries the session id and an opaque `data` mapping that the service fills per turn (this is where the service will pass the verified phone number to tools, so the model never supplies it).
- **Bad definitions fail at start-up** with `ToolSpecError`: a name the API would reject, an empty description, a duplicate name, or an input model that doesn't set `extra="forbid"`. The last one matters: without it, pydantic silently drops a field the model invents (for example a phone number it typed), and the call would run without anyone noticing.
- **`schemas()`** builds the API `tools` list from the pydantic models, in registration order (not sorted) so the list is byte-identical on every call and the prompt cache keeps hitting.
- **`parse(name, raw_input)`** never raises. An unknown tool, input that isn't a JSON object, or a validation error all come back as a `Failed` the model can read: `Invalid input for 'x': field: message; ...`, or `Unknown tool 'x'. Available tools: a, b.` The error text names the field and the rule but leaves out the offending value, which could be long or private. The wording is generic by design (the boundary test enforces it).
- **`tool_result_block`** turns a `Done`/`Failed`/`Unknown` into an API block. Only `Done` is a success. A value that JSON can't encode is stringified, not a crash.
- **Not done, on purpose:** the API's `strict: true` option on tools. It needs `additionalProperties: false` (we have it) but also restricts which pydantic features are allowed. Revisit if the model sends malformed arguments in practice. Large tool outputs are not truncated yet: add a limit if a hospital response turns out to be big.

**Commit 5: hooks.**
- Results are three types: `Allow`, `Block(reason)`, `Pause(reason, payload)`. The runner fills in `by` (the hook's name). The reason is passed on exactly as the hook wrote it.
- Four points: `before_model`, `after_model`, `before_tool`, `after_tool`. A hook is `hook(ctx, event)`, sync or async. `HookContext` has the session id, a turn id, the opaque per-turn `data`, a **live read-only view** of the turn's state, and `scratch`: a dict shared by every hook of one turn and thrown away after it. A policy such as "one change per turn" keeps its counter in `scratch` and reads the tool's `tags`, so it needs nothing from the runtime (tested with a generic version).
- **Order:** registration order, the first non-Allow decides and later hooks don't run.
- **Fail closed:** a hook that raises, times out (`hook_timeout_s`, 10s) or returns nothing counts as a Block. The Block reason names the hook and the exception type, never the exception message, so nothing private reaches the model. Returning `None` by mistake blocks instead of allowing: a safety hook someone forgot to finish must not let calls through.
- **Pause only before a tool.** A Pause from any other point becomes a Block saying it cannot pause. `payload` is not read or checked here. It must be JSON-serialisable and is checked when the loop saves it to the session state.
- **After a tool:** a Block replaces the result with a `Failed(reason)`. Intended for hiding or refusing an output, not for undoing a write.
- **Trace:** every decision is a `hook_decision` event with the hook name, point, status (`allow`/`block`/`pause`), reason and duration. Allow is recorded too, so the trace shows which checks ran.
- **Known limit:** the timeout only works for async hooks. A sync hook that blocks the thread can't be interrupted. Hooks that do I/O (such as reading a clock from the hospital) must be async.
- Not in this commit: what happens to a Block or Pause. That is the loop's job in commit 7 (a `tool_result` for a blocked call, end of turn on Pause).
- **Fix made before committing: hooks see live state, not a snapshot.** The first version gave hooks a copy of the state from the start of the turn. In the confirm flow, code marks the pending action confirmed and runs the stored call in the same turn, so a `before_tool` hook would still have seen "awaiting" and blocked the confirmed call. `HookContext.state` is now a `MappingProxyType` over the turn's own state dict (build it with `HookContext.for_turn`). The order in the confirm flow is therefore: change the turn's state, save it with `save_state` (write-ahead, 1.7), then run the call through the hooks. Hooks can't write through the view (top level only; nested values are the turn's objects and hooks must not change them). Tested both ways.

