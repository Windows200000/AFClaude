#!/usr/bin/env python3
"""The K7 cut-over of this install (phase 5b, docs/phase5b_plan.md K7; D-339, D-341, D-257, D-234/D-235): from the one
container `afclaude` (docker/compose.yml, AFCLAUDE_LAYOUT=single) to the three services of docker/compose.services.yml
on the SAME data directory. A checked script, DRY-RUN by default:

    python3 tools/cutover_5b.py                 print the preflight and every step with its exact command; change nothing
    python3 tools/cutover_5b.py --apply         do it
    python3 tools/cutover_5b.py --apply --no-rollback    on a failure after the old container stopped, leave the new
                                                services up for a look instead of rolling back (the command is printed)

Dry-run changes nothing: it runs only read-only docker/git commands (ps, inspect, images, top, `compose config`,
`git rev-parse`: `Cutover.read` refuses anything else, tested) and prints each changing step as `$ <command>`.

Preflight (both modes; a failed one is exit 1 and in --apply nothing is touched):
  - no session-window start within 30 min before or after now (deploy.check_start_guard, the 22:30-23:30 /
    03:30-04:30 windows of the schedule included; no --force here, D-257);
  - docker/.env has what the services layout needs: AFCLAUDE_HOST_REPO, HOST_HOME, AFCLAUDE_BRIDGE_USER; the flips it
    still lacks are listed (AFCLAUDE_LAYOUT=services, AFCLAUDE_SCHEDULER=runner, AFCLAUDE_DATA = the repo's data/,
    AFCLAUDE_COMPOSE_OVERRIDES=compose.host-mounts.yml,compose.subnet.yml) and written only by --apply (the old file is
    kept as docker/.env.pre-k7-<time>);
  - the data directory holds the database and the host kit's bridge key (<data>/bridge/host_bridge_ed25519);
  - the host kit is installed and is the one this code needs (deploy.check_host_kit: D-121/D-338, the OWNER installs
    it: this script does not, and the no-kit transitional mode of K6 stays until a later commit);
  - the old image `afclaude-manager:latest` (or the kept tag) exists, `docker/compose.single.yml` (the rollback file)
    exists, no `afclaude-runner|mcp|web` container exists yet, and the target compose config validates;
  - the mcp profile (COMPOSE_PROFILES) only with roles_enforce on (deploy.check_mcp_profile).
Steps (--apply): tag the old image `afclaude-manager:pre-k7` (kept one week) -> build the services image (the old
container still runs) -> drain the runner (through the container, deploy.drain) -> wait until it is quiet -> start guard
again -> stop the old container -> write the .env flips -> `compose up -d` -> wait for the health checks -> `afclaude
host check` in the new runner -> print the MCP registration line and the rollback command.
Every failure is an alert (notify) and exit 1, no silent fallback. Before the old container is stopped it only lifts the
drain; after, it rolls back: remove the new containers, restore docker/.env, `docker compose -f docker/compose.single.yml
up -d` (the kept old image, no build), wait healthy. A failed rollback is a second alert with the manual commands.
All docker, compose and git calls go through ONE injectable runner (`Cutover.run`); tests stub it (test_cutover_5b.py).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Mapping, Optional

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deploy  # noqa: E402  (the guards, the drain, the health wait: one implementation)
import store  # noqa: E402

Run = Callable[..., "subprocess.CompletedProcess[str]"]

OLD_IMAGE = "afclaude-manager:latest"          # compose.yml's image: the running old container's
ROLLBACK_TAG = "afclaude-manager:pre-k7"       # what docker/compose.single.yml runs; kept for ROLLBACK_KEEP
ROLLBACK_KEEP = timedelta(days=7)
OLD_CONTAINER = deploy.CONTAINER
COMPOSE_SINGLE = os.path.join(HERE, "docker", "compose.single.yml")
DEFAULT_OVERRIDES = "compose.host-mounts.yml,compose.subnet.yml"
HOST_KEYS = ("AFCLAUDE_HOST_REPO", "HOST_HOME", "AFCLAUDE_BRIDGE_USER")     # docker/.env, needed by compose.host-mounts.yml
BRIDGE_KEY = os.path.join("bridge", "host_bridge_ed25519")
MCP_LINE = "claude mcp add --scope user afclaude -- afclaude mcp stdio"
BUILD_LIMIT = 1800       # seconds for the image build
UP_LIMIT = 600
STOP_LIMIT = 150         # `docker stop` of the old container: its stop grace is 90 s
CHECK_LIMIT = 600        # `afclaude host check` (the non-e2e checks' budgets add up to less)
EXEC_LIMIT = 60
HEALTH_WAIT = timedelta(minutes=6)
CLEAR_WAIT = timedelta(minutes=10)

READ_ONLY_DOCKER = ("ps", "inspect", "images", "top")
COMPOSE_VALUE_OPTIONS = ("-f", "--file", "-p", "--project-name", "--profile", "--env-file", "--project-directory")


class CutoverError(RuntimeError):
    """A step failed: the caller alerts (D-235) and, once the old container is stopped, rolls back."""


def compose_subcommand(argv: List[str]) -> str:
    """The subcommand of a `docker compose [options] <subcommand> ...` call ('' when none)."""
    i = 2
    while i < len(argv):
        if argv[i] in COMPOSE_VALUE_OPTIONS:
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            return argv[i]
    return ""


def is_read_only(argv: List[str]) -> bool:
    """Whether the call only reads (what a dry-run may run): docker ps | inspect | images | image inspect |
    compose ... config, and `git -C <dir> rev-parse`."""
    if argv[:1] == ["git"]:
        return len(argv) >= 4 and argv[1] == "-C" and argv[3] == "rev-parse"
    if argv[:1] != ["docker"] or len(argv) < 2:
        return False
    if argv[1] in READ_ONLY_DOCKER or argv[1:3] == ["image", "inspect"]:
        return True
    return argv[1] == "compose" and compose_subcommand(argv) == "config"


def render(argv: List[str], env: Optional[Mapping[str, str]] = None) -> str:
    """A command as printed: `NAME=value ... docker ...` (the variables the call needs beyond the process's own)."""
    return " ".join([f"{k}={shlex.quote(v)}" for k, v in (env or {}).items()] + [shlex.quote(a) for a in argv])


def replace_file(path: str, text: str) -> None:
    """Write `text` over `path` atomically (temp file in the same directory, mode kept)."""
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o600
    fd, tmp = tempfile.mkstemp(prefix=".cutover-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def set_env_lines(text: str, flips: Mapping[str, str]) -> str:
    """docker/.env's text with every `KEY=...` line of a flipped key replaced (comments untouched), the missing ones
    appended under a marker line."""
    lines = text.splitlines()
    done: set[str] = set()
    for i, line in enumerate(lines):
        key = line.strip().partition("=")[0].strip()
        if key in flips and "=" in line and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={flips[key]}"
            done.add(key)
    missing = [k for k in flips if k not in done]
    if missing:
        lines.append("# phase 5b K7 cut-over (tools/cutover_5b.py)")
        lines += [f"{k}={flips[k]}" for k in missing]
    return "\n".join(lines) + "\n"


class Cutover:
    """One run. `run` is the only way a docker/compose/git command is made (no default: a test must stub it)."""

    def __init__(self, run: Run, apply: bool, *, rollback: bool = True,
                 out: Callable[[str], object] = print,
                 alert: Callable[[str, str], object] = deploy.alert,
                 sleep: Callable[[float], object] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 environ: Optional[Mapping[str, str]] = None):
        self.run = run
        self.apply = apply
        self.rollback_enabled = rollback
        self.out = out
        self.alert = alert
        self.sleep = sleep
        self.clock = clock
        self.now = now
        self.environ = dict(os.environ if environ is None else environ)
        self.problems: List[str] = []
        self.target: dict[str, str] = {}
        self.flips: dict[str, str] = {}
        self.version = ""
        self.old_running = False
        self.have_pre_tag = False
        self.drained = False
        self.old_stopped = False
        self.env_backup: Optional[str] = None
        self.prefix = deploy.NAME

    # --- the one place a command is made --------------------------------------------------------------------

    def read(self, argv: List[str], *, timeout: int = 30,
             env: Optional[Mapping[str, str]] = None) -> "subprocess.CompletedProcess[str]":
        """Run a READ-ONLY command (both modes). Anything else is a bug here: ValueError. env: variables on top of the
        process's own (the target configuration, for `compose config`)."""
        if not is_read_only(argv):
            raise ValueError(f"not a read-only command: {render(argv)}")
        kw: dict[str, Any] = {} if env is None else {"env": dict(self.environ, **env)}
        return self.run(argv, capture_output=True, text=True, timeout=timeout, **kw)

    def act(self, argv: List[str], *, timeout: int, env: Optional[Mapping[str, str]] = None,
            capture: bool = True) -> "Optional[subprocess.CompletedProcess[str]]":
        """A changing command: printed always, run only with --apply (None in a dry-run). Output is captured and
        printed when `capture`, else it streams through (the build)."""
        self.out(f"    $ {render(argv, env)}")
        if not self.apply:
            return None
        kw: dict[str, Any] = {"timeout": timeout}
        if env is not None:
            kw["env"] = dict(self.environ, **env)
        if capture:
            kw.update(capture_output=True, text=True)
        try:
            r: "subprocess.CompletedProcess[str]" = self.run(argv, **kw)
        except subprocess.TimeoutExpired:
            raise CutoverError(f"`{render(argv)}` did not finish within {timeout} s")
        except OSError as e:
            raise CutoverError(f"`{render(argv)}`: {e}")
        if capture and (r.stdout or r.stderr):
            self.out("      " + ((r.stdout or "") + (r.stderr or "")).strip().replace("\n", "\n      ")[:2000])
        return r

    def step(self, argv: List[str], what: str, *, timeout: int, env: Optional[Mapping[str, str]] = None,
             capture: bool = True) -> None:
        r = self.act(argv, timeout=timeout, env=env, capture=capture)
        if r is not None and r.returncode != 0:
            raise CutoverError(f"{what} failed (rc {r.returncode}): `{render(argv)}`"
                               + (f": {((r.stderr or r.stdout) or '').strip()[:300]}" if capture else ""))

    # --- configuration -----------------------------------------------------------------------------------------

    def get(self, name: str) -> str:
        """A variable as compose resolves it: the shell environment, else docker/.env."""
        return deploy.env_value(name, deploy.ENV_FILE, self.environ)

    def compose(self, *tail: str) -> List[str]:
        """`docker compose -f compose.services.yml [-f override]... <tail>` for the TARGET configuration."""
        cmd = ["docker", "compose", "-f", deploy.COMPOSE_SERVICES]
        for name in (n.strip() for n in self.target["AFCLAUDE_COMPOSE_OVERRIDES"].split(",")):
            if name:
                cmd += ["-f", os.path.join(os.path.dirname(deploy.COMPOSE_SERVICES), name)]
        return cmd + list(tail)

    def compose_env(self) -> dict[str, str]:
        return dict(self.target, AFCLAUDE_CODE_VERSION=self.version)

    def data_dir(self) -> str:
        return os.path.abspath(os.path.join(os.path.dirname(deploy.COMPOSE_SERVICES), self.target["AFCLAUDE_DATA"]))

    def runner_container(self) -> str:
        return f"{self.prefix}-runner"

    # --- preflight ---------------------------------------------------------------------------------------------

    def fail(self, what: str, detail: str) -> None:
        self.problems.append(f"{what}: {detail}")
        self.out(f"  [FAIL] {what}: {detail}")

    def ok(self, what: str, detail: str = "") -> None:
        self.out(f"  [ok]   {what}" + (f": {detail}" if detail else ""))

    def captured(self, check: Callable[[], bool]) -> "tuple[bool, str]":
        """A deploy.py guard prints why it refuses: take that text as the detail."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            good = check()
        return good, " ".join(buf.getvalue().split())

    # overridable by tests (they would otherwise read the real schedule, kit, database)
    def guard_ok(self) -> bool:
        return deploy.check_start_guard(False, self.now())

    def kit_ok(self) -> bool:
        return deploy.check_host_kit("services")

    def mcp_ok(self) -> bool:
        return deploy.check_mcp_profile("services")

    def writers_container(self) -> bool:
        conn = store.connect()
        try:
            return deploy.container_writes(conn)
        finally:
            conn.close()

    def preflight(self) -> bool:
        self.out("Preflight")
        env_ok = self.check_env()
        good, why = self.captured(self.guard_ok)
        (self.ok if good else self.fail)("session-window start guard", why or "no start within 30 min")
        if env_ok:
            self.check_data()
            self.check_images()
            self.check_containers()
            self.check_compose()
            good, why = self.captured(self.mcp_ok)
            (self.ok if good else self.fail)("mcp profile / roles", why or ("roles on" if deploy.mcp_enabled() else "mcp profile off"))
        good, why = self.captured(self.kit_ok)
        (self.ok if good else self.fail)("host kit", why or "the installed kit is the one this code needs")
        if not os.path.isfile(COMPOSE_SINGLE):
            self.fail("rollback file", f"{COMPOSE_SINGLE} is missing")
        else:
            self.ok("rollback file", "docker/compose.single.yml (runs the kept old image, no build)")
        return not self.problems

    def check_env(self) -> bool:
        missing = [k for k in HOST_KEYS if not self.get(k)]
        if not os.path.isfile(deploy.ENV_FILE):
            self.fail("docker/.env", f"{deploy.ENV_FILE} does not exist")
            return False
        if missing:
            self.fail("docker/.env", f"{', '.join(missing)} not set (the host mounts override needs them, see docker/env.example)")
            return False
        repo = self.get("AFCLAUDE_HOST_REPO")
        self.target = {
            "AFCLAUDE_LAYOUT": "services",
            "AFCLAUDE_SCHEDULER": "runner",
            "AFCLAUDE_DATA": self.get("AFCLAUDE_DATA") or os.path.join(repo, "data"),
            "AFCLAUDE_COMPOSE_OVERRIDES": self.get("AFCLAUDE_COMPOSE_OVERRIDES") or DEFAULT_OVERRIDES,
        }
        self.flips = {k: v for k, v in self.target.items() if self.get(k) != v}
        self.prefix = self.get("AFCLAUDE_NAME") or deploy.NAME
        bad = []
        for name in (n.strip() for n in self.target["AFCLAUDE_COMPOSE_OVERRIDES"].split(",")):
            path = os.path.join(os.path.dirname(deploy.COMPOSE_SERVICES), name)
            if name and (os.path.basename(name) != name or not name.endswith((".yml", ".yaml")) or not os.path.isfile(path)):
                bad.append(name)
        if bad:
            self.fail("docker/.env", f"AFCLAUDE_COMPOSE_OVERRIDES names {', '.join(bad)}: each must be a compose file name in docker/")
            return False
        if self.flips:
            self.ok("docker/.env", "flips still needed (written by --apply, the old file kept): "
                    + ", ".join(f"{k}={v}" for k, v in self.flips.items()))
        else:
            self.ok("docker/.env", "already set for the services layout")
        return True

    def check_data(self) -> None:
        data = self.data_dir()
        db = os.path.join(data, deploy.DB_NAME)
        if not os.path.isdir(data) or not os.path.isfile(db):
            self.fail("data directory", f"no database at {db}: a wrong or empty directory would come up as a fresh install")
            return
        key = os.path.join(data, BRIDGE_KEY)
        if not os.path.isfile(key):
            self.fail("data directory", f"{key} is missing: run the host kit install first (it makes <data>/bridge/, the "
                      "services' bridge key)")
            return
        store.DB_PATH = db          # the drain and the checks below talk to the database the services will mount
        self.ok("data directory", f"{data} (database and bridge key present)")

    def image_id(self, ref: str) -> Optional[str]:
        r = self.read(["docker", "image", "inspect", "--format", "{{.Id}}", ref])
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    def check_images(self) -> None:
        latest, pinned = self.image_id(OLD_IMAGE), self.image_id(ROLLBACK_TAG)
        self.have_pre_tag = pinned is not None
        if latest is None and pinned is None:
            self.fail("old image", f"neither {OLD_IMAGE} nor {ROLLBACK_TAG} exists: there would be nothing to roll back to")
        elif pinned is not None and latest is not None and pinned != latest:
            self.ok("old image", f"{ROLLBACK_TAG} already exists (an earlier attempt) and is kept; {OLD_IMAGE} is another image")
        elif pinned is not None:
            self.ok("old image", f"{ROLLBACK_TAG} already points at it")
        else:
            self.ok("old image", f"{OLD_IMAGE} will be tagged {ROLLBACK_TAG}")

    def check_containers(self) -> None:
        r = self.read(["docker", "ps", "--filter", f"name=^{OLD_CONTAINER}$", "--format", "{{.Names}}"])
        if r.returncode != 0:
            self.fail("old container", f"docker ps failed: {(r.stderr or r.stdout).strip()[:200]}")
            return
        self.old_running = OLD_CONTAINER in r.stdout.split()
        self.ok("old container", f"`{OLD_CONTAINER}` is running (drained and stopped by the steps)" if self.old_running
                else f"`{OLD_CONTAINER}` is not running (an earlier attempt: drain and stop are skipped)")
        r = self.read(["docker", "ps", "-a", "--format", "{{.Names}}"])
        if r.returncode != 0:
            self.fail("new containers", f"docker ps -a failed: {(r.stderr or r.stdout).strip()[:200]}")
            return
        there = [c for c in self.service_names(True) if c in r.stdout.split()]
        if there:
            self.fail("new containers", f"{', '.join(there)} already exist: a services stack is there (remove them with "
                      "`docker rm -f` if they are leftovers of a failed attempt)")
        else:
            self.ok("new containers", "none of the three services exists yet")

    def service_names(self, all_of_them: bool = False) -> List[str]:
        return deploy.container_names(self.prefix, with_mcp=True if all_of_them else None)

    def check_compose(self) -> None:
        self.version = self.git_version()
        r = self.read(self.compose("config", "-q"), env=self.compose_env())
        if r.returncode != 0:
            self.fail("compose config", f"the services configuration does not validate: {(r.stderr or r.stdout).strip()[:300]}")
        else:
            self.ok("compose config", f"validates with the target environment (image afclaude:{self.version})")

    def git_version(self) -> str:
        r = self.read(["git", "-C", HERE, "rev-parse", "--short", "HEAD"])
        if r.returncode != 0 or not r.stdout.strip():
            raise CutoverError(f"git rev-parse failed: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout.strip()

    # --- the steps -----------------------------------------------------------------------------------------------

    def drain_command(self) -> str:
        if self.writers_container():
            return f"docker exec {OLD_CONTAINER} afclaude runner drain"
        return "runner.drain on the host's own database connection (the database is not marked writers=container)"

    def go(self) -> int:
        """-> the exit code. Dry-run: preflight + the plan; --apply: the steps."""
        mode = "APPLY" if self.apply else "DRY RUN (nothing is changed; --apply does it)"
        self.out(f"K7 cut-over to the three services: {mode}")
        try:
            clear = self.preflight()
        except CutoverError as e:
            self.out(f"cut-over refused: {e}")
            return 1
        if not self.apply:
            if self.target:
                self.plan()
            self.out("")
            self.out("Preflight OK: --apply would run the steps above now." if clear else
                     f"Preflight found {len(self.problems)} problem(s) (above): --apply would refuse.")
            return 0 if clear else 1
        if not clear:
            self.out("cut-over refused (preflight above); nothing was touched")
            return 1
        try:
            self.steps()
        except CutoverError as e:
            return self.failed(str(e))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
            return self.failed(f"{type(e).__name__}: {e}")
        self.finish()
        return 0

    def plan(self) -> None:
        self.out("")
        self.out("Steps")
        self.out(f"  1. keep the old image for {ROLLBACK_KEEP.days} days of rollback")
        if self.have_pre_tag:
            self.out(f"     ({ROLLBACK_TAG} exists: kept, no new tag)")
        else:
            self.act(["docker", "tag", OLD_IMAGE, ROLLBACK_TAG], timeout=EXEC_LIMIT)
        self.out("  2. build the services image (the old container keeps running)")
        self.act(self.compose("build", "afclaude-runner"), timeout=BUILD_LIMIT, env=self.compose_env())
        if self.old_running:
            self.out("  3. drain the runner, wait until it is quiet, check the session-window guard again")
            self.out(f"    $ {self.drain_command()}")
            self.out(f"    (waits up to {CLEAR_WAIT.seconds // 60} min for sampler / usage review / unittest / claude processes in `{OLD_CONTAINER}`)")
            self.out("  4. stop the old container (its runner records a clean exit)")
            self.act(["docker", "stop", OLD_CONTAINER], timeout=STOP_LIMIT)
        else:
            self.out("  3./4. drain and stop: skipped, the old container is not running")
        self.out("  5. docker/.env flips" + (": " + ", ".join(f"{k}={v}" for k, v in self.flips.items()) if self.flips
                                           else ": none needed"))
        self.out("  6. start the three services on the same data directory")
        self.act(self.compose("up", "-d"), timeout=UP_LIMIT, env=self.compose_env())
        self.out(f"  7. wait for health: {', '.join(self.service_names())} (up to {HEALTH_WAIT.seconds // 60} min; the new runner's boot lifts the drain)")
        self.out("  8. the host checks in the new runner")
        self.act(self.host_check_command(), timeout=CHECK_LIMIT)
        self.out("  9. then (owner, once): switch the MCP registration to the container")
        self.out(f"    $ {MCP_LINE}")
        self.out("  Rollback (automatic after a failure past step 4, unless --no-rollback; by hand:)")
        for line in self.rollback_commands():
            self.out(f"    $ {line}")

    def host_check_command(self) -> List[str]:
        return ["docker", "exec", self.runner_container(), "afclaude", "host", "check"]

    def rollback_commands(self) -> List[str]:
        stamp = "<docker/.env.pre-k7-...>"
        return ([f"docker stop {c} && docker rm {c}" for c in self.service_names(True)]
                + [f"cp {stamp} docker/.env        (only if the cut-over wrote it: it kept the old file there)",
                   render(["docker", "compose", "-f", COMPOSE_SINGLE, "up", "-d"])
                   + f"        (runs {ROLLBACK_TAG}, no build; then `docker ps` for `{OLD_CONTAINER}` healthy)"])

    def steps(self) -> None:
        self.out("")
        self.out("Steps")
        if not self.have_pre_tag:
            self.out("  1. keep the old image")
            self.step(["docker", "tag", OLD_IMAGE, ROLLBACK_TAG], "tagging the old image", timeout=EXEC_LIMIT)
        else:
            self.out(f"  1. {ROLLBACK_TAG} exists: kept")
        self.out("  2. build the services image")
        self.step(self.compose("build", "afclaude-runner"), "building the services image", timeout=BUILD_LIMIT,
                  env=self.compose_env(), capture=False)
        if self.old_running:
            self.out("  3. drain")
            self.drain_old()
            self.out("  4. stop the old container")
            self.stop_old()
        else:
            self.out("  3./4. skipped: the old container is not running")
            self.old_stopped = True
        self.out("  5. docker/.env")
        self.write_env()
        self.out("  6. start the services")
        self.step(self.compose("up", "-d"), "starting the services", timeout=UP_LIMIT, env=self.compose_env(), capture=False)
        self.out("  7. health")
        self.wait_healthy(self.service_names(), "the services")
        self.out("  8. host checks")
        self.step(self.host_check_command(), "`afclaude host check` in the new runner", timeout=CHECK_LIMIT)

    def drain_old(self) -> None:
        conn = store.connect()
        try:
            if not deploy.drain(conn, OLD_CONTAINER, None, run=self.run):
                raise CutoverError("the runner could not be drained (a session run is still going): rerun later; the drain is lifted")
            self.drained = True
        finally:
            conn.close()
        cleared = deploy.wait_clear(CLEAR_WAIT, lister=lambda: deploy.container_processes(OLD_CONTAINER, run=self.run),
                                    sleep=self.sleep, clock=self.clock, out=self.out)
        if not cleared:
            raise CutoverError("the old container did not go quiet within 10 min (a bridge call is in flight)")
        good, why = self.captured(self.guard_ok)
        if not good:
            raise CutoverError(f"a session-window start came too close during the drain: {why}")

    def stop_old(self) -> None:
        try:
            r = self.act(["docker", "stop", OLD_CONTAINER], timeout=STOP_LIMIT)
            stopped = r is not None and r.returncode == 0
            detail = "" if r is None else (r.stderr or r.stdout or "").strip()[:300]
        except CutoverError as e:
            stopped, detail = False, str(e)
        if stopped:
            self.old_stopped = True
            return
        r2 = self.read(["docker", "ps", "--filter", f"name=^{OLD_CONTAINER}$", "--format", "{{.Names}}"])
        if r2.returncode == 0 and OLD_CONTAINER not in r2.stdout.split():
            self.old_stopped = True      # it is down although docker stop complained: past the point of the plain abort
        raise CutoverError(f"`docker stop {OLD_CONTAINER}` failed: {detail}")

    def write_env(self) -> None:
        if not self.flips:
            self.out("     docker/.env already says it: nothing written")
            return
        path = deploy.ENV_FILE
        backup = f"{path}.pre-k7-{self.now().strftime('%Y%m%dT%H%M%S')}"
        shutil.copy2(path, backup)
        self.env_backup = backup
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        replace_file(path, set_env_lines(text, self.flips))
        self.out(f"     wrote {', '.join(self.flips)} (the old file is kept as {backup})")

    def wait_healthy(self, containers: List[str], what: str) -> None:
        log: List[str] = []

        def say(m: str) -> None:
            log.append(m)
            self.out("     " + m)
        ok = deploy.wait_healthy(containers, HEALTH_WAIT, state=lambda c: deploy.health_state(c, self.run),
                                 sleep=self.sleep, clock=self.clock, out=say)
        if not ok:
            raise CutoverError(f"{what} not healthy: {' '.join(log)[:600]}")

    # --- failure, rollback, the end ------------------------------------------------------------------------------

    def failed(self, reason: str) -> int:
        self.out(f"CUT-OVER FAILED: {reason}")
        if not self.old_stopped:
            self.alert("cut-over: failed before the old container was stopped",
                       f"{reason}. Nothing was cut over; the drain is lifted again.")
            if self.drained:
                conn = store.connect()
                try:
                    deploy.undrain(conn, OLD_CONTAINER, run=self.run)
                finally:
                    conn.close()
                self.out("drain lifted (the old container keeps running)")
            return 1
        if not self.rollback_enabled:
            self.alert("cut-over: failed after the old container was stopped (no rollback requested)",
                       f"{reason}. The old container is stopped and the services may be up; roll back with: "
                       + "; ".join(self.rollback_commands()))
            self.out("--no-rollback: left as it is. To roll back:")
            for line in self.rollback_commands():
                self.out(f"    $ {line}")
            return 1
        self.alert("cut-over: failed after the old container was stopped, rolling back",
                   f"{reason}. Removing the new containers and starting the old image ({ROLLBACK_TAG}) again.")
        try:
            self.rollback()
        except (CutoverError, OSError, RuntimeError, subprocess.TimeoutExpired) as e:
            self.alert("cut-over: the ROLLBACK failed", f"{e}. By hand: " + "; ".join(self.rollback_commands()))
            self.out(f"ROLLBACK FAILED: {e}")
            return 1
        self.out(f"rolled back: `{OLD_CONTAINER}` runs the old image again (the cut-over failed: {reason})")
        return 1

    def rollback(self) -> None:
        self.out("Rollback")
        for name in self.service_names(True):
            self.out(f"  removing {name}")
            deploy.remove_container(name, run=self.run, project=deploy.compose_project())
        if self.env_backup:
            with open(self.env_backup, encoding="utf-8") as fh:
                replace_file(deploy.ENV_FILE, fh.read())
            self.out(f"  docker/.env restored from {self.env_backup}")
        self.step(["docker", "compose", "-f", COMPOSE_SINGLE, "up", "-d"], "starting the old image", timeout=UP_LIMIT,
                  capture=False)
        self.wait_healthy([OLD_CONTAINER], "the old container")

    def finish(self) -> None:
        until = (self.now() + ROLLBACK_KEEP).strftime("%Y-%m-%d")
        self.out("")
        self.out("Cut-over done: three services on the same data directory, host checks passed.")
        self.out("Owner steps:")
        self.out("  - switch the interactive MCP registration to the container (once; needs the afclaude wrapper of host kit 7 and the mcp profile):")
        self.out(f"      {MCP_LINE}")
        self.out(f"  - the old image stays tagged {ROLLBACK_TAG} until {until} (rollback week); then:  docker rmi {ROLLBACK_TAG}")
        self.out("  - to roll back before then:")
        for line in self.rollback_commands():
            self.out(f"      {line}")
        self.out("  - docker/.env now says AFCLAUDE_LAYOUT=services: `python3 tools/deploy.py` deploys the three services from here on.")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="The K7 cut-over to the three-service layout (dry-run unless --apply).")
    ap.add_argument("--apply", action="store_true", help="do it (the default prints the preflight and the steps only)")
    ap.add_argument("--no-rollback", action="store_true",
                    help="after a failure past the old container's stop, leave the new services up instead of rolling back")
    args = ap.parse_args(argv)
    return Cutover(subprocess.run, args.apply, rollback=not args.no_rollback).go()


if __name__ == "__main__":
    sys.exit(main())
