#!/usr/bin/env python3
"""Host checks (phase 5b K6, docs/phase5b_plan.md §3.6, design §10.5; D-164, D-120, D-121, D-338).

AFClaude runs in a container; its Claude sessions run on the host behind the SSH bridge (the host kit,
docker/host-kit/host_exec.py). These checks validate that host setup: the bridge answers, the installed kit
is the one this code needs, tmux works, Claude Code runs and is logged in, the MCP server starts, usage is
readable, `claude doctor` is clean, the disks have room and the clocks agree.

    python3 hostcheck.py check [name ...] [--blocking]   run the checks (all scheduled ones, or those named; `e2e`
                                                         only by name), record and print them. `afclaude host check`.
    python3 hostcheck.py status                          print the latest recorded results

Where it runs: the runner's hourly `host_check` job (runner_jobs.host_check_tick) and, right before a
session-window start, the blocking subset (pre_start, called by keepalive.window_start_pass). Each run
records one row per check in `host_checks` (actions.hostcheck_put) and writes one app_log line.

Severity (the plan's list): a check is blocking or not. A FAIL of a blocking check (bridge, kit, tmux, claude,
logged_in, mcp) is `failstop.fail("hostcheck", ...)`: alert + pause until the task-manager has looked at it
and cleared it (D-257, D-283, D-235); the start is not attempted. failstop alerts once per pause (the subject
is stable: "host check <name> failed"). A FAIL of another check (usage, doctor, disk, clock) alerts once per
episode and does not pause (the episode is the row's `since`; its `alerted_at` says it was alerted, so a fail a
manual run recorded first is still alerted by the next scheduled one). A WARN is recorded and shown, never
alerted. A check that does not answer within its budget is a FAIL (D-234); an exception inside a check is a
FAIL naming it (D-235): a flaky check is fixed here, not muted.

A check whose precondition failed is `skip`ped with the reason: if the bridge or the kit check fails (or no kit
is installed, below), the rest of the run is skipped, not failed one by one.

TRANSITIONAL (until K7, like D-339's single layout): the owner has not installed the host kit yet, the live
bridge is still the legacy docker/host_exec.py, so `host.kit_info()` returns None. While it does, a run records
ONE warn, "kit not installed", on the `kit` check (no pause, no alert, the remaining checks are skipped) and
the pre-start hook lets the start go. That branch is `NO_KIT` below and is the only place that treats a missing
kit as anything but an error; K7 removes it (kit_info() None becomes a fail then).

e2e (on demand only, never on a schedule): a Haiku session in the tmux session `afc-check-e2e`: start it with
ka_resume.sh, wait for its reply, kill it.
"""
from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import actions  # noqa: E402
import failstop  # noqa: E402
import host  # noqa: E402  (untyped: looked up per call below, tests patch it)
import remote_control  # noqa: E402
import runner_state  # noqa: E402
import store  # noqa: E402

COMPONENT = "hostcheck"
HOST_KIT_ERROR: type[Exception] = getattr(host, "HostKitError")
ACTOR = "runner:host_check"        # the runner job and the pre-start hook
CLI_ACTOR = "cli"                  # `afclaude host check` (a manual run records its results too)
BERLIN = ZoneInfo("Europe/Berlin")
STATUSES = ("ok", "warn", "fail", "skip")
KIT_FIX = "re-run the host kit install on the host (docker/install_host_bridge.sh, D-121)"
# TRANSITIONAL (module doc, K7 removes it): what the `kit` check records while no kit is installed.
NO_KIT = "kit not installed"
NO_KIT_DETAIL = (f"{NO_KIT}: the host bridge is still the legacy docker/host_exec.py; the other checks need the "
                 "host kit and are skipped, and nothing is paused or blocked (transitional until K7)")
NO_KIT_FIX = "run docker/install_host_bridge.sh on the host (D-121); K7 turns this warning into a failure"
E2E_TMUX = "afc-check-e2e"
E2E_WAIT = 150.0                    # seconds the e2e check waits for the reply
E2E_POLL = 5.0
USAGE_WARN_MIN = 90.0               # minutes since the usage cache was fetched: warn, fail at USAGE_FAIL_MIN
USAGE_FAIL_MIN = 360.0
CLOCK_WARN_S = 5.0                  # plan §3.6
CLOCK_FAIL_S = 60.0
BRIDGE_FIRST_TRY = 30.0             # seconds the bridge check's first call may take (keepalive's tmux_has allows
                                    # 30 s too); the retry gets the rest of its budget (CHECKS: 40)
_VERSION_LINE = re.compile(r"Running:[^(\n]*\(([0-9]+(?:\.[0-9]+)*)\)")

Fn = Callable[["Ctx"], "Result"]


@dataclass
class Result:
    status: str
    detail: str = ""
    fix: str = ""
    kit_version: Optional[str] = None


@dataclass
class Check:
    name: str
    blocking: bool
    budget: float                   # seconds: the whole check must answer within it, else it fails (D-234)
    fn: Fn
    scheduled: bool = True          # False: only by name (e2e)


@dataclass
class Outcome:
    name: str
    blocking: bool
    status: str
    detail: str
    fix: str
    duration_ms: int
    kit_version: Optional[str]


@dataclass
class Ctx:
    """One run's shared state: the DB connection (settings), the kit's answer, the calls shared by two checks, and
    the current check's deadline (call() never waits beyond it)."""
    conn: Any
    kit: Optional[dict[str, Any]] = None
    auth: Optional[dict[str, Any]] = None
    doctor: Optional[tuple[int, str]] = None
    deadline: float = 0.0
    budget: float = 0.0
    gate: Optional[str] = None      # why the rest of the run is skipped (a failed bridge or kit; no kit)

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired("host check", self.budget)
        return left

    def call(self, argv: list[str], input: Optional[str] = None,
             cap: Optional[float] = None) -> "subprocess.CompletedProcess[str]":
        """host.run_on_host(argv), within the check's remaining budget (TimeoutExpired = the check fails) and, given
        `cap`, within cap seconds (a call the check retries leaves room for the retry)."""
        run: Callable[..., Any] = getattr(host, "run_on_host")
        left = self.remaining()
        r: subprocess.CompletedProcess[str] = run(argv, input=input, timeout=left if cap is None else min(left, cap))
        return r

    def setting(self, key: str) -> Any:
        return actions.get_setting(self.conn, key)


def _first(text: str, n: int = 300) -> str:
    return " ".join(text.split())[:n]


def _bytes_gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


# ------------------------------------------------------------------ the checks

def check_bridge(ctx: Ctx) -> Result:
    """The ssh handshake: a harmless whitelisted command both the legacy bridge and the kit answer (tmux
    has-session: 0 or 1 = reached the host and ran tmux). Retried once on ssh rc 255 or no answer within
    BRIDGE_FIRST_TRY (a stale ControlMaster socket, a loaded host): one blip must not pause automation and hold a
    window start; the retry gets the rest of the budget, and its answer decides."""
    argv = ["tmux", "has-session", "-t", "=afc-check"]
    retried = ""
    try:
        r = ctx.call(argv, cap=BRIDGE_FIRST_TRY)
    except subprocess.TimeoutExpired:
        r = None
        retried = f" (retried once after no answer within {BRIDGE_FIRST_TRY:g} s)"
    if r is not None and r.returncode == 255:
        retried = f" (retried once after ssh rc 255: {_first(r.stderr or r.stdout, 160)})"
    if r is None or r.returncode == 255:
        r = ctx.call(argv)
    if r.returncode in (0, 1):
        return Result("ok", "the host bridge answers" + retried)
    err = _first(r.stderr or r.stdout)
    if r.returncode == 255:
        return Result("fail", f"the bridge refused or could not be reached (ssh rc 255){retried}: {err}",
                      "check that the host's authorized_keys line for the bridge key has a from= covering the "
                      "runner's network, that the host key in known_hosts is current, and that sshd runs")
    return Result("fail", f"the bridge answered rc {r.returncode}{retried}: {err}", "see the host's host_exec log")


def check_kit(ctx: Ctx) -> Result:
    """The installed kit against this code (host.check_host: version, file hashes, host.json's repo)."""
    check_host: Callable[..., Any] = getattr(host, "check_host")
    info = check_host(timeout=30)
    if info is None:        # TRANSITIONAL (module doc): no kit installed yet
        ctx.gate = NO_KIT
        return Result("warn", NO_KIT_DETAIL, NO_KIT_FIX)
    ctx.kit = info
    n = len(info.get("commands") or [])
    return Result("ok", f"host kit {info['kit_version']} ({n} commands), files and repo path match this code",
                  kit_version=str(info["kit_version"]))


def check_tmux(ctx: Ctx) -> Result:
    """The kit creates, sees and kills the tmux session afc-check."""
    r = ctx.call(["tmux-check"])
    if r.returncode == 0 and r.stdout.strip() == "ok":
        return Result("ok", "tmux creates, sees and kills a session")
    if r.returncode == 124:
        return Result("fail", "tmux-check stopped by the kit after its time limit", "check that tmux is installed "
                      "and not hanging on the host (the kit's own limit fired)")
    return Result("fail", f"tmux-check failed (rc {r.returncode}): {_first(r.stderr or r.stdout)}",
                  "install tmux on the host (or check its server: `tmux kill-server` on an idle host)")


def _auth_status(ctx: Ctx) -> tuple[int, str]:
    r = ctx.call(["claude", "auth", "status"])
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def check_claude(ctx: Ctx) -> Result:
    """Claude Code is installed and runs: `claude auth status` answers (the kit's answer is kept for logged_in)."""
    rc, text = _auth_status(ctx)
    if rc == 124:
        return Result("fail", "`claude auth status` stopped by the kit after its time limit",
                      "run `claude auth status` on the host by hand: it should answer at once")
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and "loggedIn" in doc:
        ctx.auth = doc
        return Result("ok", "Claude Code runs (`claude auth status` answers)")
    if rc == 1 and re.search(r"not logged in", text, re.I):     # the CLI's own text, not the kit's denial
        ctx.auth = {"loggedIn": False}
        return Result("ok", "Claude Code runs (`claude auth status` answers: not logged in)")
    return Result("fail", f"`claude auth status` gave no status (rc {rc}): {_first(text)}",
                  "check that Claude Code is installed on the host and that host.json's `claude` names it")


def check_logged_in(ctx: Ctx) -> Result:
    a = ctx.auth
    if a is None:
        return Result("skip", "the claude check did not answer")
    if not a.get("loggedIn"):
        return Result("fail", "Claude Code on the host is not logged in",
                      "log in on the host (`claude auth login`, or /login in a session), then clear the pause")
    method = str(a.get("authMethod") or "?")
    plan = f", {a['subscriptionType']}" if a.get("subscriptionType") else ""
    if method != "claude.ai":
        return Result("warn", f"logged in with {method!r}{plan}, not a claude.ai subscription login: the usage "
                      "numbers AFClaude plans with are the subscription's",
                      "log in with the subscription: `claude auth login`")
    return Result("ok", f"logged in with claude.ai{plan}")


def _doctor(ctx: Ctx) -> tuple[int, str]:
    if ctx.doctor is None:
        r = ctx.call(["claude", "doctor"])
        ctx.doctor = (r.returncode, (r.stdout or "") + (("\n" + r.stderr) if r.stderr else ""))
    return ctx.doctor


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split("."))


def check_claude_version(ctx: Ctx) -> Result:
    """The running version (from `claude doctor`: "Running: native (2.1.325)") against the tested prefix."""
    rc, text = _doctor(ctx)
    m = _VERSION_LINE.search(text)
    if rc != 0 or m is None:
        return Result("warn", f"no version in the `claude doctor` answer (rc {rc}): {_first(text, 160)}",
                      "hostcheck._VERSION_LINE parses `Running: <install> (<version>)`")
    tested = str(ctx.setting("host_claude_tested_version"))
    have = version_tuple(m.group(1))
    limit = version_tuple(tested)
    if have[:len(limit)] > limit:
        return Result("warn", f"Claude Code {m.group(1)} is newer than the tested {tested}.x",
                      f"after a look at the release notes, raise the setting host_claude_tested_version")
    return Result("ok", f"Claude Code {m.group(1)} (tested up to {tested}.x)")


def check_doctor(ctx: Ctx) -> Result:
    """`claude doctor` (read-only): settings errors fail; Remote Control not eligible warns while every RC
    category setting is off (D-042/D-269), else fails; anything else it prints that is not clean warns."""
    rc, text = _doctor(ctx)
    if rc == 124:
        return Result("fail", "`claude doctor` stopped by the kit after its time limit",
                      "run `claude doctor` on the host by hand")
    if rc != 0:
        return Result("fail", f"`claude doctor` failed (rc {rc}): {_first(text)}", "run `claude doctor` on the host")
    if re.search(r"No installation issues found", text):
        return Result("ok", "no installation issues")
    bad = [ln.strip() for ln in text.splitlines() if re.search(r"settings", ln, re.I) and re.search(r"error|invalid", ln, re.I)]
    if bad:
        return Result("fail", f"settings errors: {_first(' | '.join(bad))}", "fix the named settings file on the host")
    rc_lines = [ln.strip() for ln in text.splitlines()
                if re.search(r"remote control", ln, re.I) and re.search(r"not eligible|ineligible|unavailable|not available", ln, re.I)]
    if rc_lines:
        view = remote_control.view(ctx.conn)
        on = sorted(c for c, v in view.items() if v["enabled"] is not False)
        if on:
            return Result("fail", f"Remote Control is not available but is enabled for {', '.join(on)}: "
                          f"{_first(' | '.join(rc_lines))}", "turn those categories' remote_control_* settings off, "
                          "or make the account eligible")
        return Result("warn", f"Remote Control is not available ({_first(' | '.join(rc_lines), 160)}); every category is off")
    return Result("warn", f"`claude doctor` printed something not clean that this check does not know: {_first(text, 240)}",
                  "extend hostcheck.check_doctor for this text")


def check_usage(ctx: Ctx) -> Result:
    """The usage cache is readable and fresh (the sampler refreshes it every 15 minutes)."""
    cache_fn: Callable[[], Any] = getattr(host, "usage_cache")
    cache = cache_fn()
    ms = cache.get("fetchedAtMs") if isinstance(cache, dict) else None
    if not isinstance(ms, (int, float)) or ms <= 0:
        return Result("fail", "the host's usage cache has no reading", "the usage sampler should have refreshed it: "
                      "run `claude -p /usage` on the host and look at the sampler's log")
    age = (time.time() * 1000 - ms) / 60000
    text = f"the usage cache was fetched {age:.0f} min ago"
    if age > USAGE_FAIL_MIN:
        return Result("fail", text, "the usage sampler is not refreshing it: look at its log")
    if age > USAGE_WARN_MIN:
        return Result("warn", text, "the sampler refreshes every 15 min: look at its log")
    return Result("ok", text)


def check_mcp(ctx: Ctx) -> Result:
    """The MCP server's stdio form (what `afclaude mcp` and the registrations run) starts and lists tools."""
    tools = mcp_tools([sys.executable, os.path.join(HERE, "mcp_server.py"), "stdio"], ctx.remaining())
    if not tools:
        return Result("fail", "the MCP server answered tools/list with no tools", "look at mcp_server.py's startup")
    return Result("ok", f"the MCP server (stdio) lists {len(tools)} tools")


def mcp_tools(argv: list[str], timeout: float) -> list[str]:
    """Start `argv` as an MCP server on stdio, do the initialize handshake and `tools/list` (newline-delimited
    JSON-RPC), kill it. -> the tool names. TimeoutExpired when it does not answer in `timeout` s; RuntimeError
    when it exits or answers an error."""
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, cwd=HERE,
                                env=dict(os.environ))
        stdin, stdout = proc.stdin, proc.stdout
        assert stdin is not None and stdout is not None
        fd = stdout.fileno()
        buf = b""

        def send(msg: dict[str, Any]) -> None:
            stdin.write((json.dumps(msg) + "\n").encode())
            stdin.flush()

        def reply(want: int) -> dict[str, Any]:
            nonlocal buf
            while True:
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue            # not JSON-RPC (a stray log line): not ours
                    if isinstance(msg, dict) and msg.get("id") == want:
                        return msg
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                ready, _, _ = select.select([fd], [], [], min(left, 1.0))
                if ready:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        err.seek(0)
                        raise RuntimeError(f"the MCP server exited (rc {proc.poll()}): "
                                           f"{_first(err.read().decode(errors='replace'))}")
                    buf += chunk
        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "afclaude-hostcheck", "version": "1"}}})
            first = reply(1)
            if "error" in first:
                raise RuntimeError(f"initialize failed: {_first(json.dumps(first['error']))}")
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            second = reply(2)
            if "error" in second:
                raise RuntimeError(f"tools/list failed: {_first(json.dumps(second['error']))}")
            return [str(t.get("name")) for t in (second.get("result") or {}).get("tools") or []]
        finally:
            proc.kill()
            proc.wait()
            stdin.close()
            stdout.close()


def check_disk(ctx: Ctx) -> Result:
    """Free space of the data directory (here) and of the host's Claude and repo directories (the kit's df)."""
    warn, fail = float(ctx.setting("host_disk_warn_gb")) * 1e9, float(ctx.setting("host_disk_fail_gb")) * 1e9
    free = {"data directory": float(shutil.disk_usage(runner_state.data_dir()).free)}
    r = ctx.call(["df"])
    if r.returncode != 0:
        return Result("fail", f"`df` on the host failed (rc {r.returncode}): {_first(r.stderr or r.stdout)}", KIT_FIX)
    doc = json.loads(r.stdout)
    for key, label in (("claude_dir", "host Claude directory"), ("repo", "host repo directory")):
        free[label] = float(doc[key]["free_bytes"])
    text = ", ".join(f"{k} {_bytes_gb(v)} free" for k, v in free.items())
    low = [k for k, v in free.items() if v < fail]
    if low:
        return Result("fail", f"{text}; below the failure limit ({fail / 1e9:g} GB): {', '.join(low)}",
                      "free disk space; the limits are the settings host_disk_fail_gb / host_disk_warn_gb")
    low = [k for k, v in free.items() if v < warn]
    if low:
        return Result("warn", f"{text}; below the warning limit ({warn / 1e9:g} GB): {', '.join(low)}",
                      "free disk space soon")
    return Result("ok", text)


def check_clock(ctx: Ctx) -> Result:
    """The host's clock against this container's, around the call (warn > 5 s, fail > 60 s)."""
    t0 = time.time()
    r = ctx.call(["clock"])
    t1 = time.time()
    if r.returncode != 0:
        return Result("fail", f"`clock` on the host failed (rc {r.returncode}): {_first(r.stderr or r.stdout)}", KIT_FIX)
    drift = json.loads(r.stdout)["epoch_ms"] / 1000 - (t0 + t1) / 2
    text = f"the host's clock is {drift:+.1f} s from the container's"
    if abs(drift) > CLOCK_FAIL_S:
        return Result("fail", text, "fix time sync (chrony / systemd-timesyncd) on the host and of the container's host")
    if abs(drift) > CLOCK_WARN_S:
        return Result("warn", text, "check time sync on the host")
    return Result("ok", text)


def check_e2e(ctx: Ctx) -> Result:
    """On demand: a Haiku session in the tmux session afc-check-e2e: start it (ka_resume.sh --new), wait for its
    reply to a prompt with a computed answer, kill it. The session is killed in every case (a kill gets its own
    short budget) and a session that is still there afterwards is a fail."""
    import random
    name = E2E_TMUX
    a, b = random.randint(100, 499), random.randint(100, 499)
    want = re.compile(rf"AFC-{a + b}\b")
    msg = (f"This is an AFClaude host check. Reply with exactly the text AFC- followed directly by the sum of "
           f"{a} and {b}, and nothing else.")

    def has() -> bool:
        r = ctx.call(["tmux", "has-session", "-t", "=" + name])
        if r.returncode not in (0, 1):
            raise RuntimeError(f"tmux has-session failed (rc {r.returncode}): {_first(r.stderr or r.stdout)}")
        return r.returncode == 0

    def kill() -> None:
        ctx.call(["tmux", "kill-session", "-t", "=" + name])

    if has():
        kill()      # a leftover of an interrupted check (the name is reserved: names.RESERVED)
    repo: Callable[[], str] = getattr(host, "repo_dir")
    res: Optional[Result] = None
    try:
        started = ctx.call([os.path.join(repo(), "ka_resume.sh"), "--session", str(uuid.uuid4()), "--tmux", name,
                            "--message", msg, "--new", "--model", "haiku", "--effort", "low", "--no-remote-control"])
        if started.returncode != 0:
            res = Result("fail", f"ka_resume.sh did not start the session (rc {started.returncode}): "
                         f"{_first(started.stderr or started.stdout)}", "run the same by hand on the host")
        limit = time.monotonic() + E2E_WAIT
        while res is None:
            cap = ctx.call(["tmux", "capture-pane", "-e", "-p", "-t", f"={name}:"])
            if cap.returncode == 0 and want.search(cap.stdout or ""):
                res = Result("ok", "a Haiku session started in tmux, replied and was killed")
            elif cap.returncode != 0 and not has():
                res = Result("fail", "the e2e session ended before it replied", "start `claude` on the host by hand in "
                             "the working root: it may ask for trust or a login")
            elif time.monotonic() >= limit:
                res = Result("fail", f"no reply within {E2E_WAIT:g} s: {_first(cap.stdout or '', 200)}",
                             "start `claude` on the host by hand in the working root: it may ask for trust or a login")
            else:
                time.sleep(min(E2E_POLL, max(0.0, limit - time.monotonic())))
    finally:
        ctx.deadline = time.monotonic() + 30
        kill()
    if has():
        return Result("fail", f"the tmux session {name} is still there after the kill", "kill it on the host: "
                      f"`tmux kill-session -t ={name}`")
    return res


CHECKS: tuple[Check, ...] = (
    Check("bridge", True, 40, check_bridge),
    Check("kit", True, 70, check_kit),
    Check("tmux", True, 45, check_tmux),
    Check("claude", True, 70, check_claude),
    Check("logged_in", True, 5, check_logged_in),
    Check("mcp", True, 60, check_mcp),
    Check("claude_version", False, 70, check_claude_version),
    Check("doctor", False, 5, check_doctor),
    Check("usage", False, 40, check_usage),
    Check("disk", False, 40, check_disk),
    Check("clock", False, 20, check_clock),
    Check("e2e", False, 300, check_e2e, scheduled=False),
)
BY_NAME = {c.name: c for c in CHECKS}
# the worst case of one run, every check at its budget (the runner's outer timeout must exceed it: runner_jobs
# TICK_CONSTANTS, test_runner) and of the pre-start subset (runner_jobs.window_start_inner)
RUN_WORST_CASE = float(sum(c.budget for c in CHECKS if c.scheduled))
PRE_START_WORST_CASE = float(sum(c.budget for c in CHECKS if c.blocking))


# ------------------------------------------------------------------ running them

def execute(check: Check, ctx: Ctx) -> Outcome:
    """One check within its budget; a timeout or an exception is a fail naming it."""
    start = time.monotonic()
    ctx.budget = check.budget
    ctx.deadline = start + check.budget
    try:
        res = check.fn(ctx)
    except subprocess.TimeoutExpired as e:
        res = Result("fail", f"no answer within {check.budget:g} s ({_first(str(e), 160)})",
                     "a hung host call is an error (D-234): look at the host and the bridge")
    except HOST_KIT_ERROR as e:
        res = Result("fail", _first(str(e), 500), KIT_FIX)
    except Exception as e:   # noqa: BLE001 - a crashing check is a failing check, not a silent one (D-235)
        res = Result("fail", f"the check itself raised {type(e).__name__}: {_first(str(e), 400)}",
                     "a bug in hostcheck.py or an unexpected host answer: fix the check")
    ms = int((time.monotonic() - start) * 1000)
    kit_version = res.kit_version or (str(ctx.kit["kit_version"]) if ctx.kit else None)
    return Outcome(check.name, check.blocking, res.status, res.detail, res.fix, ms, kit_version)


def run_checks(conn: Any, names: Optional[list[str]] = None, *, blocking_only: bool = False) -> list[Outcome]:
    """The checks in order (all scheduled ones; `names`: those, in the table's order, e2e only when named;
    blocking_only: the pre-start subset). A failed bridge or kit, or no kit installed, skips the rest. Named checks
    pass the same gates: the bridge check runs before any of them, the kit check before any but the bridge (so
    `host check tmux` without a kit installed is a skip, not a fail)."""
    chosen = [c for c in CHECKS if (c.name in names if names else c.scheduled) and (c.blocking or not blocking_only)]
    if chosen:
        gates = ["bridge"] + (["kit"] if any(c.name != "bridge" for c in chosen) else [])
        chosen += [BY_NAME[g] for g in gates if BY_NAME[g] not in chosen]
    ctx = Ctx(conn=conn)
    out: list[Outcome] = []
    for c in CHECKS:
        if c not in chosen:
            continue
        if ctx.gate is not None:
            out.append(Outcome(c.name, c.blocking, "skip", f"not run: {ctx.gate}", "", 0, None))
            continue
        o = execute(c, ctx)
        out.append(o)
        if c.name in ("bridge", "kit") and o.status == "fail":
            ctx.gate = f"the {c.name} check failed"
    return out


def summary(results: list[Outcome], what: str = "all") -> str:
    counts = {s: [o.name for o in results if o.status == s] for s in STATUSES}
    parts = [f"{len(v)} {s}" + (f" ({', '.join(v)})" if v and s != "ok" else "") for s, v in counts.items() if v]
    return f"host checks ({what}): " + (", ".join(parts) or "none run")


def _log(conn: Any, results: list[Outcome], what: str, actor: str, via: str) -> str:
    """The run's one app_log line (level by the worst status); returns the line."""
    worst = "error" if any(o.status == "fail" for o in results) else "warn" if any(o.status == "warn" for o in results) else "info"
    now = datetime.now(BERLIN)
    line = f"[{now:%Y-%m-%d %H:%M:%S %Z}] {summary(results, what)}"
    actions.app_log_append(conn, COMPONENT, [(store.now_iso(), worst, line)], actor=actor, via=via)
    return line


def record(conn: Any, results: list[Outcome], *, actor: str = ACTOR, via: str = "runner",
           what: str = "all") -> dict[str, Optional[str]]:
    """Store the results (actions.hostcheck_put) and write the run's app_log line. -> {name: when its current
    episode was alerted (None: not yet)}."""
    alerted = actions.hostcheck_put(conn, [
        {"name": o.name, "status": o.status, "detail": o.detail, "fix": o.fix, "duration_ms": o.duration_ms,
         "kit_version": o.kit_version, "blocking": o.blocking} for o in results], actor=actor, via=via)
    _log(conn, results, what, actor, via)
    return alerted


def report_failures(conn: Any, results: list[Outcome], alerted: dict[str, Optional[str]],
                    fail: Optional[Callable[..., Any]] = None) -> list[str]:
    """Alert for each fail (module doc): a blocking one is failstop.fail (alert + pause; failstop alerts once
    per pause), another one an alert without a pause once per episode: while its row's episode is not marked
    alerted (record's answer), then marked (actions.hostcheck_alerted), whoever recorded the fail first (a manual
    `host check` records without alerting). -> the names reported."""
    fail = fail or failstop.fail
    reported: list[str] = []
    marked: list[str] = []
    for o in results:
        if o.status != "fail":
            continue
        detail = o.detail + (f"\nFix: {o.fix}" if o.fix else "")
        if o.blocking:
            fail(COMPONENT, f"host check {o.name} failed", detail)
        elif alerted.get(o.name) is None:
            fail(COMPONENT, f"host check {o.name} failed", detail, pause=False)
            marked.append(o.name)
        else:
            continue
        reported.append(o.name)
    if marked:
        actions.hostcheck_alerted(conn, marked, actor=ACTOR, via="runner")
    return reported


def run_job(conn: Any) -> str:
    """The hourly runner job: every scheduled check, recorded, failures reported. -> the run's one-line note."""
    results = run_checks(conn)
    alerted = record(conn, results)
    report_failures(conn, results, alerted)
    return summary(results)


def run_pre_start(conn: Any) -> Optional[str]:
    """Right before a session-window start (keepalive.window_start_pass): the blocking checks, recorded. A fail of
    one is reported (failstop: alert + pause) and the start must not go on: -> its reason. None = go on. While no
    host kit is installed (NO_KIT, transitional) nothing blocks."""
    results = run_checks(conn, blocking_only=True)
    alerted = record(conn, results, what="before a window start")
    bad = [o for o in results if o.status == "fail"]
    if not bad:
        return None
    report_failures(conn, results, alerted)
    return "; ".join(f"{o.name}: {o.detail}" for o in bad)[:600]


# keepalive.host_checks_before_start calls it by this name: testenv replaces it, so no window start in a test
# runs the checks over a real bridge; the tests of the checks call run_pre_start
pre_start = run_pre_start


# ------------------------------------------------------------------ CLI

def render(rows: list[dict[str, Any]]) -> str:
    out = []
    for r in rows:
        out.append(f"{r['status'].upper():<5} {r['name']:<15} {r['detail']}")
        if r["status"] in ("fail", "warn") and r["fix"]:
            out.append(f"      fix: {r['fix']}")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["status"] and len(args) == 1:
        conn = store.connect()
        try:
            rows = store.host_check_rows(conn)
        finally:
            conn.close()
        order = {c.name: i for i, c in enumerate(CHECKS)}
        rows.sort(key=lambda r: order.get(r["name"], 99))
        print(render(rows) or "no host check has run yet")
        return 0
    if args[:1] != ["check"]:
        print("usage: hostcheck.py check [name ...] [--blocking] | status   "
              f"(names: {', '.join(c.name for c in CHECKS)})", file=sys.stderr)
        return 2
    blocking = "--blocking" in args
    names = [a for a in args[1:] if a != "--blocking"]
    unknown = [n for n in names if n not in BY_NAME]
    if unknown:
        print(f"hostcheck: unknown check {', '.join(unknown)} (names: {', '.join(c.name for c in CHECKS)})", file=sys.stderr)
        return 2
    conn = store.connect()
    try:
        results = run_checks(conn, names or None, blocking_only=blocking)
        # a manual run records what it saw (so a fixed check is green on the page at once) but pauses nothing:
        # the person running it is looking at the answer
        record(conn, results, actor=CLI_ACTOR, via="cli", what=" ".join(names) or "all")
    finally:
        conn.close()
    print(render([{"status": o.status, "name": o.name, "detail": o.detail, "fix": o.fix} for o in results]))
    return 1 if any(o.status == "fail" for o in results) else 0


if __name__ == "__main__":
    sys.exit(main())
