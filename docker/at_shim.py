#!/usr/bin/env python3
"""Minimal `at` for the manager container (installed as /usr/local/bin/at).

usage_sampler.py schedules its pre-/post-weekly-reset samples (and usage_review.py its one-off
reviews) with `at -t [[CC]YY]MMDDhhmm` and the command on stdin (TZ from the environment). The
container has no atd, so jobs are stored in the AFClaude DB's scheduled_jobs (D-161: the DB is
their only copy, runner_state.py) and the crontab runs `at --run-due` every minute, which
claims every due pending job (marks it started, so it never runs twice) and starts it with
/bin/sh. A job that can't be stored (a broken DB) is recorded in data/ALERTS.fallback.md
(§7.8) and `at` exits 1.

Failures of `--run-due` (D-234/D-235, failstop.py): the jobs can't be read or claimed, or a job
can't be started: an ERROR line in at_shim.log (= app_log), an alert, and automation paused until
the owner clears it; the run stops there. The shim itself runs no AFClaude session: the jobs it
starts (the sampler's samples, a one-off review) check the pause themselves.

The old spool dir ($AFCLAUDE_AT_SPOOL, default <data>/at_spool) is a read-only legacy: its
files are never written or deleted any more; `--run-due` adds a spooled job the DB doesn't
know yet (one an older shim copy spooled without its DB write) to the DB and runs it from there.

  at -t 202610011656 < cmd      schedule a job (prints "job <id> at <UTC time>" on stderr)
  at --run-due                  run the due jobs
  at -l                         list the pending jobs
"""
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

AFCLAUDE_DIR = os.environ.get("AFCLAUDE_DIR", "/mnt/BlockVolume/Claude/work/AFClaude")
sys.path.insert(0, AFCLAUDE_DIR)
import failstop  # noqa: E402  (D-234/D-235)
import runner_state  # noqa: E402
import store  # noqa: E402

SPOOL = os.environ.get("AFCLAUDE_AT_SPOOL", os.path.join(AFCLAUDE_DIR, "data", "at_spool"))   # legacy, read-only


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


def spool(when, cmd):
    """Store the job in the DB. -> its id, or None (not stored: recorded in the fallback file)."""
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "at": when.astimezone(timezone.utc).isoformat(), "cmd": cmd, "cwd": os.getcwd()}
    return jid if runner_state.job_add(job) else None


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


def run_due():
    now = datetime.now(timezone.utc)
    try:
        _adopt_legacy({j["id"] for j in runner_state.jobs()})
        due = [j for j in jobs("pending") if _due(j, now)]
    except store.DBError as e:
        fail("scheduled jobs unreadable: no job started", f"database problem ({e.kind}): {e}")
        return 1
    for job in due:
        try:
            if not runner_state.job_claim(job["id"]):     # claim it first: never run a job twice
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
    if len(argv) == 2 and argv[0] == "-t":
        tzname = os.environ.get("TZ") or "UTC"
        try:
            when = parse_t(argv[1], ZoneInfo(tzname))
        except (ValueError, KeyError) as e:
            print(f"at: {e}", file=sys.stderr)
            return 1
        jid = spool(when, sys.stdin.read())
        if jid is None:
            print("at: the job could not be stored (database problem; recorded in data/ALERTS.fallback.md)",
                  file=sys.stderr)
            return 1
        print(f"job {jid} at {when.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC", file=sys.stderr)
        return 0
    print("usage: at -t [[CC]YY]MMDDhhmm < cmd | at --run-due | at -l", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
