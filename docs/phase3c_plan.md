# Phase 3c: compaction hand-offs (implementation plan)

Plan for design §5.5 / §13 step 3c. Most of 3c already runs live: the keep-alive compacts the AFClaude task-manager with a save prompt first and gives the hand-off back afterwards (hand-offs #1..#16 so far), and the resident runner runs it as the `compaction_check` job and the `compaction` act. What's left is the part that 3b's several task-managers (D-281) and D-144's "other long-running agents" need, plus room for the D-305 review's outcome. No code changes yet; each step in §5 is one worker branch.

Binding inputs: D-144 (compaction for agents whose total run is longer than one session length; structured hand-off fed back on resume), D-155 (hand-offs in the DB), D-191 (per tmux session; few agents run that long), D-218 ("longer than one session length" is algorithmic, not Claude's judgement), D-233 (the save prompt directly before every compaction), D-237/D-238 (compact while the cache is warm: at a run end and after ~50 min idle; `idleCompaction: false`), D-307 #9 (threshold 150k instead of the break-even; also mid-run), D-305 (compaction stays; improve the hand-off; review ~01.11.; its result decides between compaction and hand-off + `/clear`), D-262 (inbox triage during the flow; the flow isn't activity), D-224 (what a run end is), D-281 (every project is its task-manager session), D-309 (the task-manager keeps its cache warm during a run), D-234/D-235 (fail-stop).

## 0. Already decided

- **The trigger is D-238 with D-307's threshold**, not "at a start, by age" (D-191/D-218's first build, replaced; design §5.5 says so). The §13 item's "above the computed break-even" is out of date: D-307 replaced the break-even with the setting `compaction_threshold_tokens` (150k). The break-even is still computed and shown (quickview), not used as the trigger.
- **Hand-off content belongs to the D-305 review.** The review runs as the nightly task `compaction-review-2026-11-01` (01.11. 22:00, prompt `compaction_handoff_review.md`) and writes a review doc plus an open question. 3c builds nothing that changes what a hand-off holds (fields, prompts' content list, the "decision ids / files touched" of §5.5 point 2) before the owner answers it. S3 is reserved for that answer.
- **The flow itself stays as built.** Save prompt → wait for a new `handoffs` row → `/compact` → marker → hand-off back → READY (or the run goes on after a mid-run one), every failure a fail-stop. 3c changes who it runs for and what the texts point at, not its steps.
- **Never an owner's own session.** `stall` (and legacy `own`) rows are sessions the keep-alive only resumes at a limit reset (D-204); D-144 covers AFClaude-driven agents. They're never compacted.

## 1. Today (as-is inventory)

| what | where | state |
|---|---|---|
| `handoffs` table (id, session_id, project_id, created_at, created_by, body JSON, consumed_at, consumed_by_start) | `store.py` (`add_handoff`, `latest_handoff`, `list_handoffs`, `consume_handoff`, `render_handoff`, `handoff_body`); project defaults to the session's project (`session_project`) | built, per session and project |
| write / consume | `actions.handoff_write` (any session its own), `actions.handoff_consume` (runner), CLI `tasks.py handoff write|show`, MCP `afclaude_handoff` | built |
| body fields | summary, done, in_progress, next_steps, owner_messages, in_flight, lessons, working_notes (D-305), open_question_ids, notes | built; decision ids / files touched not built (D-305 review decides) |
| trigger | `compact_timing.py` (`context_state`, `decide`, `mid_run_hold`, `break_even`, cost model `usage.cost_model`); `keepalive.compaction_plan`: (a) run end (`run_ended`: last prompt was the run's, not the owner's), (b) 50 min idle (`IDLE_AFTER`, `SEND_MARGIN` before the 1-h cache expiry), (c) mid-run (D-307: idle point, ≥60 min after the last one, owner quiet 10 min); all only at ≥ `compaction_threshold_tokens`; "missed" recorded when the cache expired above it | built (D-238, D-307) |
| holds | `keepalive._compaction_hold`: a start soon (`start_soon`: window start, postponed recheck, fill-up plan, last-stretch slot within `COMPACT_GUARD`), a limit stall, session % above 100 − `COMPACT_SESSION_RESERVE`, preflight / owner hold, not live in tmux, the owner typing; `compaction_busy_reason` (turn / subagent) | built; `start_soon` is not per session (see gaps) |
| the flow | `keepalive.compact_with_handoff` → `save_before_compaction` (`session_end_save.md`, `_refresh`, `_retry`), `/compact <compact.md>`, `compact_resume_text` (`compact_resume.md`), `_compact_now` (`compact_after.md`, or `compact_after_run.md` + `mid_run_text` after a mid-run one), `inbox_notes` (D-241), `record_compaction` (state `compactions`, last `COMPACTIONS_KEPT`), `_compaction_failed` (fail-stop) | built |
| runner | jobs `compaction_check` (60 s, `paused="handler"`) and `compaction` (planned act, 60-min timeout, `on_interrupt="fail"`, `session=True`); `keepalive.run_tick` runs the check over every task-manager of `store.task_managers()` (3b S3); acts of one job run one at a time; `session_lock` per session | built |
| settings | `compaction_enabled` (true), `compaction_threshold_tokens` (150 000); repo `.claude/settings.json` `idleCompaction: false` | built |
| inbox | `inbox_worker.compaction_flow` / `HOLD_COMPACTING` (D-262: triage during the flow, flow ≠ activity) | built, per session |
| display | `export_quickview` context block: threshold, break-even, latest attempts (`compactions`) | built, no project column |
| review | nightly task `compaction-review-2026-11-01` (`compaction_handoff_review.md`) | scheduled |
| tests | `test_compaction.py`, `test_compact_timing.py`, `test_inbox_active.py`; live: `tools/test_compact_live.sh` (throwaway Haiku session) | built |

Gaps against the decisions:

1. **AFClaude-only paths in a per-session flow (3b S6 note "compaction and mid-run messages still use `PROGRESS_FILE`").** `compact_resume_text` fills `{progress}` with AFClaude's `PROGRESS.md` for every session; `progress_note` (the "compacted …" and "FAILED" lines) always appends to AFClaude's progress doc. A second project's task-manager would be told to check the wrong file, and its notes land in AFClaude's log.
2. **`start_soon` holds every task-manager for every start.** The fill-up plan (it carries `plan["session"]` since D-281), the session-window start and a last-stretch slot hold the compaction of all task-managers, though each start continues one (D-281: the picked or the chained project). A task-manager that isn't picked is held until its cache expires ("missed") for nothing. Postponed starts are already filtered by session.
3. **`latest_run_fire` counts fire records without `session` (pre-3b) for every session.** Harmless while they're older than the lookback; must not make a second task-manager look as if it had a run.
4. **D-144's other long-running agents.** Only task-managers are checked. A `review` or `request` session (D-191: few expected) that runs longer than one session length (D-218, algorithmic) gets nothing. On the live DB none has so far (4 review rows, all one-offs), so there's no live case to test against.
5. **Compaction attempts live only in the keepalive state's `compactions` (last N).** 3a S9(b) wrote run_log decision rows for the gate, timing, last stretch, fill-up and reviews, not for compactions; the dashboard's per-session history (F6) has nothing durable to read.

## 2. What changes

- The flow's texts and notes point at the compacted session's own project (its `projects.path`, its progress doc). A session with no project is an error, not a fallback to AFClaude.
- A start soon holds only the task-manager that start will continue.
- Every compaction decision that's recorded (done, failed, missed, dry-run) also gets a run_log row for its session, armed only, through `actions.run_log_add`, once per attempt key (as 3a S9(b)).
- AFClaude-driven non-task-manager sessions (`review`, `request`) that run longer than one session length (the D-218 test) are compacted by the same flow and trigger, with a continue that says "go on with your task".
- After the D-305 answer: whatever it decides (S3).

**Not part of 3c:** the hand-off's content (D-305 review, S3), the dashboard views of hand-offs (F6/F9: phase 7), agent hand-offs in run summaries (§7.9: phase 5a), the D-237 re-read tile (built in the quickview; dashboard: phase 7), compaction of an owner's own sessions (never).

## 3. Design details

### 3.1 Tables

None new. run_log rows use the existing table: component `compaction`, decision `COMPACT` / `FAILED` / `MISSED` / `DRYRUN`, reason = the recorded reason, `session_id` set. Holds aren't rows (they're logged once per reason and change every minute).

### 3.2 The D-218 test for non-task-manager sessions

A `driven_sessions` row of kind `review` or `request`, not ended, live in its tmux session, whose transcript's first user/assistant entry is before the start of the current session window (the window that contains now: its start = the next reset − one session length). That's the interpretation recorded under D-218 ("activity before the current session window started"), and it matches D-191's "existed last session and it is the end of this session" closely enough: the trigger (D-238/D-307) still decides *when*; the age test only decides *whether this session is eligible at all*. Task-managers need no age test (D-238 applies to them unconditionally).

### 3.3 The continue for a non-task-manager session

These sessions have no run fire record (`mid_run_text` needs one), and they don't stop at READY: they're mid-task. A new prompt `compact_after_task.md` (fields: `{resume}`, `{threshold}`, `{task}` = the row's task / review name) tells the session to go on with its task from the hand-off. Only the mid-run trigger (c) applies to them (they have no "run end" the keep-alive sees and are never idle for 50 min while working; once they finish they're never messaged again, so compacting them then saves nothing).

## 4. Order

S1 and S2 are independent of the D-305 review and can be built before 01.11. S3 waits for the owner's answer to the review. S1 before S2 (S2 reuses S1's per-project texts). None of them needs 3b S6 (retire); S1 touches `keepalive.py`, so it isn't merged while another live-code branch edits the compaction section (one at a time, D-307 #1).

## 5. Steps

**S1: several task-managers.**
- **Build:** `compact_resume_text` fills `{progress}` with the session's project's `PROGRESS.md` (`store.session_project` → its path; no project = `failstop.Failed`); `progress_note` takes the project (repo) and the compaction's notes in `_compact_now` / `_compaction_failed` pass the session's; `start_soon(sid)` counts the fill-up plan only when `plan["session"] == sid`, and the session-window start / a last-stretch slot only when that start would continue `sid` (the 3b S4 pick evaluated read-only now; a running run keeps its session, D-281; a failed pick raises, as `start_soon` does today); `latest_run_fire` ignores records without `session` (or the worker shows none is inside the lookback and documents why it's safe); run_log rows per §3.1 in `record_compaction`'s callers (armed only, once per key, a failed write is a fail-stop as in 3a S9(b)); the quickview's compaction rows name the project.
- **Tests:** two task-managers in a temp DB: the resume text and progress note of the second point at its own project; a window start that picks A holds A's compaction and not B's; the fill-up plan of A doesn't hold B; a session without a project fails the compaction (fail-stop, no `/compact` sent); run_log rows: one per attempt key, none in dry-run, none for holds; `test_compaction`, `test_compact_timing`, `test_inbox_active`, `test_keepalive`, `test_export_quickview`, `test_typing`.
- **Done when:** suites, `test_typing` and `check_public` green; one live compaction of the AFClaude task-manager after the deploy behaves as before (same texts, now from the project lookup) and leaves its run_log row.
- **TM verifies:** the next compaction's resume text names `…/AFClaude/PROGRESS.md`; `run_log` has the `compaction` row; the quickview's compaction list still shows it.
- **As built (S1):** `keepalive.session_progress` (the project, its directory, its `PROGRESS.md`; this checkout's project keeps `PROGRESS_FILE`, so AFClaude's texts are byte-identical) is looked up in `_compact_now` before anything is typed and passed through `compact_with_handoff(progress=)` to `compact_resume_text`; `progress_note(line, repo=)`; `_compaction_failed` writes no note for a session without a project (the alert carries it). `start_soon` evaluates `start_continues` (pick_start's candidate choice, factored out as `_pick_candidates` / `_pass_over`, preflights included) lazily, once per call. **Departure:** the fill-up plan did not carry `session` (only the D-274 run-timing plan did): `fillup_plan` now stores `plan["session"]`; a plan stored before the deploy holds nobody until its next re-plan (the next minute). `latest_run_fire` ignores records without `session`; `run_active` keeps counting them (out of scope; they're older than its one-session-length lookback). run_log rows: `_compaction_logged` (`COMPACT` / `FAILED` / `MISSED`, `once` = `compaction <key> of <sid8>:`; failed write = `fail_stop`, not raised); **no `DRYRUN` row**: §3.1 lists it, but rows are armed only, so it was not added to `RUN_LOG_DECISIONS`. Quickview: every task-manager's attempts with `project` (the HTML shows it). `tools/test_compact_live.sh` passes `progress=` (its throwaway session has no project; not run).
- **Risk:** the pick evaluated ahead can differ from the pick at the start (tasks change in between). Then the start meets a compaction of the picked session; it already waits for the session lock (`COMPACT_LOCK_WAIT`), so the cost is a later start, not a broken one. Second: compaction acts run one at a time; two task-managers due together compact one after the other (up to ~25 min each), and the second may miss its warm cache. Recorded as "missed", acceptable while projects are few; revisit if the quickview shows it.

**S2: long-running non-task-manager sessions (D-144 "others").**
- **Build:** `compaction_check` also iterates the eligible rows of §3.2 (kind `review` / `request`, the D-218 age test, never `stall` / `own`); for them `decide` uses only the mid-run trigger (c) with the same threshold and holds (minus the start holds, which are task-manager starts); after the hand-off, `compact_after_task.md` (§3.3, registered in `prompts.py` and `prompts/README.md`); the hand-off and its notes go to the row's project (S1's lookup; a row without one = error). The age test is a pure function in `compact_timing.py` (transcript head + window start), tested on its own.
- **Tests:** the age test at the window edges (started 1 min before / after the window start, DST nights, no transcript = not eligible); a stall row and an ended review are never checked; a review over the threshold mid-task gets save → `/compact` → `compact_after_task.md`; the task-manager path unchanged (`test_compaction` as is); `test_prompts`, `test_typing`.
- **Done when:** suites green; `tools/test_compact_live.sh` extended with a `--task` variant (a throwaway Haiku session driven as a `review` row, its first entry backdated by a fixture transcript, never `test_e2e_tmux.sh`) passes.
- **TM verifies:** the live test's output (marker, hand-off consumed, the session goes on), and that `runner.py status` shows no compaction key for the current review rows (none is old enough).
- **Risk:** low live risk (no session qualifies today); the risk is building for a case that never comes (see question 1).

**S3: the D-305 outcome (after the review of 01.11. 22:00 and the owner's answer).**
- **Build:** depends on the answer, and nothing of it is designed here. The two outcomes D-305 names: (a) compaction stays: add to the hand-off what the review found used from the summary but missing in the hand-off (`store.handoff_body` fields, `render_handoff`, `session_end_save.md`'s list), and mark D-191/D-218's "at a start by age" superseded in the decision store if the owner confirms (question 2); (b) hand-off + `/clear`: `compact_with_handoff` sends `/clear` instead of `/compact` (the marker check changes with it), and the trigger is whatever the owner sets (D-305's 25.10. wording allowed it only at a real run end; the mid-run and idle triggers then need the owner's word). Either way: §13 item 3c's text, design §5.5 "as built", README, GOALS 0a closed.
- **Tests:** per outcome; the full compaction suites and `test_compact_live.sh`.
- **Done when:** the owner's answer is built and one live compaction (or clear) of the AFClaude task-manager passed.
- **TM verifies:** the first live run after the deploy against the review's list of what went missing.
- **Risk:** (b) changes the live task-manager's memory across the boundary; merge outside a run, watch the first one.

## 6. Questions (settled by the task-manager, 29.10.)

1. **Non-task-manager sessions (S2):** the detector now (an alert when a review/request session passes the D-218 test), S2 itself on its first hit (task-manager call).
2. **D-191/D-218's "compact at the start by age":** already superseded: D-238's owner clarification of 14.10. states it ("Supersedes D-191/D-218's 'at the run start by age'"); D-218's algorithmic age test lives on only as S2's eligibility test. No owner question.
3. **"Not after just questions" (D-238):** the D-305 review (01.11.) also counts how often an idle compaction followed only owner questions (task-manager call: data for that review, no change before it).
