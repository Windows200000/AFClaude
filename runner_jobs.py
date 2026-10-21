#!/usr/bin/env python3
"""runner_jobs.py: the resident runner's child side (phase 3a, docs/phase3a_plan.md §3.1, §3.2).

    python3 runner_jobs.py <job> [--key K]

runner.py starts one of these per run, in its own process group, with a timeout. It runs the
job's existing code the way its cron line does: the same script with the same arguments (runpy,
so the script's own `__main__` error handling runs too), its stdout appended to the same log file,
and the same `flock -n` lock where the cron line has one. On its ORIGINAL stdout (the runner's
pipe) it prints one JSON result line: {"status": "ok"|"skipped", "next_due": ISO|null, "note": text}.
Its stderr stays the runner's pipe: the runner appends it to the job's log file (cron's 2>&1) and
keeps the tail of a failed run in runner_runs.error.

Exit codes (runner.py records the run by them):
  0      ok, or skipped (another copy holds the job's lock: what cron's flock -n does)
  3      failed, and the handler already reported it: failstop.fail ran inside (an alert, and a
         pause unless the handler said otherwise). The runner records the failure and does NOT
         report it a second time
  other  failed without a report (a crash, a non-zero exit, an unknown job, a missing setting):
         the runner reports it (failstop.fail, D-234/D-235)

The window start's inner timeouts (window_start_inner) are here too: the runner's outer timeout
for it must exceed their sum (actions.SETTINGS runner_timeout_window_start; test_runner).
"""
from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import runpy
import sys
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
    the data dir, or None. "{session}" in args is $AFCLAUDE_MANAGER_SESSION (as in the crontab)."""
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
    "window_start": Handler("keepalive.py", ("--session", "{session}", "--window-start", "--arm", "--take-over-idle"),
                            "keepalive/keepalive.log"),
}
# Jobs whose code is a function here, not a script: name -> (log file, function -> note).
FUNCTIONS: dict[str, tuple[str, Callable[[], str]]] = {}


def log_path(job: str) -> str:
    """The job's log file (what its stdout is appended to, and the runner appends its stderr to)."""
    rel = HANDLERS[job].log if job in HANDLERS else FUNCTIONS[job][0]
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
# the resume and its verification (VERIFY_TIMEOUT). The host calls' timeouts are literals in
# keepalive.py: (function, seconds of one call, calls); test_runner checks them against the source.
WINDOW_START_HOST_CALLS: tuple[tuple[str, int, int], ...] = (
    ("refresh_usage", 120, 2), ("poke_auth", 180, 1), ("agent_entries", 60, 1), ("tmux_has", 30, 1),
    ("tmux_pids", 30, 1), ("owner_typing", 30, 2), ("fire", 60, 1), ("fire", 120, 1),
)
WINDOW_START_CONSTANTS = ("COMPACT_LOCK_WAIT", "VERIFY_TIMEOUT", "TAKE_OVER_WAIT")
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


def _argv(h: Handler) -> list[str]:
    session = os.environ.get("AFCLAUDE_MANAGER_SESSION", "")
    if "{session}" in h.args and not session:
        raise RuntimeError("AFCLAUDE_MANAGER_SESSION is not set (the task-manager session the job acts on)")
    return [os.path.join(HERE, h.script)] + [session if a == "{session}" else a for a in h.args]


def run(job: str, key: Optional[str] = None) -> int:
    """Run `job` (see the module doc). -> the exit code."""
    if job not in HANDLERS and job not in FUNCTIONS:
        print(f"runner_jobs: unknown job {job!r}", file=sys.stderr)
        return EXIT_USAGE
    out = _redirect_stdout(log_path(job))
    h = HANDLERS.get(job)
    if h is not None and h.lock is not None and _lock(h.lock) is None:
        _result(out, "skipped", f"another copy holds {h.lock} (cron's flock -n)")
        return EXIT_OK
    reports = _Reports()
    reports.install()
    note = ""
    try:
        if h is not None:
            argv = _argv(h)
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
    _result(out, "ok", note)
    return EXIT_OK


def _result(out: TextIO, status: str, note: str = "", next_due: Optional[str] = None) -> None:
    out.write(json.dumps({"status": status, "next_due": next_due, "note": note[:500]}) + "\n")
    out.flush()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run one resident-runner job (runner.py starts these).")
    ap.add_argument("job", help=f"one of {', '.join(sorted(set(HANDLERS) | set(FUNCTIONS)))}")
    ap.add_argument("--key", default=None, help="the run's key (a keyed run: a window start, ...)")
    args = ap.parse_args(argv)
    return run(args.job, args.key)


if __name__ == "__main__":
    sys.exit(main())
