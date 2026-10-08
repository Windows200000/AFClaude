#!/usr/bin/env python3
"""
Task store CLI: tasks, blocked questions, and continue/ignore decisions for
stalled sessions, on store.py's SQLite DB (data/afclaude.db).

    tasks.py add "title" [-d TEXT] [-p high|medium|low] [--project P] [--kind K] [--by SESSION]
    tasks.py list [--all | --status S ...] [--project P] [--kind K]   (default: open tasks)
    tasks.py order [--project P] [--kind K]   the ready queue, in execution order
    tasks.py show ID                       task + its event history
    tasks.py edit ID [--title T] [-d TEXT] [--project P] [--kind K]
    tasks.py prio ID high|medium|low       one stage's priority
    tasks.py move ID N                     stage N (1 = first) within its project
    tasks.py project add NAME [-d TEXT] [--path DIR] [--rank N]
    tasks.py project list                  projects by rank (1 = top)
    tasks.py project move P RANK
    tasks.py project prio P high|medium|low   every open stage of the project at once
    tasks.py project edit P [--name N] [-d TEXT] [--path DIR] [--manager SESSION|""]
    tasks.py block ID "question"           -> blocked, waits for the user
    tasks.py answer ID "answer"            -> pending again, for the next pass
    tasks.py start ID [--session S]        -> in_progress
    tasks.py done ID ["summary"]
    tasks.py cancel ID ["reason"]
    tasks.py reopen ID ["reason"]          done/cancelled/in_progress -> pending
    tasks.py decide SESSION continue|ignore|clear [--note N]   (current stall only)
    tasks.py rule add session|project MATCH continue|ignore [--note N]
    tasks.py rule list | rule rm ID
    tasks.py inbox                         everything that needs the user's input

Tasks are the stages of a project; projects form a ranked list. Execution
order: every high stage (by project rank, then stage), then every medium one,
then every low one; only pending tasks are ready. A project P is a name or a
directory ("." = here; a directory maps to the project whose path is it or its
closest parent). add/edit with an unknown P create that project at the bottom.

SESSION is a session id or unique prefix. A project MATCH is a path (the
session's cwd or a parent dir; "." = here) or a ~/.claude/projects dir name
(those start with "-": put "--" before it, e.g. rule add project -- -mnt-x ignore).
--json prints machine-readable output. Times are shown in Europe/Berlin.
inbox and decide first run stalled.py's incremental scan (--no-scan skips it).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from typing import Any
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402  (reads)
import actions  # noqa: E402  (every write: validation, one transaction, audit row)

BERLIN = ZoneInfo("Europe/Berlin")


def berlin(s: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    dt = store.parse_iso(s)
    return dt.astimezone(BERLIN).strftime(fmt) if dt else "-"


def cut(s, n):
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"


def short_project(p):
    return os.path.basename(p.rstrip("/")) or p if p and p.startswith("/") else (p or "-")


def dump(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------- output

def stage_label(t):
    return f"{t['project_rank']}.{t['stage_seq']}" if t.get("project_id") else "-"


def print_tasks(rows, numbered=False):
    if not rows:
        print("no tasks")
        return
    num = f"{'#':>3}  " if numbered else ""
    print(f"{num}{'id':>4}  {'prio':6}  {'status':11}  {'project':16}  {'stage':5}  {'updated':16}  title")
    for i, t in enumerate(rows, 1):
        title = ("[backlog] " if t["kind"] == "backlog_project" else "") + t["title"]
        num = f"{i:>3}  " if numbered else ""
        print(f"{num}{t['id']:>4}  {t['priority']:6}  {t['status']:11}  {cut(short_project(t['project']), 16):16}  "
              f"{stage_label(t):5}  {berlin(t['updated_at']):16}  {cut(title, 70)}")


def print_projects(rows):
    if not rows:
        print("no projects")
        return
    print(f"{'rank':>4}  {'open':>4}  {'ready h/m/l':11}  {'name':20}  path")
    for p in rows:
        r = p["ready"]
        print(f"{p['rank']:>4}  {p['open']:>4}  {r['high']:>3}/{r['medium']}/{r['low']:<5}  "
              f"{cut(p['name'], 20):20}  {p['path'] or '-'}"
              + (f"  (managed by {p['manager_session'][:8]})" if p.get("manager_session") else ""))


def event_text(e):
    d = e["detail"] or {}
    ev = e["event"]
    if ev in ("priority", "moved"):
        return f"{d.get('from')} -> {d.get('to')}" + (f" (whole {d['via']})" if d.get("via") else "")
    if ev in ("updated", "migrated"):
        return ", ".join(f"{k}: {cut(str(v[0]), 25)} -> {cut(str(v[1]), 25)}" if isinstance(v, list)
                         else f"{k}: {cut(str(v), 25)}" for k, v in d.items())
    if ev == "created":
        return (f"prio {d.get('priority')}, {d.get('kind')}" + (f", project {d['project']}" if d.get("project") else "")
                + (f", by {d['created_by_session'][:8]}" if d.get("created_by_session") else ""))
    parts = [f"from {d['from']}"] if d.get("from") else []
    for k in ("question", "answer", "session", "summary", "reason"):
        if d.get(k):
            parts.append(f"{k}: {cut(d[k], 70)}")
    return "; ".join(parts)


def print_task(t, events):
    kind = " [backlog project]" if t["kind"] == "backlog_project" else ""
    print(f"#{t['id']}  {t['title']}{kind}")
    stage = (f", stage {t['stage_seq']} of project #{t['project_rank']} {t['project']}" if t["project_id"]
             else ", no project")
    print(f"  status {t['status']}, priority {t['priority']}{stage}")
    by = f" by {t['created_by_session']}" if t["created_by_session"] else ""
    print(f"  created {berlin(t['created_at'])}{by}, updated {berlin(t['updated_at'])}")
    if t["assigned_session"]:
        print(f"  session {t['assigned_session']}")
    if t["blocked_question"]:
        print(f"  question ({berlin(t['blocked_at'])}): {t['blocked_question']}")
    if t["answer"]:
        print(f"  answer   ({berlin(t['answered_at'])}): {t['answer']}")
    if t["result_summary"]:
        print(f"  result   ({berlin(t['done_at'])}): {t['result_summary']}")
    if t["description"]:
        print("  description:")
        for ln in t["description"].splitlines():
            print(f"    {ln}")
    print("  history (Berlin):")
    for e in events:
        print(f"    {berlin(e['ts'])}  {e['event']:9}  {event_text(e)}")


def print_session_line(s):
    kind = s.get("stall_kind") or "?"
    print(f"  {s['session_id'][:8]}  {kind} limit, stalled {berlin(s.get('stalled_since'))}, "
          f"resets {berlin(s.get('stall_reset_at'))}{'  (own)' if s.get('own') else ''}")
    print(f"      {cut(s.get('title') or '(untitled)', 50)}  {s.get('cwd') or '-'}")


SESSION_JSON = ("session_id", "title", "cwd", "project_dir", "own", "stalled_since", "stall_kind",
                "stall_reset_at", "stall_text", "hits", "last_hit", "path", "decision", "decision_source")


def session_json(s: Mapping[str, Any]) -> dict[str, Any]:
    d = {k: s.get(k) for k in SESSION_JSON}
    d["own"] = bool(d["own"])
    d["stall_reset_at_berlin"] = berlin(s.get("stall_reset_at"), "%Y-%m-%d %H:%M %Z") \
        if s.get("stall_reset_at") else None
    return d


def print_inbox(box):
    bt, us = box["blocked_tasks"], box["undecided_sessions"]
    if not bt and not us:
        print("nothing needs your input")
        return
    if bt:
        print(f"{len(bt)} blocked task(s), answer with: tasks.py answer ID \"...\"")
        for t in bt:
            print(f"  #{t['id']}  {t['priority']}  {cut(short_project(t['project']), 16)}  {cut(t['title'], 60)}")
            print(f"      Q ({berlin(t['blocked_at'])}): {t['blocked_question']}")
    if us:
        print(f"{len(us)} undecided stalled session(s), decide with: tasks.py decide ID continue|ignore")
        for s in us:
            print_session_line(s)


def print_rules(rules):
    if not rules:
        print("no standing rules")
        return
    print(f"{'id':>3}  {'scope':7}  {'decision':8}  {'created':16}  match  (note)")
    for r in rules:
        note = f"  ({r['note']})" if r["note"] else ""
        print(f"{r['id']:>3}  {r['scope']:7}  {r['decision']:8}  {berlin(r['created_at']):16}  {r['match']}{note}")


# ---------------------------------------------------------------- commands

def resolve_session(conn: sqlite3.Connection, prefix: str) -> str:
    m = store.find_sessions(conn, prefix)
    if len(m) == 1:
        return m[0]["session_id"]
    if len(m) > 1:
        raise store.NotFound(f"{len(m)} sessions match {prefix!r}: "
                             + ", ".join(x["session_id"][:13] for x in m[:10]))
    if len(prefix) == 36:       # full uuid of a session the scan hasn't seen (session rules only)
        return prefix
    raise store.NotFound(f"no session matches {prefix!r}")


def act(conn, action, /, **params):
    """One write through the shared write path (actions.py), as the CLI."""
    return actions.perform(conn, action, params, actor="cli", via="cli")


def maybe_scan(conn, args):
    if not args.no_scan:
        import stalled      # imports keepalive/usage_sampler; only needed here
        stalled.scan(conn, args.projects_dir)


def task_out(args, t):
    if args.json:
        dump(t)
    else:
        where = f"  {t['project']} stage {t['stage_seq']}" if t["project_id"] else ""
        print(f"#{t['id']}  {t['status']}  {t['priority']}{where}  {t['title']}")


def project_ref(p):
    """CLI project argument: '.', './x', '../x', '~/x' are directories (made absolute)."""
    if p is not None and (p == "." or p == ".." or p.startswith(("./", "../", "~"))):
        return os.path.abspath(os.path.expanduser(p))
    return p


def run_project(conn, args):
    pc = args.project_cmd
    if pc == "list":
        rows = store.list_projects(conn)
        return dump(rows) if args.json else print_projects(rows)
    if pc == "add":
        p = act(conn, "project.add", name=args.name, description=args.description, path=project_ref(args.path),
                rank=args.rank)
    elif pc == "move":
        p = act(conn, "project.move", project=project_ref(args.project), rank=args.rank)
    elif pc == "edit":
        f = {k: v for k, v in (("name", args.name), ("description", args.description),
                               ("path", project_ref(args.path))) if v is not None}
        if args.manager is not None:   # "" clears it
            f["manager_session"] = resolve_session(conn, args.manager) if args.manager.strip() else None
        if not f:
            raise ValueError("nothing to change (use --name/-d/--path/--manager)")
        p = act(conn, "project.edit", project=project_ref(args.project), **f)
    else:  # prio
        r = act(conn, "project.priority", project=project_ref(args.project), priority=args.priority)
        if args.json:
            return dump(r)
        return print(f"{r['project']}: {len(r['changed'])} open stage(s) set to {r['priority']}")
    if args.json:
        dump(p)
    else:
        print(f"project {p['name']}  rank {p['rank']}" + (f"  {p['path']}" if p["path"] else ""))


def run(conn, args):
    c = args.cmd
    if c == "add":
        task_out(args, act(conn, "task.add", title=args.title, description=args.description,
                           project=project_ref(args.project), priority=args.priority, kind=args.kind,
                           created_by_session=args.by))
    elif c == "list":
        status = None if args.all else (args.status or list(store.OPEN_STATUSES))
        rows = store.list_tasks(conn, status=status, project=project_ref(args.project), kind=args.kind)
        dump(rows) if args.json else print_tasks(rows)
    elif c == "order":
        rows = store.execution_order(conn, project=project_ref(args.project), kind=args.kind)
        dump(rows) if args.json else print_tasks(rows, numbered=True)
    elif c == "project":
        run_project(conn, args)
    elif c == "move":
        task_out(args, act(conn, "task.move", task_id=args.id, stage=args.stage))
    elif c == "show":
        t = store.get_task(conn, args.id)
        if t is None:
            raise store.NotFound(f"no task #{args.id}")
        ev = store.task_events(conn, args.id)
        dump(dict(t, events=ev)) if args.json else print_task(t, ev)
    elif c == "edit":
        f = {k: v for k, v in (("title", args.title), ("description", args.description),
                               ("project", project_ref(args.project)), ("kind", args.kind)) if v is not None}
        if not f:
            raise ValueError("nothing to change (use --title/-d/--project/--kind)")
        task_out(args, act(conn, "task.edit", task_id=args.id, **f))
    elif c == "prio":
        task_out(args, act(conn, "task.priority", task_id=args.id, priority=args.priority))
    elif c == "block":
        task_out(args, act(conn, "task.block", task_id=args.id, question=args.question))
    elif c == "answer":
        task_out(args, act(conn, "task.answer", task_id=args.id, answer=args.answer))
    elif c == "start":
        task_out(args, act(conn, "task.start", task_id=args.id, session=args.session))
    elif c == "done":
        task_out(args, act(conn, "task.finish", task_id=args.id, summary=args.summary))
    elif c == "cancel":
        task_out(args, act(conn, "task.cancel", task_id=args.id, reason=args.reason))
    elif c == "reopen":
        task_out(args, act(conn, "task.reopen", task_id=args.id, reason=args.reason))
    elif c == "decide":
        maybe_scan(conn, args)
        sid = resolve_session(conn, args.session)
        if args.decision == "clear":
            act(conn, "session.clear", session_id=sid)
        else:
            act(conn, "session.decide", session_id=sid, decision=args.decision, note=args.note)
        dec, src = store.effective_decision(conn, sid)
        if args.json:
            dump({"session_id": sid, "decision": dec, "source": src})
        else:
            print(f"{sid[:8]}: {dec or 'undecided'}" + (f" ({src})" if src else ""))
    elif c == "rule":
        if args.rule_cmd == "list":
            rules = store.list_rules(conn)
            dump(rules) if args.json else print_rules(rules)
        elif args.rule_cmd == "rm":
            act(conn, "rule.remove", rule_id=args.id)
            dump({"removed": args.id}) if args.json else print(f"rule #{args.id} removed")
        else:
            match = args.match
            if args.scope == "session":
                match = resolve_session(conn, match)
            elif match == "." or match.startswith(("./", "../", "~")):
                match = os.path.abspath(os.path.expanduser(match))
            r = act(conn, "rule.add", scope=args.scope, match=match, decision=args.decision, note=args.note)
            dump(r) if args.json else print_rules([r])
    elif c == "inbox":
        maybe_scan(conn, args)
        box = store.pending_user_input(conn)
        if args.json:
            dump({"blocked_tasks": box["blocked_tasks"],
                  "undecided_sessions": [session_json(s) for s in box["undecided_sessions"]]})
        else:
            print_inbox(box)
    return 0


def parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help=f"SQLite file (default {store.DB_PATH})")
    ap.add_argument("--projects-dir", help="transcripts root for the scan (default ~/.claude/projects)")
    js = argparse.ArgumentParser(add_help=False)
    js.add_argument("--json", action="store_true", help="machine-readable output")
    scan = argparse.ArgumentParser(add_help=False)
    scan.add_argument("--no-scan", action="store_true", help="skip the stalled-session scan")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", parents=[js], help="new task (pending)")
    p.add_argument("title")
    p.add_argument("-d", "--description")
    p.add_argument("-p", "--priority", choices=store.PRIORITIES, default=store.DEFAULT_PRIORITY)
    p.add_argument("--project", help="name or directory; unknown ones are created")
    p.add_argument("--kind", choices=store.TASK_KINDS, default="task")
    p.add_argument("--by", help="session that created it")
    p = sub.add_parser("list", parents=[js], help="tasks in execution order (default: open ones)")
    p.add_argument("--status", nargs="+", choices=store.TASK_STATUSES)
    p.add_argument("--all", action="store_true", help="include done and cancelled")
    p.add_argument("--project")
    p.add_argument("--kind", choices=store.ALL_TASK_KINDS)
    p = sub.add_parser("order", parents=[js], help="the ready queue in execution order")
    p.add_argument("--project")
    p.add_argument("--kind", choices=store.TASK_KINDS)
    p = sub.add_parser("move", parents=[js], help="move a task to stage N of its project")
    p.add_argument("id", type=int)
    p.add_argument("stage", type=int)
    p = sub.add_parser("project", help="the ranked project list")
    ps = p.add_subparsers(dest="project_cmd", required=True)
    q = ps.add_parser("add", parents=[js], help="new project (bottom of the list unless --rank)")
    q.add_argument("name")
    q.add_argument("-d", "--description")
    q.add_argument("--path", help="its directory, so sessions there map to it")
    q.add_argument("--rank", type=int)
    ps.add_parser("list", parents=[js], help="projects by rank")
    q = ps.add_parser("move", parents=[js], help="set a project's rank (1 = top)")
    q.add_argument("project")
    q.add_argument("rank", type=int)
    q = ps.add_parser("prio", parents=[js], help="set every open stage of a project")
    q.add_argument("project")
    q.add_argument("priority", choices=store.PRIORITIES)
    q = ps.add_parser("edit", parents=[js], help="rename / describe / set the path")
    q.add_argument("project")
    q.add_argument("--name")
    q.add_argument("-d", "--description")
    q.add_argument("--path")
    q.add_argument("--manager", metavar="SESSION",
                   help="managed project: this session works its stages, the dispatcher starts no "
                        "task sessions for it (\"\" = unmanaged)")
    p = sub.add_parser("show", parents=[js], help="task + history")
    p.add_argument("id", type=int)
    p = sub.add_parser("edit", parents=[js], help="change title/description/project/kind")
    p.add_argument("id", type=int)
    p.add_argument("--title")
    p.add_argument("-d", "--description")
    p.add_argument("--project")
    p.add_argument("--kind", choices=store.TASK_KINDS)
    p = sub.add_parser("prio", parents=[js], help="set one stage's priority")
    p.add_argument("id", type=int)
    p.add_argument("priority", choices=store.PRIORITIES)
    p = sub.add_parser("block", parents=[js], help="blocked on a question for the user")
    p.add_argument("id", type=int)
    p.add_argument("question")
    p = sub.add_parser("answer", parents=[js], help="answer a blocked task -> pending")
    p.add_argument("id", type=int)
    p.add_argument("answer")
    p = sub.add_parser("start", parents=[js], help="pending -> in_progress")
    p.add_argument("id", type=int)
    p.add_argument("--session")
    for name, arg in (("done", "summary"), ("cancel", "reason"), ("reopen", "reason")):
        p = sub.add_parser(name, parents=[js], help=f"{name} a task")
        p.add_argument("id", type=int)
        p.add_argument(arg, nargs="?")
    p = sub.add_parser("decide", parents=[js, scan], help="continue/ignore a stalled session")
    p.add_argument("session")
    p.add_argument("decision", choices=store.DECISIONS + ("clear",))
    p.add_argument("--note")
    p = sub.add_parser("rule", help="standing rules")
    rs = p.add_subparsers(dest="rule_cmd", required=True)
    q = rs.add_parser("add", parents=[js], help="always continue/ignore for a session or project")
    q.add_argument("scope", choices=store.RULE_SCOPES)
    q.add_argument("match")
    q.add_argument("decision", choices=store.DECISIONS)
    q.add_argument("--note")
    rs.add_parser("list", parents=[js])
    q = rs.add_parser("rm", parents=[js])
    q.add_argument("id", type=int)
    sub.add_parser("inbox", parents=[js, scan], help="blocked tasks + undecided stalled sessions")
    return ap


def db_error(e, args):
    """A database error -> the hand-back text on stderr (docs/dashboard_design.md §7.8): class,
    message, what was not saved, the escalation, the next step. -> exit code 1. A failed write
    was already recorded and escalated by actions.perform; any other (a read, opening the DB)
    is reported here."""
    err = store.as_db_error(e)
    if err.escalation is None:
        store.report_db_error(err, actor="cli", action=f"cli:{args.cmd}", write=False,
                              params={k: v for k, v in vars(args).items() if k not in ("db", "func")},
                              db_path=args.db)
    print(store.handback_text(err), file=sys.stderr)
    return 1


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        conn = store.connect(args.db)
    except (store.DBError, sqlite3.Error) as e:
        return db_error(e, args)
    try:
        return run(conn, args)
    except (ValueError, LookupError) as e:          # validation, bad transition, not found
        msg = e.args[0] if isinstance(e, LookupError) and e.args else str(e)
        print(f"error: {msg}", file=sys.stderr)
        return 1
    except (store.DBError, sqlite3.Error) as e:     # the DB error path, incl. the schema guard
        return db_error(e, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
