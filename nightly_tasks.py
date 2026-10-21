#!/usr/bin/env python3
"""
nightly_tasks.py: work scheduled for the next nightly run (D-254, D-261).

A nightly task is done by the task-manager inside a nightly run's budget, as one worker
subagent, instead of in a session of its own: the monthly usage review (usage_review.py adds it
when it is due, D-254) and one-off tasks with a due time (the ETA review a week on, D-261).

  python3 nightly_tasks.py add NAME --due "YYYY-MM-DD HH:MM" --title T --report DOC \\
          --prompt P [--prompt P2 ...] [--field KEY=VALUE ...] [--max-runs N]
  python3 nightly_tasks.py list [--all]     # the pending tasks (--all: also the done / cancelled ones)
  python3 nightly_tasks.py brief NAME       # the worker's brief: nightly_task.md + the prompts, filled in
  python3 nightly_tasks.py cancel NAME [--why TEXT]

A task: its name (unique, kept for good: the entries are never deleted), a title, due_from (the
--due time is Europe/Berlin wall-clock), the prompts (prompts/<P>.md, in order) and the fields
they are filled in with ({name}, {title}, {report} are always there), the report doc (review:<name>
in the DB) and its status: pending -> done | cancelled.

Carried: from due_from on, every night start of the AFClaude task-manager (keepalive._fire_start:
a session in this checkout, a window start or a postponed one; not a last-stretch slot or a
fill-up) appends nightly_tasks_due.md naming the due tasks (start_insert), and its handled entry
lists their names ("nightly_tasks"). The task-manager runs each as one worker subagent told to run
`nightly_tasks.py brief NAME` and follow it.

Done: the report doc was written at or after due_from (nightly_task.md tells the worker to write
it only once the whole task is complete). The hourly usage_review.py cron run calls track(): a done
task is marked done; one still without its report once max_runs (MAX_RUNS) night starts since
count_from took it along and the last one's run is over (RUN_SPAN) is "missed": an error the
caller raises (D-235: alert, automation paused); it stays pending with a fresh count (count_from =
now), so the nightly runs bring it again once the owner has cleared the pause (or cancels it).

The state is the runner_state component "nightly_tasks" (the DB, D-161; one row per task in its
split part "tasks", merged per entry, audited by actions.state_save). A DB that can't be read or
a save that fails raises failstop.Failed (D-235): a start that can't tell its tasks is aborted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runner_state  # noqa: E402
import failstop  # noqa: E402
import store  # noqa: E402
import afclaude_config  # noqa: E402
import prompts as prompt_registry  # noqa: E402  (add() has a parameter named prompts)

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
COMPONENT = "nightly_tasks"
STATE = runner_state.live_path(COMPONENT)       # the DB (tests point it at a side file or keep it live)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
DUE_FMT = "%Y-%m-%d %H:%M"
MAX_RUNS = 3                                   # night starts that took a task along without its report: missed
RUN_SPAN = afclaude_config.SESSION_LENGTH + timedelta(hours=1)   # ... once the last one's run is over
STATUSES = ("pending", "done", "cancelled")
ALWAYS_FIELDS = ("name", "title", "report")


def _prompt(name: str) -> str:
    """prompts/<name>.md, through the registry (prompts.py; an unknown name is an OSError)."""
    return prompt_registry.default(name)


def _ts(s: object) -> Optional[datetime]:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if isinstance(s, str) and s else None


def berlin(t: datetime) -> str:
    return t.astimezone(BERLIN).strftime("%a %d.%m. %H:%M %Z")


# ---------------------------------------------------------------- the state

def load() -> dict[str, Any]:
    """The tasks' state (the DB). Unreadable: failstop.Failed (no start may go on without it)."""
    st = runner_state.load(COMPONENT, STATE, {"tasks": {}})
    if getattr(st, "unavailable", False):
        raise failstop.Failed("the nightly tasks could not be read", runner_state.load_failed() or "")
    st.setdefault("tasks", {})
    return st


def save(st: dict[str, Any], what: str, actor: Optional[str] = None) -> None:
    """A save that failed is an error (D-235): a task is never silently lost or done twice."""
    if not runner_state.save(COMPONENT, st, STATE, actor=actor):
        raise failstop.Failed("the nightly tasks could not be saved", what)


def get(name: str, st: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    task = (st if st is not None else load())["tasks"].get(name)
    return task if isinstance(task, dict) else None


def fill(task: dict[str, Any], template: str) -> str:
    """`template` filled in with the task's fields and its name, title and report."""
    fields = dict(task.get("fields") or {}, **{k: task[k] for k in ALWAYS_FIELDS})
    return template.format(**fields)


def add(name: str, due_from: datetime, title: str, prompts: list[str], report: str,
        fields: Optional[dict[str, str]] = None, max_runs: int = MAX_RUNS, by: str = "cli",
        now: Optional[datetime] = None) -> dict[str, Any]:
    """Schedule the task `name` (see the module doc). A bad name, a name already used (entries
    are kept for good), an unknown prompt, a placeholder the fields don't fill or a bad report
    doc name: ValueError, nothing saved. -> the task."""
    now = now or datetime.now(UTC)
    if not NAME_RE.match(name):
        raise ValueError(f"bad task name {name!r} (lower-case letters, digits, . _ -; at most 64)")
    if due_from.tzinfo is None:
        raise ValueError("due_from needs a time zone")
    if not title.strip() or not prompts:
        raise ValueError("a task needs a title and at least one prompt")
    if max_runs < 1:
        raise ValueError(f"max_runs must be at least 1, got {max_runs}")
    store.doc_kind(report)                       # review:<name> (or another doc name): ValueError if not
    task: dict[str, Any] = {
        "name": name, "title": title.strip(), "due_from": due_from.astimezone(UTC).isoformat(),
        "prompts": list(prompts), "fields": dict(fields or {}), "report": report, "max_runs": max_runs,
        "status": "pending", "added_at": now.astimezone(UTC).isoformat(), "by": by, "count_from": None,
        "misses": [], "done_at": None, "why": None}
    for p in ["nightly_task", *prompts]:         # fills in now: a missing prompt or field fails here
        try:
            fill(task, _prompt(p))
        except OSError as e:
            raise ValueError(f"no prompt prompts/{p}.md ({type(e).__name__})") from None
        except (KeyError, IndexError) as e:
            raise ValueError(f"prompts/{p}.md needs the field {e} (--field KEY=VALUE)") from None
    st = load()
    if name in st["tasks"]:
        raise ValueError(f"a task named {name!r} exists already ({st['tasks'][name].get('status')})")
    st["tasks"][name] = task
    save(st, f"the new task {name}", actor=by)
    return task


def cancel(name: str, why: str, now: Optional[datetime] = None) -> dict[str, Any]:
    """The pending task `name` cancelled (it is no longer carried). Unknown or not pending: ValueError."""
    now = now or datetime.now(UTC)
    st = load()
    task = get(name, st)
    if task is None or task.get("status") != "pending":
        raise ValueError(f"no pending task named {name!r}")
    task.update(status="cancelled", done_at=now.astimezone(UTC).isoformat(), why=why or "cancelled")
    save(st, f"the cancelled task {name}")
    return task


def due(now: datetime, st: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """The pending tasks due at `now`, the oldest first."""
    tasks = [t for t in (st if st is not None else load())["tasks"].values()
             if isinstance(t, dict) and t.get("status") == "pending" and (_ts(t.get("due_from")) or now) <= now]
    return sorted(tasks, key=lambda t: (t["due_from"], t["name"]))


def brief(name: str) -> str:
    """The worker's brief for the task `name`: nightly_task.md, then its prompts, filled in."""
    task = get(name)
    if task is None:
        raise ValueError(f"no task named {name!r}")
    return "\n\n".join(fill(task, _prompt(p)).strip() for p in ["nightly_task", *task["prompts"]])


def start_insert(now: datetime) -> tuple[Optional[str], list[str]]:
    """For a night start's message: nightly_tasks_due.md naming the due tasks -> (text or None,
    their names). An unreadable state raises failstop.Failed (the caller aborts the start)."""
    tasks = due(now)
    if not tasks:
        return None, []
    listed = "; ".join(f"{t['name']} ({t['title']}; report {t['report']})" for t in tasks)
    return _prompt("nightly_tasks_due").format(tasks=listed).strip(), [t["name"] for t in tasks]


# ---------------------------------------------------------------- the hourly check

def carried(task: dict[str, Any], ka_state: dict[str, Any], run_results: tuple[str, ...]) -> list[datetime]:
    """The night starts that took `task` along since it was due (or since its count_from): the
    keep-alive's handled entries listing its name ("nightly_tasks") that started a run. -> their
    times, oldest first."""
    since = _ts(task.get("count_from")) or _ts(task.get("due_from"))
    times = []
    for r in (ka_state.get("handled") or {}).values():
        if isinstance(r, dict) and task["name"] in (r.get("nightly_tasks") or []) and r.get("result") in run_results:
            t = _ts(r.get("at"))
            if t is not None and (since is None or t >= since):
                times.append(t)
    return sorted(times)


def report_written_since(report: str, since: datetime, repo: Optional[str] = None) -> bool:
    """The doc `report` of this checkout's project exists and was written at or after `since` (an
    older doc of the same name does not count). A DB read error raises failstop.Failed (D-234/D-235)."""
    try:
        conn = store.connect()
        try:
            proj = store.get_project(conn, os.path.abspath(repo or HERE))
            doc = store.get_doc(conn, proj["id"], report) if proj is not None else None
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - an error, not "not written yet"
        raise failstop.Failed("a nightly task's report doc could not be read",
                              f"{report}: {type(e).__name__}: {e}"[:300]) from None
    written = _ts(doc["updated_at"]) if doc else None
    return written is not None and written >= since


def track(now: datetime, ka_state: dict[str, Any],
          run_results: tuple[str, ...]) -> list[tuple[dict[str, Any], str, str]]:
    """The hourly check of the due tasks (usage_review.py's cron run, with the keep-alive state
    and its RUN_RESULTS): done once the report doc was written since due_from; missed once
    max_runs night starts since count_from took it along and the last one's run is over
    (RUN_SPAN): a miss is recorded (misses) and the count starts again (count_from = now); the
    caller raises the error (D-235). An unreadable keep-alive state raises failstop.Failed.
    -> [(task, "done" | "missed", why)]."""
    st = load()
    tasks = due(now, st)
    if not tasks:
        return []
    if getattr(ka_state, "unavailable", False):
        raise failstop.Failed("the keep-alive state could not be read", "checking the nightly tasks")
    out: list[tuple[dict[str, Any], str, str]] = []
    for task in tasks:
        runs = carried(task, ka_state, run_results)
        due_from = _ts(task["due_from"])
        assert due_from is not None
        if report_written_since(task["report"], due_from):
            task.update(status="done", done_at=now.astimezone(UTC).isoformat(), runs=len(runs))
            out.append((task, "done", f"{task['report']} written, {len(runs)} nightly run(s)"))
        elif len(runs) >= int(task.get("max_runs") or MAX_RUNS) and now - runs[-1] >= RUN_SPAN:
            why = (f"{len(runs)} nightly runs took it along (the last started {berlin(runs[-1])}), but its report "
                   f"doc {task['report']} was not written")
            task.setdefault("misses", []).append({"at": now.astimezone(UTC).isoformat(), "runs": len(runs), "why": why})
            task["count_from"] = now.astimezone(UTC).isoformat()
            out.append((task, "missed", why))
    if out:
        save(st, ", ".join(f"{t['name']} {ev}" for t, ev, _ in out))
    return out


# ---------------------------------------------------------------- CLI

def parse_due(text: str) -> datetime:
    try:
        return datetime.strptime(text, DUE_FMT).replace(tzinfo=BERLIN)
    except ValueError:
        raise argparse.ArgumentTypeError(f'bad time {text!r} (want "{DUE_FMT}", Europe/Berlin)') from None


def parse_field(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key.isidentifier():
        raise argparse.ArgumentTypeError(f"bad field {text!r} (want KEY=VALUE)")
    return key, value


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Tasks scheduled for the next nightly run (D-254, D-261).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="schedule a task for the nightly runs from --due on")
    a.add_argument("name")
    a.add_argument("--due", type=parse_due, required=True, help=f'"{DUE_FMT}", Europe/Berlin')
    a.add_argument("--title", required=True)
    a.add_argument("--report", required=True, help="the doc whose writing marks it done, e.g. review:<name>")
    a.add_argument("--prompt", action="append", required=True, help="prompts/<P>.md, in order (repeatable)")
    a.add_argument("--field", type=parse_field, action="append", default=[], help="KEY=VALUE for the prompts")
    a.add_argument("--max-runs", type=int, default=MAX_RUNS)
    ls = sub.add_parser("list", help="the pending tasks")
    ls.add_argument("--all", action="store_true", help="also the done and cancelled ones")
    b = sub.add_parser("brief", help="the worker's brief for a task")
    b.add_argument("name")
    c = sub.add_parser("cancel", help="cancel a pending task")
    c.add_argument("name")
    c.add_argument("--why", default="")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "add":
            task = add(args.name, args.due, args.title, args.prompt, args.report, dict(args.field),
                       max_runs=args.max_runs)
            print(json.dumps(task, indent=1))
            print(f"due from {berlin(args.due)}: the first nightly run from then takes it along")
        elif args.cmd == "list":
            tasks = [t for t in load()["tasks"].values() if args.all or t.get("status") == "pending"]
            print(json.dumps(sorted(tasks, key=lambda t: t["due_from"]), indent=1))
        elif args.cmd == "brief":
            print(brief(args.name))
        else:
            print(json.dumps(cancel(args.name, args.why), indent=1))
    except ValueError as e:
        sys.stderr.write(f"nightly_tasks: {e}\n")
        return 2
    except failstop.Failed as e:
        sys.stderr.write(f"nightly_tasks: {e.why}: {e.detail}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
