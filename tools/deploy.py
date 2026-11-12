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

    python3 tools/deploy.py [--max-wait-drain MIN] [--max-wait-clear MIN] [--max-wait-healthy MIN] [--force]

Phase 5b K4: three services (afclaude-runner, afclaude-mcp, afclaude-web) from one image, docker/compose.yml.
Everything below is about the runner (the only one with host calls in flight); the other two are stateless and
are recreated with it. Refused first: AFCLAUDE_DATA (environment, else docker/.env) not set or not a directory
with the database (the drain must talk to the DB the containers mount); the old single container `afclaude`
still running (one runner per data directory: stop it first, the cut-over's job, K7). Optional compose
overrides come from AFCLAUDE_COMPOSE_OVERRIDES (environment, else docker/.env): file names in docker/,
comma-separated (compose.host-mounts.yml, compose.subnet.yml). After the recreate the script waits for
all three health checks (a bounded wait; an unhealthy or exited service fails the deploy with its last
health output).

Steps: 0) refuse within START_GUARD (30 min) before or after a scheduled session-window start
(schedule.py): from phase 3a S5 on, a restart during a start is an alert + pause (D-257), and a
start missed by the restart gap is one too; the refusal prints the next safe time.
1) `runner.py drain()` (existing). 2) poll `docker top` for a sampler / usage-review /
unittest / claude process (any bridge call, including a plain usage read) still running in
the runner container, bounded wait. 3) `docker compose -f docker/compose.yml [-f override ...] up -d --build` with
AFCLAUDE_CODE_VERSION=$(git rev-parse --short HEAD) (as README and the AFClaude project's deploy_rule
say to run it; the image has no .git, so without this the build commit is unknown).
Exit 1 (no rebuild, nothing drained, even with --force) when AFCLAUDE_SCHEDULER (the environment,
else docker/.env, else compose's default off) is neither runner nor off: phase 3a S9 removed `on` and
`legacy`, and the new entrypoint would only alert and idle. Exit 1 (no rebuild) inside the start guard (checked before the drain and again before the
rebuild) or if draining or clearing timed out; an aborted deploy lifts its drain again. 4) wait for the three
health checks (exit 1 if one is unhealthy or the wait ran out; the new runner's boot lifts the drain).
--force rebuilds anyway, printing what was still running.
"""
from __future__ import annotations

import argparse
import json
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

NAME = "afclaude"                              # the container name prefix (AFCLAUDE_NAME), and the old single container
SERVICES = ("runner", "mcp", "web")           # container names: <prefix>-<service>
CONTAINER = f"{NAME}-runner"                  # the one with processes to wait for
COMPOSE_FILE = os.path.join(HERE, "docker", "compose.yml")
ENV_FILE = os.path.join(HERE, "docker", ".env")      # compose's own: the scheduler mode the rebuild gets
SCHEDULER_MODES = ("runner", "off")
CLEAR_POLL = 5.0                              # seconds between docker-top polls
DEFAULT_CLEAR_WAIT = timedelta(minutes=10)
START_GUARD = timedelta(minutes=30)           # no rebuild this close to a session-window start
DEFAULT_HEALTHY_WAIT = timedelta(minutes=6)   # runner start_period 120 s + a check every 60 s, with slack
HEALTH_POLL = 5.0
DB_NAME = "afclaude.db"

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


def env_value(name: str, env_file: Optional[str] = None, environ: Optional[dict] = None) -> str:
    """A variable as compose resolves it: the shell environment, else the last line of docker/.env, else ""."""
    environ = os.environ if environ is None else environ
    if environ.get(name):
        return str(environ[name])
    value = ""
    try:
        with open(env_file or ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                key, sep, v = line.strip().partition("=")
                if sep and key.strip() == name:
                    value = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return value


def scheduler_mode(env_file: Optional[str] = None, environ: Optional[dict] = None) -> str:
    """AFCLAUDE_SCHEDULER as compose resolves it: the shell environment, else the last line of
    docker/.env, else compose.yml's default `off`."""
    return env_value("AFCLAUDE_SCHEDULER", env_file, environ) or "off"


def check_scheduler_mode() -> bool:
    """False (printed why) unless the mode the rebuild gets is runner or off."""
    mode = scheduler_mode()
    if mode in SCHEDULER_MODES:
        return True
    print(f"deploy refused: AFCLAUDE_SCHEDULER={mode} (docker/.env or the environment) must be runner or off "
          "(phase 3a S9 removed `on` and `legacy`; the new container would alert and idle). Fix docker/.env "
          "first; nothing was drained.")
    return False


def name_prefix() -> str:
    return env_value("AFCLAUDE_NAME") or NAME


def container_names(prefix: Optional[str] = None) -> List[str]:
    p = prefix or name_prefix()
    return [f"{p}-{svc}" for svc in SERVICES]


def check_data() -> Optional[str]:
    """The data directory the containers will mount (AFCLAUDE_DATA, no default: D-180/D-187) must be set and hold
    the database; the drain goes through that database, so store.DB_PATH is pointed at it. -> the database's path,
    or None (printed why: nothing was drained)."""
    data = env_value("AFCLAUDE_DATA")
    if not data:
        print("deploy refused: AFCLAUDE_DATA is not set (the environment or docker/.env): it is the data directory "
              "the services mount, there is no default. Nothing was drained.")
        return None
    db = os.path.join(os.path.abspath(os.path.join(os.path.dirname(COMPOSE_FILE), data)), DB_NAME)
    if not os.path.isfile(db):
        print(f"deploy refused: no database at {db} (AFCLAUDE_DATA={data}): a wrong or empty data directory would "
              "come up as a fresh install. Nothing was drained.")
        return None
    store.DB_PATH = db
    return db


def old_container_running(run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
                          prefix: Optional[str] = None) -> bool:
    """True when the single container of the old layout (named like the prefix alone) is running."""
    p = prefix or name_prefix()
    r = run(["docker", "ps", "--filter", f"name=^{p}$", "--format", "{{.Names}}"], capture_output=True, text=True,
            timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"docker ps failed: {r.stderr.strip() or r.stdout.strip()}")
    return p in r.stdout.split()


def check_old_container() -> bool:
    if not old_container_running():
        return True
    print(f"deploy refused: the single container `{name_prefix()}` of the old layout is still running: two runners "
          "must never share a data directory. The cut-over (tools/cutover_5b.py, K7) stops it first and brings the "
          "three services up. Nothing was drained.")
    return False


def compose_command(*tail: str) -> List[str]:
    """`docker compose -f docker/compose.yml [-f docker/<override>] ... <tail>`; the overrides are named by
    AFCLAUDE_COMPOSE_OVERRIDES (comma-separated file names in docker/, no paths)."""
    cmd = ["docker", "compose", "-f", COMPOSE_FILE]
    for name in (n.strip() for n in env_value("AFCLAUDE_COMPOSE_OVERRIDES").split(",")):
        if not name:
            continue
        if os.path.basename(name) != name or not name.endswith((".yml", ".yaml")):
            raise ValueError(f"AFCLAUDE_COMPOSE_OVERRIDES: {name!r} must be a compose file name in docker/")
        cmd += ["-f", os.path.join(os.path.dirname(COMPOSE_FILE), name)]
    return cmd + list(tail)


def health_state(container: str, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run
                 ) -> Tuple[str, str]:
    """-> (state, last health output). state: the health status (starting | healthy | unhealthy), `none` when the
    container has no health check, the container's own state (restarting, exited, ...) when it is not running,
    `missing` when there is no such container."""
    r = run(["docker", "inspect", container], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return "missing", (r.stderr or r.stdout).strip()
    st = json.loads(r.stdout)[0]["State"]
    if st.get("Status") != "running":
        return str(st.get("Status")), ""
    health = st.get("Health")
    if not health:
        return "none", ""
    log = health.get("Log") or []
    return str(health.get("Status")), (log[-1].get("Output", "").strip() if log else "")


def wait_healthy(containers: List[str], max_wait: timedelta, *, poll: float = HEALTH_POLL,
                 state: Callable[[str], Tuple[str, str]] = health_state,
                 sleep: Callable[[float], object] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 out: Callable[[str], object] = print) -> bool:
    """Wait until every container is healthy. -> True; False (printed why) as soon as one is unhealthy, not running
    or has no health check (nothing to wait for: a bug in the compose file), or when the wait ran out."""
    deadline = clock() + max_wait.total_seconds()
    while True:
        states = {c: state(c) for c in containers}
        bad = {c: s for c, s in states.items() if s[0] not in ("healthy", "starting")}
        if bad:
            out("NOT healthy: " + "; ".join(f"{c} is {s[0]}" + (f" ({s[1][:300]})" if s[1] else "") for c, s in bad.items()))
            return False
        if all(s[0] == "healthy" for s in states.values()):
            out("healthy: " + ", ".join(containers))
            return True
        if clock() >= deadline:
            waiting = [c for c, s in states.items() if s[0] != "healthy"]
            out(f"NOT healthy after {max_wait.total_seconds() / 60:.0f} min, still starting: {', '.join(waiting)}")
            return False
        sleep(poll)


def code_version(run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> str:
    return run(["git", "-C", HERE, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
              check=True).stdout.strip()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="THE AFClaude deploy command (D-257): drain the runner, wait for "
                                              "its container to go quiet, recreate the three services, wait "
                                              "for their health checks.")
    ap.add_argument("--max-wait-drain", type=float, default=None,
                    help="minutes for runner.py drain (default: its own, the longest session-run "
                         "timeout + 5)")
    ap.add_argument("--max-wait-clear", type=float, default=DEFAULT_CLEAR_WAIT.total_seconds() / 60,
                    help="minutes to wait for the runner container to go quiet (default 10)")
    ap.add_argument("--max-wait-healthy", type=float, default=DEFAULT_HEALTHY_WAIT.total_seconds() / 60,
                    help="minutes to wait for the three health checks after the recreate (default 6)")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even within 30 min of a session-window start or if draining or "
                         "clearing timed out (prints what was still running)")
    args = ap.parse_args(argv)

    if not check_scheduler_mode():             # before anything: --force does not skip it
        return 1
    if check_data() is None or not check_old_container():
        return 1
    if not check_start_guard(args.force):
        return 1
    conn = store.connect()
    try:
        drained = runner.drain(conn, timedelta(minutes=args.max_wait_drain)
                               if args.max_wait_drain is not None else None) == 0
        cleared = wait_clear(timedelta(minutes=args.max_wait_clear), lister=lambda: container_processes(f"{name_prefix()}-runner"))
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
    r = subprocess.run(compose_command("up", "-d", "--build"), env=env)
    if r.returncode != 0:
        return r.returncode
    return 0 if wait_healthy(container_names(), timedelta(minutes=args.max_wait_healthy)) else 1


if __name__ == "__main__":
    sys.exit(main())
