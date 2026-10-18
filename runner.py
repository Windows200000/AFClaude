#!/usr/bin/env python3
"""runner.py: the resident runner (phase 3a, docs/phase3a_plan.md §3; D-153/D-173).

    python3 -u runner.py serve            the runner (one per install: data/run/runner.lock)
    python3 runner.py status [--json]     its jobs, next due times, running runs, heartbeat age
    python3 runner.py drain [--max-wait MIN]   before a deploy: no new run that acts on a session;
                                          waits until none is running (exit 0), else exit 1
    python3 runner.py undrain             lift a drain (a deploy that did not happen)
    python3 runner.py wake                re-read the DB now (what the senders do after a write)

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

Ownership: RUNNER_OWNS is what this image's runner runs; the cut-over (§4) moves jobs from
docker/crontab into it step by step (test_runner checks they never overlap). Empty in S1: the
runner is built and tested but owns nothing live yet.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import importlib
import json
import math
import os
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
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
TERM_GRACE = 10.0                         # SIGTERM -> SIGKILL of a timed-out child's group
STOP_GRACE = 80.0                         # SIGTERM of the runner: running runs get this long (compose: 90 s)
STAGGER = timedelta(seconds=5)            # boot catch-up: the missed interval jobs start this far apart
DRAIN_MARGIN = timedelta(minutes=5)
DRAIN_POLL = 5.0
EXIT_CRASH = 1
EXIT_LOCKED = 2
EXIT_REPORTED = runner_jobs.EXIT_REPORTED
TINY = timedelta(microseconds=1)
# The keep-alive watcher's passes (S6). The runner refuses to start next to a running watcher only
# once it owns one of them: until then (S2-S5) the watcher runs beside it by design (§4).
WATCHER_PASSES = frozenset({"watch", "compaction", "last_stretch", "deferred_start", "fillup"})
WATCHER_ONE_SHOT = frozenset({"--window-start", "--plan-fillup-test", "--decide", "--last-mile", "--once",
                              "--work-on", "--run-stopped", "--session-rules"})


@dataclass(frozen=True)
class Job:
    """One runner job (docs/phase3a_plan.md §3.2).
    timeout_min: the default of its setting runner_timeout_<name> (test_runner keeps them equal);
    every / daily: an interval tick on the UTC grid (every k x `every` from midnight) / once a day
    at (hour, minute) UTC; window_start: a keyed run at each session-window start (schedule.py,
    key schedule.start_key); paused: while automation is paused it "runs", the "handler" checks
    (and runs anyway here), or the runner "skip"s it; on_interrupt: a run cut off by a restart is
    rerun or "fail"s (alert + pause, D-257); session: it types into the task-manager session (a
    drain holds it); pause_on_failure: False = a failure alerts only (D-239.3); cron_tag: its
    docker/crontab line's tag (the cut-over check); argv: the child's command instead of
    runner_jobs.py (tests)."""
    name: str
    timeout_min: float
    every: Optional[timedelta] = None
    daily: Optional[tuple[int, int]] = None
    window_start: bool = False
    paused: str = "runs"
    on_interrupt: str = "rerun"
    session: bool = False
    pause_on_failure: bool = True
    cron_tag: Optional[str] = None
    argv: Optional[tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if not actions.JOB_NAME_RE.match(self.name) or self.name == actions.RUNNER_ROW:
            raise ValueError(f"bad job name {self.name!r}")
        if sum((self.every is not None, self.daily is not None, self.window_start)) != 1:
            raise ValueError(f"{self.name}: exactly one of every / daily / window_start")
        if self.paused not in ("runs", "handler", "skip") or self.on_interrupt not in ("rerun", "fail"):
            raise ValueError(f"{self.name}: bad paused / on_interrupt")

    @property
    def keyed(self) -> bool:
        return self.window_start


MIN = timedelta(minutes=1)
JOBS: dict[str, Job] = {j.name: j for j in (
    Job("stall_scan", 2, every=3 * MIN),
    Job("sampler", 10, every=15 * MIN, cron_tag="AFClaude usage sampler"),
    Job("dispatcher", 8, every=10 * MIN, paused="handler", cron_tag="AFClaude dispatcher"),
    Job("quickview", 2, every=3 * MIN, pause_on_failure=False, cron_tag="AFClaude quickview export"),
    Job("docs_import", 5, every=10 * MIN, cron_tag="AFClaude docs import"),
    Job("prune_logs", 10, daily=(3, 17), cron_tag="AFClaude log retention"),
    Job("prune_runs", 10, daily=(3, 27)),
    Job("usage_review", 15, every=60 * MIN, paused="handler", cron_tag="AFClaude usage-model review"),
    Job("window_start", 70, window_start=True, paused="handler", on_interrupt="fail", session=True,
        cron_tag="AFClaude window-start"),
)}
# What this image's runner runs (§4: one owner per job per image; each cut-over step moves jobs
# here and removes their cron lines in the same commit). S1: nothing yet.
RUNNER_OWNS: frozenset[str] = frozenset()


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


def _utcnow() -> datetime:
    return datetime.now(UTC)


def next_tick(job: Job, now: datetime) -> datetime:
    """The job's first tick strictly after `now` (UTC grid / daily time)."""
    if job.every is not None:
        day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        step = job.every.total_seconds()
        k = math.floor((now - day).total_seconds() / step) + 1
        return day + timedelta(seconds=k * step)
    if job.daily is not None:
        t = now.astimezone(UTC).replace(hour=job.daily[0], minute=job.daily[1], second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)
    raise ValueError(f"{job.name} has no tick")


def _signal_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def wake(path: Optional[str] = None) -> bool:
    """Tell the runner to re-read the DB (one datagram). Never blocks or raises: if the runner
    is down a warning goes to stderr (its stale heartbeat is what alerts). -> sent."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            s.sendto(b"w", path or sock_path())
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


FailFn = Callable[..., Any]


class Runner:
    """The loop and its records. Everything outside (clock, children, alerts, the pause read, the
    schedule) is injectable for the tests; serve() is the asyncio loop, boot() the recovery."""

    def __init__(self, conn: Any, *, jobs: Optional[dict[str, Job]] = None, owns: Optional[frozenset[str]] = None,
                 spawner: Optional[Spawner] = None, clock: Optional[Callable[[], datetime]] = None,
                 boot_id: Optional[str] = None, log_fn: Optional[Callable[[str], None]] = None,
                 fail: Optional[FailFn] = None, paused: Optional[Callable[[], dict[str, Any]]] = None,
                 schedule_cfg: Optional[schedule.Config] = None, sock: Optional[str] = None,
                 stop_grace: float = STOP_GRACE) -> None:
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

    # ---- boot (§3.4)

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
            if job.keyed:
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
        if job is None or job.on_interrupt == "fail":
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
            if name in self.tasks or any(d.job.name == name for d in out):
                keep.append((name, key))
            else:
                out.append(Due(self.jobs[name], key))
        self.reruns = keep
        return out

    def _settled(self, job: Job, key: str) -> bool:
        """The keyed run (job, key) was decided already: a run row (for a job that never reruns an
        interrupted run, also an interrupted one), or the keep-alive's own dedup (handled, or a
        postponed start) for a window start (the runner and a legacy rollback share it)."""
        rows = store.key_runs(self.conn, job.name, key)
        if any(job.on_interrupt == "fail" or r["status"] != "interrupted" for r in rows):
            return True
        if job.window_start:
            if actions._state_json(["handled", key]) in store.state_rows(self.conn, "keepalive"):
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

    def start(self, due: Due, now: datetime) -> Optional[int]:
        """Record and spawn one run (or record it skipped). -> the run id, or None."""
        job, key = due.job, due.key
        actor = f"runner:{job.name}"
        label = job.name + (f" {key}" if key else "")
        if job.paused == "skip":
            p = self._paused()
            if p.get("paused"):
                why = f"automation is paused: {p.get('reason') or 'no reason given'}"
                actions.run_begin(self.conn, boot_id=self.boot_id, job=job.name, key=key, timeout_s=None,
                                  actor=actor, via=VIA, rerun=job.on_interrupt == "rerun", skipped=why)
                self.once(f"skip:{job.name}:{p.get('reason')}", f"{label} skipped: {why}")
                if not job.keyed:
                    self.job_set(job.name, next_due=_iso(next_tick(job, now)))
                return None
        timeout = self.timeout_s(job)
        run_id, why_not = actions.run_begin(self.conn, boot_id=self.boot_id, job=job.name, key=key, timeout_s=timeout,
                                            actor=actor, via=VIA, session=job.session,
                                            rerun=job.on_interrupt == "rerun")
        if run_id is None:
            self.once(f"refused:{label}:{why_not}", f"{label} not started: {why_not}")
            if not job.keyed:
                self.job_set(job.name, next_due=_iso(next_tick(job, now)))
            return None
        if not job.keyed:
            self.job_set(job.name, next_due=_iso(next_tick(job, now)))
        self.log(f"start {label} (run {run_id}, timeout {timeout:g} s)")
        self.tasks[job.name] = asyncio.get_running_loop().create_task(self._run(job, key, run_id, timeout))
        return run_id

    def _argv(self, job: Job, key: Optional[str]) -> list[str]:
        if job.argv is not None:
            return list(job.argv)
        return [sys.executable, "-u", RUNNER_JOBS, job.name] + (["--key", key] if key else [])

    async def _run(self, job: Job, key: Optional[str], run_id: int, timeout: float) -> None:
        t0 = time.monotonic()

        def on_pid(pid: int) -> None:
            self.pids[job.name] = pid
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
            self.tasks.pop(job.name, None)
            self.pids.pop(job.name, None)
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
        actions.run_end(self.conn, run_id, status, actor=f"runner:{job.name}", via=VIA, result=result, error=error)
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
        try:
            while True:
                self.wake_event.clear()
                if self.crash is not None:
                    raise self.crash
                now = self.clock()
                self.heartbeat(now)
                if self.stopping:
                    if not self.tasks or time.monotonic() >= self.stop_deadline:
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
        `running` (the next boot records them interrupted and applies the job's policy)."""
        if not self.tasks:
            return
        self.log(f"stop: {', '.join(sorted(self.tasks))} still running after {self.stop_grace:g} s; ended, "
                 "left for the next boot (interrupted)")
        tasks = list(self.tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------- watchdog, lock, watcher check

class Watchdog(threading.Thread):
    """Exits the process (WATCHDOG_EXIT) when the loop has not passed for `stale` seconds: a
    wedged loop (Docker's health check alone restarts nothing; restart: unless-stopped does)."""
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


def _is_watcher(argv: list[str]) -> bool:
    return ("--session" in argv and any(a.endswith("keepalive.py") for a in argv[:3])
            and not WATCHER_ONE_SHOT & set(argv))


def local_watchers() -> list[int]:
    """Pids of keep-alive watchers in this container / host (/proc)."""
    out: list[int] = []
    for d in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if _is_watcher(argv):
            out.append(int(d))
    return out


def host_watchers() -> list[int]:
    """Pids of keep-alive watchers on the HOST (over the bridge, as start_watcher.sh checks);
    raises if the host's process list can't be read (the caller then does not start)."""
    host = importlib.import_module("host")
    snap = host.proc_snapshot()
    return [int(pid) for pid, p in snap.items() if p.get("argv") and _is_watcher(list(p["argv"]))]


def watcher_conflict(owns: frozenset[str], local: Callable[[], list[int]] = local_watchers,
                     on_host: Callable[[], list[int]] = host_watchers) -> Optional[str]:
    """Why the runner must not start next to a keep-alive watcher, or None: only once it owns
    one of the watcher's passes (WATCHER_PASSES); a host list that can't be read is a reason too."""
    if not owns & WATCHER_PASSES:
        return None
    pids = local()
    if pids:
        return f"a keep-alive watcher runs here (pid {', '.join(map(str, pids))})"
    try:
        hp = on_host()
    except Exception as e:   # noqa: BLE001 - can't tell: don't start (as start_watcher.sh)
        return f"the host's process list is unavailable ({type(e).__name__}: {e})"
    return f"a keep-alive watcher runs on the HOST (pid {', '.join(map(str, hp))})" if hp else None


# ---------------------------------------------------------------- status, drain

def status(conn: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or _utcnow()
    rows = store.runner_job_rows(conn)
    beat = _parse((rows.get(actions.RUNNER_ROW) or {}).get("heartbeat_at"))
    jobs = []
    for name, job in JOBS.items():
        r = rows.get(name) or {}
        jobs.append({"job": name, "owned": name in RUNNER_OWNS, "next_due": r.get("next_due"),
                     "last_status": r.get("last_status"), "last_ok_at": r.get("last_ok_at"),
                     "consecutive_failures": r.get("consecutive_failures", 0)})
    st = actions.runner_state_get(conn)
    return {"heartbeat_at": _iso(beat) if beat else None,
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
        print(f"  {j['job']:<13} {'owned' if j['owned'] else 'cron ':<5} next {j['next_due'] or '-':<21} "
              f"last {j['last_status'] or '-':<9} ok {j['last_ok_at'] or '-':<21} fails {j['consecutive_failures']}")
    for r in s["running"]:
        print(f"running: run {r['id']} {r['job']} {r['key'] or ''} since {r['started_at']} pid {r['pid']}")
    for r in s["recent_failures"]:
        print(f"recent {r['status']}: run {r['id']} {r['job']} {r['key'] or ''} at {r['started_at']}: "
              f"{(r['error'] or '').splitlines()[0][:120] if r['error'] else ''}")


def session_jobs() -> list[Job]:
    return [j for j in JOBS.values() if j.session]


def drain(conn: Any, max_wait: Optional[timedelta] = None, *, poll: float = DRAIN_POLL,
          clock: Callable[[], datetime] = _utcnow, sleep: Callable[[float], object] = time.sleep,
          out: Callable[[str], object] = print) -> int:
    """Before a deploy (§3.4.5): no new run that acts on a session (until the longest such timeout
    + DRAIN_MARGIN, or the next runner boot lifts it), then wait until none is running.
    -> 0 drained, 1 still running after max_wait (do not deploy)."""
    now = clock()
    longest = max((float(actions.get_setting(conn, actions.RUNNER_TIMEOUT + j.name)) for j in session_jobs()),
                  default=0.0)
    span = timedelta(minutes=longest) + DRAIN_MARGIN
    until = now + span
    actions.runner_state_put(conn, {"drain": {"until": _iso(until), "at": _iso(now)}}, actor="cli", via="cli")
    wake()
    deadline = now + (max_wait if max_wait is not None else span)
    names = {j.name for j in session_jobs()}
    while True:
        running = [r for r in store.running_runs(conn) if r["job"] in names]
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


# ---------------------------------------------------------------- serve, CLI

def serve(owns: Optional[frozenset[str]] = None) -> int:
    o = RUNNER_OWNS if owns is None else owns
    fd = acquire_lock()
    if fd is None:
        log("another runner holds data/run/runner.lock; this one exits")
        return EXIT_LOCKED
    why = watcher_conflict(o)
    if why is not None:
        failstop.fail(COMPONENT, "the runner did not start next to a keep-alive watcher", why, log=log)
        return EXIT_CRASH
    runner: Optional[Runner] = None
    try:
        conn = store.connect()
        runner = Runner(conn, owns=o)
        r = runner
        Watchdog(lambda: r.beat_mono).start()
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
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        return serve()
    if args.cmd == "wake":
        return 0 if wake() else 1
    conn = store.connect()
    try:
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
