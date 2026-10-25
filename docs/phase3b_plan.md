# Phase 3b: task-manager unification and run-now (implementation plan)

Plan for design §5.2–§5.4 / §13 step 3b. AFClaude's own task-manager becomes the task-manager of the AFClaude project like any other project's; the keep-alive continuation (one hard-wired session id) goes away; tmux sessions get readable names; owners (and later managers) can run things now. No code changes yet; each step in §5 is one worker branch.

Binding inputs: D-147 (no special keep-alive path; readable tmux names), D-189 (one task-manager per project, no global one; choosing the project is algorithmic), D-205 (runs begin only at starts; the start picks the project by rank and continues its task-manager; the 10-min pass starts nothing), D-153 (run-now skips window and budget, starts at once) over D-036 (its "budget still applies" reading is superseded), D-184/D-190 (managers may force a run; editors may not), D-186 (owner/manager messages wake the task-manager), D-064 (project rank, stage priority), D-204 (no continue after a limit), D-206 (approved stalls at their reset), D-212 (fill-up), D-224 (run end), D-225 (standing rules by hook), D-238 (compaction at run end / idle), D-244 (per-project pause), D-260 (per-project model), D-269 (Remote Control per category), D-274/D-275 (late start, chain into the next window), D-277 (window ping), D-235/D-257 (fail-stop; runner downtime alerts and pauses), D-256 (no audit row per runner run), D-281 (a chained start keeps its project over strict rank; the live tmux session is renamed once after S2; every project is its task-manager session, with no unmanaged state — created automatically at project add or at its first start), D-282 (a failed run-now alerts and pauses automation for now; a pre-release step removes the pauses so failures only alert, D-239/D-280 excepted), D-301 (a project's name and description are both editable in the UI; the name is fed to the task-manager every time, the description is the one-off kickstart, and the task-manager is told when the description changes).

## 0. Already decided

- **The selection belongs to the start, not to the 10-min pass** (D-205, which came after D-147). The §13 wording "in the dispatcher" means "an ordinary project, chosen algorithmically" (D-189). `dispatcher.start_project_seam()` already marks the spot.
- **One run per start.** A session-window start, a postponed start and a last-stretch slot each continue one task-manager. A fill-up continues the session of the run it fills (D-212). Run-now is the only way to have a second session working at the same time.
- **The runner owns execution.** 3b builds on 3a's runner (`runner_runs` keyed runs, the wake socket, plan/act split). It adds no second loop and no new process.
- **Nothing is renamed under a live session by surprise.** Names are looked up in `driven_sessions`, never computed from the session id.

## 1. Today (as-is inventory)

| what | where | how it finds "the" task-manager |
|---|---|---|
| session-window start (gate → D-274 timing → `handle_fire` → `_fire_start` → `fire` → `ka_resume.sh`) | `keepalive.window_start_pass` (cron */30; in the runner from 3a S5) | `--session $AFCLAUDE_MANAGER_SESSION` |
| postponed / planned start, last-stretch slots, fill-up, run readings, limit detection, compaction | `keepalive.run()` watcher loop (runner jobs from 3a S6) | the watcher's `--session` (`start_watcher.sh`, env) |
| start dedup and history | `runner_state` `keepalive` (`handled`, `fires`, `run_stops`, `compactions`), deferrals and the fill-up plan | keys per start, not per project; the records carry `session` |
| tmux name | `keepalive.tmux_name()` = `ka-<sid8>`; `ka_resume.sh` `T="ka-${SID:0:8}"`; `host.py` `TMUX_NAME`/`TMUX_PANE`; `docker/host_exec.py` whitelist (`tmux-has/-panes/-kill/-clients/-capture ka-<8 hex>`); `dispatcher.TMUX_RE`; `usage_review` | computed from the id |
| identity setting | `afclaude_config.manager_session()` (machine identity, no code default any more: raises `ConfigError` if unset, D-235), env `AFCLAUDE_MANAGER_SESSION` → `DISPATCHER_MANAGER_SESSION`, `QUICKVIEW_SELF_SESSION`; `runner_jobs` `{session}` | one id |
| also read by | `actions.autonomous_sessions`, `actions.inbox_post` (default target), `run_metrics`, `compact_timing fit`, `export_quickview`, `keepalive --run-stopped` (fallback), the dispatcher's exclusions (`keepalive_sessions` + `keepalive_targets()` from `/proc` / the host's process list) | the setting |
| the project link | `projects.manager_session` (AFClaude, rank 1); `manager_launch.for_session` (D-260); `failstop` per-project pause by session | the DB: already one row per project |
| "now" | `keepalive --window-start --now` (budget still applies), `--work-on N` (a NEW session per `BACKLOG.md` item, budget applies), `usage_review --only` (its quota gate applies) | — |
| requests | `action_requests` (`continue_now`, `review_now`; `request.add`, owner-only) | **nothing consumes them** |
| `driven_sessions` | one row (`own`); no project column; `kind` free text | — |

Gaps against the decisions: `--work-on` contradicts D-189/D-205 (no separate task sessions) and D-153 (it gates on budget), as does `--now`; the setting's legacy default is a silent fallback (D-235); a project other than AFClaude cannot get a run (D-205 addendum) — resolved by D-281: every project is its task-manager session.

## 2. What changes

- **Task-manager registry = `projects.manager_session`.** `store.task_managers(conn)` → every project in rank order with session, path, launch and pause (D-281: no unmanaged projects, so no "managed only" filter). Every per-session job (watch, fill-up, run readings, compaction) runs over this list; the start picks one of them. The setting `manager_session`, the env `AFCLAUDE_MANAGER_SESSION` (and its two aliases), `runner_jobs` `{session}`, the dispatcher's `keepalive_sessions`/`keepalive_targets()` and `--session` on the start paths go away (legacy mode keeps them until 3a S9).
- **Project selection at a start** (`dispatcher.start_project_seam` → `store.pick_project(conn, now)`), after the gate and the timing passed:
  1. candidates: every project, not paused (`failstop.paused(session=…)`), whose session is not at a limit (D-204) and is not held by the owner (D-239.1); a candidate with no `manager_session` yet gets one created as part of this start (D-281: automatic, at the project's first start) — a new session in its path (`--new`, `project_start.md`), `projects.manager_session` set through `project.update` (actor `runner:request` or the firing component);
  2. those with ≥ 1 ready task first, in project rank order (D-064; the stage priority decides inside the project, by its task-manager);
  3. a chained start (D-274: the run before is still going) continues **that** run's project instead of switching by strict rank (D-281, refining D-064).
  - No candidate: the start is a `HOLD: no project to run` decision (`run_log`), not an error.
  - The decision is recorded on the start's `handled` entry (`project`, `session`, `why`) and in `run_log`.
- **Prompts per project.** `session_message(…, afclaude=…)` gets the project: AFClaude's sessions keep the hook pointer (D-225, `rules_hooked(cwd)`); another project's task-manager gets the rules inline without `manager_afclaude.md` (its cwd has no hook). The SessionStart hook itself is unchanged.
- **The project's name and description (D-301).** The name is current-state context the task-manager should always have, so `session_message` carries the project's current name on every continue, not only at the kickstart (cheap: one more field alongside the project it already resolves for §2's selection). The description is the one-off kickstart: `project_start.md`'s `{desc}` (already built, prompts/README.md) is sent once, at the project's first start, and never repeated on an ordinary continue. When the description changes after that first start (7a's `project.edit`, D-301's "the manager should still get a description update"), the task-manager needs telling once, not a resend of the whole kickstart text: this is a small gap against today's `project_start.md`-only mechanism, flagged for whichever step adds a description-changed note (a continue-bundle insert slot, 4a §3.2, is the natural fit; `project.edit` itself is 7a's, outside 3b's build). Not resolved here: 7a's `project.edit` row (phase7_plan.md §2.1) does not yet describe this notification; flagged in the report, not silently added to that table.
- **Readable tmux names** (§5.3): `afc-<project-slug>-task-manager`, `afc-review`, `afc-stall-<title-slug>-<id4>`; `[a-z0-9-]`, ≤ 40 chars, a suffix only on collision; the same string is the RC `--name`. `driven_sessions` gets `project_id` (additive column) and the kinds `task-manager`, `review`, `stall`, `request`. `keepalive.tmux_name(sid)` reads the row; no row is an error (D-235), never a computed fallback.
- **Run-now** (§5.4): `action_requests` gets the kind `work_on_now` (target = project) and the states `open → started | refused | failed` (result JSON: tmux name or reason). The runner consumes them.
  - `request.add` (owner-only until 5a's role matrix adds managers for their project, D-184/D-190) writes the row and wakes the runner.
  - The runner runs each open request as a keyed run `request:<id>` (`on_interrupt: fail`, D-257).
  - `work_on_now`: the project's task-manager is continued now with the `continue` prompt (+ held inbox and notes, like a start). No window, budget, session stop or D-274 timing (D-153). The preflight, `session_lock`, the owner check and the limit (D-204: refused "at its limit until …") still apply. A run already going: refused "running". A project with no task-manager yet counts this as its first start (D-281): a new session in its path (`--new`, `project_start.md`), `projects.manager_session` set through `project.update` (actor `runner:request`) — the same path §2's candidate-list bullet uses for an automatic start, not a special case kept only for `work_on_now`.
  - `continue_now`: one stalled session through the dispatcher's continue path (`Pass` for one target, the D-206 preflight), no decision row needed; still at its limit: refused with the reset time.
  - `review_now` (owner-only): `usage_review` one-off without its quota gate.
  - Run-now runs while automation is paused (§5.4, like D-186's messages, D-282). Its own failures alert **and pause** (D-282, like every other failure for now); a pre-release step (design §13) turns this into alert-only so work continues.
  - A started run-now is an ordinary run: run end (D-224), fill-up eligibility (D-212), compaction (D-238) apply.
- **Retired:** `keepalive.py --work-on`, `load_backlog`, `--window-start --now`, `--session` on the start paths, `start_watcher.sh`'s session argument, the `manager_session` setting and its legacy default, `dispatcher.keepalive_targets`/`keepalive_sessions`, the `ka-` regexes once no live session uses one. `keepalive.py` keeps the reusable parts (transcript parsing, preflight, budget, fire), §5.2.

**Not part of 3b:** the compaction redesign (3c; 3b only iterates the existing pass over the task-managers), roles beyond owner (5a), the dashboard buttons and request polling (7b), the D-277 ping (it is per account, not per project, and stays as built).

## 3. Design details

### 3.1 Tables (additive, `CREATE … IF NOT EXISTS` / `COLUMNS`, no schema-version bump)
- `driven_sessions.project_id` (INTEGER, FK projects). Writes through `actions.py`: a new `driven.set` (validate kind and name charset, unique live tmux name) with audit; `own_session_add` stays.
- `action_requests`: `target` also a project name/id for `work_on_now`; states extended in code (`started`, `refused`); new columns `run_id` (the `runner_runs` row) and `updated_at`. Actions `request.start`, `request.finish` (actor `runner:request`, audited: this is what the requester sees, unlike the run row itself, D-256).
- No new table.

### 3.2 Names
- `names.py` (pure): `slug()`, `tmux_name(kind, project=None, title=None, sid=None, taken=())`. Tested on its own.
- `ka_resume.sh --tmux NAME` (required from S2; the `--name` RC display name defaults to it). `run/<name>.sh` follows the name.
- `docker/host_exec.py`: the tmux checks accept `^(ka-[0-9a-f]{8}|afc-[a-z0-9-]{1,36})$`, `ka_resume.sh` gets `--tmux` with the same check. The forced command points at the repo file, so the whitelist changes with the merge; `install_host_bridge.sh` needs no rerun (S2 verifies with its self-test).
- `host.py` `TMUX_NAME`/`TMUX_PANE` the same; `dispatcher` cleanup compares with its stored names only (drop `TMUX_RE`).

### 3.3 The live AFClaude session (no lost runs)
- **Backfill (S1):** a `driven_sessions` row for the AFClaude task-manager with its **current** name `ka-<sid8>`, `kind task-manager`, `project_id` AFClaude; one for each live review or stall session the dispatcher tracks. A check (`tasks.py driven check`) compares the rows with the live tmux list and fails on a mismatch.
- **The name change:** the live tmux session keeps `ka-<sid8>` (it lives for weeks: every start types into it). Once S2 is deployed, the task-manager renames it once at a quiet moment (no run, no compaction, no client attached): a whitelisted `tmux-rename ka-<8 hex> afc-…` that also updates the row in one action (D-281: rename once after S2). The RC display name changes at the next fresh launch.
- **The switch from the setting to the registry (S3)** is a no-op for the data: `projects.manager_session` of AFClaude already equals the setting. S3 adds a boot check: setting (while it exists) == AFClaude's `manager_session`, else fail-stop.
- **Drain first:** every 3b deploy uses `runner.py drain` and is not done within 30 min of a session-window start or while a run or compaction is going (as in 3a §4).

### 3.4 Rollback
- Each step: revert its commit and redeploy (no data migration to undo; the added columns stay unused).
- After the rename: rename back (`tmux-rename afc-… ka-<sid8>` + row) **before** reverting S2, else the old code finds no tmux session, plans a resume, and the preflight refuses on the live holder: every start would be skipped.
- 3a's `legacy` mode keeps working through S3–S5: it runs the same code with `--session`, which stays accepted (and must match the registry) until 3a S9.

## 4. Order against 3a

| 3b step | needs | runs alongside |
|---|---|---|
| S1 data + backfill | 3a S1 (merged) | 3a S2–S4 (touches only `store.py`/`actions.py` additively) |
| S2 readable names | S1 | 3a S4/S5; **before 3a S6** (S6 rebases on the small `tmux_name` change, not the other way) |
| S3 registry | 3a S6 merged (the passes are runner jobs with `{session}`) | 3a S7 |
| S4 project selection | S3, 3a S5 + S6 | 3a S7/S8 |
| S5 run-now | S3, 3a S4 (keyed child runs with `on_interrupt`) | S4 |
| S6 retire | S4, S5, 3a S9 (legacy mode gone) | 3a S9 |

## 5. Steps

**S1: data model and backfill.**
- **Build:** `driven_sessions.project_id`; `driven.set` and `request.start`/`request.finish` in `actions.py`; `store.task_managers()` (read-only, rank order, every project — D-281: no unmanaged state); `action_requests` columns and the `work_on_now` kind in `request.add`; `tasks.py driven list|check`; the backfill (`store.py import-driven`, idempotent, checks rows against tmux and the projects table); the shared "create this project's task-manager session" helper (new session in its path, `--new`, `project_start.md`; `projects.manager_session` set through `project.update`) that S4's first start and S5's `work_on_now` both call, so `project.edit`'s `manager_session` field keeps only its "force a different session" meaning, not "None = unmanaged" (D-281).
- **Tests:** actions (validation, audit, unique live name, owner-only request kinds), the backfill twice (no change the second time), `task_managers` ordering and pauses; `test_typing` (`store.py`/`actions.py` stay strict).
- **Done when:** suites, `test_typing` and `check_public` green; on the live DB `tasks.py driven check` shows the AFClaude row with `ka-<sid8>` and no mismatch.
- **TM verifies:** the backfill's row count, `driven check` clean, nothing reads the new columns yet.
- **Risk:** low; additive only.

**S2: readable tmux names.**
- **Build:** `names.py`; `ka_resume.sh --tmux`; `host_exec.py`/`host.py` regexes and `tmux-rename`; `keepalive.tmux_name` from `driven_sessions`; every launcher (`keepalive.fire`, `usage_review`, the dispatcher's continues) chooses the name and writes the row before `ka_resume.sh` runs; dispatcher cleanup by stored names.
- **Tests:** `names` (charset, length, collision suffix, slug of non-ASCII titles); `host_exec` whitelist (old and new names allowed, `afc-` with `;`/space/uppercase/41 chars denied); `test_keepalive`, `test_dispatcher`, `test_usage_review` with stub-PATH tmux showing the new names; a session without a row is an error, not a `ka-` guess.
- **Done when:** a throwaway Haiku session started through `ka_resume.sh --tmux afc-test-…` (the `test_compact_live.sh` pattern, never `test_e2e_tmux.sh`) is found, captured and killed through the bridge; the next real start continues the AFClaude task-manager under its row's name.
- **TM verifies:** `install_host_bridge.sh`'s self-test passes against the merged `host_exec.py`; then the one-time rename (§3.3) at a quiet moment, `driven check` clean, the next start's `FIRED … sent-keys afc-afclaude-task-manager`.
- **Risk:** the bridge whitelist changes live with the merge; a regex slip blocks every tmux call (every start fails and pauses). Merge outside a run, run the self-test at once, revert on failure.

**S3: the registry replaces the session setting.**
- **Build:** runner jobs (`watch`, `fillup`, `run_readings`, `compaction`, the start jobs) take their sessions from `store.task_managers()` instead of `{session}`; a project whose `manager_session` is still unset (not yet started once, D-281) is skipped by these per-session jobs, not treated as an error; the per-session plan steps return one `next_due` per session (key `<job>:<sid8>`); `actions.autonomous_sessions`, `inbox.post` (target = a project, its task-manager), `run_metrics`, `compact_timing fit`, `export_quickview`, `--run-stopped` (its own session, else error) read the registry; the dispatcher's exclusions become "every task-manager and driven session"; the boot check of §3.3.
- **Tests:** two task-managers in a temp DB: each gets its own watch/fill-up/compaction plan; a pause of one project holds only its jobs (D-244); a limit hit of one doesn't touch the other (D-204); `legacy` with `--session` still runs and fails when it disagrees with the registry.
- **Done when:** one night with the start, a compaction and a fill-up (if planned) of the AFClaude task-manager behaving as before, with the setting no longer read by the runner path.
- **TM verifies:** `runner.py status` shows the per-session keys; the quickview still shows the AFClaude run; the dispatcher's skip list names the task-manager.
- **Risk:** a second task-manager doubling the watch cost; measure the `watch` run time with two rows in the shadow DB.

**S4: project selection at the start.**
- **Build:** `store.pick_project` (§2) called by the session-window start, the postponed start and the last-stretch slot after the gate and timing; a candidate with no `manager_session` yet gets S1's shared creation helper called in its place (D-281: automatic, at its first start, not only via an explicit `work_on_now`); the chosen project goes on the `handled` entry and `run_log`; the start's prompt per project (§2); D-274's `run_chain.md` only when the next start continues the same project; a chained start keeps that project over strict rank (D-281, refining D-064).
- **Tests:** rank order, a paused / limited / owner-held project skipped, no ready tasks → next project, none → `HOLD`, chained start keeps its project (D-281) even when a higher-ranked project now has ready tasks, last stretch the same, a project with no task-manager yet gets one created at this start, dedup: one start key fires one project even when the runner reruns the plan step.
- **Done when:** with only AFClaude managed, two nights' decisions equal S3's; a dry-run (`--arm` off in `testenv.py`) with a second test project ranked first and given a ready task picks it and would continue its session.
- **TM verifies:** the `run_log` line per start names the project and why; nothing else started.
- **Risk:** a project's task-manager in another repo has no SessionStart hook; the inline rules must fit one tmux line (the existing `session_message` path, checked by a length test).

**S5: run-now.**
- **Build:** the runner job `request:<id>` (wake-driven, plus the 60-s backstop), `on_interrupt: fail`; handlers for `work_on_now`, `continue_now`, `review_now` (§2); `tasks.py run-now work|continue|review` and MCP `afclaude_request` writing through `request.add`; the quickview lists open/started/refused requests. `keepalive --work-on` and `--window-start --now` print a pointer to `tasks.py run-now` and exit non-zero.
- **Tests:** each kind: started (tmux name in the result), refused (running, at its limit, owner holds it, unknown target), failed (no reply: alert **and pause**, D-282 — plain assertions today, re-run once the pre-release step of design §13 lands to confirm it flips to alert-only); skips window and budget but not the preflight; runs while paused (D-282); a restart during a request fails it (D-257); one open request per (kind, target); `work_on_now` against a project with no task-manager yet creates one and sets `manager_session` through `project.update` (D-281, via S1's shared helper — unconditional, not a special case).
- **Done when:** `tasks.py run-now work AFClaude` outside the window, with the gate on HOLD, continues the task-manager within seconds; `run-now review` starts the review without the quota wait.
- **TM verifies:** the request row's states and audit rows, the run's end (D-224) and the next start unaffected (dedup keys untouched).
- **Risk:** run-now spends budget outside the plan; the CLI prints the current session % and the weekly headroom before it queues.

**S6: retire the keep-alive continuation** (with 3a S9).
- **Build:** remove what §2 lists as retired; `ka-` regexes go once `driven check` shows no live `ka-` name; README file table, design §5.2–§5.4 "as built", D-036 marked superseded by D-153 in the decision store (task-manager, through `tasks.py decision`).
- **Done when:** `grep` finds no `manager_session()` / `AFCLAUDE_MANAGER_SESSION` / `ka-[0-9a-f]` outside tests; 7 calm days.
- **Risk:** a forgotten reader of the env (compose); `docker compose config` must succeed without it.

## 6. Owner's answers (D-281, D-282)

The plan's four open questions are answered; §2–§5 above already carry the consequences inline. Recorded here for reference:

1. **Which project a start picks when a run chains (D-274 vs D-064).** Answered by D-281: a chained start continues the running project instead of switching by strict rank. Strict rank among projects with ready tasks still applies to every non-chained start.
2. **Projects without a task-manager (D-189, D-205 addendum).** Answered by D-281: this question is void. There is no such project — every project *is* its Claude task-manager session, no exceptions. The session is created automatically when the project is added or (if not already created then) at its first start; it is not gated behind an explicit `work_on_now`. S1 builds one shared creation helper for this; S4's start and S5's `work_on_now` both call it, so run-now's "project with no task-manager" case is the ordinary first-start path, not a special case.
3. **Run-now while automation is paused by a failure (D-235/D-257).** Answered by D-282: yes for now, like D-186's messages — a failed run-now alerts and pauses like any other failure (the request shows the pause reason, its own failure alerts and pauses again). This is a development-phase measure to surface errors and edge cases, not a permanent design: design §13 gets a pre-release gate step that removes every fail-stop pause site (alert-only from then on, work continues), except the named exceptions D-239 and D-280, which stay as they are.
4. **Renaming the live task-manager tmux session** (`ka-f…` → `afc-afclaude-task-manager`). Answered by D-281: rename once, at a quiet moment after 3b S2 is deployed (no run, no compaction, no client attached), as §3.3 already describes.
