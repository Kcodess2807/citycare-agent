# How a message moves through the system

Status: Phase 2 design agreed (docs/design-log.md, 2.1 to 2.10). Built so far: agentcore (Phase 1, complete) and Phase 2 commits 1 to 3 (hospital client and read tools, section 6; `lock_held` and `history_transform` on the loop, see the notes under section 2; the router, section 7). Sections 2 and 3 are implemented; section 1 is the Phase 2 design, built in six commits (2.10). Updated in the same commit as the code it describes.
Blocks marked (P2) are built tonight, (P3) in Phase 3.

## 1. One `/chat` turn (Phase 2 design)

The service takes the session lock once for the whole turn. `run_turn` is told the lock is already held (commit 2), so it does not lock again.

```mermaid
flowchart TD
    A[POST /chat: session_id, phone, message] --> L[Take the session lock, load state]
    L --> E[Emergency check: stub returns None tonight, real check P3]
    E -- emergency P3 --> X[Fixed reply: call 108, emergency=true, handoff, clear pending]
    E -- ok --> R[Router: haiku, structured output: agent + language]
    R -- unusable output --> RF[Fallback: last turn's agent, else info]
    R -- ok --> S
    RF --> S{Which agent}
    S -- human --> HU[Fixed handoff reply, no model call]
    S -- info --> AI[Info agent: hospital_info.md in prompt, no tools]
    S -- booking / billing --> AB[Specialist agent loop: read tools only, phone from ctx]
    AI --> RES
    AB --> RES{Loop result}
    RES -- done --> OK[Commit TEXT-ONLY history, save facts]
    RES -- paused P3 --> OK
    RES -- failed --> FL[Drop history, keep state, save facts, build reply from outcomes, mark outcomes told]
    OK --> K[Reply: reply, agent, handoff, emergency, trace_id. Always HTTP 200]
    FL --> K
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
- `agent_service/`: hospital client, tools, prompts, hooks (ownership, confirmation, 4-hour rule, emergency, one write per turn), router, agents.

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
