#!/usr/bin/env python3
"""
driven.py: the driven_sessions backfill and check (phase 3b S1, docs/phase3b_plan.md §3.3).

Phase 3b looks tmux names up in driven_sessions instead of computing them from the session id.
S1 only fills and checks the table; no runner reads the new rows yet (S2 on).

  backfill (`python3 store.py import-driven`, idempotent; writes through actions.driven_set,
  actor runner:import):
    - every project with a task-manager session (projects.manager_session): a 'task-manager' row
      with that project and the name today's code gives it, ka-<id8>, running or not (the name its
      next start resumes it under);
    - every review session (usage_review state: runs, one_off_runs) and every stalled session the
      dispatcher continued in a tmux session it started (its state's sessions, status running),
      whose tmux session is alive now: a 'review' / 'stall' row;
    - a live 'review' / 'stall' / 'request' row whose tmux session is gone: ended (that name is free);
    - an existing row is never changed (a renamed task-manager row stays renamed);
    then the check below; a problem makes the import fail (exit 1).
  check (`tasks.py driven check`, exit 1 on a problem):
    - each project with a task-manager session has exactly its live row (same session id);
    - each live review / stall / request row's tmux session is alive; no live name twice;
    - on the host, each live ka-/afc- tmux session has a live row (in the container the bridge
      can't list tmux sessions: that part is skipped and the result says so);
    - notes (not problems): a project without a task-manager session yet (D-281: created at its
      first start), a task-manager whose tmux session isn't running now.

tmux runs on the host (host.py: locally, or over the bridge in the container); a tmux call that
fails (not a plain "no such session") raises: the backfill and the check stop (D-235).
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402
import actions  # noqa: E402

ACTOR = "runner:import"
TRANSIENT = ("review", "stall", "request")      # a row of these kinds lives as long as its tmux session
Alive = Callable[[str], bool]


def legacy_tmux(session_id: str) -> str:
    """The tmux name today's launchers compute (ka_resume.sh, keepalive.tmux_name): the backfill
    names a running session by it. S2 stores names instead; S6 retires this."""
    return "ka-" + session_id[:8]


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


def _iso(value: object) -> Optional[str]:
    """A state's time (ISO text, any offset) as store.iso() text; None if it isn't one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return store.iso(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def candidates(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[str]]:
    """The sessions that should have a row -> ([{session_id, kind, tmux, project_id, started_at,
    live_only, source}], notes on state entries that can't be one). One per session id, the
    task-manager first."""
    out: dict[str, dict[str, Any]] = {}
    notes: list[str] = []

    def add(sid: object, kind: str, tmux: str, source: str, project_id: Optional[int] = None,
            started_at: Optional[str] = None) -> None:
        if not isinstance(sid, str) or not store.SESSION_ID_RE.match(sid):
            notes.append(f"{source}: {sid!r} is not a session id (no row)")
            return
        out.setdefault(sid, {"session_id": sid, "kind": kind, "tmux": tmux, "project_id": project_id,
                             "started_at": started_at, "live_only": kind in TRANSIENT, "source": source})

    for p in store.list_projects(conn):
        if p["manager_session"]:
            add(p["manager_session"], "task-manager", legacy_tmux(p["manager_session"]),
                f"project {p['name']}", project_id=int(p["id"]))
    review = store.state_rows(conn, "usage_review")
    for part in ("runs", "one_off_runs"):
        runs = json.loads(review.get(json.dumps([part]), "[]"))
        for run in runs if isinstance(runs, list) else []:
            if isinstance(run, dict) and run.get("session"):
                add(run["session"], "review", legacy_tmux(str(run["session"])), f"usage_review {part}",
                    started_at=_iso(run.get("at")))
    for path, payload in store.state_rows(conn, "dispatcher").items():
        key = json.loads(path)
        if not (isinstance(key, list) and len(key) == 2 and key[0] == "sessions"):
            continue
        e = json.loads(payload)
        if not (isinstance(e, dict) and e.get("status") == "running" and e.get("own_tmux") and e.get("tmux")):
            continue          # finished, or a tmux session someone else started (it stays theirs)
        if e.get("kind", "continue") != "continue":
            notes.append(f"dispatcher session {str(key[1])[:8]}: kind {e.get('kind')!r} is no phase 3b kind (no row)")
            continue
        add(key[1], "stall", str(e["tmux"]), "dispatcher sessions", started_at=_iso(e.get("sent_at")))
    return list(out.values()), notes


def _set(conn: sqlite3.Connection, actor: str, **params: Any) -> Any:
    return actions.perform(conn, "driven.set", params, actor=actor, via="runner")


def backfill(conn: sqlite3.Connection, alive: Optional[Alive] = None,
             listed: Optional[Callable[[], Optional[list[str]]]] = None, dry_run: bool = False,
             actor: str = ACTOR) -> dict[str, Any]:
    """The backfill (module doc) -> {added, ended, kept, not_running, notes, check}. alive / listed:
    tmux_alive / tmux_list unless given."""
    alive = alive or tmux_alive
    cands, notes = candidates(conn)
    added: list[dict[str, Any]] = []
    kept: list[str] = []
    not_running: list[str] = []
    for c in cands:
        if store.get_driven_session(conn, c["session_id"]) is not None:
            kept.append(c["session_id"])
            continue
        if c["live_only"] and not alive(c["tmux"]):
            not_running.append(c["session_id"])
            continue
        params = {k: c[k] for k in ("session_id", "kind", "tmux", "started_at") if c[k] is not None}
        if c["project_id"] is not None:
            params["project"] = c["project_id"]
        if not dry_run:
            _set(conn, actor, **params)
        added.append(dict(params, source=c["source"]))
    ended: list[dict[str, Any]] = []
    for r in store.driven_sessions(conn, include_ended=False):
        if r["kind"] in TRANSIENT and r["tmux"] and not alive(r["tmux"]):
            if not dry_run:
                _set(conn, actor, session_id=r["session_id"], ended_at=store.now_iso())
            ended.append({"session_id": r["session_id"], "kind": r["kind"], "tmux": r["tmux"]})
    return {"dry_run": dry_run, "added": added, "ended": ended, "kept": kept, "not_running": not_running,
            "notes": notes, "check": None if dry_run else check(conn, alive, listed)}


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
                   else "no live task-manager row (python3 store.py import-driven)")
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
            problems.append(f"{r['kind']} row {r['session_id'][:8]}: tmux {r['tmux']} is gone (python3 store.py "
                            "import-driven ends it)")
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


def main(argv: Optional[list[str]] = None) -> int:
    """`python3 store.py import-driven [--db PATH] [--dry-run]` (the check and the list are
    `tasks.py driven check|list`)."""
    ap = argparse.ArgumentParser(description="the driven_sessions backfill (phase 3b S1), then the check")
    ap.add_argument("cmd", choices=("import",))
    ap.add_argument("--db", default=None, help="database path (default: $AFCLAUDE_DB or data/afclaude.db)")
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = ap.parse_args(argv)
    try:
        conn = store.connect(args.db)
    except store.DBError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        out = backfill(conn, dry_run=args.dry_run)
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return 0 if args.dry_run or out["check"]["ok"] else 1
    except (store.DBError, ValueError, LookupError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
