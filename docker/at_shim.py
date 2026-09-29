#!/usr/bin/env python3
"""Minimal `at` for the manager container (installed as /usr/local/bin/at).

usage_sampler.py schedules its pre-/post-weekly-reset samples with
`at -t [[CC]YY]MMDDhhmm` and the command on stdin (TZ from the environment). The
container has no atd, so jobs are spooled as JSON in $AFCLAUDE_AT_SPOOL
(default <data>/at_spool, persistent) and the crontab runs `at --run-due` every
minute, which starts every due job with /bin/sh and removes it.

  at -t 202610011656 < cmd      spool a job (prints "job <id> at <UTC time>" on stderr)
  at --run-due                  run and remove the due jobs
  at -l                         list spooled jobs
"""
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SPOOL = os.environ.get("AFCLAUDE_AT_SPOOL",
                       os.path.join(os.environ.get("AFCLAUDE_DIR", "/mnt/BlockVolume/Claude/work/AFClaude"),
                                    "data", "at_spool"))


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
    os.makedirs(SPOOL, exist_ok=True)
    jid = uuid.uuid4().hex[:12]
    path = os.path.join(SPOOL, f"{jid}.json")
    with open(path + ".tmp", "w") as fh:
        json.dump({"id": jid, "at": when.astimezone(timezone.utc).isoformat(), "cmd": cmd,
                   "cwd": os.getcwd()}, fh)
    os.replace(path + ".tmp", path)
    return jid


def jobs():
    out = []
    for name in sorted(os.listdir(SPOOL)) if os.path.isdir(SPOOL) else []:
        if name.endswith(".json"):
            try:
                with open(os.path.join(SPOOL, name)) as fh:
                    out.append((os.path.join(SPOOL, name), json.load(fh)))
            except (OSError, ValueError):
                continue
    return out


def run_due():
    now = datetime.now(timezone.utc)
    for path, job in jobs():
        if datetime.fromisoformat(job["at"]) > now:
            continue
        try:
            os.remove(path)          # claim it first: never run a job twice
        except FileNotFoundError:
            continue
        print(f"{now.isoformat()} at-shim: running job {job['id']} (due {job['at']})", flush=True)
        cwd = job.get("cwd") if os.path.isdir(job.get("cwd") or "") else "/"
        subprocess.Popen(["/bin/sh", "-c", job["cmd"]], cwd=cwd, start_new_session=True,
                         stdin=subprocess.DEVNULL)


def main(argv):
    if argv == ["--run-due"]:
        return run_due()
    if argv == ["-l"]:
        for _, job in jobs():
            print(f"{job['id']}\t{job['at']}\t{job['cmd'].strip()[:100]}")
        return 0
    if len(argv) == 2 and argv[0] == "-t":
        tzname = os.environ.get("TZ") or "UTC"
        try:
            when = parse_t(argv[1], ZoneInfo(tzname))
        except (ValueError, KeyError) as e:
            print(f"at: {e}", file=sys.stderr)
            return 1
        jid = spool(when, sys.stdin.read())
        print(f"job {jid} at {when.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC", file=sys.stderr)
        return 0
    print("usage: at -t [[CC]YY]MMDDhhmm < cmd | at --run-due | at -l", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
