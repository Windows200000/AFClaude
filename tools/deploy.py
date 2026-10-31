#!/usr/bin/env python3
"""THE AFClaude deploy command (D-257; the 21.10. incident).

`docker compose -f docker/compose.yml up -d --build` replaces the container outright: the
old one is torn down while it may still have an SSH-bridge call in flight (host.run_on_host;
usage_sampler.py's /usage refresh, keepalive.py's checks, ...). The bridge has no pty and no
server-side ClientAliveInterval (docker/install_host_bridge.sh), so killing the container
does not reliably end the matching host_exec.py/claude process on the host: on 21.10. a
rebuild cut a sampler /usage read, and every bridge /usage read afterwards (keepalive, the
next sampler run) kept returning the old cache for ~16 min -- fail_stop paused automation
(D-234/D-235) until an unrelated manual `claude -p /usage` on the host happened to get
through. `runner.py drain` only holds new session runs (ka_resume.sh); it does not touch the
cron-triggered sampler / usage-review / keepalive reads (RUNNER_OWNS is still empty, phase3a
S1). This script is the other half: drain, then wait for the container's own processes to go
quiet, so a rebuild never starts while one of these is in flight.

    python3 tools/deploy.py [--max-wait-drain MIN] [--max-wait-clear MIN] [--force]

Steps: 0) refuse within START_GUARD (30 min) before or after a scheduled session-window start
(schedule.py): from phase 3a S5 on, a restart during a start is an alert + pause (D-257), and a
start missed by the restart gap is one too; the refusal prints the next safe time.
1) `runner.py drain()` (existing). 2) poll `docker top` for a sampler / usage-review /
unittest / claude process (any bridge call, including a plain usage read) still running in
the container, bounded wait. 3) `docker compose -f docker/compose.yml up -d --build` with
AFCLAUDE_CODE_VERSION=$(git rev-parse --short HEAD) (as README and the AFClaude project's deploy_rule
say to run it; the image has no .git, so without this the build commit is unknown).
Exit 1 (no rebuild, nothing drained, even with --force) when AFCLAUDE_SCHEDULER (the environment,
else docker/.env, else compose's default off) is neither runner nor off: phase 3a S9 removed `on` and
`legacy`, and the new entrypoint would only alert and idle. Exit 1 (no rebuild) inside the start guard (checked before the drain and again before the
rebuild) or if draining or clearing timed out; an aborted deploy lifts its drain again.
--force rebuilds anyway, printing what was still running.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import runner  # noqa: E402  (drain)
import schedule  # noqa: E402
import store  # noqa: E402

CONTAINER = "afclaude"
COMPOSE_FILE = os.path.join(HERE, "docker", "compose.yml")
ENV_FILE = os.path.join(HERE, "docker", ".env")      # compose's own: the scheduler mode the rebuild gets
SCHEDULER_MODES = ("runner", "off")
CLEAR_POLL = 5.0                              # seconds between docker-top polls
DEFAULT_CLEAR_WAIT = timedelta(minutes=10)
START_GUARD = timedelta(minutes=30)           # no rebuild this close to a session-window start

# Any of these in one of the container's own process lines means a bridge call (or a plain
# usage read: it still goes out as `claude -p ... /usage`) that a rebuild must not cut off
# (D-257, the 21.10. incident). Word-boundary \bclaude\b: "afclaude"/"AFCLAUDE_*" never match
# (no boundary before "claude" inside that word).
WATCH_RE = re.compile(r"usage_sampler\.py|usage_review\.py|\bunittest\b|\bclaude\b")


def container_processes(container: str = CONTAINER,
                        run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run
                        ) -> List[str]:
    """The container's own process lines (one per running process), via `docker top` (reads
    /proc directly -- works even though the image is read_only and has no ps of its own).
    [] if the container is not running or `docker top` fails (nothing to wait for)."""
    try:
        r = run(["docker", "top", container, "-eo", "pid,args"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    lines = r.stdout.splitlines()
    return lines[1:] if lines else []   # docker top always prints a "PID COMMAND" header


def busy(lines: List[str]) -> List[str]:
    """The lines (of container_processes()) that must finish before a rebuild (WATCH_RE)."""
    return [l for l in lines if WATCH_RE.search(l)]


def wait_clear(max_wait: timedelta, *, poll: float = CLEAR_POLL,
              lister: Callable[[], List[str]] = container_processes,
              sleep: Callable[[float], object] = time.sleep,
              clock: Callable[[], float] = time.monotonic,
              out: Callable[[str], object] = print) -> bool:
    """Wait until the container has no sampler / usage-review / unittest / claude process
    running (busy()). -> True cleared (safe to rebuild now), False timed out (the caller
    must not deploy)."""
    deadline = clock() + max_wait.total_seconds()
    while True:
        still = busy(lister())
        if not still:
            out("container clear: no sampler/usage-review/unittest/claude process running")
            return True
        if clock() >= deadline:
            out(f"NOT clear after {max_wait.total_seconds() / 60:.0f} min, still running: "
               + "; ".join(still) + "; do not deploy now")
            return False
        sleep(poll)


def start_guard(now: datetime, cfg: schedule.Config,
                guard: timedelta = START_GUARD) -> Tuple[Optional[datetime], datetime]:
    """-> (the session-window start within `guard` before or after `now`, or None; the next
    safe time: `now` itself when none is that close, else the first time at least `guard`
    after every start that close, chained if the next start is near too). A start exactly
    `guard` away is safe. Both UTC."""
    t = now.astimezone(timezone.utc)
    near: Optional[datetime] = None
    while True:
        close = [s for s, _ in schedule.session_slots(t - guard, t + guard, cfg)]
        if not close:
            return near, t
        near = near or close[0]
        t = close[-1] + guard


def check_start_guard(force: bool, now: Optional[datetime] = None,
                      cfg: Optional[schedule.Config] = None,
                      out: Callable[[str], object] = print) -> bool:
    """True = rebuild now is fine (no session-window start within START_GUARD, or --force,
    which only prints the warning); False = refused, the next safe time printed."""
    c = cfg if cfg is not None else schedule.load()
    near, safe = start_guard(now or datetime.now(timezone.utc), c)
    if near is None:
        return True
    msg = (f"a session-window start is at {near.astimezone(c.zone):%Y-%m-%d %H:%M %Z}: no rebuild "
           f"within {START_GUARD.total_seconds() / 60:.0f} min of it (a restart during a start alerts "
           f"+ pauses, D-257); next safe time {safe.astimezone(c.zone):%Y-%m-%d %H:%M %Z}")
    if force:
        out(f"--force: {msg}; deploying anyway")
        return True
    out(f"deploy refused: {msg}; rerun with --force to rebuild anyway")
    return False


def lift_drain(conn: object) -> int:
    """An aborted deploy lifts its drain at once (runner.undrain): else it would hold the next
    session runs until the drain expires (the longest session-run timeout + 5 min), long enough
    to turn a start into a missed one (D-257). -> 1 (the deploy's exit code)."""
    runner.undrain(conn)
    print("drain lifted (no rebuild)")
    return 1


def scheduler_mode(env_file: Optional[str] = None, environ: Optional[dict] = None) -> str:
    """AFCLAUDE_SCHEDULER as compose resolves it: the shell environment, else the last line of
    docker/.env, else compose.yml's default `off`."""
    environ = os.environ if environ is None else environ
    if environ.get("AFCLAUDE_SCHEDULER"):
        return str(environ["AFCLAUDE_SCHEDULER"])
    mode = ""
    try:
        with open(env_file or ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                key, sep, value = line.strip().partition("=")
                if sep and key.strip() == "AFCLAUDE_SCHEDULER":
                    mode = value.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return mode or "off"


def check_scheduler_mode() -> bool:
    """False (printed why) unless the mode the rebuild gets is runner or off."""
    mode = scheduler_mode()
    if mode in SCHEDULER_MODES:
        return True
    print(f"deploy refused: AFCLAUDE_SCHEDULER={mode} (docker/.env or the environment) must be runner or off "
          "(phase 3a S9 removed `on` and `legacy`; the new container would alert and idle). Fix docker/.env "
          "first; nothing was drained.")
    return False


def code_version(run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> str:
    return run(["git", "-C", HERE, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
              check=True).stdout.strip()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="THE AFClaude deploy command (D-257): drain, wait for "
                                              "the container to go quiet, then rebuild.")
    ap.add_argument("--max-wait-drain", type=float, default=None,
                    help="minutes for runner.py drain (default: its own, the longest session-run "
                         "timeout + 5)")
    ap.add_argument("--max-wait-clear", type=float, default=DEFAULT_CLEAR_WAIT.total_seconds() / 60,
                    help="minutes to wait for the container to go quiet (default 10)")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even within 30 min of a session-window start or if draining or "
                         "clearing timed out (prints what was still running)")
    args = ap.parse_args(argv)

    if not check_scheduler_mode():             # before anything: --force does not skip it
        return 1
    if not check_start_guard(args.force):
        return 1
    conn = store.connect()
    try:
        drained = runner.drain(conn, timedelta(minutes=args.max_wait_drain)
                               if args.max_wait_drain is not None else None) == 0
        cleared = wait_clear(timedelta(minutes=args.max_wait_clear))
        if not (drained and cleared) and not args.force:
            print("deploy aborted: not drained/clear (see above); rerun with --force to rebuild anyway")
            return lift_drain(conn)
        if not check_start_guard(args.force):  # again: the drain and the clear wait can be long
            return lift_drain(conn)
    finally:
        conn.close()

    version = code_version()
    print(f"rebuilding (AFCLAUDE_CODE_VERSION={version}) ...")
    env = dict(os.environ, AFCLAUDE_CODE_VERSION=version)
    r = subprocess.run(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--build"], env=env)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
