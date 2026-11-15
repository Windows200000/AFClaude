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

Phase 5b K4, two layouts (D-339), chosen by AFCLAUDE_LAYOUT (environment, else docker/.env):
  single (the default until the K7 cut-over): the one container `afclaude`, docker/compose.yml, exactly the deploy
    below (drain, quiet wait, start guard, rebuild); no data-directory or health checks of its own.
  services: three services (afclaude-runner, afclaude-mcp, afclaude-web) from one image, docker/compose.services.yml;
    K7 flips docker/.env to it. Everything below is about the runner (the only one with host calls in flight);
    the other two are stateless and are recreated with it. Refused first: AFCLAUDE_DATA (environment, else
    docker/.env) not set or not a directory with the database (the drain must talk to the DB the containers
    mount); the old single container `afclaude` still running (one runner per data directory: stop it first,
    the cut-over's job, K7). Optional compose overrides come from AFCLAUDE_COMPOSE_OVERRIDES (environment, else
    docker/.env): file names in docker/, comma-separated (compose.host-mounts.yml, compose.subnet.yml). After the
    recreate the script waits for all three health checks (a bounded wait; an unhealthy or exited service fails
    the deploy with its last health output, alerted). afclaude-mcp is in the compose profile `mcp` (K5 part 2a): it
    is neither started nor waited for unless COMPOSE_PROFILES (environment, else docker/.env) names `mcp`, and then
    only with roles_enforce `on` (its 0.0.0.0 bind refuses to start otherwise: the deploy refuses first, D-343).

Steps: 0) refuse within START_GUARD (30 min) before or after a scheduled session-window start
(schedule.py): from phase 3a S5 on, a restart during a start is an alert + pause (D-257), and a
start missed by the restart gap is one too; the refusal prints the next safe time.
1) `runner.py drain()` (existing). 2) poll `docker top` for a sampler / usage-review /
unittest / claude process (any bridge call, including a plain usage read) still running in
the runner container, bounded wait. 3) `docker compose -f docker/compose.yml up -d --build` (services: compose.services.yml and its overrides) with
AFCLAUDE_CODE_VERSION=$(git rev-parse --short HEAD) (as README and the AFClaude project's deploy_rule
say to run it; the image has no .git, so without this the build commit is unknown).
Exit 1 (no rebuild, nothing drained, even with --force) when the layout's compose file uses the host kit's bridge
key and the installed kit (its own kit-version answer, run on this host as sshd runs it) has another version or
other kit files than the code being deployed (check_host_kit; the owner re-runs the install first), and when AFCLAUDE_SCHEDULER (the environment,
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
import host  # noqa: E402  (the kit files and HOST_KIT_VERSION of the code being deployed)
import schedule  # noqa: E402
import store  # noqa: E402

NAME = "afclaude"                              # the container name prefix (AFCLAUDE_NAME, services layout), and the single container
SERVICES = ("runner", "mcp", "web")           # container names: <prefix>-<service>
MCP_PROFILE = "mcp"                           # compose profile of the afclaude-mcp service (compose.services.yml)
CONTAINER = NAME                              # the single layout's container (its name is fixed in compose.yml)
LAYOUTS = ("single", "services")
DEFAULT_LAYOUT = "single"                     # until K7 flips it (D-339)
COMPOSE_FILE = os.path.join(HERE, "docker", "compose.yml")                  # the single layout
COMPOSE_SERVICES = os.path.join(HERE, "docker", "compose.services.yml")     # the three services
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


def layout() -> str:
    """AFCLAUDE_LAYOUT (environment, else docker/.env): single (default) or services. ValueError for anything else."""
    v = env_value("AFCLAUDE_LAYOUT") or DEFAULT_LAYOUT
    if v not in LAYOUTS:
        raise ValueError(f"AFCLAUDE_LAYOUT must be one of {', '.join(LAYOUTS)}, not {v!r}")
    return v


def alert(subject: str, body: str) -> None:
    """A failed deploy step is an alert, not only stdout (D-235)."""
    import notify
    notify.notify(subject, body)


def name_prefix() -> str:
    return env_value("AFCLAUDE_NAME") or NAME


def mcp_enabled(env_file: Optional[str] = None, environ: Optional[dict] = None) -> bool:
    """Whether compose starts afclaude-mcp: COMPOSE_PROFILES as compose resolves it (the shell environment, else
    docker/.env) names the `mcp` profile (or `*`, all of them)."""
    names = [n.strip() for n in env_value("COMPOSE_PROFILES", env_file, environ).split(",")]
    return MCP_PROFILE in names or "*" in names


def container_names(prefix: Optional[str] = None, with_mcp: Optional[bool] = None) -> List[str]:
    """The services' containers: afclaude-mcp only when its profile is enabled (with_mcp, default: as configured)."""
    p = prefix or name_prefix()
    with_mcp = mcp_enabled() if with_mcp is None else with_mcp
    return [f"{p}-{svc}" for svc in SERVICES if svc != "mcp" or with_mcp]


def check_mcp_profile(lay: str, mode: Optional[Callable[[], str]] = None) -> bool:
    """False (printed why) when the services layout enables the `mcp` profile while roles_enforce is not `on`: the
    service binds 0.0.0.0 and would exit 78 at once (mcp_http.check_bind, D-343), so the deploy would end in a failed
    health wait after the drain. Needs check_data() first (it points store.DB_PATH at the mounted database)."""
    if lay != "services" or not mcp_enabled():
        return True
    import actions

    def read() -> str:
        conn = store.connect()
        try:
            return actions.roles_mode(conn, report=False)
        finally:
            conn.close()
    try:
        current = (mode or read)()
    except actions.RolesFailure as e:
        current = f"unusable ({e})"
    if current == "on":
        return True
    print(f"deploy refused: COMPOSE_PROFILES enables the mcp service (it listens on 0.0.0.0), but roles_enforce is "
          f"{current!r}, not 'on': it would exit 78 at start. Set roles on first (tasks.py roles mode --set on) or drop "
          "the mcp profile from COMPOSE_PROFILES. Nothing was drained.")
    return False


def check_data() -> Optional[str]:
    """The data directory the containers will mount (AFCLAUDE_DATA, no default: D-180/D-187) must be set and hold
    the database; the drain goes through that database, so store.DB_PATH is pointed at it. -> the database's path,
    or None (printed why: nothing was drained)."""
    data = env_value("AFCLAUDE_DATA")
    if not data:
        print("deploy refused: AFCLAUDE_DATA is not set (the environment or docker/.env): it is the data directory "
              "the services mount, there is no default. Nothing was drained.")
        return None
    db = os.path.join(os.path.abspath(os.path.join(os.path.dirname(COMPOSE_SERVICES), data)), DB_NAME)
    if not os.path.isfile(db):
        print(f"deploy refused: no database at {db} (AFCLAUDE_DATA={data}): a wrong or empty data directory would "
              "come up as a fresh install. Nothing was drained.")
        return None
    store.DB_PATH = db
    return db


def old_container_running(run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
                          prefix: Optional[str] = None) -> bool:
    """True when the single container of the old layout is running. Its name is fixed in compose.yml
    (`afclaude`), whatever AFCLAUDE_NAME the services get."""
    p = prefix or NAME
    r = run(["docker", "ps", "--filter", f"name=^{p}$", "--format", "{{.Names}}"], capture_output=True, text=True,
            timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"docker ps failed: {r.stderr.strip() or r.stdout.strip()}")
    return p in r.stdout.split()


def check_old_container() -> bool:
    if not old_container_running():
        return True
    print(f"deploy refused: the single container `{NAME}` of the old layout is still running: two runners "
          "must never share a data directory. The cut-over (tools/cutover_5b.py, K7) stops it first and brings the "
          "three services up. Nothing was drained.")
    return False


# A compose file whose container uses the host kit's bridge key (the install's copy in <data>/bridge/): its host
# calls go to the installed kit. A non-comment line only (mapping or `- NAME=value` form); the pre-kit
# compose.yml (the rollback) sets the key to a secrets path. kit_mode refuses (fail closed) whatever it cannot
# read instead of guessing what compose would make of it: a value built with ${...} / $VAR, a key without a value
# (passed through from the deploying shell) and an env_file: source that may set it.
KIT_KEY_RE = re.compile(r"""(?m)^[ \t]*(?:-[ \t]*)?["']?AFCLAUDE_BRIDGE_KEY["']?(?P<rest>[ \t]*(?:[:=#][^\n]*)?)$""")
ENV_FILE_RE = re.compile(r"(?m)^[ \t]*env_file[ \t]*:")
KIT_KEY_DIR = "/app/data/bridge/"
KIT_TIMEOUT = 30                              # seconds for the installed kit's kit-version answer
INSTALL_HINT = ("re-run the host kit install on the host with the options of the first install "
                "(docker/install_host_bridge.sh --root <working root> [--from <subnet>], README \"Host kit\")")


class KitModeUnknown(RuntimeError):
    """The compose file sets the bridge key in a way not read here (interpolation, passthrough, env_file): whether
    it is the kit's cannot be told."""


def kit_mode(lay: str) -> bool:
    """True when the layout's compose file gives the container the host kit's bridge key. KitModeUnknown when a
    key line has a value with `$` (interpolation) or none at all (passthrough), or when an env_file: may set the
    key: the deploy refuses rather than skip the kit guard (D-235)."""
    path = COMPOSE_FILE if lay == "single" else COMPOSE_SERVICES
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    found = False
    for m in KIT_KEY_RE.finditer(text):
        rest = m.group("rest").strip()
        value = "" if rest.startswith("#") else re.sub(r"(^|[ \t]+)#.*$", "", rest[1:]).strip().strip("\"'")
        if value in ("", "null", "~"):
            raise KitModeUnknown(f"{path} has AFCLAUDE_BRIDGE_KEY without a value (passed through from the shell "
                                 "that runs compose): write the key path out literally, so the kit guard can tell")
        if "$" in value:
            raise KitModeUnknown(f"{path} sets AFCLAUDE_BRIDGE_KEY to {value!r} (an interpolation): write the "
                                 "path out literally, so the kit guard can tell")
        found = found or value.startswith(KIT_KEY_DIR)
    if not found and ENV_FILE_RE.search(text):
        raise KitModeUnknown(f"{path} has an env_file: source that may set AFCLAUDE_BRIDGE_KEY: set the key in the "
                             "compose file's environment literally, so the kit guard can tell")
    return found


def installed_kit_exec() -> str:
    """<share>/afclaude/host-kit/current/host_exec.py, the share directory as docker/install_host_bridge.sh picks it."""
    share = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(share, "afclaude", "host-kit", "current", "host_exec.py")


def installed_kit_answer(run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> dict:
    """The installed kit's own `kit-version` answer, asked on this host exactly as sshd runs the forced command
    (SSH_ORIGINAL_COMMAND set, which also makes the kit ignore its test overrides). RuntimeError when the kit is
    missing, refuses, times out or does not answer the JSON (D-234/D-235: the caller refuses the deploy)."""
    path = installed_kit_exec()
    if not os.path.isfile(path):
        raise RuntimeError(f"no host kit installed at {path}")
    env = dict(os.environ, SSH_ORIGINAL_COMMAND="kit-version")
    try:
        r = run([path], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=KIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"{path} kit-version: {e}")
    if r.returncode != 0:
        raise RuntimeError(f"{path} kit-version failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[:300]!r}")
    try:
        info = json.loads(r.stdout)
    except ValueError as e:
        raise RuntimeError(f"{path} kit-version answered no JSON ({e}): {r.stdout[:200]!r}")
    if not isinstance(info, dict) or not isinstance(info.get("kit_version"), str):
        raise RuntimeError(f"{path} kit-version answered no kit_version: {r.stdout[:200]!r}")
    return info


def check_host_kit(lay: str, answer: Callable[[], dict] = installed_kit_answer) -> bool:
    """False (printed why) when the layout uses the host kit and the code being deployed has other kit files
    (docker/host-kit/host_exec.py, ka_resume.sh) or another HOST_KIT_VERSION than the installed kit: the new
    container's host check would fail at once and pause every start (K6, review:k6-fixes) until the owner
    re-runs the install. Not in kit mode (the pre-kit compose.yml): True, nothing asked."""
    try:
        if not kit_mode(lay):
            return True
    except KitModeUnknown as e:
        print(f"deploy refused: {e}. Nothing was drained.")
        return False
    try:
        info = answer()
    except RuntimeError as e:
        print(f"deploy refused: the {lay} layout uses the host kit, but {e}: {INSTALL_HINT}. Nothing was drained.")
        return False
    mine = {"kit_version": host.HOST_KIT_VERSION, "files": host.kit_source_hashes()}
    theirs = {"kit_version": info.get("kit_version"), "files": info.get("files")}
    if mine == theirs:
        return True
    print(f"deploy refused: the installed host kit is not the one this code needs (installed {theirs!r}, this code "
          f"{mine!r}): the new container's host check would pause every start. First {INSTALL_HINT}, then deploy. "
          "Nothing was drained.")
    return False


def compose_command(*tail: str, lay: str = DEFAULT_LAYOUT) -> List[str]:
    """`docker compose -f docker/compose.yml <tail>` (single); services: `-f docker/compose.services.yml [-f
    docker/<override>] ...`, the overrides named by AFCLAUDE_COMPOSE_OVERRIDES (comma-separated file names in
    docker/, no paths)."""
    if lay == "single":
        return ["docker", "compose", "-f", COMPOSE_FILE] + list(tail)
    cmd = ["docker", "compose", "-f", COMPOSE_SERVICES]
    for name in (n.strip() for n in env_value("AFCLAUDE_COMPOSE_OVERRIDES").split(",")):
        if not name:
            continue
        if os.path.basename(name) != name or not name.endswith((".yml", ".yaml")):
            raise ValueError(f"AFCLAUDE_COMPOSE_OVERRIDES: {name!r} must be a compose file name in docker/")
        cmd += ["-f", os.path.join(os.path.dirname(COMPOSE_SERVICES), name)]
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
                                              "its container to go quiet, rebuild (the single container, or "
                                              "the three services and wait for their health checks).")
    ap.add_argument("--max-wait-drain", type=float, default=None,
                    help="minutes for runner.py drain (default: its own, the longest session-run "
                         "timeout + 5)")
    ap.add_argument("--max-wait-clear", type=float, default=DEFAULT_CLEAR_WAIT.total_seconds() / 60,
                    help="minutes to wait for the runner container to go quiet (default 10)")
    ap.add_argument("--max-wait-healthy", type=float, default=DEFAULT_HEALTHY_WAIT.total_seconds() / 60,
                    help="services layout: minutes to wait for the three health checks after the recreate (default 6)")
    ap.add_argument("--force", action="store_true",
                    help="rebuild even within 30 min of a session-window start or if draining or "
                         "clearing timed out (prints what was still running)")
    args = ap.parse_args(argv)

    try:
        lay = layout()
    except ValueError as e:
        print(f"deploy refused: {e}. Nothing was drained.")
        return 1
    if not check_scheduler_mode():             # before anything: --force does not skip it
        return 1
    if lay == "services" and (check_data() is None or not check_old_container()):
        return 1
    if not check_mcp_profile(lay):
        return 1
    if not check_host_kit(lay):              # --force does not skip it either: the start would pause, not race
        return 1
    if not check_start_guard(args.force):
        return 1
    conn = store.connect()
    try:
        drained = runner.drain(conn, timedelta(minutes=args.max_wait_drain)
                               if args.max_wait_drain is not None else None) == 0
        cleared = wait_clear(timedelta(minutes=args.max_wait_clear), lister=lambda: container_processes(CONTAINER if lay == "single" else f"{name_prefix()}-runner"))
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
    r = subprocess.run(compose_command("up", "-d", "--build", lay=lay), env=env)
    if r.returncode != 0:
        alert("deploy: docker compose up failed", f"exit {r.returncode} ({lay} layout): the old containers may still be "
              "running; the drain was lifted. See the deploy's output.")
        conn = store.connect()
        try:
            lift_drain(conn)               # no new runner boot will lift it (a failed build changes nothing)
        finally:
            conn.close()
        return r.returncode
    if lay == "single":
        return 0
    healthy: List[str] = []
    ok = wait_healthy(container_names(), timedelta(minutes=args.max_wait_healthy), out=lambda m: (healthy.append(m), print(m)))
    if not ok:
        alert("deploy: the services are not healthy", "; ".join(healthy)[:1200])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
