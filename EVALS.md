# EVALS

> **Read first.** (1) These three reported runs are the last three of **13** real eval runs made with the API key; all 13 are listed in "Every eval run made with the key" below, including the invalid run and the sonnet comparison. (2) **Scenario 14 uses a check type of our own, `payment_link_count`. On an unmodified runner it fails with "unknown check type payment_link_count"** (the run continues, nothing crashes), so on an unmodified runner the best possible result on our 14 scenarios is 13 of 14. The given 10 and their checks are unchanged.

Three chaos runs of the agent against the mock hospital, on one frozen commit. Every number below comes from the three result files in `evals/reported/` and their traces. Nothing here is an average of runs we liked.

## What was run

- **Commit:** `21b4690e6e7978dbb7c730528bfa3161782f2a57`, working tree clean for all three runs (checked after each run: `dirty=0`). `pytest` on that tree: 575 passed, no API key set.
- **Scenarios:** the 10 given files, unchanged since the starter commit (`git diff 255121a HEAD -- evals/scenarios` shows only our 4 new files), and our own `11_` to `14_` (4 scenarios, CLAUDE.md asks for 3).
- **Chaos:** `--chaos`, the mock's default settings: 10% of calls answer 503 before saving, 0.3 to 2.0 s latency, 10% of bookings lose the slot to someone else, 5% of writes are saved and still answer 503. Scenario 04 and our 13 also script a saved-then-503 on the first write.
- **Models:** router, specialists and the second yes layer are all `claude-haiku-5-5` (specialists at effort `low`). One earlier sonnet comparison is in `docs/design-log.md` (3.12), not part of these runs.
- **Command:** `uv run python tools/run_eval_with_budget.py --limit 0.50 --chaos`, three times back to back, each with a $0.50 hard stop (never near). The wrapper starts both servers, runs `evals.run_evals`, adds up cost from the trace file and stops the servers.
- **When:** 2026-11-25, runs started at about 01:47, 01:51 and 01:55 (IST).

## Rules, set before the runs

1. The first three completed runs count, whatever they score.
2. A run is invalid only if more than half of its model calls fail with API timeouts or connection errors. An invalid run is re-run, its file is kept and it is disclosed.
3. Nothing in code, prompts or scenarios changes between or after the runs.

All three runs were valid (0 failed model calls out of 97, 94 and 96). No run was re-run, so the first three completed runs are the three reported ones. Nothing was changed between them.

## Results

| | Run 1 (`014704`) | Run 2 (`015121`) | Run 3 (`015551`) |
|---|---|---|---|
| Pass rate, 10 given | 9/10 | 10/10 | 10/10 |
| Pass rate, our 4 | 4/4 | 4/4 | 4/4 |
| All 14 | 13/14 (93%) | 14/14 (100%) | 14/14 (100%) |
| Time per turn, median / worst (38 turns, server side) | 5.5 s / 13.6 s | 5.1 s / 17.9 s | 4.4 s / 18.5 s |
| Scenario wall time, all 14 | 210.8 s | 220.0 s | 234.7 s |
| Cost (from traces) | $0.0125 | $0.0114 | $0.0115 |
| Model calls (failed) | 97 (0) | 94 (0) | 96 (0) |
| Failed hospital calls, retried or verified | 9 | 8 | 5 |
| Second yes layer used | 2, both `confirm`, both right | 2, same | 2, same |
| Router emergency flag fired | 0 | 0 | 0 |

Across the three runs: 41 of 42 scenario results passed; 29 of 30 on the given 10; 12 of 12 on ours. Total cost $0.0354, about $0.0009 per conversation. "Time per turn" is the server's own time from the trace (first event to last event of one turn), so it leaves out the HTTP hop.

## Per model call, and the sonnet comparison

**Per model call, the three reported runs together** (from the traces; 287 calls, none failed). The per-role costs add up to $0.0353, one hundredth of a cent under the $0.0354 above because each run's total was rounded.

| Role | Model (effort) | Calls | Median / p90 / worst time per call | Cost | Per call |
|---|---|---|---|---|---|
| Router | `claude-haiku-5-5` (low) | 77 | 1.12 s / 1.85 s / 4.16 s | $0.0028 | $0.00004 |
| Specialists (booking, billing, info) | `claude-haiku-5-5` (low) | 204 | 0.94 s / 1.76 s / 16.44 s | $0.0323 | $0.00016 |
| Second yes layer | `claude-haiku-5-5` (low) | 6 | 1.11 s / 1.22 s / 1.80 s | $0.0002 | $0.00004 |

Thinking tokens are billed as output: they were 11,510 of the specialists' 25,372 output tokens (45%) and none of the router's. A turn is several calls (router, then one to five specialist calls and tool calls), which is why the median turn is 4.4 to 5.5 s while the median call is about 1 s.

**Sonnet comparison (one run, not one of the three).** Same chaos settings, the specialists on `claude-sonnet-5-5` through a temporary edit that was reverted straight afterwards; the router and the yes layer stayed on haiku. Ledger row 10 above; the haiku column is ledger row 7 (the chaos run of the same ten scenarios on the tree just before our four were added). Cost is $0.17 against about $0.01 for a haiku run of all 14.

| | Haiku, chaos, given 10 | Sonnet, chaos, given 10 |
|---|---|---|
| Pass rate | 9/10 | 10/10 |
| Time per turn (25 turns), median / worst | 4.6 s / 16.9 s | 4.7 s / 14.9 s |
| Cost | $0.0075 | $0.1115 (about 15 times) |

On our four scenarios sonnet passed 4/4 ($0.0579, median 6.3 s per turn); all 14 together 14/14, median 4.7 s, worst 14.9 s, $0.1695. The one haiku failure in that comparison was the router (haiku in both runs) sending a general money question to billing, so sonnet's 10/10 is the router answering differently, not sonnet being better at the specialists' job. One run each cannot tell the two models apart on harder, unseen conversations.

## Every failing case

There is one, in one run.

**Run 1, `reschedule_own`: `A1003 is on 2026-12-03, expected 2026-12-05`.** From its trace: the first turn went to the read-back for Sat 5 Dec 10:00am. On "Yes, that's fine, please move it." the strict yes rule said no, the second yes layer said `confirm`, the gate re-checked the records, and `reschedule_appointment` was called. The mock answered `409 slot_taken: That slot was just taken` (its injected race, 10% of bookings and reschedules under chaos). A 409 is a definite no, so the call was not retried, and the code replied "That time was just taken by someone else, so your appointment was not moved. Tell me another time and I'll check what's free." Nothing was changed and the reply was true. It fails because the scenario expects the appointment to end up moved.
- **Why we did not fix it:** after a lost race on a booking we offer the next free slot (design-log 3.8); we deliberately did not build that for reschedule, so the patient has to ask again. It is a known limit (below), and in runs 2 and 3 the same scenario passed because the race did not hit it.

No other scenario failed in any run. Two passes are worth knowing about because chaos changed the path: in run 2, `book_new_patient` lost its 5:00pm slot to the injected race, the code said so and offered 5:30pm as a new read-back, and the patient's "Yes." booked that slot (the scenario's check, a booking after 5pm, still passed). In run 1, `emergency_while_booking_pending` ended with the fixed staff handoff text on the late "yes", in runs 2 and 3 with a model-written reply that refuses to book and points to 108 (see weaknesses).

## Replies read by hand

The runner's reply checks are keyword checks. For example, `error_after_commit` wants one of "booked", "confirmed" or "scheduled" in the last reply, and "it is not booked" contains "booked". We cannot change the given checks, so we read every reply of all three runs (114 replies) as well as running a script that cross-checks each reply against its trace.
- **False claims of a change: none.** Every reply that says something was booked, moved, cancelled or sent was either built by code from a finished write ("Booked: ...", "Done: ...", "Here is your payment link ...": 31 replies over the three runs) or came after one and matched the hospital's records (`list_appointments` / the facts line). The model-written ones with those words (4, 3 and 4 per run) all follow a finished write.
- **Status questions** ("So is it booked or not?", "So is it cancelled?", "Yes." after a booking): all correct in all three runs (8 model-answered turns of this kind, 8 correct; a ninth was answered by the code's own "Booked:" reply). In an earlier run (design-log 3.10) two of four were wrong ("nothing has been booked" when it was); the prompt line and the finished-write facts added after that are the fix, and these runs and the two before them are the only evidence it holds (8 of 8 here, 4 of 4 before).
- **Exactly one link:** `payment_link_twice` returned the same URL both times in every run, and the hospital held one link.
- **Replies grounded in the document:** the insurance question (`info_not_in_docs`) was answered "I don't have that information" and offered staff in all three runs; run 1 also said the fee is Rs 500 and cash, UPI and cards are accepted, which is in `hospital_info.md` lines 26 to 27.
- **Medical advice: none.** See the post-emergency replies in the weaknesses.

## Changes to the scenario set (disclosed)

- **Scenario 13 was changed once, after its first real run, before these runs.** The first version had 3 turns and checks on fixed turns. On haiku (calm) the model asked "Shall I cancel it?" itself, so the patient's "yes" went to the model, the real cancel never ran, and the honest "not yet, still booked" failed the checks. The scenario now has one more "yes" and path-independent checks (`any` / `all` / `last` turns), so it holds whether the model asks first or goes straight to the read-back. It is our own file, and the change is logged in `docs/design-log.md` (3.12).
- **One new check type, `payment_link_count`** (bill, equals / min / max), added to `evals/run_evals.py` for scenario 14, because the given link checks can only say "at least one" or "none". No existing check changed. **On an unmodified runner (the starter's `evals/run_evals.py`, or a grader's own copy) scenario 14 fails with `unknown check type payment_link_count`.** That is what the original code does with any check type it does not know: it returns the text as the failure reason, the scenario is marked failed, and the run carries on (it does not crash). We did not make scenario 14 depend on a given check instead, because none can say "exactly one link". So scenario 14 only counts with our runner, and the pass rates for "our 4" and "all 14" above were measured with our runner. The pass rates for the given 10 do not depend on the change.

## Every eval run made with the key

This is every run of `evals.run_evals` through the real API that left a result file: 13 runs, one per file in `evals/results/` (the runner writes one file per run). The three reported ones are the last three rows. Times are local (IST), all on 2026-11-24 and 2026-11-25. Cost is the model cost added up from the traces. A run whose result file had been deleted would not appear here; we know of none. Earlier runs ran on earlier code, so their numbers are not comparable with the reported ones.

| # | Result file | Tree it ran on | What | Result | Cost | Valid? and why it was run |
|---|---|---|---|---|---|---|
| 1 | `213231` (11-24 21:32) | Phase 2, read-only agents (no write tools yet) | calm, given 10 | 3/10 | $0.0089 | valid. First run, predicted: 7 scenarios need writes (design-log 2.12) |
| 2 | `001702` (11-25 00:17) | `36cf352`, first tree with write tools | calm, given 10 | 6/10 | $0.0104 | valid. Failures were the agent asking for confirmation itself and not picking a slot (3.7) |
| 3 | `005706` (00:57) | `f802e96`, second yes layer | calm, given 10 | 7/10 | $0.0037 | **INVALID, kept and disclosed:** 102 of 119 specialist model calls failed with `APITimeoutError` (about 5 s each, scenario 01 took 86.6 s). Network problem on this machine; the API answered in 0.6 to 2 s right afterwards. Some scenarios "passed" by accident (a failing turn hands off, and one scenario expects a handoff). Re-run as #4 and #5 |
| 4 | `010016` (01:00) | `f802e96` | calm, given 10 | 10/10 | $0.0078 | valid. A hand read found two replies saying "nothing is booked" when it was booked, which the runner's keyword check cannot catch (3.10) |
| 5 | `010231` (01:02) | `f802e96` | calm, given 10 | 10/10 | $0.0076 | valid. Same tree as #4, to see variance: those two replies were correct this time |
| 6 | `011659` (01:16) | `ef46035` plus the three fixes that became `9af3fdc`, `026bde9`, `a3a201c` (run before they were committed; same tree as `a3a201c`) | calm, given 10 | 10/10 | $0.0084 | valid. Check after the three fixes |
| 7 | `012000` (01:20) | same as #6 | chaos, given 10 | 9/10 | $0.0075 | valid. `info_not_in_docs` failed: the router sent the insurance question to billing (3.11) |
| 8 | `012956` (01:29) | `a3a201c` plus the router line and scenarios 11 to 14 (uncommitted), scenario 13 in its first version | calm, only our 4 | 3/4 | $0.0046 | valid. Checked that our own scenarios work. 13 failed because the model asked "Shall I cancel it?" itself and the cancel never ran |
| 9 | `013047` (01:30) | same, scenario 13 changed | calm, only scenario 13 | 1/1 | $0.0011 | valid. Re-check after the change described under "Changes to the scenario set" |
| 10 | `013548` (01:35) | same, with `SPECIALIST_MODEL` temporarily set to `claude-sonnet-5-5` (the edit was reverted straight after) | **sonnet comparison**, chaos, all 14 | 14/14 | $0.1695 | valid. For the haiku vs sonnet comparison (design-log 3.12), $1.50 stop |
| 11 | `014704` (01:47) | `21b4690e6e7978dbb7c730528bfa3161782f2a57` | **reported run 1**, chaos, all 14 | 13/14 | $0.0125 | valid |
| 12 | `015121` (01:51) | `21b4690...` | **reported run 2** | 14/14 | $0.0114 | valid |
| 13 | `015551` (01:55) | `21b4690...` | **reported run 3** | 14/14 | $0.0115 | valid |

Total for the 13 eval runs: about $0.265, of which the sonnet comparison is $0.17. This counts model spend by the agents only; Claude Code's own cost is separate and not tracked here. The reported runs started after the tree was frozen, and rows 1 to 10 were development runs on moving code: we read their failures, fixed what they exposed, and ran again. The 10 given scenarios were visible throughout. The fixes are general (the prompts state rules, not scenario wording), but several were prompted by a failure in a given scenario; for example the extra router line about billing versus general money questions came from row 7. We did not put scenario wording into any prompt or word list.

**Real-API calls that were not eval runs** (small, by hand, each with a hard stop): the Phase 1 smoke test (`tools/smoke_real.py`, two models, 23 model calls, $0.03, design-log 1.12); a router output-format check (`tools/check_router_format.py`, one call, $0.00004, 2.x); the second yes layer's check on the real model (`tools/check_yes_layer.py`, 6 calls, then 4 more, then 10 calls, about $0.0007 in all, 3.9); and a few one-line API reachability tests during the invalid run (under a cent in all).

## Known weaknesses

The full list of known weaknesses, including the ones below, is in DESIGN.md section 5.

- **Reschedule has no next-slot offer after a lost race** (the only failure above).
- **The router is one cheap model call and can route an ambiguous question either way.** It sent an insurance question to billing in one earlier chaos run; after a general boundary line was added, it routed that question to info in all three of these runs. Three runs do not prove it.
- **The second router layer for emergencies is unproven on the real API.** The emergency flag fired 0 times in every run; the word lists caught both emergency messages first (0 model calls). Its schema was accepted by the API, but it has never fired for real.
- **After an emergency, the next message goes to a normal agent.** In `emergency_while_booking_pending` the late "yes, please book it" got a model-written reply in runs 2 and 3. Both refuse to book and point to 108 and the Emergency department. Run 3's also says chest pain and breathlessness "is an emergency". That is the same message our fixed reply gives, but it is the model's own wording, not code-fixed text.
- **The model sometimes drops items from a list.** In run 3 the first turn of `book_new_patient` listed 5:00pm to 7:00pm and left out 7:30pm, which the hospital had free. Nothing was invented, but the list was incomplete (seen once in the Phase 2 run as well).
- **The second yes layer widens "yes" with a model.** It only recognises agreement with a read-back that code built, never chooses anything, fails closed, and the gate re-checks everything before running (design-log 3.9). Its guard word lists are English, Hinglish and basic Hindi only, and a list is brittle. It was used twice per run and was right every time; that is a small sample.
- **Two strikes count consecutive failed turns, not the same request** (a failure, a different request, then another failure count as two).
- **The keyword reply checks in the given runner are lenient** (see above), so a pass rate from the runner alone overstates quality; the hand read is what we rely on.
- **What these runs do not show:** behaviour on scenarios we have not seen, on any other chaos settings than the defaults, over more than three runs, or with real patients. The sonnet comparison (design-log 3.12) is one run, and it does not show sonnet to be better at the specialists' job for about 15 times the cost.

## Files

- `evals/reported/run-20261125-014704-1.json`, `-015121-1.json`, `-015551-1.json`: the three result files, as the runner wrote them (transcripts, per-scenario results and reasons).
- The matching `.traces.jsonl` files (every model call, tool call, hook decision, cost and time) are kept locally in `evals/results/` and not committed.
- Reproduce: start nothing by hand; `uv run python tools/run_eval_with_budget.py --limit 0.50 --chaos` starts both servers, runs all 14 scenarios once, prints the same table, and stops the servers. It needs an API key in the environment and costs about a cent.
