#!/usr/bin/env python3
"""
Task store CLI: tasks, blocked questions, and continue/ignore decisions for
stalled sessions, on store.py's SQLite DB (data/afclaude.db).

    tasks.py add "title" [-d TEXT] [-p high|medium|low] [--project P] [--kind K] [--by SESSION]
    tasks.py list [--all | --status S ...] [--project P] [--kind K]   (default: open tasks)
    tasks.py order [--project P] [--kind K]   the ready queue, in execution order
    tasks.py show ID                       task + its event history
    tasks.py edit ID [--title T] [-d TEXT] [--project P] [--kind K] [--sessions N|""]
                                           --sessions: estimated session windows the stage still
                                           needs (stage ETA, D-213); "" clears it
    tasks.py prio ID high|medium|low       one stage's priority
    tasks.py move ID N                     stage N (1 = first) within its project
    tasks.py project add NAME [-d TEXT] [--path DIR] [--rank N]
    tasks.py project list                  projects by rank (1 = top)
    tasks.py project move P RANK
    tasks.py project prio P high|medium|low   every open stage of the project at once
    tasks.py project edit P [--name N] [-d TEXT] [--path DIR] [--manager SESSION|""]
    tasks.py project launch P [--model M] [--effort E]   the model / effort of P's task-manager session
                                           (D-260; "default" clears one); used at its next start/resume
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
    tasks.py inbox list [--all]            the owner's messages from the dashboard (D-241): open ones
                                           (--all: the recent ones), newest first
    tasks.py inbox show ID                 one message in full, with its verdict, status and answer
    tasks.py inbox reply ID "answer"       answer it (the owner reads it on the dashboard)
    tasks.py inbox retry ID                send a failed message again (triage / delivery)
    tasks.py handoff write --session S --json BODY [--project P]   a compaction hand-off (D-144):
                                           BODY = JSON {summary, done, in_progress, next_steps,
                                           open_question_ids, notes} ("-" = read it from stdin)
    tasks.py handoff show [--session S] [--all] [--json]   the latest hand-off (--all: recent ones)
    tasks.py automation [status]           is automation paused (D-234/D-235), why, since when: the
                                           install-wide pause and every paused project (D-244)
    tasks.py automation resume [--project P | --all]   clear the install-wide pause (after looking
                                           at the error it names); --project: that project's pause
                                           only; --all: the install-wide one and every project's
    tasks.py automation pause [REASON] [--project P]   pause every runner (or one project's) by hand
    tasks.py doc list [--project P]        the project's docs in the DB (phase 2d, project_docs.py)
    tasks.py doc show NAME [--project P] [--entries N]   a doc's text (--entries: its newest entries)
    tasks.py doc append NAME TEXT|- [--project P]   add an entry to progress|exceptions|alerts
                                           (a newline is added if TEXT doesn't end with one)
    tasks.py doc set NAME TEXT|- [--project P] [--version N]   replace goals / review:<name> (or rewrite a log)
    tasks.py doc import [--repo DIR] [--project P] [--force]   take hand edits of the files into the DB (also every 10 min)
    tasks.py doc export [--repo DIR] [--project P] [--check]   regenerate PROGRESS.md, GOALS.md,
                                           EXCEPTIONS.md from the DB (D-181; run before committing)
    tasks.py decision tldr [--all]         the owner's decisions in the DB (D-242, decisions.py): one
                                           line per current one (--all: done/superseded too)
    tasks.py decision find KW... [--all] [--limit N]   full entries matching any keyword or phrase
                                           (keyword matches first, then full-text hits; default 10)
    tasks.py decision show ID              one decision's full entry (D-NNN / M-NNN)
    tasks.py decision keywords ID add|remove KW...   keep a decision's keywords current (audited;
                                           kept apart from DECISIONS.md's Keywords line)
    tasks.py decision import [--file F] [--force]   DECISIONS.md into the DB now (also every 10 min
                                           and at each compaction; the file is never written)

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
import manager_launch  # noqa: E402  (D-260: the task-manager launch choices)

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
              + (f"  (managed by {p['manager_session'][:8]})" if p.get("manager_session") else "")
              + launch_text(p))


def launch_text(p):
    """"  [model M, effort E]" for a project whose task-manager launch is set (D-260), else ""."""
    set_ = [f"{k} {p[f'manager_{k}']}" for k in ("model", "effort") if p.get(f"manager_{k}")]
    return f"  [task-manager {', '.join(set_)}]" if set_ else ""


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
    elif pc == "launch":
        if args.model is None and args.effort is None:
            v = manager_launch.view(store._project_row(conn, project_ref(args.project)))
            return dump(v) if args.json else print(
                f"task-manager: model {v['model']} ({v['source']['model']}), effort {v['effort']} "
                f"({v['source']['effort']})")
        p = act(conn, "project.manager_launch", project=project_ref(args.project), model=args.model,
                effort=args.effort)
        if not args.json:
            v = p["launch"]
            return print(f"project {p['name']}: task-manager model {v['model']}, effort {v['effort']} "
                         "(from its next start/resume)")
    else:  # prio
        r = act(conn, "project.priority", project=project_ref(args.project), priority=args.priority)
        if args.json:
            return dump(r)
        return print(f"{r['project']}: {len(r['changed'])} open stage(s) set to {r['priority']}")
    if args.json:
        dump(p)
    else:
        print(f"project {p['name']}  rank {p['rank']}" + (f"  {p['path']}" if p["path"] else ""))


def run_handoff(conn, args):
    if args.handoff_cmd == "write":
        body = sys.stdin.read() if args.body == "-" else args.body
        sid = resolve_session(conn, args.session)
        h = act(conn, "handoff.write", session_id=sid, body=body, project=project_ref(args.project))
        return print(f"hand-off #{h['id']} saved for {h['session_id'][:8]}: HANDOFF SAVED")
    sid = resolve_session(conn, args.session) if args.session else None
    rows = store.list_handoffs(conn, sid, limit=20 if args.all else 1)
    if args.json:
        return dump(rows if args.all else (rows[0] if rows else None))
    if not rows:
        return print("no hand-off" + (f" for {sid[:8]}" if sid else ""))
    for h in rows:
        state = f"consumed {berlin(h['consumed_at'])} by {h['consumed_by_start']}" if h["consumed_at"] else "open"
        print(f"#{h['id']}  {h['session_id'][:8]}  {berlin(h['created_at'])}  {state}")
        print("   " + store.render_handoff(h))


def decision_out(d):
    """A decision's full entry, plus the keywords in the DB when the task-manager changed them."""
    print(d["body"])
    if d["keywords_added"] or d["keywords_removed"]:
        print(f"- (keywords in the DB: {', '.join(d['effective_keywords'])})")


def run_decision(conn, args):
    import decisions
    dc = args.decision_cmd
    if dc == "import":
        r = decisions.import_file(args.file, actor="cli", via="cli", force=args.force, conn=conn)
        if args.json:
            return dump(r)
        parts = [f"{k} {', '.join(r[k])}" for k in ("added", "changed", "removed") if r[k]]
        print(f"{r['entries']} decisions from {r['file']}: " + ("; ".join(parts) or "unchanged")
              + (f" ({r['moved']} moved)" if r["moved"] else ""))
        if r["no_status"]:
            print(f"no Status line ({len(r['no_status'])}, counted as active unless the title names a "
                  f"state): {', '.join(r['no_status'])}")
        return None
    if dc == "tldr":
        rows = store.list_decision_entries(conn, None if args.all else decisions.CURRENT)
        if args.json:
            return dump([{"id": d["id"], "title": d["title"], "state": d["state"], "status": d["status"]}
                         for d in rows])
        return print(decisions.tldr_text([decisions.tldr_line(d) for d in rows]))
    if dc == "show":
        d = store.get_decision_entry(conn, args.id)
        if d is None:
            raise store.NotFound(f"no decision {args.id}")
        return dump(d) if args.json else decision_out(d)
    if dc == "keywords":
        r = act(conn, "decision.keywords", decision=args.id, op=args.op, keywords=args.keywords)
        return dump(r) if args.json else print(f"{r['id']} keywords: {', '.join(r['keywords'])}")
    hits = store.find_decision_entries(conn, args.keywords, None if args.all else decisions.CURRENT)
    shown = hits if args.limit == 0 else hits[:args.limit]
    if args.json:
        return dump({"hits": shown, "more": len(hits) - len(shown)})
    if not hits:
        return print(f"no {'' if args.all else 'current '}decision matches {' / '.join(args.keywords)}"
                     + ("" if args.all else " (--all: also done and superseded ones)"))
    for n, d in enumerate(shown):
        if n:
            print()
        how = f"keywords: {', '.join(d['matched'])}" if d["match"] == "keyword" else "text match"
        print(f"=== {d['id']} ({how})")
        decision_out(d)
    if len(hits) > len(shown):
        print(f"\n({len(hits) - len(shown)} more matches not shown: "
              + ", ".join(d["id"] for d in hits[len(shown):]) + "; --limit 0 shows all)")
    return None


def doc_project(conn, p):
    """--project for the doc commands: a name or directory, default this directory's project."""
    ref = project_ref(p) if p else os.getcwd()
    proj = store.get_project(conn, ref)
    if proj is None:
        raise store.NotFound(f"no project {p!r}" if p else f"no project for {ref} (pass --project)")
    return proj


def entry_text(text):
    """An appended entry ends with a newline (the next one starts on its own line)."""
    return text if text.endswith("\n") else text + "\n"


def run_doc(conn, args):
    import project_docs
    dc = args.doc_cmd
    if dc in ("import", "export"):
        repo = os.path.abspath(args.repo) if args.repo else None
        if dc == "import":
            r = project_docs.import_all(args.db, repo, None, args.project, actor="cli", via="cli", force=args.force)
            ok = not (r["totals"]["conflict"] or r["totals"]["unverified"])
            if args.json:
                dump(r)
            else:
                for name, d in r["docs"].items():
                    extra = f"  +{d['appended']}" if d.get("appended") else ""
                    print(f"{name:<12} {d['outcome']}{extra}" + (f"  {d['error']}" if d.get("error") else ""))
        else:
            r = project_docs.export(args.db, repo, args.project, check=args.check, actor="cli", via="cli")
            ok = r["ok"]
            if args.json:
                dump(r)
            else:
                for fname, d in r["files"].items():
                    print(f"{fname:<14} {d['result']}" + (f"  {d['error']}" if d.get("error") else ""))
                    for f in d.get("findings", []):
                        print(f"    {f}")
        return 0 if ok else 1
    proj = doc_project(conn, args.project)
    if dc == "list":
        rows = store.list_docs(conn, proj["id"])
        if args.json:
            return dump(rows)
        if not rows:
            return print(f"no docs for {proj['name']}")
        for d in rows:
            print(f"{d['name']:<12} {d['kind']:<4}  {d['entries']:>4} entries  v{d['version']}  "
                  f"{berlin(d['updated_at'])}  {d['updated_by']}")
        return None
    if dc == "show":
        doc = store.get_doc(conn, proj["id"], args.name)
        if doc is None:
            raise store.NotFound(f"no doc {args.name!r} for {proj['name']}")
        if args.entries:
            rows = store.doc_entries(conn, doc["id"], args.entries)
            if args.json:
                return dump(rows)
            for e in rows:
                keep = f" keep {e['keep']}" if e["op"] == "replace" else ""
                print(f"#{e['id']}  {e['op']}{keep}  {berlin(e['ts'])}  {e['author']}")
                print("   " + cut(e["body"], 200))
            return None
        text = store.doc_render(conn, doc["id"])
        if args.json:
            return dump(dict({k: v for k, v in doc.items() if k != "file_text"}, text=text))
        sys.stdout.write(text)
        return None
    text = sys.stdin.read() if args.text == "-" else args.text
    if dc == "append":
        r = act(conn, "doc.append", project=proj["name"], name=args.name, text=entry_text(text))
    else:
        r = act(conn, "doc.set", project=proj["name"], name=args.name, text=text, version=args.version)
    if args.json:
        return dump(r)
    return print(f"{r['project']}/{r['name']}: " + ("unchanged" if r.get("unchanged") else f"entry #{r['entry']} saved"))


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
        if args.sessions is not None:
            f["estimated_sessions"] = float(args.sessions) if args.sessions.strip() else None
        if not f:
            raise ValueError("nothing to change (use --title/-d/--project/--kind/--sessions)")
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
    elif c == "handoff":
        run_handoff(conn, args)
    elif c == "doc":
        return run_doc(conn, args) or 0
    elif c == "decision":
        run_decision(conn, args)
    elif c == "automation":
        run_automation(conn, args)
    elif c == "inbox" and args.inbox_cmd:
        run_inbox_messages(conn, args)
    elif c == "inbox":
        maybe_scan(conn, args)
        box = store.pending_user_input(conn)
        if args.json:
            dump({"blocked_tasks": box["blocked_tasks"],
                  "undecided_sessions": [session_json(s) for s in box["undecided_sessions"]]})
        else:
            print_inbox(box)
    return 0


INBOX_OPEN = ("pending", "delivered", "noted", "error")


def print_inbox_message(m, full=False):
    verdict = f"{m['verdict']}: {m['verdict_reason']}" if m["verdict"] else "not triaged yet"
    print(f"#{m['id']}  {m['status']}  {berlin(m['created_at'])}  ({verdict})")
    text = m["text"] if full else cut(" ".join(m["text"].split()), 200)
    print("   " + text.replace("\n", "\n   "))
    if m["hold"]:
        print(f"   waiting since {berlin(m['hold_since'])}: {m['hold']}")
    if m["error"]:
        print(f"   ERROR {berlin(m['error_at'])}: {m['error']}")
    if m["included_by"]:
        print(f"   sent along with {m['included_by']} at {berlin(m['included_at'])}")
    if m["answer"]:
        ans = m["answer"] if full else cut(" ".join(m["answer"].split()), 200)
        print(f"   answer ({berlin(m['answered_at'])}): " + ans.replace("\n", "\n   "))


def run_inbox_messages(conn, args):
    """The owner's messages to the task-manager (D-178/D-241): read them, answer them."""
    ic = args.inbox_cmd
    if ic == "list":
        rows = store.list_inbox(conn, None if args.all else INBOX_OPEN, limit=50)
        if args.json:
            return dump(rows)
        if not rows:
            return print("no open messages from the owner" if not args.all else "no messages")
        for m in rows:
            print_inbox_message(m)
        return None
    if ic == "show":
        m = store.inbox_row(conn, args.id)
        return dump(m) if args.json else print_inbox_message(m, full=True)
    if ic == "reply":
        m = act(conn, "inbox.reply", message_id=args.id, answer=args.answer)
    else:   # retry
        m = act(conn, "inbox.retry", message_id=args.id)
    if args.json:
        return dump(m)
    return print(f"inbox #{m['id']}: {m['status']}")


def run_automation(conn, args):
    """D-234/D-235: a runner pauses automation on any failure; the owner looks, then resumes.
    Two scopes (D-244): the install-wide pause and a project's (an inbox error pauses only the
    project its message was for). resume / pause act on the install-wide one; --project on that
    project's; resume --all on both kinds."""
    import failstop
    cmd = getattr(args, "automation_cmd", None)
    project = getattr(args, "project", None)
    if project is not None and project.isdigit():
        project = int(project)                     # an id (store.get_project: a string is a name or path)
    if cmd == "resume":
        if project is not None and args.all:
            raise ValueError("automation resume: --project or --all, not both")
        if project is not None:
            act(conn, "automation.set_project", project=project, paused=False)
        else:
            act(conn, "automation.set", paused=False)
            if args.all:
                for pp in store.project_pauses(conn):
                    act(conn, "automation.set_project", project=pp["project_id"], paused=False)
    elif cmd == "pause":
        reason = args.reason or "paused by hand (tasks.py)"
        if project is not None:
            act(conn, "automation.set_project", project=project, paused=True, reason=reason)
        else:
            act(conn, "automation.set", paused=True, reason=reason)
    row = store.get_setting_row(conn, "automation_paused")
    st = {"paused": bool(actions.get_setting(conn, "automation_paused")),
          "reason": actions.get_setting(conn, "automation_paused_reason") or None,
          "since": row["updated_at"] if row else None, "by": row["updated_by"] if row else None,
          "projects": [{"project": r["project_id"], "name": r["name"], "reason": r["reason"],
                        "since": r["updated_at"], "by": r["updated_by"]} for r in store.project_pauses(conn)]}
    if args.json:
        return dump(st)
    if st["paused"]:
        print(f"automation PAUSED since {berlin(st['since'])} (by {st['by']}): {st['reason'] or '(no reason given)'}")
    else:
        print("automation running (not paused)" if not st["projects"] else
              "automation running install-wide (not paused); paused project(s) below")
    for pp in st["projects"]:
        print(f"project {pp['name']} PAUSED since {berlin(pp['since'])} (by {pp['by']}): "
              f"{pp['reason'] or '(no reason given)'}")
    if st["paused"] or st["projects"]:
        print("look at the error (tasks.py doc show alerts), fix it, then: "
              + "; ".join(([failstop.RESUME_CMD] if st["paused"] else [])
                          + [failstop.resume_cmd(pp["name"]) for pp in st["projects"]])
              + (f" (or all at once: {failstop.RESUME_CMD} --all)" if len(st["projects"]) + st["paused"] > 1 else ""))
    return None


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
    q = ps.add_parser("launch", parents=[js], help="show / set the model and effort of the project's "
                      "task-manager session (D-260, owner-only)")
    q.add_argument("project")
    q.add_argument("--model", help=f"{'|'.join(manager_launch.MODELS)} or \"default\"")
    q.add_argument("--effort", help=f"{'|'.join(manager_launch.EFFORTS)} or \"default\"")
    p = sub.add_parser("show", parents=[js], help="task + history")
    p.add_argument("id", type=int)
    p = sub.add_parser("edit", parents=[js], help="change title/description/project/kind")
    p.add_argument("id", type=int)
    p.add_argument("--title")
    p.add_argument("-d", "--description")
    p.add_argument("--project")
    p.add_argument("--kind", choices=store.TASK_KINDS)
    p.add_argument("--sessions", metavar="N", help='estimated session windows this stage still needs '
                   '(stage ETA, D-213); "" = clear (the default from history is used)')
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
    p = sub.add_parser("inbox", parents=[js, scan], help="blocked tasks + undecided stalled sessions; "
                       "list|show|reply|retry: the owner's messages from the dashboard (D-241)")
    ibs = p.add_subparsers(dest="inbox_cmd")
    q = ibs.add_parser("list", parents=[js], help="the owner's open messages (--all: the recent ones)")
    q.add_argument("--all", action="store_true")
    q = ibs.add_parser("show", parents=[js], help="one message in full")
    q.add_argument("id", type=int)
    q = ibs.add_parser("reply", parents=[js], help="answer a message (shown on the dashboard)")
    q.add_argument("id", type=int)
    q.add_argument("answer")
    q = ibs.add_parser("retry", parents=[js], help="send a failed message again")
    q.add_argument("id", type=int)
    p = sub.add_parser("handoff", help="compaction hand-offs (D-144)")
    hs = p.add_subparsers(dest="handoff_cmd", required=True)
    q = hs.add_parser("write", help="save a hand-off for your compacted self (prints HANDOFF SAVED)")
    q.add_argument("--session", required=True, help="the writing session (full id or unique prefix)")
    q.add_argument("--json", dest="body", required=True, metavar="BODY",
                   help="JSON {summary, done, in_progress, next_steps, open_question_ids, notes}; - = stdin")
    q.add_argument("--project", help="name or directory (default: the project the session manages)")
    q = hs.add_parser("show", parents=[js], help="the latest hand-off")
    q.add_argument("--session", help="only this session's (id or unique prefix)")
    q.add_argument("--all", action="store_true", help="the recent ones, newest first")
    p = sub.add_parser("automation", help="the automation pause (D-234/D-235): status | resume | pause")
    ats = p.add_subparsers(dest="automation_cmd")
    ats.add_parser("status", parents=[js], help="paused or not, why, since when; every paused project (the default)")
    q = ats.add_parser("resume", parents=[js], help="clear the install-wide pause: the runners start and continue "
                       "again (a paused project stays paused: --project / --all)")
    q.add_argument("--project", help="clear only this project's pause (name, directory or id)")
    q.add_argument("--all", action="store_true", help="clear the install-wide pause and every project's")
    q = ats.add_parser("pause", parents=[js], help="pause every runner (or one project's) by hand")
    q.add_argument("reason", nargs="?", help="why (shown in the quickview)")
    q.add_argument("--project", help="pause only this project's runs (name, directory or id)")
    p.set_defaults(json=False)
    p = sub.add_parser("doc", help="project docs in the DB: progress, goals, exceptions, alerts, reviews")
    ds = p.add_subparsers(dest="doc_cmd", required=True)
    q = ds.add_parser("list", parents=[js], help="the project's docs")
    q.add_argument("--project", help="name or directory (default: this directory's project)")
    q = ds.add_parser("show", parents=[js], help="a doc's current text")
    q.add_argument("name")
    q.add_argument("--project", help="name or directory (default: this directory's project)")
    q.add_argument("--entries", type=int, default=0, metavar="N", help="its newest N entries instead")
    q = ds.add_parser("append", parents=[js], help="add an entry to a log doc (progress|exceptions|alerts)")
    q.add_argument("name")
    q.add_argument("text", help="the entry (- = stdin)")
    q.add_argument("--project", help="name or directory (default: this directory's project)")
    q = ds.add_parser("set", parents=[js], help="replace a doc's whole text (goals, review:<name>)")
    q.add_argument("name")
    q.add_argument("text", help="the new text (- = stdin)")
    q.add_argument("--project", help="name or directory (default: this directory's project)")
    q.add_argument("--version", type=int, help="the doc version you read (a mismatch is refused)")
    q = ds.add_parser("import", parents=[js], help="sync the doc files into the DB (idempotent)")
    q.add_argument("--repo", help="the project's directory (default: this repo)")
    q.add_argument("--project", help="project name (default: the project of --repo)")
    q.add_argument("--force", action="store_true", help="on a conflict the file wins")
    q = ds.add_parser("export", parents=[js], help="regenerate PROGRESS.md, GOALS.md, EXCEPTIONS.md (D-181)")
    q.add_argument("--repo", help="the project's directory (default: this repo)")
    q.add_argument("--project", help="project name (default: the project of --repo)")
    q.add_argument("--check", action="store_true", help="report only, write nothing")
    p = sub.add_parser("decision", help="the owner's decisions in the DB (D-242): tldr | find | show | keywords | import")
    dcs = p.add_subparsers(dest="decision_cmd", required=True)
    q = dcs.add_parser("tldr", parents=[js], help="one line per current decision: id, title, [state]")
    q.add_argument("--all", action="store_true", help="also the done and superseded ones")
    q = dcs.add_parser("find", parents=[js], help="the full entries matching keywords (keyword matches "
                                                  "first, then full-text hits)")
    q.add_argument("keywords", nargs="+", help="keywords or phrases (quote a phrase); any may match")
    q.add_argument("--all", action="store_true", help="also the done and superseded ones")
    q.add_argument("--limit", type=int, default=10, help="entries shown (default 10; 0 = all)")
    q = dcs.add_parser("show", parents=[js], help="one decision's full entry")
    q.add_argument("id", help="D-NNN or M-NNN")
    q = dcs.add_parser("keywords", parents=[js], help="add or remove keywords of a decision (audited)")
    q.add_argument("id", help="D-NNN or M-NNN")
    q.add_argument("op", choices=("add", "remove"))
    q.add_argument("keywords", nargs="+", help="one or more keywords (quote one with spaces)")
    q = dcs.add_parser("import", parents=[js], help="import DECISIONS.md now (also every 10 min and at "
                                                    "each compaction; the file is not changed)")
    q.add_argument("--file", help="the source (default: DECISIONS.md next to tasks.py)")
    q.add_argument("--force", action="store_true", help="allow removing more than half of the decisions")
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
