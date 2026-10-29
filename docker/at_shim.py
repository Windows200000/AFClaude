#!/usr/bin/env python3
"""Minimal `at` for the manager container (installed as /usr/local/bin/at).

usage_sampler.py schedules its pre-/post-weekly-reset samples (and usage_review.py its one-off
reviews and the review chain's steps) with `at -t [[CC]YY]MMDDhhmm` and the command on stdin (TZ
from the environment). The container has no atd, so jobs are stored in the AFClaude DB's
scheduled_jobs (D-161: the DB is their only copy, runner_state.py). A job that can't be stored (a
broken DB) is recorded in data/ALERTS.fallback.md (§7.8) and `at` exits 1.

Who runs them (phase 3a S4, docs/phase3a_plan.md): the resident runner (runner.py, job "at"), at
their minute, each as a tracked child with a timeout; `at -t` wakes it after storing the job (only
when a runner listens: its socket exists). Two fields of the job are for the runner:
  --on-interrupt rerun|fail   a run cut off by a runner restart is run again ("rerun": the samples,
                              the chain steps) or is an alert + pause ("fail", the default: D-257)
  --timeout MIN               minutes the run may take (default: the setting runner_timeout_at)
In the rollback mode AFCLAUDE_SCHEDULER=legacy the crontab still runs `at --run-due` every minute
instead (it is kept for that mode only), which claims every due pending job (marks it started, so
it never runs twice) and starts it detached with /bin/sh; its result is not recorded.

Failures of `--run-due` (D-234/D-235, failstop.py): the jobs can't be read or claimed, or a job
can't be started: an ERROR line in at_shim.log (= app_log), an alert, and automation paused until
the owner clears it; the run stops there. One deliberate retry first: a read or claim that failed
only because the database stayed locked or busy through store.py's whole retry budget (~30 s:
another writer held the lock that long) gets one more try after LOCK_RETRY_WAIT (a WARN line),
since a missed job and paused automation cost more than the wait; any other DB error, or the
second try failing too, is the failure above. The shim itself runs no AFClaude session: the jobs it
starts (the sampler's samples, a one-off review) check the pause themselves.

The old spool dir ($AFCLAUDE_AT_SPOOL, default <data>/at_spool) is a read-only legacy: its
files are never written or deleted any more; `--run-due` adds a spooled job the DB doesn't
know yet (one an older shim copy spooled without its DB write) to the DB and runs it from there.

  at [--on-interrupt rerun|fail] [--timeout MIN] -t 202610011656 < cmd
                                schedule a job (prints "job <id> at <UTC time>" on stderr)
  at --run-due                  run the due jobs (legacy mode only)
  at -l                         list the pending jobs
"""
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

AFCLAUDE_DIR = os.environ.get("AFCLAUDE_DIR", "/mnt/BlockVolume/Claude/work/AFClaude")
sys.path.insert(0, AFCLAUDE_DIR)
import failstop  # noqa: E402  (D-234/D-235)
import runner_state  # noqa: E402
import store  # noqa: E402

SPOOL = os.environ.get("AFCLAUDE_AT_SPOOL", os.path.join(AFCLAUDE_DIR, "data", "at_spool"))   # legacy, read-only
LOCK_RETRY_WAIT = 10.0       # seconds before the one extra try of a step whose DB stayed locked (module doc)
LOCK_KINDS = ("locked", "io")  # store.DBError kinds of a transient error that outlasted store's retries
_sleep = time.sleep           # patched by tests


def parse_t(value, tz):
    """[[CC]YY]MMDDhhmm (no seconds) -> aware datetime."""
    digits = value.split(".")[0]
    now = datetime.now(tz)
    if len(digits) == 12:
        fmt = "%Y%m%d%H%M"
    elif len(digits) == 10:
        digits, fmt = str(now.year)[:2] + digits, "%Y%m%d%H%M"
    elif len(digits) == 8:
        digits, fmt = str(now.year) + digits, "%Y%m%d%H%M"
    else:
        raise ValueError(f"bad -t time {value!r}")
    return datetime.strptime(digits, fmt).replace(tzinfo=tz)


def spool(when, cmd, on_interrupt=None, timeout=None):
    """Store the job in the DB (with the runner's fields when given). -> its id, or None (not
    stored: recorded in the fallback file)."""
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "at": when.astimezone(timezone.utc).isoformat(), "cmd": cmd, "cwd": os.getcwd()}
    if on_interrupt is not None:
        job["on_interrupt"] = on_interrupt
    if timeout is not None:
        job["timeout"] = timeout
    return jid if runner_state.job_add(job) else None


def wake_runner():
    """Make the resident runner re-read the jobs now (S4), if one listens (its socket exists: none
    in legacy mode, where `--run-due` runs them). Never fails the scheduling: a wake that is not
    sent is a warning on stderr (the runner re-reads the DB within a minute anyway)."""
    try:
        import runner
        if os.path.exists(runner.sock_path()):
            runner.wake()
    except Exception as e:   # noqa: BLE001 - the job is stored; the runner's next pass finds it
        print(f"at: the runner was not woken ({type(e).__name__}: {e}); it finds the job within a minute",
              file=sys.stderr)


def parse_schedule_args(argv):
    """[--on-interrupt rerun|fail] [--timeout MIN] -t TIME, in any order. -> (time, on_interrupt,
    timeout) or raises ValueError."""
    t, oi, timeout = None, None, None
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if not rest:
            raise ValueError(f"{flag} needs a value")
        val = rest.pop(0)
        if flag == "-t" and t is None:
            t = val
        elif flag == "--on-interrupt" and oi is None and val in ("rerun", "fail"):
            oi = val
        elif flag == "--timeout" and timeout is None:
            try:
                timeout = float(val)
            except ValueError:
                raise ValueError(f"bad --timeout {val!r} (minutes)") from None
            if not 0 < timeout <= 720:
                raise ValueError(f"bad --timeout {val!r} (minutes, at most 720)")
        else:
            raise ValueError(f"bad argument {flag} {val}")
    if t is None:
        raise ValueError("-t TIME is required")
    return t, oi, timeout


def _adopt_legacy(known):
    """Spool files of an older shim copy whose job the DB doesn't know: into the DB (pending)."""
    for job in runner_state.legacy_jobs(SPOOL):
        if str(job.get("id")) not in known:
            runner_state.job_add(job)


def jobs(status="pending"):
    return runner_state.jobs(status)


def _due(job, now):
    try:
        t = store.parse_iso(job["run_at"])
    except ValueError:
        return False
    return t is not None and (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) <= now


def log(msg):
    """One line into at_shim.log (stdout, the cron redirect) and app_log."""
    line = f"{datetime.now(timezone.utc).isoformat()} at-shim: {msg}"
    print(line, flush=True)
    runner_state.tee("at_shim", line)


def fail(subject, detail=""):
    """A failure (D-234/D-235): ERROR line, alert, automation paused (failstop.fail)."""
    return failstop.fail("at_shim", subject, detail, log=log)


def _db_step(what, step):
    """step() (a DB read or claim), with the one extra try of the module doc: a DBError of a
    LOCK_KINDS kind is logged (WARN) and step() runs once more after LOCK_RETRY_WAIT; whatever
    that raises reaches the caller, as does any other DBError at once. -> step()'s result."""
    try:
        return step()
    except store.DBError as e:
        if e.kind not in LOCK_KINDS:
            raise
        log(f"WARN: {what}: database {e.kind} through store's whole retry budget ({e}); "
            f"one more try in {LOCK_RETRY_WAIT:g} s")
    _sleep(LOCK_RETRY_WAIT)
    return step()


def _read_due(now):
    _adopt_legacy({j["id"] for j in runner_state.jobs()})
    return [j for j in jobs("pending") if _due(j, now)]


def run_due():
    now = datetime.now(timezone.utc)
    try:
        due = _db_step("reading the scheduled jobs", lambda: _read_due(now))
    except store.DBError as e:
        fail("scheduled jobs unreadable: no job started", f"database problem ({e.kind}): {e}")
        return 1
    for job in due:
        try:
            if not _db_step(f"claiming job {job['id']}",       # claim it first: never run a job twice
                            lambda: runner_state.job_claim(job["id"])):
                continue
        except store.DBError as e:
            fail("a due job could not be claimed: no further job started",
                 f"job {job['id']} ({e.kind}): {e}")
            return 1
        log(f"running job {job['id']} (due {job['run_at']})")
        cwd = job.get("cwd") if os.path.isdir(job.get("cwd") or "") else "/"
        try:
            subprocess.Popen(["/bin/sh", "-c", job["cmd"]], cwd=cwd, start_new_session=True,
                             stdin=subprocess.DEVNULL)
        except OSError as e:
            fail("a claimed job could not be started", f"job {job['id']} ({job['cmd'].strip()[:200]}): {e}")
            return 1
    return 0


def main(argv):
    if argv == ["--run-due"]:
        return run_due()
    if argv == ["-l"]:
        try:
            for job in jobs("pending"):
                print(f"{job['id']}\t{job['run_at']}\t{job['cmd'].strip()[:100]}")
        except store.DBError as e:
            print(f"at: database problem ({e.kind}): {e}", file=sys.stderr)
            return 1
        return 0
    if "-t" in argv:
        tzname = os.environ.get("TZ") or "UTC"
        try:
            t, on_interrupt, timeout = parse_schedule_args(argv)
            when = parse_t(t, ZoneInfo(tzname))
        except (ValueError, KeyError) as e:
            print(f"at: {e}", file=sys.stderr)
            return 1
        jid = spool(when, sys.stdin.read(), on_interrupt, timeout)
        if jid is None:
            print("at: the job could not be stored (database problem; recorded in data/ALERTS.fallback.md)",
                  file=sys.stderr)
            return 1
        wake_runner()           # before the job line: the callers keep the tail of stderr
        print(f"job {jid} at {when.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC", file=sys.stderr)
        return 0
    print("usage: at [--on-interrupt rerun|fail] [--timeout MIN] -t [[CC]YY]MMDDhhmm < cmd | at --run-due | at -l",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
