#!/usr/bin/env python3
"""
driven.py: the driven_sessions check (phase 3b, docs/phase3b_plan.md §3.3).

Phase 3b looks tmux names up in driven_sessions instead of computing them from the session id; the
launchers write the rows (actions.driven_set). S1's one-time backfill (`store.py import-driven`, the
rows of the old ka-<id8> sessions) is retired since S6.

  check (`tasks.py driven check`, exit 1 on a problem):
    - each project with a task-manager session has exactly its live row (same session id);
    - each live review / stall / request row's tmux session is alive; no live name twice;
    - on the host, each live afc- tmux session has a live row (in the container the bridge
      can't list tmux sessions: that part is skipped and the result says so);
    - notes (not problems): a project without a task-manager session yet (D-281: created at its
      first start), a task-manager whose tmux session isn't running now.

tmux runs on the host (host.py: locally, or over the bridge in the container); a tmux call that
fails (not a plain "no such session") raises: the check stops (D-235).
"""
from __future__ import annotations

import importlib
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from typing import Any, Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402
import actions  # noqa: E402

Alive = Callable[[str], bool]


def tmux_alive(name: str) -> bool:
    """tmux has-session on the host. rc 0: alive; rc 1: no such session (or no tmux server);
    anything else (the bridge failed, a name the bridge refuses): raises."""
    host = importlib.import_module("host")
    r = host.run_on_host(["tmux", "has-session", "-t", "=" + name], timeout=30)
    if r.returncode in (0, 1):
        return bool(r.returncode == 0)
    raise RuntimeError(f"tmux has-session {name} failed (rc {r.returncode}): {str(r.stderr).strip()[:200]}")


def tmux_list() -> Optional[list[str]]:
    """The host's tmux session names; None in the container (the bridge has no list shape)."""
    host = importlib.import_module("host")
    if host.in_container():
        return None
    r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True,
                       timeout=30)
    if r.returncode != 0:
        if re.search(r"no server running|error connecting", r.stderr):
            return []
        raise RuntimeError(f"tmux list-sessions failed (rc {r.returncode}): {r.stderr.strip()[:200]}")
    return [n.strip() for n in r.stdout.splitlines() if n.strip()]


def check(conn: sqlite3.Connection, alive: Optional[Alive] = None,
          listed: Optional[Callable[[], Optional[list[str]]]] = None) -> dict[str, Any]:
    """The check (module doc) -> {ok, problems, notes, rows}. alive / listed: tmux_alive /
    tmux_list unless given."""
    alive = alive or tmux_alive
    listed = listed or tmux_list
    problems: list[str] = []
    notes: list[str] = []
    rows = [r for r in store.driven_sessions(conn, include_ended=False) if r["kind"] in actions.DRIVEN_KINDS]
    for name, n in Counter(r["tmux"] for r in rows if r["tmux"]).items():
        if n > 1:
            problems.append(f"tmux {name} is the live name of {n} rows")
    projects = {int(p["id"]): p for p in store.list_projects(conn)}
    for p in projects.values():
        sid = p["manager_session"]
        if not sid:
            notes.append(f"project {p['name']}: no task-manager session yet (created at its first start, D-281)")
            continue
        tm = [r for r in rows if r["kind"] == "task-manager" and r["project_id"] == p["id"]]
        own = store.get_driven_session(conn, sid)
        if not tm:
            why = (f"its row is of kind {own['kind']!r}" if own and own["kind"] != "task-manager"
                   else "no live task-manager row")
            problems.append(f"project {p['name']}: task-manager {sid[:8]}: {why}")
        elif [r["session_id"] for r in tm] != [sid]:
            problems.append(f"project {p['name']}: task-manager {sid[:8]}, but the live task-manager row(s) are "
                            f"{', '.join(r['session_id'][:8] for r in tm)}")
        elif not alive(tm[0]["tmux"]):
            notes.append(f"project {p['name']}: task-manager {sid[:8]} isn't running now (tmux {tm[0]['tmux']}; "
                         "its next start resumes it under that name)")
    for r in rows:
        if r["kind"] == "task-manager":
            pr = projects.get(int(r["project_id"])) if r["project_id"] is not None else None
            if pr is None or pr["manager_session"] != r["session_id"]:
                problems.append(f"task-manager row {r['session_id'][:8]} (tmux {r['tmux']}): "
                                + ("no project" if pr is None else
                                   f"project {pr['name']}'s task-manager is {(pr['manager_session'] or 'none')[:8]}"))
        elif not r["tmux"]:
            problems.append(f"{r['kind']} row {r['session_id'][:8]} has no tmux name")
        elif not alive(r["tmux"]):
            problems.append(f"{r['kind']} row {r['session_id'][:8]}: tmux {r['tmux']} is gone")
    names = listed()
    if names is None:
        notes.append("tmux sessions without a row: not checked (the host bridge can't list tmux sessions; "
                     "run the check on the host)")
    else:
        known = {r["tmux"] for r in rows}
        for name in names:
            if actions.TMUX_RE.match(name) and name not in known:
                problems.append(f"tmux {name} runs without a live driven_sessions row")
    return {"ok": not problems, "problems": problems, "notes": notes, "rows": rows}


def describe(conn: sqlite3.Connection, include_ended: bool = False) -> list[dict[str, Any]]:
    """The rows (live ones unless include_ended), each with its project's name."""
    names = {int(p["id"]): p["name"] for p in store.list_projects(conn)}
    return [dict(r, project=names.get(int(r["project_id"])) if r["project_id"] is not None else None)
            for r in store.driven_sessions(conn, include_ended=include_ended)]


def print_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("no driven sessions")
    for r in rows:
        print(f"{r['session_id'][:8]}  {r['kind']:<12}  {r['tmux'] or '-':<40}  "
              f"{r['project'] or '-'}" + (f"  ended {r['ended_at']}" if r["ended_at"] else ""))


def print_check(rep: dict[str, Any]) -> None:
    print("driven check: " + ("OK" if rep["ok"] else f"{len(rep['problems'])} problem(s)"))
    for p in rep["problems"]:
        print("  PROBLEM " + p)
    for n in rep["notes"]:
        print("  note " + n)
