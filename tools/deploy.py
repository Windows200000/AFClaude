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

Steps: 1) `runner.py drain()` (existing). 2) poll `docker top` for a sampler / usage-review /
unittest / claude process (any bridge call, including a plain usage read) still running in
the container, bounded wait. 3) `docker compose -f docker/compose.yml up -d --build` with
AFCLAUDE_CODE_VERSION=$(git rev-parse --short HEAD) (as README/prompts/manager_afclaude.md
say to run it; the image has no .git, so without this the build commit is unknown).
Exit 1 (no rebuild) if draining or clearing timed out; --force rebuilds anyway, printing
what was still running.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import timedelta
from typing import Callable, List, Optional

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import runner  # noqa: E402  (drain)
import store  # noqa: E402

CONTAINER = "afclaude"
COMPOSE_FILE = os.path.join(HERE, "docker", "compose.yml")
CLEAR_POLL = 5.0                              # seconds between docker-top polls
DEFAULT_CLEAR_WAIT = timedelta(minutes=10)

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
                    help="rebuild even if draining or clearing timed out (prints what was still running)")
    args = ap.parse_args(argv)

    conn = store.connect()
    try:
        drained = runner.drain(conn, timedelta(minutes=args.max_wait_drain)
                               if args.max_wait_drain is not None else None) == 0
    finally:
        conn.close()
    cleared = wait_clear(timedelta(minutes=args.max_wait_clear))
    if not (drained and cleared) and not args.force:
        print("deploy aborted: not drained/clear (see above); rerun with --force to rebuild anyway")
        return 1

    version = code_version()
    print(f"rebuilding (AFCLAUDE_CODE_VERSION={version}) ...")
    env = dict(os.environ, AFCLAUDE_CODE_VERSION=version)
    r = subprocess.run(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--build"], env=env)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
