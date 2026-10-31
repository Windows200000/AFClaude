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
(Phase 3a S9 removed `at --run-due`, the legacy mode's per-minute cron runner of the jobs; the spool
files of a pre-DB shim copy in $AFCLAUDE_AT_SPOOL are only listed by the quickview, never run here.)

  at [--on-interrupt rerun|fail] [--timeout MIN] -t 202610011656 < cmd
                                schedule a job (prints "job <id> at <UTC time>" on stderr)
  at -l                         list the pending jobs
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

AFCLAUDE_DIR = os.environ.get("AFCLAUDE_DIR", "/mnt/BlockVolume/Claude/work/AFClaude")
sys.path.insert(0, AFCLAUDE_DIR)
import runner_state  # noqa: E402
import store  # noqa: E402


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
    while the container idles, AFCLAUDE_SCHEDULER=off). Never fails the scheduling: a wake that is not
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


def jobs(status="pending"):
    return runner_state.jobs(status)


def main(argv):
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
    print("usage: at [--on-interrupt rerun|fail] [--timeout MIN] -t [[CC]YY]MMDDhhmm < cmd | at -l",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
