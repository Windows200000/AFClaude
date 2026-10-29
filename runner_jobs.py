#!/usr/bin/env python3
"""runner_jobs.py: the resident runner's child side (phase 3a, docs/phase3a_plan.md §3.1, §3.2).

    python3 runner_jobs.py <job> [--key K]
    python3 runner_jobs.py at --key <scheduled job id>     an at-job (phase 3a S4, at_job)

runner.py starts one of these per run, in its own process group, with a timeout. It runs the
job's existing code the way its cron line does: the same script with the same arguments (runpy,
so the script's own `__main__` error handling runs too) in the repo directory, its stdout appended
to the same log file, and the same `flock -n` lock where the cron line has one. On its ORIGINAL
stdout (the runner's pipe) it prints one JSON result line: {"status": "ok"|"skipped", "next_due":
ISO|null, "note": text}.
Its stderr stays the runner's pipe: the runner appends it to the job's log file (cron's 2>&1) and
keeps the tail of a failed run in runner_runs.error.

Exit codes (runner.py records the run by them):
  0      ok, or skipped (another copy holds the job's lock: what cron's flock -n does)
  3      failed, and the handler already reported it: failstop.fail ran inside (an alert, and a
         pause unless the handler said otherwise). The runner records the failure and does NOT
         report it a second time
  other  failed without a report (a crash, a non-zero exit, an unknown job, a missing setting):
         the runner reports it (failstop.fail, D-234/D-235)

An at-job (`at --key ID`): the scheduled job's command (scheduled_jobs, stored by `at -t`; the runner
claimed it in the run's own begin, actions.run_begin(claim=)) runs with /bin/sh in its working
directory as the at-shim's `--run-due` started it, but as this child (same process group: the
runner's timeout ends it), not detached; its stdout goes to data/at_shim.log (the --run-due cron
line's redirect). A fail() inside the command is seen through failstop.REPORTS_ENV (exit 3).

The window start's inner timeouts (window_start_inner) and the ticks' (tick_inner, S3) are here
too: the runner's outer timeout for each must exceed their sum (actions.SETTINGS runner_timeout_*;
test_runner), so a slow run within its own limits is never killed by the runner.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Optional, TextIO

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runner_state  # noqa: E402

EXIT_OK = 0
EXIT_REPORTED = 3
EXIT_USAGE = 2


@dataclass(frozen=True)
class Handler:
    """A job's existing code: `script` (in the repo) run with `args` as its cron line runs it.
    log: its log file under the data dir (the cron line's redirect); lock: its flock file under
    the data dir, or None. "{session}" in args is the task-manager session the start continues, from
    the registry (keepalive.start_session, phase 3b S3; the crontab's $AFCLAUDE_MANAGER_SESSION is
    not read), "{key}" the run's key (a keyed run: the window start's schedule.start_key, S5)."""
    script: str
    args: tuple[str, ...]
    log: str
    lock: Optional[str] = None


HANDLERS: dict[str, Handler] = {
    "stall_scan": Handler("stalled.py", ("scan",), "stall_scan.log"),
    "sampler": Handler("usage_sampler.py", (), "sampler.log", ".sampler.lock"),
    "dispatcher": Handler("dispatcher.py", ("--arm", "--once"), "dispatcher.log"),
    "quickview": Handler("export_quickview.py", (), "quickview_export.log", ".quickview.lock"),
    "docs_import": Handler("project_docs.py", ("import", "--cron"), "docs_import.log", ".docs_import.lock"),
    "prune_logs": Handler("store.py", ("prune-logs",), "prune_logs.log", ".prune_logs.lock"),
    "usage_review": Handler("usage_review.py", (), "keepalive/keepalive.log"),
    # S5: the start the runner planned (--start-key: keepalive refuses it unless it is still the current
    # one, and claims it in its "window_starts", the dedup it shares with cron's legacy line)
    "window_start": Handler("keepalive.py", ("--session", "{session}", "--window-start", "--start-key", "{key}",
                                             "--arm", "--take-over-idle"), "keepalive/keepalive.log"),
}
# Jobs whose code is a function here, not a script: name -> (log file, function -> note).
FUNCTIONS: dict[str, tuple[str, Callable[[], str]]] = {}
# S6: the keep-alive watcher's passes, run in this process as keepalive.runner_pass(job, key) (the
# sessions: every task-manager of the registry, phase 3b S3; armed, as the watcher ran), their
# output appended to the keep-alive's log as the watcher's was. The ticks plan; the acts (a key: the
# run key their plan stored) type into the session. -> the result line's status, note and next_due.
PASS_LOG = "keepalive/keepalive.log"
PASSES: tuple[str, ...] = ("watch", "run_readings", "compaction_check", "last_stretch", "deferred_start", "fillup",
                           "compaction")
AT_JOB = "at"               # the at-jobs (runner.JOBS "at", keyed by the scheduled job's id): at_job()
AT_LOG = "at_shim.log"      # the --run-due cron line's log


def log_path(job: str) -> str:
    """The job's log file (what its stdout is appended to, and the runner appends its stderr to)."""
    rel = (AT_LOG if job == AT_JOB else HANDLERS[job].log if job in HANDLERS else PASS_LOG if job in PASSES
           else FUNCTIONS[job][0])
    return os.path.join(runner_state.data_dir(), rel)


def prune_runs() -> str:
    """The daily retention prune of the runner's run records (actions runner.prune, audited)."""
    import actions
    import store
    conn = store.connect()
    try:
        r = actions.perform(conn, "runner.prune", {}, actor="runner:prune_runs", via="runner")
    finally:
        conn.close()
    line = f"runner_runs: deleted {r['deleted']} run record(s) started before {r['cutoff']}"
    print(line)
    return line


FUNCTIONS["prune_runs"] = ("prune_logs.log", prune_runs)

# ---- the window start's inner timeouts (docs/phase3a_plan.md §3.2: outer = their sum + a margin)
#
# keepalive.py --window-start, worst case: the usage refresh (/usage, the token-refresh request,
# /usage again), the preflight (agents list, tmux checks), the owner-typing check, the wait for a
# running compaction (COMPACT_LOCK_WAIT), the take-over of an idle holder (stop + TAKE_OVER_WAIT),
# the resume and its verification (VERIFY_TIMEOUT); or, for a start that does not fire at once, the
# window-start ping (D-277: its request, WINDOW_PING_TIMEOUT; the wait, WINDOW_PING_VERIFY_AFTER; a
# second usage refresh). Both paths are summed (the safe side). The host calls' timeouts are literals
# in keepalive.py: (function, seconds of one call, calls); test_runner checks them against the source.
WINDOW_START_HOST_CALLS: tuple[tuple[str, int, int], ...] = (
    ("refresh_usage", 120, 4), ("poke_auth", 180, 2), ("agent_entries", 60, 1), ("tmux_has", 30, 1),
    ("tmux_pids", 30, 1), ("owner_typing", 30, 2), ("fire", 60, 1), ("fire", 120, 1),
)
WINDOW_START_CONSTANTS = ("COMPACT_LOCK_WAIT", "VERIFY_TIMEOUT", "TAKE_OVER_WAIT", "WINDOW_PING_TIMEOUT",
                          "WINDOW_PING_VERIFY_AFTER")
OUTER_MARGIN = timedelta(minutes=5)


def window_start_inner() -> dict[str, timedelta]:
    """{what: worst-case time} of one session-window start's inner timeouts (keepalive's own
    constants, read from it, plus its host calls)."""
    ka = importlib.import_module("keepalive")
    out: dict[str, timedelta] = {}
    for name in WINDOW_START_CONSTANTS:
        v = getattr(ka, name)
        out[name] = v if isinstance(v, timedelta) else timedelta(seconds=float(v))
    for fn, secs, calls in WINDOW_START_HOST_CALLS:
        out[f"host:{fn}"] = out.get(f"host:{fn}", timedelta(0)) + timedelta(seconds=secs * calls)
    return out


def window_start_min_timeout() -> timedelta:
    """The least outer timeout of a window start: its inner timeouts + OUTER_MARGIN."""
    return sum(window_start_inner().values(), timedelta(0)) + OUTER_MARGIN


# ---- the ticks' inner timeouts (S3: a tick's outer timeout is above their sum + TICK_MARGIN)
#
# One run's worst case, every host call hitting its own timeout: (module, function, seconds of one
# call, calls), plus the module constants it waits for. test_runner checks each (module, function,
# seconds) against a literal `timeout=` in that function's source, and each tick's default timeout
# (runner.JOBS, actions.SETTINGS) against the sum. The jobs not listed (docs_import, prune_logs,
# prune_runs, stall_scan) only read and write the DB.
TICK_HOST_CALLS: dict[str, tuple[tuple[str, str, int, int], ...]] = {
    # the usage refresh with its one token-refresh retry (keepalive.refresh_usage_checked), the usage
    # cache, `claude agents`, the hourly Haiku judgements (budgeted for two active sessions)
    "sampler": (("keepalive", "refresh_usage", 120, 2), ("keepalive", "poke_auth", 180, 1),
                ("host", "usage_cache", 30, 1), ("usage_sampler", "agents_snapshot", 60, 1),
                ("usage_sampler", "haiku_judgement", 180, 2)),
    # one approved stalled continue (usage refresh, preflight, resume) and one tmux clean-up
    # (keepalive.tmux_has: both dispatcher's own check, dispatcher.py:548, and the generic one,
    # tmux_alive delegates to it without a timeout of its own)
    "dispatcher": (("keepalive", "refresh_usage", 120, 2), ("keepalive", "poke_auth", 180, 1),
                   ("keepalive", "agent_entries", 60, 1), ("keepalive", "tmux_has", 30, 1),
                   ("keepalive", "tmux_pids", 30, 1), ("host", "proc_snapshot", 30, 1),
                   ("keepalive", "fire", 60, 1), ("keepalive", "fire", 120, 1),
                   ("keepalive", "tmux_has", 30, 1), ("dispatcher", "kill_tmux", 30, 1)),
    # the usage refresh and one review start (the resume), after its lock wait (TICK_CONSTANTS)
    "usage_review": (("keepalive", "refresh_usage", 120, 2), ("keepalive", "poke_auth", 180, 1),
                     ("keepalive", "fire", 60, 1), ("keepalive", "fire", 120, 1),
                     ("keepalive", "tmux_has", 30, 1)),
    "quickview": (("export_quickview", "watcher_running", 10, 1), ("keepalive", "tmux_has", 30, 1)),
}
# S6, the keep-alive watcher's ticks. The watch: the usage cache read by the detection, the last-stretch
# plan, its next slot start and the fill-up plan (+ one spare), and the fill-up's usage refresh ahead of
# its start (with its one token-refresh retry). The run readings: one usage refresh with its retry and the
# cache reads around it. The compaction check: the holds (the usage cache twice, the preflight's tmux /
# `claude agents` / process calls, the owner-typing check's two tmux reads).
TICK_HOST_CALLS.update({
    "watch": (("host", "usage_cache", 30, 5), ("keepalive", "refresh_usage", 120, 2),
              ("keepalive", "poke_auth", 180, 1)),
    "run_readings": (("host", "usage_cache", 30, 3), ("keepalive", "refresh_usage", 120, 2),
                     ("keepalive", "poke_auth", 180, 1)),
    "compaction_check": (("host", "usage_cache", 30, 2), ("keepalive", "tmux_has", 30, 1),
                         ("keepalive", "tmux_pids", 30, 1), ("keepalive", "agent_entries", 60, 1),
                         ("host", "proc_snapshot", 30, 1), ("keepalive", "owner_typing", 30, 2)),
})
TICK_CONSTANTS: dict[str, tuple[tuple[str, str], ...]] = {
    "usage_review": (("usage_review", "LOCK_WAIT"),),      # waits this long for another pass
}
TICK_MARGIN = timedelta(minutes=1)


def tick_inner(job: str) -> dict[str, timedelta]:
    """{what: worst-case time} of one run of the tick `job` (TICK_HOST_CALLS, TICK_CONSTANTS)."""
    out: dict[str, timedelta] = {}
    for mod, name in TICK_CONSTANTS.get(job, ()):
        v = getattr(importlib.import_module(mod), name)
        out[f"{mod}.{name}"] = v if isinstance(v, timedelta) else timedelta(seconds=float(v))
    for mod, fn, secs, calls in TICK_HOST_CALLS.get(job, ()):
        k = f"{mod}.{fn}"
        out[k] = out.get(k, timedelta(0)) + timedelta(seconds=secs * calls)
    return out


def tick_min_timeout(job: str) -> timedelta:
    """The least outer timeout of the tick `job`: its inner timeouts + TICK_MARGIN."""
    return sum(tick_inner(job).values(), timedelta(0)) + TICK_MARGIN


# ---- the watcher passes' acts (S6): outer timeout = their inner timeouts + OUTER_MARGIN
#
# A start (last_stretch, deferred_start, fillup) re-plans and then fires as a session-window start does
# (handle_fire): its inner timeouts are the window start's (window_start_inner, the ping included: the
# safe side). The compaction re-runs its check (the compaction_check tick's calls), then waits for the
# turn to end twice (before the save prompt and before /compact), for the hand-off, the compaction marker
# and the reply to the hand-off, and types three times (the save prompt, /compact, the hand-off).
START_ACTS = ("last_stretch", "deferred_start", "fillup")
COMPACTION_CONSTANTS: tuple[tuple[str, int], ...] = (("COMPACT_IDLE_WAIT", 2), ("SAVE_TIMEOUT", 1),
                                                     ("COMPACT_TIMEOUT", 1), ("VERIFY_TIMEOUT", 1))
COMPACTION_HOST_CALLS: tuple[tuple[str, int, int], ...] = (("fire", 120, 3),)


def act_inner(job: str) -> dict[str, timedelta]:
    """{what: worst-case time} of one run of the act `job` (a start or the compaction)."""
    if job in START_ACTS:
        return window_start_inner()
    if job != "compaction":
        raise KeyError(f"{job} is not an act")
    ka = importlib.import_module("keepalive")
    out = dict(tick_inner("compaction_check"))
    for name, n in COMPACTION_CONSTANTS:
        out[name] = out.get(name, timedelta(0)) + getattr(ka, name) * n
    for fn, secs, calls in COMPACTION_HOST_CALLS:
        out[f"host:{fn}"] = out.get(f"host:{fn}", timedelta(0)) + timedelta(seconds=secs * calls)
    return out


def act_min_timeout(job: str) -> timedelta:
    """The least outer timeout of the act `job`: its inner timeouts + OUTER_MARGIN."""
    return sum(act_inner(job).values(), timedelta(0)) + OUTER_MARGIN


# ---- an at-job (phase 3a S4)

def at_job(key: str) -> int:
    """Run the claimed scheduled job `key` (module doc). -> the exit code: 0 its command exited 0,
    EXIT_REPORTED a fail() inside it reported its failure, else 1 (or the command's own code)."""
    import failstop
    import store
    conn = store.connect()
    try:
        row = store.retrying(lambda: store.get_job(conn, key), conn)
    finally:
        conn.close()
    if row is None or row["status"] != "started":
        print(f"runner_jobs: scheduled job {key} is " + ("unknown" if row is None else
              f"{row['status']}, not claimed by this run"), file=sys.stderr)
        return 1
    cwd = row["cwd"] if os.path.isdir(row["cwd"] or "") else "/"
    run_dir = os.path.join(runner_state.data_dir(), "run")
    os.makedirs(run_dir, exist_ok=True)
    fd, marks = tempfile.mkstemp(prefix=f".at-{key}-", suffix=".reports", dir=run_dir)
    os.close(fd)
    try:
        sys.stdout.flush()
        code = subprocess.call(["/bin/sh", "-c", str(row["cmd"])], cwd=cwd, stdin=subprocess.DEVNULL,
                               env=dict(os.environ, **{failstop.REPORTS_ENV: marks}))
        with open(marks, encoding="utf-8") as fh:
            reported = [ln for ln in fh.read().splitlines() if ln.strip()]
    finally:
        os.unlink(marks)
    what = f"at-job {key} ({' '.join(str(row['cmd']).split())[:200]})"
    if reported:
        print(f"runner_jobs: {what} exited {code} and reported its failure itself: {'; '.join(reported)[:500]}",
              file=sys.stderr)
        return EXIT_REPORTED
    if code != 0:
        print(f"runner_jobs: {what} " + (f"ended by signal {-code}" if code < 0 else f"exited {code}"),
              file=sys.stderr)
        return code if 0 < code < 256 and code != EXIT_REPORTED else 1
    print(f"runner_jobs: {what} exited 0")
    return EXIT_OK


# ---- running one job

class _Reports:
    """Counts failstop.fail calls made inside the handler (it reported a failure itself)."""
    def __init__(self) -> None:
        self.subjects: list[str] = []

    def install(self) -> None:
        import failstop
        orig = failstop.fail

        def spy(*a: Any, **kw: Any) -> dict[str, Any]:
            self.subjects.append(str(a[1] if len(a) > 1 else kw.get("subject", "?")))
            return orig(*a, **kw)
        failstop.fail = spy


def _exit_code(e: SystemExit) -> int:
    """sys.exit's value as the process would exit with it (a text: printed, exit 1)."""
    if e.code is None:
        return 0
    if isinstance(e.code, int):
        return e.code
    print(e.code, file=sys.stderr)
    return 1


def _redirect_stdout(path: str) -> TextIO:
    """Point fd 1 (and sys.stdout) at the job's log file (append), as the cron redirect does;
    -> the original stdout (the runner's pipe), for the result line."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sys.stdout.flush()
    result = os.fdopen(os.dup(1), "w", encoding="utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    os.dup2(fd, 1)
    os.close(fd)
    return result


def _lock(name: str) -> Optional[int]:
    """flock -n on the data dir's lock file: the fd, or None if another copy holds it."""
    path = os.path.join(runner_state.data_dir(), name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _argv(h: Handler, key: Optional[str] = None) -> list[str]:
    session = ""
    if "{session}" in h.args:     # the registry's task-manager for this start (a registry without one raises)
        session = importlib.import_module("keepalive").start_session()
    if "{key}" in h.args and not key:
        raise RuntimeError(f"{h.script}: this job needs --key (its run's key)")
    subst = {"{session}": session, "{key}": key or ""}
    return [os.path.join(HERE, h.script)] + [subst.get(a, a) for a in h.args]


def run(job: str, key: Optional[str] = None) -> int:
    """Run `job` (see the module doc). -> the exit code."""
    if job not in HANDLERS and job not in FUNCTIONS and job not in PASSES and job != AT_JOB:
        print(f"runner_jobs: unknown job {job!r}", file=sys.stderr)
        return EXIT_USAGE
    if job == AT_JOB and not key:
        print("runner_jobs: an at-job needs --key <scheduled job id>", file=sys.stderr)
        return EXIT_USAGE
    os.chdir(HERE)          # the cron lines' `cd $AFCLAUDE_DIR`
    out = _redirect_stdout(log_path(job))
    if job == AT_JOB:
        try:
            code = at_job(str(key))
        except BaseException:   # noqa: BLE001 - a crash (the job unreadable, ...): its traceback is the error
            traceback.print_exc()
            code = 1
        sys.stdout.flush()
        if code == EXIT_OK:
            _result(out, "ok", f"scheduled job {key}")
        return code
    h = HANDLERS.get(job)
    if h is not None and h.lock is not None and _lock(h.lock) is None:
        _result(out, "skipped", f"another copy holds {h.lock} (cron's flock -n)")
        return EXIT_OK
    reports = _Reports()
    reports.install()
    note, status, next_due = "", "ok", None
    try:
        if job in PASSES:
            res = importlib.import_module("keepalive").runner_pass(job, key)
            note, status, next_due = str(res.get("note") or ""), str(res.get("status") or "ok"), res.get("next_due")
            code = 0
        elif h is not None:
            argv = _argv(h, key)
            saved = sys.argv
            sys.argv = argv
            try:
                runpy.run_path(argv[0], run_name="__main__")
                code = 0
            finally:
                sys.argv = saved
        else:
            note = FUNCTIONS[job][1]()
            code = 0
    except SystemExit as e:
        code = _exit_code(e)
    except BaseException:   # noqa: BLE001 - a crash: its traceback is the runner's error text
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    if reports.subjects:
        print(f"runner_jobs: {job} reported its failure itself: {'; '.join(reports.subjects)[:500]}",
              file=sys.stderr)
        return EXIT_REPORTED
    if code != 0:
        print(f"runner_jobs: {job} exited {code}", file=sys.stderr)
        return code if 0 < code < 256 and code != EXIT_REPORTED else 1
    _result(out, status, note, next_due)
    return EXIT_OK


def _result(out: TextIO, status: str, note: str = "", next_due: Optional[str] = None) -> None:
    out.write(json.dumps({"status": status, "next_due": next_due, "note": note[:500]}) + "\n")
    out.flush()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run one resident-runner job (runner.py starts these).")
    ap.add_argument("job", help=f"one of {', '.join(sorted(set(HANDLERS) | set(FUNCTIONS) | set(PASSES) | {AT_JOB}))}")
    ap.add_argument("--key", default=None, help="the run's key (a keyed run: a window start, ...)")
    args = ap.parse_args(argv)
    return run(args.job, args.key)


if __name__ == "__main__":
    sys.exit(main())
