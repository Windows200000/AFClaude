# Phase 3a: the resident runner (implementation plan)

Plan for design §5.1 / §13 step 3a: one resident runner (`runner.py`) replaces supercronic, the at-shim's `--run-due`, the keep-alive watcher loop and the inbox worker thread. No code changes yet; each step in §5 is one worker branch.

Binding inputs: D-153/D-173 (one resident runner; run-now later, in 3b), D-154 (one write path with audit), D-161 (everything in the DB), D-202–D-206 (starts only at session-window starts; the 10-min pass only cleans up; approved stalls at their reset), D-212 (fill-up), D-224 (run end), D-228/D-251 (review chain), D-234/D-235/D-239 (fail-stop), D-238 (compaction timing), D-241/D-244/D-252 (inbox, per-project pause).

## 0. Already decided

- **Build, don't adopt.** A small stdlib `asyncio` loop. Checked and rejected on 13.10.: APScheduler 3 (its own job-store schema outside `actions.py`, no window semantics), APScheduler 4 (alpha), Huey (a task queue with its own tables), rq-scheduler/Celery beat (need Redis or a broker), Dagster/Prefect (far too big), supercronic/systemd (what is being replaced).
- **`schedule.py` owns the windows.** The runner asks it for start times and never computes window times itself.
- **Must be tested:** catching up missed ticks, handler timeouts, coalescing wakes.
- **15.10. lesson:** a process that waits inside the container dies silently on every deploy. **The rule here: no waiting inside a process.** A wait is stored in the DB as state plus a due time, and the next run of the job checks it.
- **`review-failfast`** (in progress) moves the D-228 chain into `usage_review` state (`st["chain"]`). An at-job `--chain-step` advances it every 5 min; the hourly run is the backstop that re-queues it; a file lock makes sure only one pass runs at a time; there is a quota gate and early death detection. 3a only changes what triggers it (§5, S3/S4). It does not touch its logic.

## 1. Today

| what | trigger | process | survives a restart? |
|---|---|---|---|
| usage sampler | cron */15 | one-shot | yes (next tick) |
| window start | cron */30, acts if `schedule.due_session_start` | one-shot, up to ~75 min (waits for a compaction lock up to 40 min, then save, `/compact`, continue) | **no**: killed mid-sequence, nothing records it |
| watcher watchdog | cron :07/:22/:37/:52 + entrypoint `start_watcher.sh --boot` | — | — |
| keep-alive watcher (`keepalive.run`) | 30-s loop: stall/limit detection, last-stretch slots, postponed starts, fill-up (60 s), run readings (5 min), compaction (60 s) | long-lived; next due times kept **in memory** | partly: dedup state is in the DB, but a start, fill-up or compaction cut off mid-way is lost silently |
| usage review | cron hourly | one-shot (the 6-h chain waiter is removed by `review-failfast`) | yes after `review-failfast` |
| quickview export | cron */3 | one-shot | yes |
| dispatcher (10-min pass + `stalled.scan`) | cron */10 | one-shot | yes |
| at-jobs (`scheduled_jobs`) | cron */1 `at_shim --run-due` | **claimed, then detached**; the result is never recorded | **no**: a claimed job killed by a restart is lost, with no error |
| docs import | cron */10 | one-shot | yes |
| inbox API + worker | entrypoint + cron */5 `inbox_api ensure` | long-lived HTTP server; a worker thread (30 s + wake); **a verify thread per delivered message (up to 10 min)** | **no**: a restart during verify leaves the message `delivered`, never checked and never failed |
| log retention | cron 03:17 | one-shot | yes |

The rows in bold are the same kind of failure as on 15.10. 3a fixes all of them.

## 2. What moves, what stays

**Moves into the runner by the end of 3a:** every crontab line, the at-job execution, the watcher's passes, the window starts, the inbox worker and its verify step, and supervision of the inbox API process.

**Stays outside:**
- `inbox_api.py serve` stays its own process: the HTTP endpoint behind the quickview proxy, and later part of `afclaude-web` (phase 6). It only stores a message and wakes the runner. The runner starts and supervises it, replacing the */5 cron watchdog.
- The CLIs keep working unchanged: `tasks.py`, `keepalive.py --run-stopped / --decide / --work-on / --plan-fillup-test`, `at -t` (scheduling only, plus a wake), `store.py import-prompts` (the entrypoint runs it before the runner starts), the MCP server and the SessionStart hook.
- **Not part of 3a:**
  - run-now / `action_requests`, the task-manager unification and retiring the keep-alive continuation: 3b;
  - the compaction redesign: 3c (the runner runs today's `compaction_pass` as it is);
  - Litestream, backups and host checks: 5b/5c.

## 3. Runner design

### 3.1 Process and loop
- `python3 -u runner.py serve`: one asyncio loop in the main thread.
- **Single instance:**
  - an `flock` on `data/run/runner.lock`;
  - at boot it refuses to start (fail-stop) while a keep-alive watcher runs in the container or on the host (the host check from `start_watcher.sh`);
  - each process gets a `boot_id` (uuid).
- **Loop:**
  1. read the due times;
  2. `await` whichever comes first: the earliest due time, a wake, or a child exiting;
  3. start the due runs;
  4. repeat.
  - There is no fixed poll. A 60-s maximum sleep is the backstop for a lost wake.
- **Handlers run as child processes:** `python3 runner_jobs.py <job> [--key K]` via `asyncio.create_subprocess_exec`, each in its own process group. A hang or crash can't wedge the loop, and a timeout can kill the child.
  - The child calls the existing function and prints one JSON result line: `{status, next_due, note}`.
  - **Exit codes:**

    | exit | meaning |
    |---|---|
    | 0 | ok |
    | 3 | failed, and the handler already reported it (`failstop.fail` inside) |
    | anything else (crash, signal, timeout) | the runner reports it |

    So there is never a second alert and never a silent failure.
- **At most one run per job at a time.** A job that is still running when it comes due again is not started twice. The overrun is logged once; it is not an error until its timeout.
- The jobs that type into the task-manager session (start, fill-up, compaction, inbox delivery) keep `keepalive.session_lock` as their guard. It works across processes, so it also covers a legacy rollback.

### 3.2 Jobs

| job | trigger | timeout (setting `runner_timeout_<job>`) | while paused | on interrupt | calls |
|---|---|---|---|---|---|
| `stall_scan` | 3 min (new: its own tick, §5.1) | 2 min | runs | rerun | `stalled.scan` |
| `sampler` | 15 min | 20 min (S3) | runs (D-239.4) | rerun | `usage_sampler.main` |
| `dispatcher` | 10 min | 25 min (S3) | the handler checks | rerun | `dispatcher.main(--arm --once)` |
| `quickview` | 3 min | 2 min | runs; a failure alerts but never pauses (D-239.3) | rerun | `export_quickview` |
| `docs_import` | 10 min | 5 min | runs | rerun | `project_docs import --cron` |
| `prune_logs` | daily 03:17 UTC | 10 min | runs | rerun | `store.py prune-logs` |
| `usage_review` | hourly | 30 min (S3) | the handler checks | rerun | `usage_review.main` (the backstop for the chain) |
| `at:<id>` | `scheduled_jobs.run_at` | 30 min or payload `timeout` | the job checks | payload `on_interrupt` (default `fail`; chain steps and samples `rerun`) | the job's command |
| `window_start:<start_key>` | each `schedule.next_session_start`, exact | sum of its inner waits + 5 min | the handler checks | **fail** | `keepalive.window_start_pass` |
| `deferred_start:<key>`, `last_stretch:<key>`, `fillup:<key>` | `next_due` from the plan step | as for `window_start` | the handler checks | **fail** | the keep-alive passes' act step (S6) |
| `watch` | 60 s | 2 min | runs (detection only) | rerun | `stall_status` + `note_limit_hit` + `watch_run_usage` + the plan steps (next postponed recheck, slot start, fill-up time) |
| `compaction` (check) / `compaction:<key>` (act) | 60 s / when the check says act | 1 min / inner waits + 5 min | the handler checks | rerun / **fail** | `compaction_pass`, split |
| `inbox` | a wake from the inbox API, plus 30 s while any message is pending or held | 5 min | runs (owner messages are not automation, D-186/D-244) | rerun | `inbox_worker.process` |
| `inbox_verify` | 5 s while any verification is pending | 30 s | runs | rerun (its state is in the DB) | one verify check per pending message |
| `inbox_api` | a supervised child | — | runs | restart + alert | `inbox_api.py serve` |

- **Plan and act are separate runs.** A tick only reads and plans, so rerunning it is safe. Typing into a session is always a keyed run, whose interruption is a failure. S6 splits the watcher's passes this way.
- **Dedup keys:** the keyed runs use the existing keys: `schedule.start_key`, the deferral key, `last_mile_key`, and the fill-up's window key. The runner and a legacy rollback therefore share one dedup (`keepalive` state `handled`).
- **Outer timeout = the sum of the handler's inner timeouts + a margin**, computed from the `keepalive` constants. A test asserts it, so an inner timeout (with its own clear error) always fires first.
- **Due times:**
  - The parent computes window starts itself (`schedule.py` is pure; the settings come from the DB).
  - Every other exact time (a postponed recheck, a last-stretch slot, a planned fill-up, the usage refresh 2 min before it) comes back as `next_due` in the handler's result and is stored. A restart therefore wakes at the planned time.

### 3.3 Tables (additive: `RUNNER_SCHEMA`, `CREATE … IF NOT EXISTS`, no schema-version bump)

- **`runner_jobs`**, the "job table" of §13, one row per job: `name` PK, `next_due`, `last_run_id`, `last_status`, `last_ok_at`, `consecutive_failures`, `heartbeat_at`, `updated_at`, `version` (bumped by a trigger).
- **`runner_runs`**, one row per run:
  - columns: `id`, `boot_id`, `job`, `key` (NULL for a tick), `status` (`running|ok|failed|timeout|interrupted|skipped`), `started_at`, `finished_at`, `pid`, `timeout_s`, `result` JSON, `error` (stderr tail ≤ 2 kB), `actor` (`runner:<job>`), `via`.
  - `UNIQUE(job, key) WHERE key IS NOT NULL`: a keyed run can't start twice, also across a restart.
  - A trigger lets `status` leave `running` only once.
  - Retention: a setting `runner_runs_retention_days` (30), with deletes only through a prune gate, the way `app_log` does it.
- **Writes** go through `actions.py`: `runner_job_set`, `run_begin`, `run_end` (with `_check_writer`, `store.retrying` and the §7.8 DB-error path). Audit volume: see question 1.
- **Pending work stays in the handlers' own `runner_state` components**, as `review-failfast` does with `st["chain"]`. Inbox verifications become `runner_state` "inbox" entries `{msg, path, offset, deadline}`. The runner itself stores only due times and run records.

### 3.4 Restart recovery
At boot, after taking the lock:
1. **`running` rows from another `boot_id`** become `interrupted`, then follow the job's policy (table above):
   - `rerun`: the job is due now;
   - `fail`: `failstop.fail("runner", "<job> <key> interrupted by a runner restart", …)`. That is an alert and a pause (D-235), and the run is never rerun: a start or compaction cut off mid-way leaves the session in an unknown state.
2. **The previous boot ended without a clean exit** (`runner_state` `runner.clean_exit` is false): an error (question 2).
3. **Missed timed runs:**
   - The last session start is within the catch-up grace (setting `runner_start_catchup`, default 30 min = today's `CRON_STEP` tolerance) and not yet handled: it runs now.
   - It is older than that: an error, "missed start" (question 2).
   - Overdue at-jobs run now (as today).
   - Each interval job runs once, not once per missed tick, staggered by a few seconds.
4. **SIGTERM (`docker stop`):**
   - no new runs start;
   - running runs get up to `stop_grace_period` (compose: 90 s);
   - the runner records `clean_exit` and exits.
   - Children still alive at the kill show up as interrupted at the next boot.
5. **Deploys use `runner.py drain` first:**
   - no new runs that act on a session start;
   - it waits until none is running (bounded by the longest timeout);
   - then the deploy runs.
   - The manager prompt's deploy steps are updated so that a deploy never kills a start.

### 3.5 Timeouts (D-234)
- **On a timeout:** the child's process group gets SIGTERM, then SIGKILL 10 s later; the run is marked `timeout`; `failstop.fail` pauses and alerts.
- All timeouts are settings in the registry, with the defaults in the table above.
- No timeout is retried silently. A deliberate retry exists only where one exists today (D-239.6).

### 3.6 Fail-stop and pause
- **Before spawning a job that pauses**, the runner checks `failstop.paused()` (install-wide). A DB that can't be read counts as paused, as today.
  - A skipped run is recorded as `skipped`, with the reason logged once per reason.
  - The jobs that run while paused are listed in the table (D-239).
- **Per-project pause (D-244):** the handlers keep their checks (`automation_paused(what, sid)` resolves the session's project). In 3a the runner does not duplicate the project mapping.
- **The runner's own failures** (a run row can't be written, the socket can't be bound, the loop crashes): `failstop.fail("runner", …)`, then a non-zero exit, then `restart: unless-stopped`. The next boot sees the unclean exit.
- **Liveness:**
  - A watchdog thread checks the loop's heartbeat. If the loop is stuck for more than 5 min, it logs and calls `os._exit(70)`, which restarts the container.
  - Docker's health check by itself doesn't restart a container, so §5.1's "the container restarts on a stale heartbeat" needs this thread.
  - The heartbeat is written to `runner_jobs` at most every 30 s.

### 3.7 Wake socket
- `data/run/runner.sock`, `SOCK_DGRAM`. Any datagram means "re-read the DB".
- **Senders in 3a:**
  - `at -t` after it stores a job;
  - `inbox_api` after it stores a message;
  - `tasks.py automation resume`;
  - `window.set` and settings writes, after their commit.
- **Sending never blocks or fails a write.** If the runner is down, the sender logs a warning line; the stale heartbeat is what raises the alert. The DB is the truth, and the next tick catches a lost wake.
- **Coalescing:** one `asyncio.Event`. N wakes while the loop is busy cause one re-scan.

### 3.8 Logging and status
- **Runner lines** go to component `runner` (`data/runner.log` + `app_log` via `runner_state.tee`, plus stdout for `docker logs`):
  - the boot;
  - each run's start and end (job, key, duration, status);
  - skips, once per reason.
- **Handlers keep their own logs** (`keepalive.log`, `dispatcher.log`, …).
- **A failed or timed-out run** keeps its stderr tail in `runner_runs.error`.
- **`runner.py status`** shows the jobs, next due times, last status, the running runs and the heartbeat age.
- **The quickview's "cron" section** reads `runner_jobs`, plus the crontab lines still left while the cut-over runs.

## 4. Cut-over

- **One owner per job per image.** A test reads `docker/crontab` and the runner's `RUNNER_OWNS` set and fails if they overlap.
  - Each step moves jobs by removing their cron lines and adding them to `RUNNER_OWNS` in one commit, deployed in one go.
  - The deploy restarts the container, which stops the old owner (supercronic and the watcher are processes in that container). The runner's boot catch-up covers the restart gap.
- **Double-start guards that don't depend on ownership:**
  - the existing dedup (`handled` keys, `scheduled_jobs` claims, the flocks, `session_lock`);
  - `runner_runs` `UNIQUE(job, key)`;
  - the runner's single-instance lock and its watcher check.
- **No gap:**
  - boot catch-up (§3.4);
  - `runner.py drain` before each deploy;
  - no cut-over deploy within 30 min of a session-window start, or while a run is going.
- **`AFCLAUDE_SCHEDULER` modes:**

  | mode | what runs |
  |---|---|
  | `off` | nothing (unchanged) |
  | `on` (S2–S7) | supercronic with `docker/crontab` + the runner for `RUNNER_OWNS`; since S8 an alias of `runner` |
  | `runner` (S8) | the runner only (`exec`ed by the entrypoint, no supercronic) |
  | `legacy` | supercronic with `docker/crontab.legacy` (today's crontab, frozen at S2) + the watcher + the inbox `ensure`; no runner |
- **Rollback:**
  - Set `legacy` in `docker/.env` and run `docker compose up -d` (recreates the container, no rebuild).
  - The dedup state is shared, so nothing starts twice across the switch.
  - A pause set by an interrupted run stays until the owner clears it.
  - Inside one step: revert the step's commit and redeploy.
  - `legacy` is removed only after 7 days of `runner` mode with no runner incident (S9).

## 5. Steps

**S0, prerequisite:** `review-failfast` merged and deployed. 3a builds on its stored chain and only changes how it is triggered.

**S1: runner core, not started live.**
- **Build:**
  - `runner.py` and `runner_jobs.py`;
  - the tables, the `actions.py` writes, the settings;
  - the loop, child processes, timeouts, wake, lock, recovery, `drain` and `status`;
  - both files on the `mypy --strict` list.
- **Tests:**
  - a fake clock and fake spawner;
  - real children (`sleep` past the timeout: killed and recorded; a crash; exit 3: no second alert);
  - catch-up (a boot after missed ticks runs each once; a start within or after the grace);
  - coalescing (100 datagrams during a run cause 1 re-scan);
  - recovery (a `running` row from another boot is rerun or fails per policy);
  - `UNIQUE` key;
  - two runners: one exits;
  - SIGTERM drain;
  - the outer > inner timeout check.
- **Done when:** all suites, `test_typing` and `check_public` are green. The deploy is an image rebuild with no change in behaviour.
- **Risk:** asyncio subprocess and signal handling under tini in the read-only container. Check it with `docker exec` against a temp DB (`testenv.py`).
- **As built:**
  - D-256 (no audit row per run) and D-257 (unclean exit, an interrupted start, a start missed by > 30 min: alert + pause) are in; a crash the runner reported itself is not alerted a second time at the next boot.
  - `RUNNER_OWNS` is empty; the jobs whose code exists today are defined (`stall_scan`, `sampler`, `dispatcher`, `quickview`, `docs_import`, `prune_logs`, `usage_review`, `window_start`), plus a new daily `prune_runs` (the `runner_runs` retention). The watcher passes, at-jobs and the inbox jobs come with S4, S6 and S7.
  - The watcher check at boot applies only once the runner owns a watcher pass (S6): in S2-S5 the watcher runs beside it by design.
  - A boot lifts a drain (the deploy that drained the old runner is done; the new one catches up). `runner.py undrain` lifts one by hand.
  - `runner_start_catchup` is at most 30 min for now: `keepalive.py --window-start` itself acts only within `CRON_STEP` of a start (S5 can lift both; S5 lifted the `CRON_STEP` check for the runner's child and kept the 30 min, D-257).
  - The window start's outer timeout (default 70 min; 80 min since S5, with the window-start ping) has the sum of its inner timeouts + 5 min as its lower bound (`runner_jobs.window_start_inner`, checked against keepalive's source by `test_runner`).

**S2: the runner live, owning only new jobs.**
- **Build:**
  - The entrypoint (`on`) starts `runner.py serve` next to supercronic.
  - The runner owns the heartbeat and `stall_scan`.
  - A temporary cron watchdog `runner.py ensure` (*/5) runs until S8.
  - **Shadow log:** "would run at" for every job cron owns, compared with cron's actual runs over 2 nights.
  - Add `docker/crontab.legacy` and the `legacy` mode.
- **Done when:**
  - 48 h of continuous heartbeat;
  - no failed runs;
  - the shadow times match cron (window starts to the second);
  - the rollback to `legacy` has been exercised once.
- **Risk:** `stall_scan` running at the same time as the dispatcher's and the quickview's scans. They already overlap today; check that there are no DB lock errors.
- **As built:**
  - `RUNNER_OWNS` = `stall_scan` and `prune_runs` (the two new jobs, no cron line). `stall_scan` ticks every 3 min **90 s off the minute** (`Job.offset`), so it never starts at the same second as the quickview's and the dispatcher's scans (cron starts every line on a whole minute).
  - The entrypoint (`on`) starts `runner.py serve` and supercronic in the background and stays their parent: tini passes a `docker stop` only to its child, so its TERM trap sends SIGTERM to supercronic and runs `runner.py stop` (SIGTERM to every running `serve`, then waits for the clean exit). Compose gets `stop_grace_period: 90s` now (was §5 S8) so a stop never kills the runner before its clean exit (which would be an alert + pause at the next boot, D-257). If supercronic ends by itself, the runner is stopped and the container exits (restart policy).
  - `runner.py ensure` (*/5): a running runner with a heartbeat older than 10 min is an alert + pause; none running: it starts one detached (output to `data/runner.log`) and alerts + pauses only what nobody else reports: a runner that had exited cleanly (stopped while the container ran) or never booted, or one that does not start; a reported crash or an unclean exit is left to the new boot (D-257).
  - **Shadow:** for every job cron still runs, the runner wakes at the time it would start it and logs `<job> [<key>] due <time> at <now>` to `data/runner_shadow.log` (component `runner_shadow`, dual-written to `app_log`). The entrypoint copies supercronic's job-start lines to `data/cron_runs.log` (a FIFO + `tee -p`; docker logs keep them too), because docker logs do not survive a deploy. `runner.py shadow-report [--hours 48]` compares: every shadow time needs a cron start within 2 s (and vice versa while the runner ran); a window start needs cron's */30 run at its second and the keep-alive's `window-start:` decision line within 10 min, and a decision line without a shadow start is a mismatch. Exit 1 on any mismatch.
  - `docker/crontab.legacy` = the crontab before S2 (no `runner.py ensure` line); `legacy` mode exports `AFCLAUDE_CRONTAB` to it (the quickview lists what runs).
  - Container check of S1's open item (asyncio subprocesses and signals under tini, read-only root fs, temp DB): children, timeouts (a grandchild in the group is ended too), SIGKILL of a child ignoring SIGTERM, SIGTERM drain and clean exit all work; asyncio uses its pidfd child watcher there. One finding: the container's `net.unix.max_dgram_qlen` is 10 (the host's 512), so a blocking sender stalls after 10 queued wakes. `wake()` was already non-blocking; a full queue now counts as sent (the runner has wakes waiting), and the coalescing test sends through `wake()`.

**S3: the simple ticks.**
- **Build:**
  - `sampler`, `quickview`, `docs_import`, `prune_logs`, `usage_review` (hourly) and `dispatcher` move to the runner, and their 6 cron lines are removed;
  - `export_quickview`'s cron section reads `runner_jobs`.
- **Done when:** one day in which each cadence matches cron's, with no failed runs. The scripts' own flocks stay for now; they do no harm.
- **Risk:** the dispatcher pass and the sampler's Haiku call sometimes run long. Measure their p99 duration first and set the timeouts above it.
- **As built:**
  - `RUNNER_OWNS` + `sampler`, `dispatcher`, `quickview`, `docs_import`, `prune_logs`, `usage_review`; their 6 lines are gone from `docker/crontab` (`on` mode). `docker/crontab.legacy` is unchanged, so `AFCLAUDE_SCHEDULER=legacy` still runs them all from cron (the rollback stays one setting). `test_runner` checks per mode which file has which line, that each handler is its legacy line (script, arguments, log file, flock) and that each cadence is its line's (every tick over two days). A job's child now runs in the repo directory (the cron lines' `cd $AFCLAUDE_DIR`).
  - **Measured** (read-only, cron's runs before S3): the sampler took p50 5.4 s, p99 10.4 s, max 13.8 s (1129 runs 09.-21.10., the time from its sample to its final log line's `app_log` row; the hourly Haiku call is ~5 s of it). The dispatcher pass took p99 2 s, max 2 s (3440 armed passes 29.09.-21.10., `dispatcher.log`; 1-s resolution; no pass continued a session then).
  - **Timeouts:** the measured p99s are far below the old defaults, but the handlers' own worst-case inner timeouts were not (the sampler: the usage refresh with its retry, `claude agents`, two Haiku calls = 14.5 min; the dispatcher with one continue: 13.5 min; the usage review: its 10-min lock wait + a refresh + a review start = 20.5 min). The outer timeout must not kill a slow run that is still within its own limits (that would turn a slow sampler into a pause), so `runner_jobs.TICK_HOST_CALLS` declares each tick's inner timeouts (checked against the literal `timeout=` in each function by `test_runner`) and the defaults are above their sum + 1 min: sampler 20 min, dispatcher 25 min, usage review 30 min; quickview 2, docs import 5, log prune 10 unchanged.
  - **A timeout of a tick** is that run's failure (D-234, §3.5): an alert and the pause (the quickview only alerts, D-239.3; the sampler keeps sampling while paused, D-239.4). It is not runner downtime (D-257): the run row ends as `timeout`, the next tick is on time, and a later boot neither reruns it nor alerts for it again; only `running` rows (a restart) and an unclean exit are D-257 events.
  - **The quickview's schedule section** lists the crontab's AFClaude lines (owner `cron`) and, while the runner runs (`AFCLAUDE_SCHEDULER` `on`/`runner`, not `legacy`), the runner's heartbeat and each owned job's cadence, next due time, last status and failures in a row from `runner_jobs` (owner `runner`); a failed read is shown, never raised.
  - The shadow now covers only `window_start` (the one cron job with a runner counterpart left).

**S4: at-jobs.**
- **Build:**
  - The runner runs `scheduled_jobs`: it claims the job (`job_claim`), then runs it as a tracked child with a timeout.
  - A new payload field `on_interrupt` (the sampler's samples and `review-failfast`'s `--chain-step` jobs get `rerun`).
  - `at -t` sends a wake.
  - `--run-due` is kept for `legacy` only, and the */1 cron line is removed.
- **Done when:**
  - the next pre/post-reset samples run on their minute and are recorded `ok`;
  - a test job interrupted by a restart is rerun or fails according to its flag.
- **Risk:** a job that relied on being detached. List every `at -t` caller and check that each returns within its timeout.
- **As built:**
  - `RUNNER_OWNS` + `at` (one job, `Job(at_jobs=True)`): each pending `scheduled_jobs` row is a keyed run `at <id>` at its `run_at`; the `runner_jobs` row `at` has the earliest `run_at` still ahead as its `next_due`, so the loop wakes on the minute, and `at -t` wakes it for a new job (only when a runner listens, i.e. its socket exists: none in `legacy`). The `*/1 at --run-due` line is gone from `docker/crontab`; `docker/crontab.legacy` keeps it (`--run-due` is unchanged and runs only there).
  - **The claim is inside the run's begin:** `actions.run_begin(claim=<id>)` marks the job started (audited `job.started`, as the shim's claim) in the same transaction as the run row and its checks. So a job never runs twice (also against a `--run-due` in a rollback: whoever claims first runs it), and a run that is refused (a drain) never leaves its job claimed but not run.
  - **The child:** `runner_jobs.py at --key <id>` runs the job's command with `/bin/sh` in its cwd, as `--run-due` did, but in the child's process group (the timeout ends it) and not detached; stdout goes to `data/at_shim.log`, stderr to the runner (appended there too, and its tail kept in `runner_runs.error` on a failure). A non-zero exit is the run's failure (alert + pause). A `failstop.fail` inside the command can't be seen by the runpy spy of S1, so `failstop.fail` also appends its subject to the file named by `AFCLAUDE_FAILSTOP_REPORTS` (`failstop.REPORTS_ENV`), which the child sets for the shell: a reported failure exits 3 and is not alerted twice (the sampler exits 1 after its own `fail()`).
  - **Payload fields** (`at --on-interrupt rerun|fail --timeout MIN -t ...`, checked by `actions.job_put`): `on_interrupt` (default `fail`, the safe side, also for every job stored before S4) and `timeout` (minutes; default setting `runner_timeout_at` = 30). The sampler's samples and the review chain's `--chain-step` jobs pass `rerun`; one-off reviews pass `fail` explicitly. Several at-jobs may run at once (one slot per job id); the job checks the pause itself (`paused="handler"`: the samples run while paused, D-239.4; `usage_review.py` checks).
  - **Drain:** an at-job whose interruption fails (a one-off review start) is a session run for the drain: not started while a drain is in force (it stays pending, not claimed, and runs after it) and `runner.py drain` waits for a running one; `rerun` jobs are not held.
  - **`at -t` callers** (grep `["at",`; `test_runner` fails on a new one), each tracked now and each within the default 30 min: `usage_sampler.py` (pre/post-reset samples `--tag pre_reset|post_reset`: the sampler's path without Haiku, worst case 8.5 min: usage refresh with its retry, `poke_auth`, the usage cache, `claude agents`; measured p99 of the sampler 10.4 s) and `usage_review.py` (`--chain-step` every 5 min and the one-off `--only X --now [--attempt N]` from `spool_review`/`schedule_once`: the usage-review path under its lock, worst case 20.5 min = `tick_min_timeout("usage_review")`). Both commands redirect their own output to a log file (`>> ... 2>&1`), so no grandchild keeps the runner's pipe open; neither starts anything that must outlive it (a review session runs in tmux on the host, started over the bridge). `test_runner` asserts the default is above both ticks' worst case.
  - Not carried over: the adoption of legacy spool files (`data/at_spool`, read-only since 2d) stays in `--run-due`; every file there is known to the DB.
  - **Open:** an interrupted chain step is rerun, but `chain_eta_step` clears `st["chain"]` before the ETA launch (so a crash never fires it twice): a restart in the middle of that launch leaves the launch unknown without an error (the rerun finds no chain). The jobs already pending at the deploy have no `on_interrupt` and fail on an interruption (alert + pause) until the next cycle's jobs are stored with the field.

**S5: window starts.**
- **Build:** the exact-time `window_start` job, keyed by `start_key`, with a 30-min catch-up; the */30 cron line is removed.
- **Done when:**
  - two nights of 23:00 and 04:00 starts, fired at the second;
  - the decision lines (`FIRE`/`HOLD`/`POSTPONE`) are of the same kind as cron's;
  - a DST test at the runner level (the DST transition gives the next start).
- **Risk:** the postponed path still runs in the watcher during this step. That is fine: it is the same split as today.
- **As built:**
  - Built on main's `keepalive.window_start_pass` as it is (main merged into the branch first): the run timing (D-274/D-275: a start whose gate passed is planned late in its window, or chained at once, `run_start_factor`) and the window-start ping (D-277/D-280: a start whose run begins later or is held pings Claude while no session window is open; a failed ping is only logged) are unchanged and run inside the runner's child. The planned late start and a postponed one are still fired by the watcher (`keepalive.deferred`), as the risk above says.
  - `RUNNER_OWNS` + `window_start`; the */30 `--window-start` line is gone from `docker/crontab`; `docker/crontab.legacy` keeps it. The job row's `next_due` is `schedule.next_session_start`, so the loop wakes at the start and starts it at its second (`Due.at` = the start).
  - **The child:** `runner_jobs.py window_start --key K` runs the legacy line's command plus `--start-key K` (a handler argument `{key}`). `keepalive.py --window-start --start-key K` (new `window_start_cli`) checks that K is still the current session-window start (else `SystemExit`: the runner reports it, alert + pause) and does not apply cron's 30-min slot check (the runner owns the timing and the catch-up, so a start caught up at 29:59 is not dropped silently by a child that starts a second later).
  - **One decision per start, shared with the legacy line:** whoever comes first (the runner's child or cron's `--window-start`) claims the start's key in the keepalive state's new split part `window_starts` (`{at, by}`, the last 60 kept) before `window_start_pass`; a later caller for the same start only logs "decided already". The runner's `_settled` reads these claims too, so after a rollback to `legacy` and back the starts cron decided (also a HOLD, which leaves no `handled` key) are not "missed starts" (D-257). `--now` never claims.
  - **Catch-up:** `runner_start_catchup` stays at most 30 min (a start missed by more is an alert + pause and never started late, D-257); the S1 boot logic is unchanged.
  - **Timeout:** the ping adds its request (`WINDOW_PING_TIMEOUT`, 2 min), its wait (`WINDOW_PING_VERIFY_AFTER`, 90 s) and a second usage refresh to the inner waits; both paths are summed (79 min), so `runner_timeout_window_start` is now 80 min (default and lower bound; was 70).
  - **Check:** the shadow is empty since S5 (no cron job with a runner counterpart is left). `runner.py window-report [--hours 48]` replaces it for the "Done when": each session-window start in the range needs its `window_start` run started within 2 s and ended `ok`, and the keep-alive's `window-start: FIRE|HOLD|POSTPONE…` decision line within 10 min; a decision line at no start is a mismatch. Exit 1 on any mismatch.
  - **Tests:** the DST nights at the runner level (25.10.2026 fall back: 23:00 CEST, then 03:00 CET, then 23:00 CET; 29.03.2026 spring forward: 23:00 CET, then 05:00 CEST, then 23:00 CEST), the wake and the start at its second, the legacy line vs the handler, a start decided in `legacy` neither run nor missed, the claim in both orders, a stale key, the catch-up not cut by the cron slot, the report.
  - **Deploys:** from S5 on, a restart during a start is an alert + pause (D-257): every deploy runs `runner.py drain` first and is not done within 30 min of a session-window start.

**S6: the watcher passes.**
- **Build:**
  - Refactor `keepalive` into one function per pass (`watch`, `last_stretch`, `deferred_start`, `fillup`, `run_readings`, `compaction`), each split into a plan step that returns `next_due` and a keyed act step. `keepalive.run()` calls the same functions, which keeps `legacy` working.
  - Add the runner jobs for these passes.
  - The entrypoint no longer starts the watcher, and the watchdog cron line is removed.
- **Done when:**
  - a full night with a start, a compaction and a fill-up (if one is planned) all run by the runner;
  - the fill-up starts within seconds of its plan;
  - `test_keepalive` and `test_compaction` pass through both callers.
- **Risk: the largest step.** The watcher's in-memory timing (`lm_start`, `WAKE_SLACK`, the 2-min usage refresh before a fill-up) must become stored `next_due` values. A compaction cut off by a deploy fails and pauses; `drain` is there to prevent that.
- **As built:**
  - Built on main merged first (`tools/deploy.py`, the manager-session fallback removal, the 3b S1 data model, the chain-gap fix). `tools/deploy.py` now refuses (unless `--force`) within 30 min before or after a scheduled session-window start and prints the next safe time, checked before the drain and again before the rebuild; an aborted deploy lifts its drain (else it would hold the next starts for up to 85 min).
  - **keepalive:** each pass is a plan step and an act step: `watch_pass` (detection), `last_stretch_plan`/`last_stretch_act`, `deferred_plan`/`deferred_act`, `fillup_plan`/`fillup_act` (and `fillup_test_plan`/`_act`), `compaction_plan`/`compaction_act`; `watch_run_usage` is the run readings. `run()` (the watcher, `legacy`) calls the composed `last_mile_pass`, `deferred_window_start_pass`, `fillup_pass`, `compaction_pass` as before, unchanged in behaviour (run timing D-274/D-275, the ping D-277/D-280, last stretch, compaction D-238, fill-up D-212, the inbox messages going along with a start or a compaction).
  - **The runner's side** (`keepalive.runner_pass(job, key)`, run by `runner_jobs.py` in the child, on `$AFCLAUDE_MANAGER_SESSION`, armed and without `--take-over-idle`, as the container watcher ran; output to `keepalive.log`): the ticks `watch` (60 s, +15 s: the detection and the plan steps of the last stretch, the postponed / planned start and the fill-up), `compaction_check` (60 s, +45 s) and `run_readings` (5 min, +50 s), and the acts `last_stretch`, `deferred_start`, `fillup` (80 min, the window start's inner timeouts) and `compaction` (60 min: its check, two turn waits, the save, `/compact`, the reply, three sends). A plan step stores its act as `acts.<job>` = {dedup key, time, run key} in the new runner state `keepalive.watch` (born in the DB; `store_act`); the runner's new job kind `planned` reads it (`runner.planned_act`), wakes at the time and starts the act under the run key, once (`UNIQUE (job, key)`). The act re-plans first and acts only if the same dedup key is still due, with the watcher's dedup (`handled`, `compactions`), then rewrites or drops its entry.
  - **The in-memory timing, stored:** a planned start (deferred, fill-up) is the act's time + `WAKE_SLACK`; a HOLD's recheck (last stretch) and the owner-busy retry (fill-up, a deferred start) are the act's next time; an act due now that is still pending keeps its run key (a plan every minute never plans it twice), one that ran without starting (a DB pause, the session lock) is planned anew at the next tick under a new run key. The next last-stretch slot start and the fill-up's usage refresh 2 min before its start are the `watch` tick's `next_due` (the runner moves the tick earlier). The runner's `window_start` child stores a start it planned late or postponed as the act at once (not only at the next tick). The log-once memory of the passes (the detection line, the fill-up and compaction lines, the pause reason logged, the busy owner) is kept in `keepalive.watch` (`memo`) between the children.
  - **Failures:** a part of the `watch` tick that fails is reported as the watcher reported it (`fail_stop`, the same subjects) and the other parts still plan; a failed act is reported by the runner (alert + pause) and its entry dropped, so after the owner cleared the pause the next plan decides again (as the watcher's next pass would); an act cut off by a restart stays settled and never reruns (D-257). The watcher's `STOP` / `PAUSED` files hold the runner's passes too (skipped, logged once; an act due meanwhile is dropped and planned anew) until S9 retires them.
  - **Cut-over:** `RUNNER_OWNS` + `WATCHER_PASSES`; the `:07/:22/:37/:52` watcher watchdog line is gone from `docker/crontab` (`watch` carries its cron tag) and the entrypoint starts the watcher only in `legacy`; the runner's boot check refuses to start next to a watcher. The quickview's watcher tile shows the runner's `watch` job (its last ok run within 5 min) in the `on` mode; its schedule list says "when its pass plans it" for an act.
  - **Tests:** `test_keepalive` (`PassesThroughTheRunner`), `test_compaction` (the idle, run-end and failure classes again through the runner, `*Runner`, plus the acts) and `test_fillup` (`FillupThroughTheRunner`) run the passes through both callers (`testenv.runner_pass`: one child, a fresh process each); `test_runner` (`WatcherPasses`, `PassChild`) the planned jobs, the act timeouts, an interrupted act, a drain, the child's result line.
  - **Follow-ups (after review):** the guard the other way round: `keepalive.run`, `docker/start_watcher.sh` and `start_keepalive.sh` refuse to start (exit 1; `runner.runner_conflict`, `runner.py watcher-check`) under `AFCLAUDE_SCHEDULER=on` or while a runner that did not exit cleanly has a heartbeat under `STALE_BEAT` (10 min, `ensure`'s bound); `legacy` after a clean stop starts as before. A cut-off act (timeout, restart) left its `acts.<job>` entry under a run key that has a run, so no plan could replace it: `store_act` now plans it anew under a new run key once the pause it raised is cleared (kept while paused, D-239/D-257).
  - **Open:** the deploy guard checks only the session-window starts; a planned act (a late start, a fill-up, a compaction) due during a deploy is held by the drain and runs after the new boot, deciding on its numbers then (its own deadlines apply). The "Done when" is a live night (not checked here).

**S7: the inbox.**
- **Build:**
  - `inbox_worker.process` becomes the `inbox` job (woken by `inbox_api`);
  - the verify threads become stored checks (`inbox_verify`);
  - `inbox_api` only serves HTTP, stores the message and sends the wake;
  - the runner supervises `inbox_api` (restart + alert), and both the entrypoint start and the `ensure` cron line are removed.
- **Done when:**
  - `test_inbox` and `test_inbox_active` pass with the new driver;
  - a restart during a verify is resumed and ends correctly (seen, or an error at its deadline);
  - end-to-end with the quickview box: one note and one "now" message.
- **Risk:** delivery now depends on the runner; a down runner shows up as a stale heartbeat.
- **As built:**
  - **New job kind `poll`** (`Job.poll`, `runner._plan_poll`): `inbox` (30 s) and `inbox_verify` (5 s) only run while `POLL_PENDING[name]` (`inbox_worker.pending`: any `pending` message, held ones included; `inbox_worker.verify_pending`: any stored entry) says there is work, at most once per `poll` (the next due time is stored when the run starts); with none, the job row has no due time and only a wake starts it again. A boot does not catch a poll job up (its first plan finds the work). Both run while paused (D-186/D-244), rerun on interrupt (no alert), no `session` flag (a drain does not hold them). Timeouts 5 min / 30 s (settings `runner_timeout_inbox`, `runner_timeout_inbox_verify`). They are not in `TICK_HOST_CALLS`: the 1-min `TICK_MARGIN` floor does not fit a 30-s job, and `inbox`'s host calls (the 120-s Haiku call, `keepalive.fire`) use named constants, which the literal-`timeout=` source check cannot see; `test_runner` skips poll jobs in that check. A batch of many "now" messages could outlast the 5 min; that would be a timeout (alert + pause), as the plan states.
  - **Stored verification:** `inbox_worker.deliver` calls `store_verify(msg, path, offset, now)` (default; tests inject their own) = a runner_state component `inbox` (new, split part `entries`, actor `runner:inbox`) entry `{msg, path, offset, deadline = now + VERIFY_TIMEOUT}`, then wakes the runner. `verify_tick` (the `inbox_verify` job) checks each entry (`check_verify`: marker seen after the offset: settled; before the deadline: pending; past it: `failed` = error on the message + alert + pause of its project, D-244), so a restart resumes it (`test_inbox.Delivery.test_verify_resumes_after_a_restart_...`). The verify threads (`verify`, `start_verify`, `VERIFY_POLL`) are gone. `run_forever` (the `legacy` thread) stays and now also calls `verify_tick` each pass, so the rollback still verifies.
  - **`inbox_api.py serve --no-worker`:** no worker thread; a post or a retry sends the runner the wake datagram (`_WakeRunner`, same `.set()` interface as the thread's event, so the handler is unchanged). Plain `serve` and `ensure` are the `legacy` mode as before. Its log lines (`posted`, `http`, tracebacks) go to `data/inbox_api.log` in both modes: `ensure` redirects the legacy child's output there, and `serve --no-worker` does it itself (`log_to_file`: stdout and stderr dup'd onto the file once the lock and key checks passed; the runner's child pipe is read only at the child's exit, so nothing would reach the log otherwise). An early exit (lock held, no key) stays on the pipe: that is the tail the runner's alert quotes. `test_inbox.Endpoint.test_no_worker_serve_writes_its_log_lines_to_the_log_file`.
  - **Supervision:** `runner.SUPERVISED` = `inbox_api` -> `inbox_api.py serve --no-worker`. `Runner.start_supervised` starts it at `serve()` (no alert); `_supervise` waits for its exit (no timeout, no run row) and, unless a stop was requested, alerts + pauses (`failstop.fail`, install-wide, as the old `ensure` did) and starts it again after `SUPERVISE_DELAY` (5 s, so a child that dies at once is no tight loop). `request_stop` SIGTERMs it at once (it never ends by itself); `_stop_children` cancels what is left. I read "restart + alert" as alert + pause like every other runner failure and as the old watchdog did (D-244: inbox errors stop automation); the alert is raised at every unexpected exit, not once per outage.
  - **Cut-over:** `RUNNER_OWNS` + `inbox`, `inbox_verify`; the entrypoint starts `inbox_api.py ensure --boot` only in `legacy`; the `*/5 inbox_api.py ensure` line is gone from `docker/crontab` (`crontab.legacy` keeps it). `test_runner` checks per mode: that line is the one extra legacy-only line no job's cron tag accounts for.
  - **Open:** the live check (a note and a "now" message through the quickview box, a kill of `inbox_api` and of the runner during a verify) is for after the deploy. Deploy: the image is rebuilt anyway; the first boot starts `inbox_api` under the runner, so the old detached one (entrypoint-started) must be gone (a rebuild restarts the container); a stale `data/api/inbox.sock` is replaced by `bind`.

**S8: runner only.**
- **Build:**
  - `on` becomes `runner`: the entrypoint `exec`s `runner.py serve` (still under tini), and supercronic no longer starts;
  - the temporary watchdog line is removed;
  - the internal loop watchdog plus `restart: unless-stopped`;
  - compose `stop_grace_period: 90s` and a heartbeat health check.
- **Done when:**
  - 7 days with every job's cadence met and no unexplained pause;
  - a test that blocks the loop (a synchronous sleep injected into a test job) makes the watchdog thread exit the process, and Docker restarts the container.
- **Risk:** there is no outside watchdog anymore. The watchdog thread is the only one, so it is tested on its own.
- **As built:**
  - **Modes:** `runner` and `on` both mean "the runner only" (`runner.RUNNER_MODES`, the quickview, `runner_conflict` and the entrypoint treat them alike). I read "`on` becomes `runner`" as an alias, not a removal: the S2-S7 deploys set `on` in `docker/.env`, so the S8 deploy needs no env change to take effect, and `docker/crontab` has no job line left that an `on` with supercronic could still run, so keeping the old meaning would only start an idle supercronic. `runner` is the name to use from now on (`env.example`, compose docs); S9 removes `on` with `legacy`. `legacy` and `off` are unchanged.
  - **Entrypoint:** `on|runner` `exec`s `python3 -u runner.py serve` after the common start (known hosts, prompt import); no shell stays, so tini's only child is the runner and `docker stop`'s SIGTERM is the runner's own (clean exit within `STOP_GRACE` 80 s < compose's 90 s). The FIFO, `tee`, `data/cron_runs.log` and the TERM trap with `runner.py stop` are gone. Exit codes pass through: the loop watchdog's 70, a reported crash (`EXIT_CRASH`), a second runner (`EXIT_LOCKED` 2).
  - **Cron:** the `*/5 runner.py ensure` line is removed; `docker/crontab` is comments only (kept for the quickview's listing and the `AFCLAUDE_CRONTAB` default; S9 removes it). `ensure`, `stop` and `shadow-report` stay as CLI commands for S9 to remove; nothing runs them.
  - **Loop watchdog:** the thread already existed since S1 (`runner.Watchdog`, `STALE_LOOP` 300 s, checked every 15 s, `os._exit(70)`); S8 makes `serve()` read the constants at call time (a test patches them) and documents it as the only watchdog. It alerts nothing itself: a wedged process may not reach the DB or the bridge, and the exit must not wait for them; the restarted container's boot finds no clean exit and alerts + pauses (D-257, tested), and the cut-off run is recorded `interrupted` and handled by its job policy. A crash loop (a persistent boot failure) is slowed by Docker's restart back-off and alerts at every crash (failstop).
  - **Compose:** `restart: unless-stopped` and `stop_grace_period: 90s` were already there (S2). New: `healthcheck: runner.py health` (interval 60 s, timeout 30 s, 3 retries, start period 120 s). `health` = a live `runner.py serve` process and a `runner_jobs 'runner'` heartbeat younger than `HEALTH_STALE` (3 min; the loop writes it at least every 60 s, the watchdog fires at 5 min, so health shows first); an unreadable DB is unhealthy too; `legacy` and `off` are always healthy. Docker only marks the container `unhealthy` (visible in `docker ps`); it restarts nothing itself, the watchdog thread does.
  - **Tests:** a synchronous `time.sleep` in a job's spawner (a) in the loop with the exit injected (the watchdog fires during the block), (b) in a real `runner.py serve` process (exit code 70, no clean exit, lock released, the next boot alerts and records the run interrupted); `health` (modes, no process, no/stale heartbeat, unreadable DB, CLI); the bounds between `STALE_LOOP`, `HEALTH_STALE`, `STOP_GRACE` and compose; the entrypoint with fakes (same pid as the runner, no supercronic, exit code passes through); no cron line left in `docker/crontab` (`test_runner`, `test_export_quickview`).
  - **Deploy:** `AFCLAUDE_SCHEDULER` stays `on` (works as `runner`) or becomes `runner` in `docker/.env`; no other host step. It is an image rebuild (entrypoint, compose healthcheck): `tools/deploy.py` as usual (drain first, not within 30 min of a window start). The "7 days with every job's cadence met" check and the live "Docker restarts the container" check (a blocked loop on a temp container) are for after the deploy.

**S9: cleanup and observation.**
- **(a) Cleanup**, after 7 calm days in `runner` mode:
  - remove the `legacy` mode, both crontabs, supercronic from the image, `at_shim --run-due`, `start_watcher.sh` and the `keepalive.run()` loop;
  - the watcher's `STOP`/`PAUSED` files give way to `automation_paused`;
  - update the README file table and design §5.1 "as built".
- **As built (S9a, with 3b S6, branch `retire-legacy`):** removed `docker/crontab`, `docker/crontab.legacy`, `start_watcher.sh`, `start_keepalive.sh`, supercronic in the Dockerfile, the `PAUSED` symlink and `AFCLAUDE_CRONTAB`; the modes `legacy` and `on` (entrypoint: `runner|off`, anything else exit 2; `runner.RUNNER_MODES`, the quickview); `at --run-due` (`at -t` / `-l` and the old spool's read-only reader stay); `keepalive.run()`, `--once` and the `STOP` / `PAUSED` holds in `runner_pass` (pausing is `automation_paused` alone); `runner.py ensure|stop|shadow-report|watcher-check` and the shadow / conflict helpers; `inbox_api.py ensure` and the worker thread (`inbox_worker.run_forever`, `serve --no-worker`: `serve` is now always the runner's supervised, HTTP-only server). Kept: every keep-alive pass, `--window-start`, `--run-stopped`, `--decide`, `LOCK_FILE`, `schedule.CRON_STEP` / `due_session_start` (the keyless `--window-start` path), `RUNNER_OWNS`. Deploy: `docker/.env` `AFCLAUDE_SCHEDULER=on` must become `runner` first.
- **(b) The remaining 3a items of the design** (can start any time after S1):
  - a `status_snapshot` row written each loop, at most once a minute;
  - `run_log` decisions and `driven_sessions` rows for starts and reviews, through `actions.py`.
- **As built (S9b, part: the `status_snapshot` row):**
  - **Table:** `status_snapshot(section, account_id, computed_at, state ok|error, payload JSON, error, builder, version)`, PK `(section, account_id)`, additive (`store.STATUS_SNAPSHOT_SCHEMA`, no version bump). It is the shape of `docs/phase6c_plan.md` §3.3, so 6c R1 adds sections and the sizes/models, not a table. One row per section, always the latest value: rows are replaced, so there is no history and **no retention** (the design states none).
  - **Design text** (`04_architecture.md`: "usage %, resets, budget decision, `threshold_info()`, ratios") is the only statement of the content; the owner decisions name the next-run tile (D-166, D-200, D-201). Kept minimal: four sections, the quickview's own blocks (so R1 can derive `status.json` from the same values): `usage` (session/weekly %, resets, age, the budget decision), `next_run` ("holding until", `pacing.next_run`, assuming no more usage), `threshold` (`pacing.threshold_info`: threshold, ratios, prediction error), `runner` (automation pause incl. per-project pauses, the runs going, the loop heartbeat). Not built here (6c R1): `limits`, `eta`, `fill`, `fillup`, `context`, `next_review`, `runtime`, the Pydantic models and the contract tests.
  - **Who writes:** a runner job `status_snapshot` (`runner_jobs.status_snapshot_tick` -> `status_snapshot.run`), an interval tick every minute 30 s off the minute (`paused="runs"`: it runs while automation is paused, the pause is part of the snapshot), so "at most once a minute" is the job's grid, not a loop-side throttle, and a wake or a restart never adds a second write inside the minute. It is a child process like every tick, so the model code (pacing, keepalive) never blocks the loop and the loop's watchdog. Timeout setting `runner_timeout_status_snapshot` (2 min; inner: two usage-cache reads).
  - **Write path:** `actions.snapshot_put` (a plain runner write like `run_begin`: writer check, runner actors only, `store.retrying`, the §7.8 DB-error path, all sections in one transaction), no `audit_log` row (D-154, D-256: 1,440 audit rows a day for a replaced value).
  - **Failure (D-234/D-235):** a DB error raises, the child exits non-zero, the runner alerts and pauses (`pause_on_failure` stays True). A section whose block cannot be built (no usage cache, a model exception, a payload over 64 KiB) is written as `state='error'` with the reason and no payload: shown by the dashboard with its age, never an old value passed off as current (same rule as the quickview's error blocks).
- **As built (S9b, rest: `run_log` decisions and `driven_sessions`):**
  - **Rows** (`actions.run_log_add`, a log row with no audit row, D-154/D-256; written only when armed (a dry run writes none) and before the decision is acted on; a failed write is `keepalive.decided` -> `fail_stop` and the start is aborted, D-234/D-235, never skipped; after a start that is handled already the row is alerted and paused like S4's `_pick_logged`). The design text names no decision list (only "`run_log` (decisions)" and the schema comment "FIRE, HOLD, WAIT_WINDOW, skip, start, cleanup"), so the set is the brief's: `RUN_LOG_DECISIONS` = `PICK`, `HOLD`, `GO`, `POSTPONE`, `DEFER`, `FIRE`, `FILLUP`, `START`. Reason lines start with the start key.
    - **Gate** (`window_start_pass`, `deferred_act`: `GO`; `HOLD`; `POSTPONE` = re-decided later or skipped to the next start; a paused or failed-reading start is a `HOLD` too): component `keepalive`, `window start <key>: <budget reason>`. **Timing, D-274** (after a `GO`): `DEFER` (the run starts later, with its time) or `FIRE` (now, chained or not). **Last stretch** (`last_stretch_act`): `last-stretch slot <key>: ...` as `GO` / `POSTPONE` / `HOLD`, and a `HOLD` for the wait for a limit reset. **Fill-up**: `FILLUP`, `start <key>: <reason>: <the handled result>` (in `handle_fire`'s no-pick path, after the start is decided; no gate row: the fill-up's plan is its own and logged as before). **Starts** keep S4's `PICK` / `HOLD`.
    - **One row per decision, not per retry:** an owner-busy or DB-pause retry re-runs the gate and the timing of the same start, so `GO`, `FIRE` and the last stretch's rows are written once per key and decision (`once` prefix, checked in the insert's transaction: `store.has_run_log`); `POSTPONE` / `DEFER` / a `HOLD` of a window start are new decisions each time. Cost of the key: a gate that flips GO, POSTPONE, GO within one start shows its second `GO` only in the log lines; the last stretch shows only the first `HOLD` / `POSTPONE` of a slot.
    - **Usage reviews** (component `usage_review`, actor `runner:usage_review`): `START` from `run_once` (every review launch: the one-off, both chain legs, run-now's `review_now`; ok or launch failed, with the session) and `POSTPONE` from `note_postponed` (quota). The monthly review is a nightly task (D-254), so it has no launch of its own here. No dry run exists in `usage_review.py`, so these are always written.
  - **`driven_sessions`:** nothing left to add: 3b S1/S2/S4 already write the rows through `driven.set` (audited) for every launcher, `claim_tmux` for a review / stall row before its launch, the task-manager's row at its first start, the dispatcher's cleanup ending a review / stall row when its tmux session is gone. S9b checked this against the review launcher (`run_once` -> `claim_tmux(sid, "review")`, test `test_names`) and writes no further row; the review's `START` run_log row carries its session id, which joins to that row.
  - **Tests:** `test_run_log_rows.py` (17), `test_pick` (the fill-up row). With this, S9(b) is done; the 6c R1 overlap (the snapshot's sections) stays as noted above. 6c's `runs` view reads these rows (`/api/v1/runs`).

## 6. Open questions for the owner

1. **Audit per runner run (D-154).**
   - Each run row would be written through `actions.py`, with actor and via on the row, but without its own `audit_log` row. That is the same as `app_log` lines today.
   - The cost of a full audit: about 3,000 runs a day, mostly 1- and 3-min ticks, means about 6,000 extra `audit_log` rows a day, with no retention on them.
   - What a run changes in work or state is still audited by the handler's own actions.
   - **Recommended:** run rows without audit rows.
2. **Runner downtime as a failure (D-235).** Cases: an unclean runner exit (crash, host reboot), a start or compaction killed mid-way, a session-window start missed by more than 30 min.
   - **Recommended:** alert and pause until cleared, as D-235 reads literally, with deploys draining first so they never trigger it.
   - **Alternative:** alert only when AFClaude's code didn't cause it (host reboot, Docker restart), and keep the pause for crashes and interrupted runs.
