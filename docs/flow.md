# How a message moves through the system

Status: everything in this file is built and was exercised by the three reported chaos runs on commit `21b4690` (EVALS.md). The sections follow the order they were built: agentcore (sections 2 and 3, Phase 1), the service (sections 1 and 6 to 9, Phase 2: hospital client and read tools, router, agents and the facts note, `/chat` and the trace endpoint) and the safety pieces (the "Phase 3" sections at the end: the confirmation gate, the 4-hour rule, handoffs and strikes, the next-slot offer, the second yes layer). Section 1 is the whole turn as built. Updated in the same commit as the code it describes.

Headings such as "(built, Phase 2 commit 4)" say when a piece was first built; their contents were brought up to date for Phase 3 (tool lists, the pause path).

## 1. One `/chat` turn

The service takes the session lock once for the whole turn. `run_turn` is told the lock is already held (commit 2), so it does not lock again.

```mermaid
flowchart TD
    A[POST /chat: session_id, phone, message] --> L[Take the session lock, load state]
    L --> E[Emergency check: keyword code, en/hi/hinglish, no model]
    E -- emergency --> X[Fixed reply: call 108, emergency=true, handoff, clear pending]
    E -- ok --> P{A write waiting for a yes?}
    P -- strict yes, or the guarded yes layer says confirm --> CF[Code runs the stored action after checking again. Reply built from the real result]
    P -- none, or not a yes: the pending action is spent --> R[Router: haiku, structured output: agent + language + emergency flag]
    R -- emergency_suspected --> X
    R -- unusable output --> RF[Fallback: last turn's agent, else info. Counts as a strike]
    R -- ok --> S
    RF --> S{Which agent}
    S -- human --> HU[Fixed handoff reply, no model call]
    S -- info --> AI[Info agent: hospital_info.md in prompt, one tool: contact_staff]
    S -- booking / billing --> AB[Specialist agent loop: read tools, contact_staff and write tools behind the gate, phone from ctx]
    AI --> RES
    AB --> RES{Loop result}
    RES -- done --> OK[Commit TEXT-ONLY history, save facts]
    RES -- paused: a write waits, reply is the code's read-back --> OK
    RES -- failed --> FL[Drop history, keep state, save facts, build reply from outcomes, mark outcomes told]
    OK --> K[Reply: reply, agent, handoff, emergency, trace_id. Always HTTP 200]
    FL --> K
    CF --> K
    HU --> K
    X --> K
```

What the specialist receives each turn: its own system prompt and tools (stable, cached), the shared text-only history, and a marked "known facts" block (ids of doctors, slots, appointments, bills from earlier turns) in front of the user message. The facts block is stripped when the history is committed.

Known limit: one agent per turn. A message with two topics gets one answered, and the reply tells the patient to ask the other next.

### 1b. The facts note

```mermaid
flowchart LR
    T[Read tool runs] --> W[Writes ids into ctx.data facts for this turn]
    W --> M[Service merges into state.facts after the turn, also after a failed turn]
    M --> N[Next turn: facts block added in front of the user message for any agent]
    N --> C[Commit transform strips the block from stored history]
```

## 2. The agent loop (agentcore)

```mermaid
flowchart TD
    S[Start step n] --> B1{n > step limit or budget gone?}
    B1 -- yes --> FAIL[status=failed, handoff]
    B1 -- no --> M[before_model hooks]
    M -- Block --> FAIL
    M -- Allow --> CALL[Model call with retries and timeout]
    CALL --> R{stop_reason}
    R -- end_turn --> DONE[status=done, text reply]
    R -- max_tokens --> RT{already retried?}
    RT -- no --> CALL2[Retry step with 2x max_tokens]
    CALL2 --> R
    RT -- yes --> FAIL
    R -- refusal --> FAIL
    R -- pause_turn --> S
    R -- tool_use --> T[For each tool call, in order]
    T --> H1[before_tool hooks]
    H1 -- Block --> TR1[tool_result: blocked + reason]
    H1 -- Pause --> TR2[tool_result: the hook's reason; payload held in memory; turn will end paused]
    H1 -- Allow --> EX[Executor, see section 3]
    EX --> TR3[tool_result: done / failed / unknown]
    TR1 --> NEXT[Append tool_use + tool_results together]
    TR2 --> NEXT
    TR3 --> NEXT
    NEXT --> S
```

Notes on section 2: a `max_tokens` retry counts as a step against the step limit; the paused payload is written to the session state only when the turn ends paused, together with the history that holds the read-back, never when the hook pauses; a failed turn drops the history but keeps what the executor saved.

Two options on `run_turn` (Phase 2 commit 2): `lock_held=True` skips taking the session lock when the caller already holds it. `history_transform` is applied to the messages only at commit time (done or paused), after which the store's pairing check runs; the model sees the untransformed messages during the turn. A transform that raises fails the turn as `history_invalid` and stores nothing.

## 3. The tool executor (one atomic unit for a write)

```mermaid
flowchart TD
    P[Budget >= reserve_s?] -- no --> NOCH[Do nothing: nothing changed, handoff]
    P -- yes --> W1[Save state: inflight = tool + input]
    W1 --> RUN[run: one attempt]
    RUN -- ok --> DONE[Done]
    RUN -- ToolFailed --> FAILED[Failed: nothing changed]
    RUN -- Retryable / Unknown --> Q{idempotent, policy allows, attempts left?}
    Q -- yes --> BO[Backoff] --> RUN
    Q -- no --> V{tool has verify?}
    V -- yes --> VER[verify against real state]
    V -- no --> UNK
    VER -- confirmed --> DONE
    VER -- not done --> FAILED
    VER -- verify failed --> UNK[Unknown: handoff]
    DONE --> W2[Save state: clear inflight, record outcome]
    FAILED --> W2
    UNK --> W2
```

The whole box from `Save state: inflight` to `record outcome` runs shielded: a budget timeout or a client disconnect can't stop it halfway.

## 4. What is stored where

| Thing | Where | Lifetime | Rolled back on a failed turn? |
|---|---|---|---|
| Messages (history) | SessionStore | 30 min idle | yes |
| `state`: pending payload, inflight, outcomes, caches | SessionStore | 30 min idle | **no** |
| Trace events | TraceStore (LRU + JSONL) | outlives the session | no |

## 5. Who owns what

- `agentcore/`: loop, registry, executor, hooks mechanism, trace, stores. No hospital words, no hospital imports.
- `agent_service/`: hospital client, tools (reads, `contact_staff`, the four write tools), prompts, the confirmation gate (one `before_tool` hook: ownership, confirmation, the 4-hour rule), the emergency check (plain code in the service, before the router, not a hook), the second yes layer, router, agents. There is no separate "one write per turn" hook: the first write call pauses the turn, a later call in the same step is skipped, any tool call after the pause is refused, and a second attempt fails the turn to a handoff (`agentcore/loop.py`, tested in `tests/test_confirm.py`).

## 6. One read tool call (built, Phase 2 commit 1)

```mermaid
flowchart TD
    M[Model asks for a read tool + input] --> P{Input valid? no phone field allowed}
    P -- no --> PF[tool_result: Invalid input, field and rule]
    P -- yes --> X[Executor: run, one attempt]
    X --> T[Tool reads phone from ctx.data, never from the model]
    T -- no phone --> NF[ToolFailed: no verified patient]
    T --> H[HospitalClient.get, 6s timeout]
    H -- 2xx --> OK[Strip phone from records, write ids to ctx.data facts, return value]
    H -- 4xx --> TF[ToolFailed: hospital's error and message, no retry]
    H -- 5xx --> TR[ToolRetryable: executor retries with backoff]
    H -- timeout / dropped --> TU[ToolOutcomeUnknown: executor retries with backoff]
    TR --> X
    TU --> X
    TR -. retries used up .-> UA[Failed: unavailable. A read has no verify and is not atomic]
```

## 7. The router (built, Phase 2 commit 3)

```mermaid
flowchart TD
    M[Message + last 2 turns + last agent + kind of pending action] --> C[Haiku call: effort low, JSON schema agent + language, 10s timeout]
    C -- API error --> RT{Retry left?}
    RT -- yes, backoff / retry-after --> C
    RT -- no, or a 400 --> FB
    C -- answer --> V{Valid JSON and agent in the four?}
    V -- no, not retried --> FB[Fallback agent: pending owner, else last agent, else info. fallback=true is a strike]
    V -- yes --> P{Pending action and 3 words or fewer and not human?}
    P -- yes --> OWN[Agent that owns the pending action]
    P -- no --> PICK[The router's agent]
    FB --> OUT[Route: agent, language, fallback]
    OWN --> OUT
    PICK --> OUT
```

Every attempt is a `model_call` trace event (role router) with tokens and cost. Unusable answers are marked `unusable`.

## 8. What one specialist call receives (built, Phase 2 commit 4; tool lists as of Phase 3)

```mermaid
flowchart LR
    subgraph Cached prefix: identical for every patient and turn
        SP[System prompt: common rules + job. Info also holds hospital_info.md]
        TL[Tool list. info: contact_staff. booking: find_doctors, free_slots, list_appointments, contact_staff + book, reschedule, cancel. billing: list_bills, contact_staff + create_payment_link]
    end
    subgraph Per turn, after the prefix
        H[History: text only, patient words + replies the patient saw]
        FN[Facts note: hospital time, doctor, slot, appointment, bill ids]
        UM[Patient message, markers broken]
    end
    SP --> M[Model call]
    TL --> M
    H --> M
    FN --> M
    UM --> M
    M --> C[Commit: text_only drops tool traffic, thinking, the note and unseen pre-tool text]
```

Human handoff makes no model call: the reply is a fixed text in the patient's language, `handoff: true`.

## 9. `/chat` as built (Phase 2 commit 5)

```mermaid
flowchart TD
    A[POST /chat: session_id, phone, message] --> K[Storage key = sha256 of phone + session_id]
    K --> L[Take that key's lock, load history + state]
    L --> E{emergency check: keyword code (agent_service/emergency.py)}
    E -- hit --> X[Clear pending, fixed reply, agent safety, emergency + handoff]
    E -- none --> R[Router, same Tracer]
    R -- human --> H[Fixed reply in the patient's language, handoff true]
    R -- booking --> T[Fetch /now, build facts note]
    R -- billing / info --> N[Build facts note, no clock]
    T --> S[run_turn: lock_held, text_only, same Tracer]
    N --> S
    S -- done --> OK[Reply = model text, handoff = escalate]
    S -- failed --> F{Outcomes saved?}
    F -- unknown --> HO[Human text, handoff]
    F -- done --> DN[It went through, couldn't finish the reply. Outcomes marked told]
    F -- none, escalate --> HO
    F -- none --> FR[Fixed failure text, no handoff]
    OK --> SV[Save facts, last agent, language]
    DN --> SV
    HO --> SV
    FR --> SV
    SV --> Z[ChatResponse: reply, agent, handoff, emergency, trace_id. Always 200]
    X --> Z
    H --> Z
    A -. any exception anywhere .-> LR[Human reply en, handoff true, stop event internal] --> Z
```

`GET /trace/{session_id}`: every event of the session (router and specialist share one trace id per turn) and the totals over all turns. Traces are filed under the request's session id, state under the hashed key, so one session id used by two phones is two conversations with one shared trace list.

## Phase 3: a write (commit 3)

```mermaid
flowchart TD
    M[Message] --> E{emergency?}
    E -- yes --> EX[Fixed reply, clear pending, emergency + handoff]
    E -- no --> P{pending action?}
    P -- yes --> SP[Spend it: pop and save]
    SP --> Y{strict yes?}
    Y -- yes --> C[Check again against the hospital's records]
    C -- changed --> CH[Reply: something changed, nothing ran]
    C -- same --> X[Executor runs the STORED action with the STORED key]
    X --> R{result}
    R -- Done --> D[Reply from the real result: booked / cancelled / link]
    R -- Failed --> F[Reply: not done, nothing changed / slot taken]
    R -- Unknown --> U[Reply: could not confirm, don't retry. handoff]
    Y -- no --> RT[Router, agent as usual]
    P -- no --> RT
    RT --> W{model calls a write tool}
    W --> G[Gate hook: fetch records, own id? refuse, same words for foreign and missing]
    G -- ok --> PA[Pause: store pending, reply = code read-back]
    G -- refused --> B[Block: the model is told, nothing stored]
```

## Phase 3: handoffs and strikes (commit 4a)

```mermaid
flowchart TD
    M[Message] --> E{word-list emergency?}
    E -- yes --> EX
    E -- no --> RT[Router: agent, language, emergency_suspected]
    RT --> RF{emergency_suspected?}
    RF -- yes --> EX[Same path: clear pending, fixed 108 reply, emergency + handoff]
    RF -- no --> A[Specialist turn]
    A --> W{cancel / reschedule tool call}
    W --> G[Gate: own id, still booked, then /now]
    G -- start - now under 4h, or already past --> S[kind = soon]
    G -- /now unreachable after retries --> K[kind = clock]
    G -- ok --> PA[Pause with read-back, as before]
    A --> CS{contact_staff called}
    CS --> AS[kind = asked]
    S --> H
    K --> H
    AS --> H[Reply from a fixed template, handoff true. Model text is dropped]
    A --> ST{turn failed, or router answer unusable?}
    ST -- yes --> N[strikes + 1]
    ST -- no --> Z[strikes = 0]
    N --> T{strikes = 2?}
    T -- yes --> H2[Human reply, handoff true, strikes = 0]
```

At the yes the gate checks again, so an appointment that moved inside 4 hours after the read-back goes to staff instead of running.

## Phase 3: how the write tools reach an agent (commit 4b)

```mermaid
flowchart LR
    S[ChatService start] --> H[HookRunner] --> I[Gate.install: confirm_gate at before_tool]
    I --> B{build_agents with write tools}
    B -- hooks has confirm_gate --> OK[booking: book, reschedule, cancel. billing: payment link]
    B -- no gate --> ERR[ValueError at start-up]
    OK --> T[Model calls a write tool] --> G[Gate: pause, nothing runs] --> Y[Next message: strict yes, code runs the stored action]
```

## Phase 3: a lost race on a booking (fix 2)

```mermaid
flowchart TD
    Y[Strict yes on a pending booking] --> X[Executor runs the stored booking]
    X -- 409 slot taken --> N[Gate.next_free_slot: same doctor, after the taken slot, up to 7 days]
    N -- found --> C[Gate check, new pending action with a new key]
    C --> R[Reply: it was not booked + the next slot as a full read-back]
    R --> Y2[Next message: strict yes runs it, anything else spends it]
    N -- none or hospital cannot say --> P[Plain: not booked, no pending]
```

## Phase 3: the second yes layer (fix 3)

```mermaid
flowchart TD
    M[Message, a pending action exists] --> S{strict yes?}
    S -- yes --> RUN
    S -- no --> G{sendable? no digit, number word, ?, process word, 25 words or fewer}
    G -- no --> NY[Not a yes: pending spent, router as usual]
    G -- yes --> H[One haiku call: read-back + message, answer confirm / change / decline / other]
    H -- confirm --> RUN[Gate checks again, executor runs the STORED action with the STORED key]
    H -- anything else, error, timeout, odd output --> NY
```
