# Design log

## Status

- **Current phase:** 0 (understand) done, quiz passed (8.7/10). Next: Phase 1 design discussion, no code yet
- **Code written:** none yet
- **Decided:**
  - 0.1 and 0.4: payment links. Retry on 503, never auto-retry on timeout (hand off), cache `bill_id -> link` per session
  - 0.2: the eval runner uses one session id per scenario per run
  - 0.3: hospital client timeout above 2s; a timeout on a write is treated like saved-then-503
  - 0.5: booking idempotency key is fresh per confirmation, stored in a pending action
- **Open:**
  - Client timeout, retry count, backoff and per-turn budget (Phase 1)
  - How code detects a clear yes (Phase 3)

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
