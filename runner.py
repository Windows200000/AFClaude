#!/usr/bin/env python3
"""runner.py: the resident runner (phase 3a, docs/phase3a_plan.md §3; D-153/D-173).

    python3 -u runner.py serve            the runner (one per install: data/run/runner.lock)
    python3 runner.py status [--json]     its jobs, next due times, running runs, heartbeat age
    python3 runner.py drain [--max-wait MIN]   before a deploy: no new run that acts on a session;
                                          waits until none is running (exit 0), else exit 1
    python3 runner.py undrain             lift a drain (a deploy that did not happen)
    python3 runner.py wake                re-read the DB now (what the senders do after a write)
    python3 runner.py window-report [--hours H]   S5: each session-window start vs its run and decision
    python3 runner.py health              S8: the container's health check (a live runner, a fresh heartbeat)

One asyncio loop: read the due times, wait for the earliest of them, a wake datagram
(data/run/runner.sock) or a child's exit, start what is due, repeat; a 60-s longest sleep is the
backstop for a lost wake. There is no waiting inside a process: everything the runner waits for is
a due time in the DB (runner_jobs.next_due), so a restart wakes at the planned time.

Jobs (JOBS) run as child processes, `python3 runner_jobs.py <job> [--key K]`, each in its own
process group, with an outer timeout (setting runner_timeout_<job>, D-234): on a timeout the
group gets SIGTERM, SIGKILL 10 s later, the run is recorded `timeout` and fails. Exit 0 = ok,
3 = failed and already reported by the handler, anything else = failed and reported here
(failstop.fail: an alert and the pause, D-235; a job with pause_on_failure=False only alerts,
D-239.3). At most one run per job at a time; a tick still running at its next tick skips that tick.
A tick that timed out or failed is that run's failure (D-234), not runner downtime (D-257): its
row is ended, its next tick is on time, and a later boot neither reruns nor reports it again.

Records: runner_jobs (one row per job + the row 'runner' with the loop's heartbeat) and
runner_runs (one row per run; a keyed run, e.g. a session-window start, can't run twice: UNIQUE
(job, key)), written only through actions.py (D-154; no audit row per run, D-256). Its own state
(runner_state 'runner'): this boot, whether it exited cleanly, a drain.

Boot (Runner.boot), after the lock:
  1. runs another boot left `running` become `interrupted`; a job whose interruption is safe
     (on_interrupt "rerun") is due now, one that types into a session (a start: "fail") is an
     alert + pause and never runs again (D-257);
  2. the previous boot did not exit cleanly (a crash, a kill, a host reboot): alert + pause (D-257);
  3. any drain is lifted (a deploy drained the old runner; this one must catch up);
  4. catch-up: each owned interval job that missed its tick runs once (staggered); a
     session-window start since the last heartbeat runs if it is less than runner_start_catchup
     (30 min) old, an older one is a "missed start" (alert + pause, D-257).
SIGTERM: no new runs; running ones get up to STOP_GRACE (under compose's 90-s stop grace), then
their groups get SIGTERM and their rows stay `running` (the next boot: interrupted); the runner
records the clean exit. A watchdog thread exits the process (70) when the loop's heartbeat is
older than 5 min (a wedged loop; Docker's restart policy then restarts the container).

S8, the runner only (AFCLAUDE_SCHEDULER=runner): docker/entrypoint.sh `exec`s `runner.py serve` as
tini's only child; no cron (S9 removed supercronic, its crontabs and the rollback mode `legacy`).
A `docker stop` reaches the runner as SIGTERM (clean exit within compose's 90-s grace); a crash or
the watchdog's exit ends the container and `restart: unless-stopped` starts it again, whose boot alerts + pauses for the unclean
exit (D-257). The docker HEALTHCHECK is `runner.py health` (a live runner process and a loop
heartbeat under HEALTH_STALE); it only reports, the watchdog thread is the one that acts.

Ownership: RUNNER_OWNS is what this image's runner runs: every job since the cut-over (§4: S2-S7
moved them from docker/crontab into it step by step; S9 removed the crontabs). S7 added the inbox
API as a supervised child (SUPERVISED).

Watcher passes (S6, WATCHER_PASSES; keepalive.runner_pass in the child): the ticks `watch`,
`run_readings` and `compaction_check` only read and plan; the acts `last_stretch`, `deferred_start`,
`fillup` and `compaction` are `planned` jobs: a keyed run at the time the pass's plan step stored in
the keep-alive's runner state (PLANS_COMPONENT, acts.<job>:<sid8> = {key, at, run, session}; planned_act),
one plan per task-manager of the registry (phase 3b S3: store.manager_sessions, rank order; the ticks run
over all of them, the earliest due act of a job runs, one at a time), under its run key once (a run row
settles it); a session run (a drain holds it), a cut-off one is an alert +
pause and never reruns (D-257). The job row's next_due is that time while it is ahead.

Window starts (S5, the job "window_start"): a keyed run at each session-window start, to the second
(schedule.next_session_start is the job row's next_due; key schedule.start_key), as
`keepalive.py --window-start --start-key KEY` (keepalive's window_start_pass unchanged: its run
timing, D-274/D-275, and its window-start ping, D-277/D-280). A start is settled by its run row or
by the keep-alive's own records (handled, a postponed start, its "window_starts" claim), so a start
is never decided twice. A
start the runner was not up for runs late only within runner_start_catchup (30 min); later it is a
missed start (alert + pause, D-257), never started late. `runner.py window-report` checks the
starts of the last nights against the runner's runs and the keep-alive's decision lines.

At-jobs (S4, the job "at"): every pending scheduled_jobs row (stored by `at -t`, which then wakes
the runner) is a keyed run `at <id>` at its run_at: its run claims the job in its own begin
(actions.run_begin(claim=): never twice) and runs its command
as a tracked child (runner_jobs.at_job) with the job's payload `timeout` (minutes; default the
setting runner_timeout_at). Several at-jobs may run at once (one slot per key). A run a restart cut
off follows the job's payload `on_interrupt`: "rerun" (the sampler's samples, the review chain's
steps) runs it again at the boot, "fail" (the default: a one-off review start) is an alert + pause
(D-257); a "fail" job is held by a drain, a "rerun" one is not. The runner_jobs row "at" has the
earliest future run_at as its next_due.

The inbox (S7, the jobs "inbox" and "inbox_verify"; §3.2/§3.3): a Job with `poll` only runs while
POLL_PENDING[name](conn, now) says there is work, at most once every `poll` while that lasts (no
due time, only a wake, once it is idle again; _plan_poll). `inbox` (inbox_worker.process, woken by
inbox_api after a post or a retry, §3.7) triages and delivers "now" messages every 30 s while one is
pending or held; `inbox_verify` (inbox_worker.verify_tick) checks every 5 s the post-send
verifications a delivery stored (runner_state "inbox" entries {msg, path, offset, deadline}), so a
restart resumes one from the same offset and deadline and ends it seen or as an error at its
deadline. The runner also keeps `inbox_api.py serve` running (SUPERVISED, started at
boot and again, with an alert, whenever it exits before a stop was requested): it only serves HTTP,
stores the message and wakes the runner; no run row, no outer timeout, no cron watchdog. Owner
messages are not automation (D-186/D-244), so both run while paused.

Run-now (phase 3b S5, the job "request"; docs/phase3b_plan.md §2): every open action_requests row
(request.add, whose callers wake the runner; the loop's 60-s sleep is the backstop) is a keyed run
`request <id>`, due at once, one slot per request; its child decides it (run_now.run: started /
refused / failed, through request.start / request.finish). It runs while paused (D-282), a drain
holds it, and a restart during one fails it (on_interrupt "fail": the request is recorded failed,
alert + pause, D-257); a run that ends without deciding its request records it failed (alert + pause).

"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import math
import os
import re
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import actions  # noqa: E402
import failstop  # noqa: E402
import runner_jobs  # noqa: E402
import runner_state  # noqa: E402
import schedule  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
COMPONENT = "runner"
VIA = "runner"
RUNNER_JOBS = os.path.join(HERE, "runner_jobs.py")
MAX_SLEEP = 60.0                          # the longest sleep: the backstop for a lost wake
HEARTBEAT_EVERY = timedelta(seconds=30)   # runner_jobs 'runner'.heartbeat_at, at most this often
STALE_LOOP = 300.0                        # the watchdog exits the process after this long without a loop pass
WATCHDOG_EVERY = 15.0
WATCHDOG_EXIT = 70
HEALTH_STALE = timedelta(minutes=3)       # S8: `health` fails when the loop heartbeat is older (the watchdog exits at STALE_LOOP)
TERM_GRACE = 10.0                         # SIGTERM -> SIGKILL of a timed-out child's group
STOP_GRACE = 80.0                         # SIGTERM of the runner: running runs get this long (compose: 90 s)
STAGGER = timedelta(seconds=5)            # boot catch-up: the missed interval jobs start this far apart
DRAIN_MARGIN = timedelta(minutes=5)
DRAIN_POLL = 5.0
EXIT_LOCKED = 2
EXIT_REPORTED = runner_jobs.EXIT_REPORTED
EXIT_CRASH = EXIT_REPORTED                # serve() failed and reported it (failstop.fail)
TINY = timedelta(microseconds=1)
SUPERVISE_DELAY = 5.0                     # S7: a supervised child that exited is started again after this long
# The keep-alive watcher's passes (S6; the watcher itself is gone since S9).
WATCHER_PASSES = frozenset({"watch", "run_readings", "compaction_check", "last_stretch", "deferred_start", "fillup",
                            "compaction"})
# Where a planned job's next act is (S6): the keep-alive's runner state, acts.<job>:<sid8> = {key, at, run,
# session} per task-manager of the registry (3b S3; its plan step writes it, keepalive.store_act; the runner
# only reads it).
PLANS_COMPONENT = "keepalive.watch"


@dataclass(frozen=True)
class Job:
    """One runner job (docs/phase3a_plan.md §3.2).
    timeout_min: the default of its setting runner_timeout_<name> (test_runner keeps them equal);
    every / daily: an interval tick on the UTC grid (every k x `every` from midnight, shifted by
    `offset`) / once a day at (hour, minute) UTC; window_start: a keyed run at each session-window
    start (schedule.py, key schedule.start_key); paused: while automation is paused it "runs",
    the "handler" checks (and runs anyway here), or the runner "skip"s it; on_interrupt: a run cut
    off by a restart is rerun or "fail"s (alert + pause, D-257); session: it types into the
    task-manager session (a drain holds it); pause_on_failure: False = a failure alerts only
    (D-239.3); argv: the child's command instead of runner_jobs.py (tests); offset: see every; what:
    what it does, for the quickview's schedule list (default: the job's name); at_jobs: the scheduled_jobs rows, one keyed
    run per row at its run_at (S4; on_interrupt and timeout_min are the defaults of the row's payload
    fields on_interrupt and timeout); planned: a keyed act at the time its pass's plan step stored
    (S6, planned_act: the run's key is the plan's `run`); poll: it only runs while POLL_PENDING[name]
    says there is work (S7: `inbox`, `inbox_verify`), at most once every `poll`; no due time (only a
    wake starts it) while there is none (_plan_poll); requests: the open action_requests rows, one
    keyed run per row (phase 3b S5, run-now: _plan_requests; due at once, a wake or the 60-s sleep finds
    a new one), a run's end or interruption decides the request if its handler did not."""
    name: str
    timeout_min: float
    every: Optional[timedelta] = None
    daily: Optional[tuple[int, int]] = None
    window_start: bool = False
    at_jobs: bool = False
    planned: bool = False
    poll: Optional[timedelta] = None
    requests: bool = False
    paused: str = "runs"
    on_interrupt: str = "rerun"
    session: bool = False
    pause_on_failure: bool = True
    argv: Optional[tuple[str, ...]] = None
    offset: timedelta = timedelta(0)
    what: str = ""

    def __post_init__(self) -> None:
        if not actions.JOB_NAME_RE.match(self.name) or self.name == actions.RUNNER_ROW:
            raise ValueError(f"bad job name {self.name!r}")
        if sum((self.every is not None, self.daily is not None, self.window_start, self.at_jobs, self.planned,
               self.poll is not None, self.requests)) != 1:
            raise ValueError(f"{self.name}: exactly one of every / daily / window_start / at_jobs / planned / poll "
                             "/ requests")
        if self.paused not in ("runs", "handler", "skip") or self.on_interrupt not in ("rerun", "fail"):
            raise ValueError(f"{self.name}: bad paused / on_interrupt")
        if self.offset and (self.every is None or not timedelta(0) < self.offset < self.every):
            raise ValueError(f"{self.name}: an offset needs an interval longer than it")

    @property
    def keyed(self) -> bool:
        return self.window_start or self.at_jobs or self.planned or self.requests

    def slot(self, key: Optional[str]) -> str:
        """Its runs' slot (Runner.tasks): one run per job at a time, an at-job / a request one per key."""
        return f"{self.name} {key}" if (self.at_jobs or self.requests) and key else self.name


SEC, MIN = timedelta(seconds=1), timedelta(minutes=1)
# The ticks' timeouts (S3) are above the sum of each handler's own worst-case inner timeouts + 1 min
# (runner_jobs.tick_inner; test_runner checks it), so a slow run that is still within its own limits
# is never killed, and a hung host call fails with its own clear error first. Measured on cron's
# runs before S3: sampler p99 10.4 s, max 13.8 s (1129 runs, 09.-21.10., app_log; its hourly Haiku
# call is ~5 s of that); dispatcher pass p99 2 s, max 2 s (3440 armed passes, 29.09.-21.10.,
# dispatcher.log; no pass continued a session in that time). Every timeout is far above them.
JOBS: dict[str, Job] = {j.name: j for j in (
    # 90 s off the minute: never at the same second as the quickview's and the dispatcher's scans
    # (cron starts every line on a whole minute), so the three don't contend for the DB at once
    Job("stall_scan", 2, every=3 * MIN, offset=90 * SEC, what="stall scan (stalled.scan)"),
    Job("sampler", 20, every=15 * MIN, what="usage sampler"),
    Job("dispatcher", 25, every=10 * MIN, paused="handler", what="dispatcher"),
    Job("quickview", 2, every=3 * MIN, pause_on_failure=False, what="quickview export"),
    Job("docs_import", 5, every=10 * MIN, what="docs import"),
    Job("prune_logs", 10, daily=(3, 17), what="log retention"),
    Job("prune_runs", 10, daily=(3, 27), what="runner run-record retention (runner_runs)"),
    # S9(b): the status snapshot rows (usage, next run, pause, running runs), once a minute and also while
    # paused (the pause is part of it); 30 s off the minute, between the other ticks' offsets
    Job("status_snapshot", 2, every=MIN, offset=30 * SEC, what="status snapshot (status_snapshot.py): the DB rows "
                                                               "the dashboard reads"),
    Job("usage_review", 30, every=60 * MIN, paused="handler", what="usage-model review"),
    Job("window_start", 90, window_start=True, paused="handler", on_interrupt="fail", session=True,
        what="window-start"),
    # the scheduled jobs check the pause themselves (the sampler samples while paused, D-239.4)
    Job("at", 30, at_jobs=True, paused="handler", on_interrupt="fail",
        what="at-jobs (scheduled_jobs: the weekly-reset samples, one-off reviews, the review chain's steps)"),
    # S6: the keep-alive watcher's passes (keepalive.runner_pass). The ticks only read and plan (a rerun
    # is safe); off the minute, never at the second of the other ticks (cron's grid, stall_scan's +90 s).
    # The watch runs while paused (detection only); the compaction check and the acts check the pause.
    Job("watch", 12, every=MIN, offset=15 * SEC,
        what="keep-alive watch: limit detection; the plans of the last stretch, the postponed / planned starts "
             "and the fill-up"),
    Job("run_readings", 10, every=5 * MIN, offset=50 * SEC, what="usage readings during a run (D-207)"),
    Job("compaction_check", 15, every=MIN, offset=45 * SEC, paused="handler",
        what="compaction check (D-238): is one due; a long-running review / request session (3c S2's detector)"),
    # the acts: each types into the task-manager session, at the time its plan step stored; a cut-off
    # one is never rerun (D-257), a drain holds it
    Job("last_stretch", 90, planned=True, paused="handler", on_interrupt="fail", session=True,
        what="last-stretch slot start (D-020/D-204)"),
    Job("deferred_start", 90, planned=True, paused="handler", on_interrupt="fail", session=True,
        what="postponed or planned session-window start (D-202/D-274)"),
    Job("fillup", 90, planned=True, paused="handler", on_interrupt="fail", session=True,
        what="fill-up start (D-212)"),
    Job("compaction", 65, planned=True, paused="handler", on_interrupt="fail", session=True,
        what="compaction: save, /compact, the hand-off back (D-238)"),
    # S7: the inbox (docs/phase3a_plan.md §5 S7). Both run while paused (D-186/D-244: the owner's own
    # message is no automation); a rerun after an interrupted one is safe (the message's own status
    # settles it: inbox.deliver can't apply twice, a stuck verify entry just resumes, §3.3/§3.4).
    Job("inbox", 5, poll=30 * SEC, what="inbox: triage, then deliver \"now\" messages (inbox_worker.process)"),
    Job("inbox_verify", 0.5, poll=5 * SEC,
        what="inbox: resume pending post-send verifications (inbox_worker.verify_tick)"),
    # phase 3b S5: run-now (D-153). Runs while paused (D-282); it types into or starts a session, so a
    # drain holds it and a restart during one fails it (D-257); one run per open request, at once
    Job("request", 90, requests=True, on_interrupt="fail", session=True,
        what="run-now requests (action_requests: work_on_now, continue_now, review_now; run_now.py)"),
)}
def _inbox_pending(conn: Any, now: datetime) -> bool:
    import inbox_worker
    return bool(inbox_worker.pending(conn, now))


def _inbox_verify_pending(conn: Any, now: datetime) -> bool:
    import inbox_worker
    return bool(inbox_worker.verify_pending(conn, now))


# S7: a job that only ticks while POLL_PENDING[name](conn, now) says there is work (Job.poll); no
# due time while there is none (_plan_poll), only a wake.
POLL_PENDING: dict[str, Callable[[Any, datetime], bool]] = {"inbox": _inbox_pending, "inbox_verify": _inbox_verify_pending}
# S7: a long-lived child the runner keeps alive (not a tick: no run row, no outer timeout) -> its
# argv. inbox_api.py serve only serves HTTP, stores the message and wakes this runner
# (§3.7); the runner's own `inbox` / `inbox_verify` jobs above do the triage, delivery and
# verification.
SUPERVISED: dict[str, tuple[str, ...]] = {"inbox_api": ("inbox_api.py", "serve")}
# What this image's runner runs: every job (§4: each cut-over step S2-S7 moved jobs here and removed
# their cron lines in the same commit; S9 removed the crontabs).
RUNNER_OWNS: frozenset[str] = frozenset({"stall_scan", "prune_runs", "status_snapshot", "sampler", "dispatcher",
                                         "quickview", "docs_import", "prune_logs", "usage_review", "at", "window_start",
                                         "inbox", "inbox_verify", "request"}
                                        | WATCHER_PASSES)


# ---------------------------------------------------------------- paths, log, small helpers

def run_dir() -> str:
    return os.path.join(runner_state.data_dir(), "run")


def lock_path() -> str:
    return os.path.join(run_dir(), "runner.lock")


def sock_path() -> str:
    return os.path.join(run_dir(), "runner.sock")


def log(msg: str) -> None:
    """A runner line: data/runner.log (+ app_log, component runner) and stdout (docker logs).
    When stdout IS runner.log (a shell redirect), it is written once. Never raises."""
    line = f"[{datetime.now(BERLIN):%Y-%m-%d %H:%M:%S %Z}] {msg}"
    path = runner_state.live_path(COMPONENT, runner_state.LOG_FILES)
    try:
        a, b = os.fstat(sys.stdout.fileno()), os.stat(path)
        same = (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except (AttributeError, OSError, ValueError):   # no fd (a test's StringIO), no log yet
        same = False
    try:
        print(line, flush=True)
        if not same:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as e:
        sys.stderr.write(f"runner log: {type(e).__name__}: {e}\n")
    runner_state.log_lines(COMPONENT, line)


def _iso(dt: datetime) -> str:
    s = store.iso(dt)
    assert s is not None
    return s


def _parse(s: object) -> Optional[datetime]:
    return store.parse_iso(s) if isinstance(s, str) and s else None


def _parse_text(s: object) -> Optional[datetime]:
    """_parse for text read from a log (a line that is not ours: None)."""
    try:
        return _parse(s)
    except ValueError:
        return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def next_tick(job: Job, now: datetime) -> datetime:
    """The job's first tick strictly after `now` (UTC grid / daily time)."""
    if job.every is not None:
        day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + job.offset
        step = job.every.total_seconds()
        k = math.floor((now - day).total_seconds() / step) + 1
        return day + timedelta(seconds=k * step)
    if job.daily is not None:
        t = now.astimezone(UTC).replace(hour=job.daily[0], minute=job.daily[1], second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)
    raise ValueError(f"{job.name} has no tick")


def at_policy(row: Optional[store.Row], job: Job) -> tuple[str, Optional[float]]:
    """A scheduled job's (on_interrupt, timeout minutes or None) from its payload; a field that is
    absent (a job stored before S4) is the "at" job's default (on_interrupt "fail": the safe side)."""
    try:
        p = json.loads(row["payload"]) if row else {}
    except ValueError:
        p = {}
    p = p if isinstance(p, dict) else {}
    oi = p.get("on_interrupt")
    t = p.get("timeout")
    return (oi if oi in actions.JOB_ON_INTERRUPT else job.on_interrupt,
            float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 else None)


def _check_pid(pid: int) -> None:
    """A pid to signal must be a real process: killpg(1, ...) is kill(-1, ...) (glibc), which signals every
    process of the user, and 0 is the caller's own group. A fake's pid reaching here is a bug: fail fast (D-235)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise ValueError(f"refusing to signal pid {pid!r}: not a real child process (kill(-1) would signal everything)")


def _signal_group(pid: int, sig: int) -> None:
    _check_pid(pid)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def wake(path: Optional[str] = None) -> bool:
    """Tell the runner to re-read the DB (one datagram). Never blocks or raises: if the runner
    is down a warning goes to stderr (its stale heartbeat is what alerts). -> sent.
    A full queue counts as sent: the runner has wakes waiting already (the container's
    net.unix.max_dgram_qlen is 10, a host's often 512)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            s.sendto(b"w", path or sock_path())
        return True
    except BlockingIOError:
        return True
    except OSError as e:
        sys.stderr.write(f"runner wake not sent ({type(e).__name__}: {e}); the runner re-reads the DB "
                         "within a minute anyway\n")
        return False


# ---------------------------------------------------------------- child processes

@dataclass
class ChildOutcome:
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool


class Spawner(Protocol):
    async def run(self, argv: list[str], timeout_s: float, on_pid: Callable[[int], object]) -> ChildOutcome: ...


class SubprocessSpawner:
    """Real children: own process group (start_new_session), stdout/stderr piped, the outer timeout
    (SIGTERM to the group, SIGKILL `term_grace` later). Cancelled (the runner stops): the group gets
    SIGTERM and the cancellation goes on (the run stays `running` for the next boot)."""
    def __init__(self, term_grace: float = TERM_GRACE) -> None:
        self.term_grace = term_grace

    async def run(self, argv: list[str], timeout_s: float, on_pid: Callable[[int], object]) -> ChildOutcome:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    start_new_session=True)
        comm = asyncio.ensure_future(proc.communicate())
        timed_out = False
        try:
            on_pid(proc.pid)
            try:
                out, err = await asyncio.wait_for(asyncio.shield(comm), timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                _signal_group(proc.pid, signal.SIGTERM)
                try:
                    out, err = await asyncio.wait_for(asyncio.shield(comm), self.term_grace)
                except asyncio.TimeoutError:
                    _signal_group(proc.pid, signal.SIGKILL)
                    out, err = await comm
        except BaseException:      # cancelled (the runner stops) or on_pid failed: never leave it running
            _signal_group(proc.pid, signal.SIGTERM)
            comm.cancel()
            raise
        return ChildOutcome(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                            timed_out)


# ---------------------------------------------------------------- the runner

@dataclass(frozen=True)
class Due:
    job: Job
    key: Optional[str] = None
    at: Optional[datetime] = None
    rerun: bool = False            # an interrupted run of it runs again (an at-job's is claimed already)


FailFn = Callable[..., Any]


class Runner:
    """The loop and its records. Everything outside (clock, children, alerts, the pause read, the
    schedule) is injectable for the tests; serve() is the asyncio loop, boot() the recovery."""

    def __init__(self, conn: Any, *, jobs: Optional[dict[str, Job]] = None, owns: Optional[frozenset[str]] = None,
                 spawner: Optional[Spawner] = None, clock: Optional[Callable[[], datetime]] = None,
                 boot_id: Optional[str] = None, log_fn: Optional[Callable[[str], None]] = None,
                 fail: Optional[FailFn] = None, paused: Optional[Callable[[], dict[str, Any]]] = None,
                 schedule_cfg: Optional[schedule.Config] = None, sock: Optional[str] = None,
                 stop_grace: float = STOP_GRACE,
                 supervise: Optional[dict[str, tuple[str, ...]]] = None) -> None:
        self.conn = conn
        self.jobs = dict(JOBS if jobs is None else jobs)
        self.owns = frozenset(RUNNER_OWNS if owns is None else owns)
        if self.owns - set(self.jobs):
            raise ValueError(f"unknown owned job(s) {sorted(self.owns - set(self.jobs))}")
        self.spawner: Spawner = spawner or SubprocessSpawner()
        self.clock = clock or _utcnow
        self.boot_id = boot_id or uuid.uuid4().hex
        self.log = log_fn or log
        self._fail: FailFn = fail or failstop.fail
        self._paused = paused or (lambda: failstop.paused())
        self.cfg = schedule_cfg
        self.sock = sock
        self.stop_grace = stop_grace
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.pids: dict[str, int] = {}
        # S7: a long-lived supervised child (SUPERVISED), not a tick: started once at boot and
        # again (alert + restart) whenever it exits before a stop was requested.
        self.supervise = dict(SUPERVISED if supervise is None else supervise)
        self.sup_tasks: dict[str, asyncio.Task[None]] = {}
        self.sup_pids: dict[str, int] = {}
        self.wake_event = asyncio.Event()
        self.scans = 0
        self.wakes = 0
        self.stopping = False
        self.stop_deadline = 0.0
        self.crash: Optional[BaseException] = None
        self.ws_anchor: Optional[datetime] = None
        self.last_beat: Optional[datetime] = None
        self.beat_mono = time.monotonic()
        self.logged: set[str] = set()
        self.reruns: list[tuple[str, str]] = []

    # ---- small parts

    def fail(self, subject: str, detail: str = "", *, pause: bool = True) -> None:
        self._fail(COMPONENT, subject, detail, log=self.log, pause=pause)

    def once(self, reason: str, msg: str) -> None:
        """Log `msg` once per `reason` (skips, refusals, overruns)."""
        if reason not in self.logged:
            self.logged.add(reason)
            self.log(msg)

    def job_set(self, name: str, **fields: Any) -> None:
        actions.runner_job_set(self.conn, name, actor=actions.RUNNER_ACTOR, via=VIA, **fields)

    def timeout_s(self, job: Job) -> float:
        key = actions.RUNNER_TIMEOUT + job.name
        minutes = actions.get_setting(self.conn, key) if key in actions.SETTINGS else job.timeout_min
        return float(minutes) * 60

    def catchup(self) -> timedelta:
        return timedelta(minutes=float(actions.get_setting(self.conn, "runner_start_catchup")))

    def schedule_cfg(self) -> schedule.Config:
        return self.cfg if self.cfg is not None else schedule.load()

    def heartbeat(self, now: datetime, force: bool = False) -> None:
        self.beat_mono = time.monotonic()
        if force or self.last_beat is None or now - self.last_beat >= HEARTBEAT_EVERY:
            self.job_set(actions.RUNNER_ROW, heartbeat_at=_iso(now))
            self.last_beat = now

    def _next_due(self, name: str) -> Optional[datetime]:
        row = store.get_runner_job(self.conn, name)
        return _parse(row["next_due"]) if row else None

    def boot(self) -> None:
        now = self.clock()
        st = actions.runner_state_get(self.conn)
        row = store.get_runner_job(self.conn, actions.RUNNER_ROW)
        prev_beat = _parse(row["heartbeat_at"]) if row else None
        for r in store.running_runs(self.conn):
            if r["boot_id"] != self.boot_id:
                self._interrupted(r, now)
        prev = st.get("boot") if isinstance(st.get("boot"), dict) else None
        crashed = st.get("crashed") if isinstance(st.get("crashed"), dict) else None
        if prev is not None and st.get("clean_exit") is not True and crashed is not None \
                and crashed.get("boot_id") == prev.get("boot_id"):
            self.log(f"the previous runner (boot {str(prev.get('boot_id'))[:8]}) crashed at {crashed.get('at')}; "
                     "it reported that itself (alert + pause)")
        elif prev is not None and st.get("clean_exit") is not True:
            self.fail("the previous runner did not exit cleanly",
                      f"boot {str(prev.get('boot_id'))[:8]} (started {prev.get('started_at')}, pid {prev.get('pid')}) "
                      f"ended without its clean exit; its last heartbeat {_iso(prev_beat) if prev_beat else 'none'}: "
                      "a crash, a kill or a host reboot (D-257)")
        if st.get("drain") is not None:
            self.log(f"the drain {json.dumps(st['drain'])} is lifted (a new boot catches up)")
        actions.runner_state_put(self.conn, {"boot": {"boot_id": self.boot_id, "pid": os.getpid(),
                                                      "started_at": _iso(now)},
                                             "clean_exit": False, "drain": None, "crashed": None},
                                 actor=actions.RUNNER_ACTOR, via=VIA)
        n = 0
        for name in sorted(self.owns):
            job = self.jobs[name]
            if job.keyed or job.poll is not None:    # a poll job's first plan() finds its own due time
                continue
            nd = self._next_due(name)
            if nd is None:
                self.job_set(name, next_due=_iso(next_tick(job, now)))
            elif nd <= now:
                self.job_set(name, next_due=_iso(now + n * STAGGER))
                n += 1
        self.ws_anchor = prev_beat if prev_beat is not None and prev_beat < now else now
        self.heartbeat(now, force=True)
        self.log(f"boot {self.boot_id[:8]} pid {os.getpid()}: owns {', '.join(sorted(self.owns)) or 'nothing'}; "
                 f"{n} missed tick(s) caught up")

    def _interrupted(self, r: store.Row, now: datetime) -> None:
        """A run another boot left running: its leftover child group is ended, the row becomes
        interrupted, then the job's policy (rerun now / alert + pause, D-257)."""
        self._reap(r)
        actions.run_end(self.conn, int(r["id"]), "interrupted", actor=str(r["actor"]), via=VIA,
                        error=f"interrupted by a runner restart (boot {str(r['boot_id'])[:8]} -> {self.boot_id[:8]})")
        job = self.jobs.get(str(r["job"]))
        what = str(r["job"]) + (f" {r['key']}" if r["key"] else "")
        policy = job.on_interrupt if job is not None else "fail"
        if job is not None and job.at_jobs:
            policy = at_policy(store.get_job(self.conn, str(r["key"])) if r["key"] else None, job)[0]
        if job is not None and job.requests and r["key"]:     # D-257: a restart during a request fails it
            self._request_failed(str(r["key"]), int(r["id"]), "a runner restart cut its run off (D-257)")
        if job is None or policy == "fail":
            self.fail(f"{r['job']} interrupted by a runner restart",
                      f"run {r['id']} ({what}, started {r['started_at']}) was cut off by a runner restart; it is not "
                      "rerun: a start or compaction cut off mid-way leaves the session in an unknown state (D-257)")
        elif r["key"] is not None:
            self.reruns.append((job.name, str(r["key"])))
            self.log(f"{what}: interrupted by a restart, rerun now")
        else:
            self.job_set(job.name, next_due=_iso(now))
            self.log(f"{what}: interrupted by a restart, due now")

    def _reap(self, r: store.Row) -> None:
        """End the child group of a run another boot left running, if it is still there and still
        one of ours (its command line names runner_jobs.py: a pid can be reused)."""
        pid = r["pid"]
        if not isinstance(pid, int) or pid <= 1:
            return
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ")
        except OSError:
            return
        if b"runner_jobs.py" not in cmd:
            return
        self.log(f"run {r['id']} ({r['job']}): its child {pid} is still running; ending its process group")
        _signal_group(pid, signal.SIGTERM)
        deadline = time.monotonic() + TERM_GRACE
        while time.monotonic() < deadline and os.path.exists(f"/proc/{pid}"):
            time.sleep(0.2)
        _signal_group(pid, signal.SIGKILL)

    # ---- planning

    def plan(self, now: datetime) -> list[Due]:
        """The runs due at `now` (owned jobs only); advances what is skipped (an overrun tick)."""
        out: list[Due] = []
        for name in sorted(self.owns):
            job = self.jobs[name]
            if job.window_start:
                d = self._plan_window_start(job, now)
                if d is not None:
                    out.append(d)
                continue
            if job.at_jobs:
                out.extend(self._plan_at(job, now))
                continue
            if job.planned:
                d = self._plan_planned(job, now)
                if d is not None:
                    out.append(d)
                continue
            if job.requests:
                out.extend(self._plan_requests(job, now))
                continue
            if job.poll is not None:
                d = self._plan_poll(job, now)
                if d is not None:
                    out.append(d)
                continue
            nd = self._next_due(name)
            if nd is None:
                self.job_set(name, next_due=_iso(next_tick(job, now)))
                continue
            if nd > now:
                continue
            if name in self.tasks:
                self.once(f"overrun:{name}:{_iso(nd)}", f"{name} is still running at its next tick; that tick is skipped")
                self.job_set(name, next_due=_iso(next_tick(job, now)))
                continue
            out.append(Due(job))
        keep: list[tuple[str, str]] = []
        for name, key in self.reruns:
            job = self.jobs[name]
            if job.slot(key) in self.tasks or any(job.slot(key) == d.job.slot(d.key) for d in out):
                keep.append((name, key))
            else:
                out.append(Due(job, key, rerun=True))
        self.reruns = keep
        return out

    def _plan_at(self, job: Job, now: datetime) -> list[Due]:
        """The pending scheduled jobs whose run_at has come (not running already), earliest first;
        the job row's next_due becomes the earliest run_at still ahead (the loop wakes for it; `at -t`
        wakes it for a new one)."""
        out: list[Due] = []
        ahead: Optional[datetime] = None
        for row in store.list_jobs(self.conn, "pending"):
            key = str(row["id"])
            at = _parse_text(row["run_at"])
            if at is None:
                if f"at-bad:{key}" not in self.logged:      # reported once per boot
                    self.once(f"at-bad:{key}", f"at {key}: its run_at {row['run_at']!r} is not a time; not run")
                    self.fail("a scheduled job has no valid run time", f"scheduled job {key}: run_at {row['run_at']!r}")
                continue
            if at > now:
                ahead = at if ahead is None else min(ahead, at)
            elif job.slot(key) not in self.tasks:
                out.append(Due(job, key, at))
        want = ahead.replace(microsecond=0) if ahead else None
        if self._next_due(job.name) != want:
            self.job_set(job.name, next_due=_iso(ahead) if ahead else None)
        return out

    def _plan_planned(self, job: Job, now: datetime) -> Optional[Due]:
        """The act its pass's plan step stored (planned_act) for one of the registry's task-managers
        (store.manager_sessions, rank order; phase 3b S3: one plan per session, acts.<job>:<sid8>),
        due once its time has come and it is not running or settled (a run row under its key: it ran,
        or it was cut off and never reruns). One act of a job runs at a time: the earliest due (a tie:
        the better-ranked project), the next one when it ends. The job row's next_due is the earliest
        time still ahead (the loop wakes for it). A stored act that is not valid is an error (alert +
        pause), reported once per boot."""
        ready: list[tuple[datetime, str]] = []
        ahead: Optional[datetime] = None
        for t in store.manager_sessions(self.conn):
            name = runner_state.act_name(job.name, str(t["session"]))
            try:
                act = planned_act(self.conn, name)
            except ValueError as e:
                if f"plan-bad:{name}:{e}" not in self.logged:
                    self.once(f"plan-bad:{name}:{e}", f"{name}: its stored plan is not valid ({e}); not run")
                    self.fail("a planned act is not valid", f"{name}: {e}")
                continue
            if act is None:
                continue
            run, at = act
            if at > now:
                ahead = at if ahead is None else min(ahead, at)
            elif not store.key_runs(self.conn, job.name, run):
                ready.append((at, run))
        want = ahead.replace(microsecond=0) if ahead else None
        if self._next_due(job.name) != want:
            self.job_set(job.name, next_due=_iso(ahead) if ahead else None)
        if not ready or job.name in self.tasks:
            return None
        at, run = min(ready, key=lambda r: r[0])
        return Due(job, run, at)

    def _plan_requests(self, job: Job, now: datetime) -> list[Due]:
        """Phase 3b S5 (run-now, D-153): every open request without a run yet, oldest first, due now
        (no due time: request.add's caller wakes the runner, the loop's MAX_SLEEP is the backstop).
        An open request that has a run already is never run again: a run still going is its slot's (or
        the boot's, as interrupted), one that has finished without deciding it (finish() died on a DB
        error between run_end and the request's write) is decided failed here, at the next plan after
        the boot, so request.add does not find it open for ever. A DB error here ends the runner's
        loop like any other (D-234/D-235)."""
        out: list[Due] = []
        for row in reversed(store.list_requests(self.conn, "open", limit=200)):
            key = str(row["id"])
            if job.slot(key) in self.tasks:
                continue
            runs = store.key_runs(self.conn, job.name, key)
            if runs:
                if all(r["status"] != "running" for r in runs):
                    self._request_failed(key, int(runs[-1]["id"]), "ended without deciding the request")
                continue
            out.append(Due(job, key, now))
        if self._next_due(job.name) is not None:
            self.job_set(job.name, next_due=None)
        return out

    def _request_failed(self, key: str, run_id: int, why: str) -> None:
        """A request whose run ended without deciding it (failed, timed out, cut off by a restart):
        recorded failed (request.finish, audited, D-154) while it is still open. The run's own
        failure is reported by the caller (alert + pause, D-282/D-257)."""
        r = store.get_request(self.conn, int(key))
        if r is None or r["status"] != "open":     # decided by its handler (a started one stays started)
            return
        actions.perform(self.conn, "request.finish", {"request_id": int(key), "status": "failed",
                                                      "reason": f"its run {run_id} {why}", "run_id": run_id},
                        actor="runner:request", via=VIA)

    def _plan_poll(self, job: Job, now: datetime) -> Optional[Due]:
        """S7 (`inbox`, `inbox_verify`): due now if POLL_PENDING[job.name] says there is work and
        either nothing is due yet (the first pending work, or a wake after it was dormant) or its
        `poll` cooldown since the last run has passed; no due time (dormant, only a wake starts it
        again) while there is none. start() sets the next due time (now + job.poll) when it runs."""
        pending = POLL_PENDING[job.name](self.conn, now)
        nd = self._next_due(job.name)
        if not pending:
            if nd is not None:
                self.job_set(job.name, next_due=None)
            return None
        if (nd is None or nd <= now) and job.name not in self.tasks:
            return Due(job)
        return None

    def _settled(self, job: Job, key: str) -> bool:
        """The keyed run (job, key) was decided already: a run row (for a job that never reruns an
        interrupted run, also an interrupted one), or the keep-alive's own dedup for a window start
        (handled, its "window_starts" claim, a postponed start)."""
        rows = store.key_runs(self.conn, job.name, key)
        if any(job.on_interrupt == "fail" or r["status"] != "interrupted" for r in rows):
            return True
        if job.window_start:
            ka = store.state_rows(self.conn, "keepalive")
            if actions._state_json(["handled", key]) in ka or actions._state_json(["window_starts", key]) in ka:
                return True
            if actions._state_json([key]) in store.state_rows(self.conn, "keepalive.deferred"):
                return True
        return False

    def _plan_window_start(self, job: Job, now: datetime) -> Optional[Due]:
        """The session-window starts since the last pass (ws_anchor): the latest one less than
        runner_start_catchup old that is not settled is due (once); an older unsettled one is a
        missed start: recorded `skipped` (so it is reported once) and an alert + pause (D-257)."""
        cfg = self.schedule_cfg()
        nxt = schedule.next_session_start(now, cfg)
        if self._next_due(job.name) != (nxt.replace(microsecond=0) if nxt else None):
            self.job_set(job.name, next_due=_iso(nxt) if nxt else None)
        anchor = self.ws_anchor or now
        starts = [s for s, _ in schedule.session_slots(anchor, now + TINY, cfg)]
        grace = self.catchup()
        pending: Optional[tuple[datetime, str]] = None
        missed: list[tuple[datetime, str]] = []
        for s in starts:
            key = schedule.start_key(s, cfg)
            if self._settled(job, key):
                continue
            if s == starts[-1] and now - s < grace:
                pending = (s, key)
            else:
                missed.append((s, key))
        for s, key in missed:
            actions.run_begin(self.conn, boot_id=self.boot_id, job=job.name, key=key, timeout_s=None,
                              actor=f"runner:{job.name}", via=VIA, rerun=False,
                              skipped=f"missed start {_iso(s)}: not started within runner_start_catchup "
                                      f"({grace}) of it (the runner was down or busy)")
        if missed:
            self.fail("missed session-window start",
                      f"{', '.join(k for _, k in missed)}: the runner did not start it within {grace} (it was "
                      "not running, or drained); it is not started late (D-257)")
        if pending is None or job.name in self.tasks:
            self.ws_anchor = now if pending is None else pending[0] - TINY
            return None
        self.ws_anchor = pending[0] - TINY      # until its run row exists (a drain may hold it)
        return Due(job, pending[1], pending[0])

    # ---- runs

    def _advance(self, job: Job, now: datetime) -> datetime:
        """The next due time stored right when a non-keyed job starts: the grid tick (`every` /
        `daily`), or, for a poll job (S7), a `poll`-long cooldown from now (_plan_poll re-admits it
        once that passes and POLL_PENDING still says there is work; none: it falls dormant again)."""
        return now + job.poll if job.poll is not None else next_tick(job, now)

    def start(self, due: Due, now: datetime) -> Optional[int]:
        """Record and spawn one run (or record it skipped). -> the run id, or None."""
        job, key = due.job, due.key
        actor = f"runner:{job.name}"
        label = job.name + (f" {key}" if key else "")
        if job.at_jobs:
            return self._start_at(due, now)
        if job.paused == "skip":
            p = self._paused()
            if p.get("paused"):
                why = f"automation is paused: {p.get('reason') or 'no reason given'}"
                actions.run_begin(self.conn, boot_id=self.boot_id, job=job.name, key=key, timeout_s=None,
                                  actor=actor, via=VIA, rerun=job.on_interrupt == "rerun", skipped=why)
                self.once(f"skip:{job.name}:{p.get('reason')}", f"{label} skipped: {why}")
                if not job.keyed:
                    self.job_set(job.name, next_due=_iso(self._advance(job, now)))
                return None
        timeout = self.timeout_s(job)
        run_id, why_not = actions.run_begin(self.conn, boot_id=self.boot_id, job=job.name, key=key, timeout_s=timeout,
                                            actor=actor, via=VIA, session=job.session,
                                            rerun=job.on_interrupt == "rerun")
        if run_id is None:
            self.once(f"refused:{label}:{why_not}", f"{label} not started: {why_not}")
            if not job.keyed:
                self.job_set(job.name, next_due=_iso(self._advance(job, now)))
            return None
        if not job.keyed:
            self.job_set(job.name, next_due=_iso(self._advance(job, now)))
        self.log(f"start {label} (run {run_id}, timeout {timeout:g} s)")
        self.tasks[job.slot(key)] = asyncio.get_running_loop().create_task(self._run(job, key, run_id, timeout))
        return run_id

    def _start_at(self, due: Due, now: datetime) -> Optional[int]:
        """An at-job's run: its payload's timeout and interrupt policy; its begin claims the
        scheduled job (a rerun's is claimed already); a "fail" job is held by a drain (it stays
        pending and runs once the drain is over)."""
        job, key = due.job, due.key
        assert key is not None
        label = f"{job.name} {key}"
        on_interrupt, minutes = at_policy(store.get_job(self.conn, key), job)
        timeout = minutes * 60 if minutes is not None else self.timeout_s(job)
        run_id, why_not = actions.run_begin(self.conn, boot_id=self.boot_id, job=job.name, key=key, timeout_s=timeout,
                                            actor=f"runner:{job.name}", via=VIA, session=on_interrupt == "fail",
                                            rerun=on_interrupt == "rerun", claim=None if due.rerun else key)
        if run_id is None:
            self.once(f"refused:{label}:{why_not}", f"{label} not started: {why_not}")
            return None
        late = f", {(now - due.at).total_seconds():.0f} s after its time" if due.at and now - due.at > MIN else ""
        self.log(f"start {label} (run {run_id}, timeout {timeout:g} s, on interrupt {on_interrupt}"
                 f"{', a rerun' if due.rerun else ''}{late})")
        self.tasks[job.slot(key)] = asyncio.get_running_loop().create_task(self._run(job, key, run_id, timeout))
        return run_id

    def _argv(self, job: Job, key: Optional[str]) -> list[str]:
        if job.argv is not None:
            return list(job.argv)
        return [sys.executable, "-u", RUNNER_JOBS, job.name] + (["--key", key] if key else [])

    async def _run(self, job: Job, key: Optional[str], run_id: int, timeout: float) -> None:
        t0 = time.monotonic()

        slot = job.slot(key)

        def on_pid(pid: int) -> None:
            self.pids[slot] = pid
            actions.run_pid(self.conn, run_id, pid, actor=f"runner:{job.name}", via=VIA)
        try:
            try:
                out = await self.spawner.run(self._argv(job, key), timeout, on_pid)
            except OSError as e:          # the child could not be started: a failed run
                out = ChildOutcome(None, "", f"could not start the child: {type(e).__name__}: {e}", False)
            self.finish(job, key, run_id, out, timeout, time.monotonic() - t0)
        except asyncio.CancelledError:
            raise
        except BaseException as e:   # noqa: BLE001 - the runner's own failure (a record not written): it crashes
            self.crash = e
        finally:
            self.tasks.pop(slot, None)
            self.pids.pop(slot, None)
            self.wake_event.set()

    def finish(self, job: Job, key: Optional[str], run_id: int, out: ChildOutcome, timeout: float,
               duration: float) -> str:
        """Record a run's end from its child's outcome, report what the handler did not. -> status."""
        label = job.name + (f" {key}" if key else "")
        tail = out.stderr[-actions.RUN_ERROR_MAX:]
        if out.stderr and job.argv is None:
            self._append_log(job, out.stderr)
        result: Optional[str] = None
        report = True
        next_due: Optional[datetime] = None
        if out.timed_out:
            status, error = "timeout", f"timed out after {timeout:g} s; its process group was ended\n{tail}"
        elif out.returncode == 0:
            line = (out.stdout.strip().splitlines() or [""])[-1]
            try:
                res = json.loads(line)
                if not isinstance(res, dict):
                    raise ValueError("not an object")
            except ValueError:
                status, error = "failed", f"exit 0 without its JSON result line (got {line[:200]!r})\n{tail}"
            else:
                status, error, report, result = "ok", None, False, line
                next_due = _parse(res.get("next_due"))
        elif out.returncode == EXIT_REPORTED:
            status, error, report = "failed", f"failed, reported by the handler\n{tail}", False
        elif out.returncode is not None and out.returncode < 0:
            status, error = "failed", f"ended by signal {-out.returncode}\n{tail}"
        else:
            status, error = "failed", f"exit {out.returncode}\n{tail}"
        undecided = False
        if job.requests and key is not None:
            req = store.get_request(self.conn, int(key))
            undecided = req is not None and req["status"] == "open"
            if undecided and status == "ok":       # a handler that returned without deciding it: an error
                status, report, result = "failed", True, None
                error = "exit 0, but its request is still open (the handler did not decide it)"
        actions.run_end(self.conn, run_id, status, actor=f"runner:{job.name}", via=VIA, result=result, error=error)
        if undecided:
            assert key is not None
            self._request_failed(key, run_id, f"ended {status} without deciding it")
        row = store.get_runner_job(self.conn, job.name) or {}
        fails = 0 if status == "ok" else int(row.get("consecutive_failures") or 0) + 1
        fields: dict[str, Any] = {"last_run_id": run_id, "last_status": status, "consecutive_failures": fails}
        if status == "ok":
            fields["last_ok_at"] = _iso(self.clock())
        planned = _parse(row.get("next_due"))
        if next_due is not None and not job.keyed and (planned is None or next_due < planned):
            fields["next_due"] = _iso(next_due)
        self.job_set(job.name, **fields)
        self.log(f"end {label} (run {run_id}): {status} after {duration:.1f} s"
                 + (f"; {error.splitlines()[0]}" if error and status != "ok" else ""))
        if report:
            what = "timed out" if status == "timeout" else "failed"
            self.fail(f"{job.name} {what}", f"run {run_id} ({label}): {error or ''}"[:1200], pause=job.pause_on_failure)
        return status

    def _append_log(self, job: Job, text: str) -> None:
        """The child's stderr into the job's log file, as the cron line's 2>&1 did."""
        try:
            path = runner_jobs.log_path(job.name)
        except KeyError:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text if text.endswith("\n") else text + "\n")
        except OSError as e:
            self.log(f"{job.name}: its stderr could not be added to {path} ({type(e).__name__}: {e})")

    # ---- a long-lived supervised child (S7: inbox_api)

    def start_supervised(self, name: str) -> None:
        """Start one supervised child; never while stopping, never twice."""
        if self.stopping or name in self.sup_tasks:
            return
        argv = [sys.executable, "-u", os.path.join(HERE, self.supervise[name][0])] + list(self.supervise[name][1:])
        self.log(f"start {name} (supervised: {' '.join(self.supervise[name])})")
        self.sup_tasks[name] = asyncio.get_running_loop().create_task(self._supervise(name, argv))

    async def _supervise(self, name: str, argv: list[str]) -> None:
        """Run one supervised child until it exits (no timeout); then alert + pause (every time) and
        restart it after SUPERVISE_DELAY (no tight loop for a child that dies at once, e.g. a missing
        key), unless a stop was requested meanwhile (then its exit is expected: request_stop already
        signalled it)."""
        def on_pid(pid: int) -> None:
            self.sup_pids[name] = pid
        try:
            out = await self.spawner.run(argv, math.inf, on_pid)
        except asyncio.CancelledError:
            raise
        except BaseException as e:   # noqa: BLE001 - the runner's own failure: it crashes (as a job run does)
            self.crash = e
            return
        finally:
            self.sup_tasks.pop(name, None)
            self.sup_pids.pop(name, None)
        if not self.stopping:
            tail = out.stderr[-actions.RUN_ERROR_MAX:]
            self.log(f"{name} (supervised) exited ({out.returncode}){': ' + tail.splitlines()[0] if tail else ''}")
            self.fail(f"{name} was not running", f"it exited ({out.returncode}); the runner starts it again in "
                      f"{SUPERVISE_DELAY:g} s\n{tail}"[:1200])
            await asyncio.sleep(SUPERVISE_DELAY)
            self.start_supervised(name)
        self.wake_event.set()

    # ---- the loop

    def sleep_for(self, now: datetime) -> float:
        """Seconds until the earliest due time of an owned job (at most MAX_SLEEP)."""
        best = MAX_SLEEP
        for name in self.owns:
            nd = self._next_due(name)
            if nd is not None and name not in self.tasks:
                best = min(best, max(0.0, (nd - now).total_seconds()))
        return best

    def request_stop(self) -> None:
        if not self.stopping:
            self.stopping = True
            self.stop_deadline = time.monotonic() + self.stop_grace
            self.log(f"stop requested: no new runs; {len(self.tasks)} running get up to {self.stop_grace:g} s")
            for name, pid in list(self.sup_pids.items()):   # never finish on their own: end them now
                self.log(f"stop: ending {name} (supervised, pid {pid})")
                _signal_group(pid, signal.SIGTERM)
        self.wake_event.set()

    def on_datagram(self, sock: socket.socket) -> None:
        """Drain every queued wake datagram; one event however many (coalescing)."""
        while True:
            try:
                sock.recv(64)
            except (BlockingIOError, InterruptedError):
                break
            self.wakes += 1
        self.wake_event.set()

    def bind(self) -> socket.socket:
        path = self.sock or sock_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)          # a stale socket of a dead runner (the lock says no other runs)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(path)
        s.setblocking(False)
        return s

    async def serve(self, install_signals: bool = True) -> int:
        """The loop until a stop (SIGTERM / request_stop). -> exit code 0; raises on the runner's
        own failure (the caller reports it and exits non-zero, without the clean exit)."""
        loop = asyncio.get_running_loop()
        self.wake_event = asyncio.Event()     # of this loop (Python 3.9 binds an Event to a loop at creation)
        if install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.request_stop)
        sock = self.bind()
        loop.add_reader(sock.fileno(), self.on_datagram, sock)
        for name in self.supervise:          # S7: inbox_api, started fresh each boot (no alert)
            self.start_supervised(name)
        try:
            while True:
                self.wake_event.clear()
                if self.crash is not None:
                    raise self.crash
                now = self.clock()
                self.heartbeat(now)
                if self.stopping:
                    if (not self.tasks and not self.sup_tasks) or time.monotonic() >= self.stop_deadline:
                        break
                    delay = max(0.0, min(MAX_SLEEP, self.stop_deadline - time.monotonic()))
                else:
                    self.scans += 1
                    for due in self.plan(now):
                        self.start(due, now)
                    delay = self.sleep_for(now)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.wake_event.wait(), delay)
            await self._stop_children()
        finally:
            loop.remove_reader(sock.fileno())
            sock.close()
            with contextlib.suppress(OSError):
                os.unlink(self.sock or sock_path())
        actions.runner_state_put(self.conn, {"clean_exit": True, "exited_at": _iso(self.clock())},
                                 actor=actions.RUNNER_ACTOR, via=VIA)
        self.log(f"boot {self.boot_id[:8]}: clean exit")
        return 0

    async def _stop_children(self) -> None:
        """Runs still going at the end of the stop grace: their groups get SIGTERM, their rows stay
        `running` (the next boot records them interrupted and applies the job's policy). A
        supervised child (request_stop already sent it SIGTERM) is cancelled the same way, with no
        row to leave behind."""
        if self.tasks:
            self.log(f"stop: {', '.join(sorted(self.tasks))} still running after {self.stop_grace:g} s; ended, "
                     "left for the next boot (interrupted)")
        if self.sup_tasks:
            self.log(f"stop: {', '.join(sorted(self.sup_tasks))} (supervised) still running; ended")
        tasks = list(self.tasks.values()) + list(self.sup_tasks.values())
        if not tasks:
            return
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------- watchdog, lock, watcher check

class Watchdog(threading.Thread):
    """Exits the process (WATCHDOG_EXIT) when the loop has not passed for `stale` seconds: a
    wedged loop (a synchronous call that blocks the asyncio loop, a hung DB write). Since S8 the
    runner is the container's main process (docker/entrypoint.sh execs it) and this thread the only
    watchdog: the exit ends the container and `restart: unless-stopped` starts it again; the new
    boot finds no clean exit and alerts + pauses (D-257). The thread alerts nothing itself: a
    wedged process may not reach the DB or the bridge, and the exit must not wait for them."""
    def __init__(self, beat: Callable[[], float], stale: float = STALE_LOOP, every: float = WATCHDOG_EVERY,
                 exit_fn: Callable[[int], object] = os._exit, log_fn: Callable[[str], None] = log) -> None:
        super().__init__(name="runner-watchdog", daemon=True)
        self.beat, self.stale, self.every, self.exit_fn, self.log_fn = beat, stale, every, exit_fn, log_fn

    def stuck(self, now: Optional[float] = None) -> bool:
        return (time.monotonic() if now is None else now) - self.beat() > self.stale

    def run(self) -> None:
        while True:
            time.sleep(self.every)
            if self.stuck():
                self.log_fn(f"ERROR: runner: the loop has not passed for more than {self.stale:g} s; exiting "
                            f"({WATCHDOG_EXIT}) so the container restarts")
                self.exit_fn(WATCHDOG_EXIT)
                return


def acquire_lock(path: Optional[str] = None) -> Optional[int]:
    """The single-instance lock (flock): its fd (keep it open), or None if another runner holds it."""
    p = path or lock_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _proc_argvs(proc: str = "/proc") -> Iterable[tuple[int, list[str]]]:
    """(pid, argv) of every process in this container / host (a zombie has an empty argv)."""
    for d in os.listdir(proc) if os.path.isdir(proc) else []:
        if not d.isdigit():
            continue
        try:
            with open(os.path.join(proc, d, "cmdline"), "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        yield int(d), argv


def _is_serve(argv: list[str]) -> bool:
    return any(a.endswith("runner.py") and argv[i + 1:i + 2] == ["serve"] for i, a in enumerate(argv[:4]))


def runner_pids(proc: str = "/proc") -> list[int]:
    """Pids of live `runner.py serve` processes (not this one; not a zombie)."""
    return [pid for pid, argv in _proc_argvs(proc) if pid != os.getpid() and _is_serve(argv)]


RUNNER_MODES = ("runner",)                # AFCLAUDE_SCHEDULER values with the resident runner (as export_quickview)


def planned_act(conn: Any, name: str) -> Optional[tuple[str, datetime]]:
    """The next act of the planned job `name` (S6): (its run key, when) from the keep-alive's runner
    state (PLANS_COMPONENT, acts.<name>: {key, at, run}, written by the pass's plan step), or None. `name`
    is runner_state.act_name(job, session): <job>:<sid8>.
    A stored entry that is not valid raises ValueError."""
    raw = store.state_rows(conn, PLANS_COMPONENT).get(actions._state_json(["acts", name]))
    if raw is None:
        return None
    ent = json.loads(raw)
    if ent is None:
        return None
    if not isinstance(ent, dict):
        raise ValueError(f"acts.{name} is not an object: {raw[:120]}")
    run, at = ent.get("run"), _parse_text(ent.get("at"))
    if not isinstance(run, str) or not actions.RUN_KEY_RE.match(run) or at is None:
        raise ValueError(f"acts.{name} has no valid run key and time: {raw[:200]}")
    return run, at


# ---------------------------------------------------------------- status, drain

def status(conn: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or _utcnow()
    rows = store.runner_job_rows(conn)
    beat = _parse((rows.get(actions.RUNNER_ROW) or {}).get("heartbeat_at"))
    jobs = []
    for name, job in JOBS.items():
        r = rows.get(name) or {}
        jobs.append({"job": name, "owned": name in RUNNER_OWNS,
                     "owner": "runner" if name in RUNNER_OWNS else None,
                     "next_due": r.get("next_due"),
                     "last_status": r.get("last_status"), "last_ok_at": r.get("last_ok_at"),
                     "consecutive_failures": r.get("consecutive_failures", 0)})
    st = actions.runner_state_get(conn)
    tms: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []
    for t in store.manager_sessions(conn):      # phase 3b S3: the registry's per-session plans (acts.<job>:<sid8>)
        tms.append({"project": t["name"], "session": str(t["session"])[:8], "paused": t["paused"]})
        for jname, job in JOBS.items():
            if not job.planned:
                continue
            key = runner_state.act_name(jname, str(t["session"]))
            try:
                act = planned_act(conn, key)
            except ValueError as e:
                plans.append({"act": key, "project": t["name"], "error": str(e)[:200]})
                continue
            if act is not None:
                plans.append({"act": key, "project": t["name"], "run": act[0], "at": _iso(act[1])})
    return {"heartbeat_at": _iso(beat) if beat else None, "task_managers": tms, "plans": plans,
            "heartbeat_age_s": round((now - beat).total_seconds()) if beat else None,
            "boot": st.get("boot"), "clean_exit": st.get("clean_exit"), "drain": actions.runner_drain(conn),
            "jobs": jobs, "running": store.running_runs(conn),
            "recent_failures": [r for r in store.recent_runs(conn, 50) if r["status"] in ("failed", "timeout",
                                                                                           "interrupted")][:10]}


def print_status(s: dict[str, Any]) -> None:
    age = s["heartbeat_age_s"]
    print(f"heartbeat: {s['heartbeat_at'] or 'never'}" + (f" ({age} s ago)" if age is not None else ""))
    b = s["boot"] or {}
    print(f"boot: {str(b.get('boot_id', '-'))[:8]} pid {b.get('pid', '-')} since {b.get('started_at', '-')}; "
          f"clean exit: {s['clean_exit']}; drain: {s['drain']['until'] if s['drain'] else 'none'}")
    for j in s["jobs"]:
        print(f"  {j['job']:<13} {j['owner'] or '-':<6} next {j['next_due'] or '-':<24} "
              f"last {j['last_status'] or '-':<9} ok {j['last_ok_at'] or '-':<21} fails {j['consecutive_failures']}")
    for t in s["task_managers"]:
        print(f"task-manager: {t['project']} {t['session']}" + (" (paused)" if t["paused"] else ""))
    for p in s["plans"]:
        print(f"  plan {p['act']:<26} " + (f"ERROR {p['error']}" if "error" in p else f"at {p['at']} run {p['run']}"))
    for r in s["running"]:
        print(f"running: run {r['id']} {r['job']} {r['key'] or ''} since {r['started_at']} pid {r['pid']}")
    for r in s["recent_failures"]:
        print(f"recent {r['status']}: run {r['id']} {r['job']} {r['key'] or ''} at {r['started_at']}: "
              f"{(r['error'] or '').splitlines()[0][:120] if r['error'] else ''}")


def session_jobs() -> list[Job]:
    return [j for j in JOBS.values() if j.session]


def _held_by_drain(conn: Any, r: store.Row) -> bool:
    """A running run a drain waits for: a session job's, or an at-job's whose interruption fails."""
    job = JOBS.get(str(r["job"]))
    if job is None:
        return False
    if job.at_jobs:
        return at_policy(store.get_job(conn, str(r["key"])) if r["key"] else None, job)[0] == "fail"
    return job.session


def drain(conn: Any, max_wait: Optional[timedelta] = None, *, poll: float = DRAIN_POLL,
          clock: Callable[[], datetime] = _utcnow, sleep: Callable[[float], object] = time.sleep,
          out: Callable[[str], object] = print) -> int:
    """Before a deploy (§3.4.5): no new run that acts on a session (until the longest such timeout
    + DRAIN_MARGIN, or the next runner boot lifts it), then wait until none is running. An at-job
    whose interruption fails (on_interrupt "fail": a one-off review start) counts as one (S4).
    -> 0 drained, 1 still running after max_wait (do not deploy)."""
    now = clock()
    held = session_jobs() + [j for j in JOBS.values() if j.at_jobs]
    longest = max((float(actions.get_setting(conn, actions.RUNNER_TIMEOUT + j.name)) for j in held), default=0.0)
    span = timedelta(minutes=longest) + DRAIN_MARGIN
    until = now + span
    actions.runner_state_put(conn, {"drain": {"until": _iso(until), "at": _iso(now)}}, actor="cli", via="cli")
    wake()
    deadline = now + (max_wait if max_wait is not None else span)
    while True:
        running = [r for r in store.running_runs(conn) if _held_by_drain(conn, r)]
        if not running:
            out(f"drained until {_iso(until)}: no run that acts on a session is running")
            return 0
        if clock() >= deadline:
            out("NOT drained: still running: " + ", ".join(f"run {r['id']} {r['job']} {r['key'] or ''}".strip()
                                                         for r in running) + "; do not deploy now")
            return 1
        sleep(poll)


def undrain(conn: Any) -> None:
    actions.runner_state_put(conn, {"drain": None}, actor="cli", via="cli")
    wake()


# ---------------------------------------------------------------- the health check and the window report

WINDOW_TOLERANCE = 2.0                    # seconds between a start and its run's start that still match
DECISION_WINDOW = timedelta(minutes=10)   # a window start's decision line comes within this of it


def health(conn: Any, env: Optional[dict[str, str]] = None, now: Optional[datetime] = None,
           pids: Callable[[], list[int]] = runner_pids) -> Optional[str]:
    """The container's health check (S8): None when healthy, else why not. Only the runner mode
    (AFCLAUDE_SCHEDULER=runner) has a runner to check (off: healthy; any other value is unhealthy:
    the entrypoint idles the container then, phase 3a S9 removed `on` and `legacy`). The runner
    must be a live process and its loop heartbeat (runner_jobs 'runner', written at most every
    HEARTBEAT_EVERY and at every loop pass, so at most MAX_SLEEP + HEARTBEAT_EVERY old) younger
    than HEALTH_STALE. It only reports: the restart is the loop watchdog's (Watchdog) and compose's.
    The schema state comes first: a database this code must not write to (a migration that failed,
    a database from newer code) is the reason, whatever else follows from it (the entrypoint idles
    the container then, no runner)."""
    mode = (os.environ if env is None else env).get("AFCLAUDE_SCHEDULER", "off")
    if mode == "off":
        return None
    if mode not in RUNNER_MODES:
        return f"unknown AFCLAUDE_SCHEDULER={mode} (runner or off; the container idles)"
    problem = store.schema_problem(conn)
    if problem is not None:
        return f"schema: {problem}"
    if not pids():
        return "no runner process (runner.py serve) is running"
    row = store.get_runner_job(conn, actions.RUNNER_ROW)
    beat = _parse(row["heartbeat_at"]) if row else None
    if beat is None:
        return "the runner has no heartbeat"
    age = (now or _utcnow()) - beat
    if age > HEALTH_STALE:
        return (f"the runner's heartbeat is {int(age.total_seconds())} s old "
                f"(healthy: under {int(HEALTH_STALE.total_seconds())} s)")
    return None


_DECISION_RE = re.compile(r"\] window-start: (?P<word>[A-Z]+)")


def parse_decisions(lines: Iterable[str]) -> list[tuple[datetime, str]]:
    """(time, FIRE|HOLD|POSTPONE...) of the keep-alive's window-start decision lines (not the
    postponed rechecks: those come from the watcher, not from a start)."""
    out: list[tuple[datetime, str]] = []
    for ln in lines:
        m = _DECISION_RE.search(ln)
        ts = runner_state.line_ts(ln) if m else None
        t = _parse_text(ts) if m and ts else None
        if m and t is not None:
            out.append((t, m.group("word")))
    return out


def window_report(starts: list[tuple[datetime, str]], runs: list[store.Row], decisions: list[tuple[datetime, str]],
                  *, since: datetime, until: datetime, tolerance: float = WINDOW_TOLERANCE) -> tuple[list[str], int]:
    """S5's check (docs/phase3a_plan.md §5 S5, "Done when"): every session-window start between
    since and until (`starts`: (time, schedule.start_key)) has the runner's window_start run under
    its key, started within `tolerance` of the start (to the second) and ended ok, and the
    keep-alive's decision line (FIRE / HOLD / POSTPONE ..., the kind cron's runs logged) within
    DECISION_WINDOW after it; a decision line at no start is a mismatch too. -> (report lines,
    number of mismatches)."""
    tol = timedelta(seconds=tolerance)
    st = [(t, k) for t, k in starts if since <= t <= until]
    by_key = {str(r["key"]): r for r in runs if r.get("key")}
    out = [f"window starts vs the runner's runs, {_iso(since)} .. {_iso(until)} (tolerance {tolerance:g} s)"]
    bad: list[str] = []
    for t, key in st:
        r = by_key.get(key)
        d = next((w for x, w in decisions if t <= x <= t + DECISION_WINDOW), None)
        began = _parse_text(r["started_at"]) if r else None
        lag = f"{(began - t).total_seconds():+.3f} s" if began else "-"
        out.append(f"  {_hm_berlin(t)} {key:<34} run {r['id'] if r else '-'} {r['status'] if r else 'none':<11} "
                   f"start {lag:>10}  decision {d or 'none'}")
        if r is None:
            bad.append(f"MISMATCH {key}: no window_start run for the start at {_iso(t)}")
            continue
        if r["status"] == "skipped":
            bad.append(f"MISMATCH {key}: not started ({r['error'] or 'skipped'})")
            continue
        if began is None or abs(began - t) > tol:
            bad.append(f"MISMATCH {key}: started {lag} from its start {_iso(t)} (more than {tolerance:g} s)")
        if r["status"] != "ok":
            bad.append(f"MISMATCH {key}: its run {r['id']} ended {r['status']}")
        if d is None:
            bad.append(f"MISMATCH {key}: the keep-alive logged no window-start decision within {DECISION_WINDOW}")
    for x, w in decisions:
        if since <= x <= until and not any(t <= x <= t + DECISION_WINDOW for t, _ in st):
            bad.append(f"MISMATCH: the keep-alive decided {w} at {_iso(x)}, at no session-window start")
    out.extend(bad[:50])
    if len(bad) > 50:
        out.append(f"... and {len(bad) - 50} more")
    out.append(f"{len(bad)} mismatch(es)" if bad else f"OK: {len(st)} start(s), each run at its second and decided")
    return out, len(bad)


def _hm_berlin(t: datetime) -> str:
    return t.astimezone(BERLIN).strftime("%a %d.%m. %H:%M %Z")


def _read_lines(path: str) -> list[str]:
    if path == "-":
        return sys.stdin.read().splitlines()
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()


# ---------------------------------------------------------------- serve, CLI

def serve(owns: Optional[frozenset[str]] = None) -> int:
    o = RUNNER_OWNS if owns is None else owns
    fd = acquire_lock()
    if fd is None:
        log("another runner holds data/run/runner.lock; this one exits")
        return EXIT_LOCKED
    runner: Optional[Runner] = None
    try:
        conn = store.connect()
        runner = Runner(conn, owns=o)
        r = runner
        Watchdog(lambda: r.beat_mono, stale=STALE_LOOP, every=WATCHDOG_EVERY).start()   # (read now: tests patch them)
        runner.boot()
        return asyncio.run(runner.serve())
    except Exception as e:   # noqa: BLE001 - the runner's own failure: report, exit non-zero (no clean exit)
        failstop.fail(COMPONENT, "the runner crashed", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}",
                      log=log)
        if runner is not None:   # the next boot then does not alert a second time for it
            with contextlib.suppress(Exception):
                actions.runner_state_put(runner.conn, {"crashed": {"boot_id": runner.boot_id, "at": _iso(_utcnow()),
                                                                   "error": f"{type(e).__name__}: {e}"[:300]}},
                                         actor=actions.RUNNER_ACTOR, via=VIA)
        return EXIT_CRASH
    finally:
        os.close(fd)


def _window_report_cli(conn: Any, args: argparse.Namespace) -> int:
    until = _utcnow()
    since = until - timedelta(hours=args.hours)
    cfg = schedule.load()
    starts = [(t, schedule.start_key(t, cfg)) for t, _ in schedule.session_slots(since - TINY, until, cfg)]
    runs = [r for r in store.recent_runs(conn, 1000, "window_start")
            if (_parse_text(r["started_at"]) or until) >= since - DECISION_WINDOW]
    try:
        decisions = parse_decisions(_read_lines(args.decisions_log or runner_jobs.log_path("window_start")))
    except OSError as e:
        print(f"window-report: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    lines, bad = window_report(starts, runs, decisions, since=since, until=until, tolerance=args.tolerance)
    print("\n".join(lines))
    return 1 if bad else 0


def _health_cli() -> int:
    """`health`: 0 healthy, 1 unhealthy (the reason on stdout, which `docker inspect` keeps); a DB that
    cannot be read is unhealthy too (never reported as healthy, D-235)."""
    try:
        conn = store.connect()
        try:
            why = health(conn, pids=runner_pids)
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 - the check itself failed: unhealthy with the error
        why = f"the health check failed: {type(e).__name__}: {e}"
    print(f"unhealthy: {why}" if why else "healthy")
    return 1 if why else 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="The AFClaude resident runner (phase 3a).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the loop (one per install)")
    s = sub.add_parser("status", help="jobs, next due times, running runs, heartbeat age")
    s.add_argument("--json", action="store_true")
    d = sub.add_parser("drain", help="before a deploy: hold new session runs, wait until none runs")
    d.add_argument("--max-wait", type=float, default=None, help="minutes to wait at most (default: the longest "
                                                                 "session-run timeout + 5)")
    sub.add_parser("undrain", help="lift a drain")
    sub.add_parser("wake", help="make the runner re-read the DB now")
    sub.add_parser("health", help="the container's health check (S8): exit 1 (the reason on stdout) when no runner "
                                  "runs or its loop heartbeat is stale; healthy in the off mode")
    w = sub.add_parser("window-report", help="S5: each session-window start against the runner's run and the "
                                             "keep-alive's decision line (exit 1 on a mismatch)")
    w.add_argument("--hours", type=float, default=48.0, help="the last H hours (default 48)")
    w.add_argument("--decisions-log", default=None, help="the keep-alive's log (default data/keepalive/keepalive.log)")
    w.add_argument("--tolerance", type=float, default=WINDOW_TOLERANCE, help="seconds (default 2)")
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        return serve()
    if args.cmd == "wake":
        return 0 if wake() else 1
    if args.cmd == "health":
        return _health_cli()
    conn = store.connect()
    try:
        if args.cmd == "window-report":
            return _window_report_cli(conn, args)
        if args.cmd == "status":
            st = status(conn)
            if args.json:
                print(json.dumps(st, indent=2, default=str))
            else:
                print_status(st)
            return 0
        if args.cmd == "drain":
            return drain(conn, timedelta(minutes=args.max_wait) if args.max_wait is not None else None)
        undrain(conn)
        print("drain lifted")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
