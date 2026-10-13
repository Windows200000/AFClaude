#!/usr/bin/env python3
"""
project_docs.py: a project's docs in the DB (dashboard phase 2d part 2, D-161, D-152, D-181;
docs/dashboard_design.md §7.2, §7.7). The tables are in store.py (DOCS_SCHEMA: docs, the
append-only doc_entries), the write path in actions.py (doc.append, doc.set, doc.sync); this
module maps the files onto them, imports them and writes the public build log back out:

  file (in the project's directory)    doc (per project)      kind
  PROGRESS.md                          progress               log   (entries appended)
  GOALS.md                             goals                  body  (versioned: newest = current)
  EXCEPTIONS.md                        exceptions             log
  ALERTS.md                            alerts                 log
  data/reviews/<name>.md               review:<name>          body
  (BACKLOG.md stays a file until gate 8d, D-065/D-173; OPEN_QUESTIONS.md and DECISIONS.md move in
  phase 4b, D-151/D-158.)

The DB is the source (the §7.7 switch, built): the writers go through actions.py, audited:
  - the task-manager and the review sessions: `python3 tasks.py doc append progress "…"`,
    `tasks.py doc set goals -`, `tasks.py doc set review:<date> -` (MCP afclaude_doc);
  - the runners' notes: notify.py's alerts (alerts + a progress line) and keepalive.py's progress
    notes call append_note() below (doc.append, actor runner:*); they no longer write the files.
Hand edits of PROGRESS.md / GOALS.md / EXCEPTIONS.md stay possible (the owner and the task-manager
edit them directly): the importer takes them in, on a schedule (docker/crontab, every 10 min,
`import --cron`) and as the first step of every export:
    python3 project_docs.py import [--cron]     (= python3 store.py import-docs; idempotent, files untouched)
It reads every file and runs doc.sync for it (actor runner:import): an unchanged file is a no-op
(or "DB ahead" when the DB moved on), an appended one adds its new entries, an in-place edit is
one 'replace' entry keeping the unchanged entries; when the file and the DB both changed since
the last sync (a hand edit while a runner appended a note), the two are merged against the
file's text at the last sync (actions.merge3; "merged", the DB is then ahead of the file), and
only edits of the same lines are a conflict (nothing imported; `--cron` alerts once per episode;
`--force`: the file wins, the dropped entries stay in the history). Afterwards the DB's text of
each doc that is in sync is checked against the file, byte for byte.

The export (D-181, §7.7) is the ONLY file output: the AFClaude repo's public build log, PROGRESS.md,
GOALS.md and EXCEPTIONS.md, regenerated from the DB into the repo (byte-identical to the format the
files have, so the git diff is just what changed). The task-manager runs it before each commit
that includes them:
    python3 project_docs.py export [--check]        (or: python3 tasks.py doc export)
It first imports the files (a hand edit since the last sync is merged in, never overwritten; a
conflict stops that file), then writes each doc whose DB text differs from its file, after the
public-repo check (tools/check_public.py's line checks) found nothing in it; --check only reports.
ALERTS.md and the reviews are never written (no local exports, D-181): the alerts are read with
`tasks.py doc show alerts` / MCP afclaude_doc / the quickview; a legacy ALERTS.md or
data/reviews/*.md is still imported (unchanged files are no-ops). data/ALERTS.fallback.md stays a
file: the DB-broken path (§7.8, D-171).

    python3 project_docs.py status          docs, entries, sync marks
(tasks.py doc list|show|append|set|import|export is the same for the task-manager, MCP afclaude_doc.)
"""
from __future__ import annotations

import argparse
import glob
import importlib
import json
import os
import sqlite3
import sys
import tempfile
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402  (reads)
import actions  # noqa: E402  (every write)

FILES = (("progress", "PROGRESS.md"), ("goals", "GOALS.md"), ("exceptions", "EXCEPTIONS.md"),
         ("alerts", "ALERTS.md"))
BUILD_LOG = ("progress", "goals", "exceptions")      # the public build log: the only docs written out (D-181)
IMPORT_ACTOR = "runner:import"
CONFLICT_MARK = "docs_conflict_alerted:"      # meta key prefix (+ project/name): the --cron alert's episode
NOTE_PAYLOAD_MAX = 64 * 1024                 # characters of a note kept in the fallback file


def reviews_dir_default(db_path: Optional[str] = None) -> str:
    """data/reviews next to the database."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path or store.DB_PATH)), "reviews")


def read_text(path: str) -> Optional[str]:
    """The file's exact text (UTF-8, no newline translation); None if it doesn't exist."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return None
    return data.decode("utf-8")


def doc_files(repo: str, reviews_dir: Optional[str]) -> list[tuple[str, str]]:
    """(doc name, file path) for every file the importer reads (missing ones included)."""
    out = [(name, os.path.join(repo, fname)) for name, fname in FILES]
    if reviews_dir:
        for path in sorted(glob.glob(os.path.join(reviews_dir, "*.md"))):
            out.append(("review:" + os.path.basename(path)[:-3], path))
    return out


def resolve_project(conn: sqlite3.Connection, project: Optional[str], repo: str) -> store.Row:
    """The named project, else the project of `repo` (its path or the closest parent's)."""
    p = store.get_project(conn, project if project else os.path.abspath(repo))
    if p is None:
        raise store.NotFound(f"no project {project!r}" if project else
                             f"no project has the directory {os.path.abspath(repo)} (or a parent of it); "
                             "pass --project")
    return p


def _sync(conn: sqlite3.Connection, project: str, name: str, text: str, actor: str, via: str,
          force: bool) -> dict[str, Any]:
    try:
        r: dict[str, Any] = actions.perform(conn, "doc.sync", {"project": project, "name": name, "text": text,
                                                               "force": force}, actor=actor, via=via)
        return r
    except actions.Conflict as e:
        return {"project": project, "name": name, "outcome": "conflict", "error": str(e)}


def _current(conn: sqlite3.Connection, project_id: int, name: str) -> Optional[str]:
    d = store.get_doc(conn, project_id, name)
    return store.doc_render(conn, d["id"]) if d is not None else None


def import_all(db_path: Optional[str] = None, repo: Optional[str] = None, reviews_dir: Optional[str] = None,
               project: Optional[str] = None, actor: str = IMPORT_ACTOR, via: str = "runner",
               force: bool = False) -> dict[str, Any]:
    """Import (sync) every doc file of the project; idempotent, never changes a file. After each
    import the DB's text is compared with the file (verified). -> a report per doc + totals."""
    repo = repo or HERE
    rdir = reviews_dir if reviews_dir is not None else reviews_dir_default(db_path)
    conn = store.connect(db_path)
    try:
        p = resolve_project(conn, project, repo)
        docs: dict[str, Any] = {}
        totals = {"files": 0, "imported": 0, "merged": 0, "in_sync": 0, "db_ahead": 0, "conflict": 0,
                  "missing": 0, "entries_added": 0, "unverified": 0}
        for name, path in doc_files(repo, rdir):
            try:
                store.doc_kind(name)
            except ValueError as e:
                docs[name] = {"file": path, "outcome": "skipped", "error": str(e)}
                continue
            text = read_text(path)
            if text is None:
                docs[name] = {"file": path, "outcome": "missing"}
                totals["missing"] += 1
                continue
            totals["files"] += 1
            r = _sync(conn, p["name"], name, text, actor, via, force)
            outcome = str(r["outcome"])
            totals[outcome] += 1
            totals["entries_added"] += int(r.get("appended") or 0)
            rep: dict[str, Any] = {"file": path, "outcome": outcome, "chars": len(text)}
            for k in ("appended", "kept", "replaced", "error"):
                if r.get(k) not in (None, 0, False):
                    rep[k] = r[k]
            if outcome in ("imported", "in_sync"):
                rep["verified"] = _current(conn, p["id"], name) == text
                if not rep["verified"]:
                    totals["unverified"] += 1
            docs[name] = rep
        return {"project": p["name"], "repo": os.path.abspath(repo), "docs": docs, "totals": totals,
                "counts": {k.split("/", 1)[1]: v for k, v in store.doc_counts(conn).items()
                           if k.split("/", 1)[0] == p["name"]}}
    finally:
        conn.close()


def append_note(name: str, text: str, actor: str, repo: Optional[str] = None,
                db_path: Optional[str] = None) -> bool:
    """A runner's note (notify.py's alerts and their progress line, keepalive.py's progress notes):
    one doc.append entry (audited, via runner) in the log doc `name` of the project whose
    directory is `repo` (default: this repo), exactly as given. No file is written (§7.7: the
    export writes the build log; ALERTS.md is never written). Never raises: a database failure
    takes the §7.8 path (actions.perform / store.report_db_error: recorded in
    data/ALERTS.fallback.md for a replay, escalated when it fails again), a project the DB
    doesn't know is recorded there too. -> stored."""
    where = os.path.abspath(repo or HERE)
    try:
        conn = store.connect(db_path)
        try:
            p = resolve_project(conn, None, where)
            actions.perform(conn, "doc.append", {"project": p["name"], "name": name, "text": text},
                            actor=actor, via="runner")
        finally:
            conn.close()
        return True
    except (store.NotFound, ValueError) as e:
        store.fallback_note(f"note not stored in the DB ({actor}, {name}): {e}",
                            {"event": "note", "actor": actor, "doc": name, "repo": where, "error": str(e)[:500],
                             "text": text[:NOTE_PAYLOAD_MAX]}, db_path)
    except Exception as e:   # noqa: BLE001 - a note never takes a runner down
        err = store.as_db_error(e)
        if err.escalation is None:          # not reported yet (perform reports its own failures)
            store.report_db_error(err, actor=actor, action="doc.append",
                                  params={"repo": where, "name": name, "text": text},
                                  payload_max=NOTE_PAYLOAD_MAX, db_path=db_path, runner=True)
    try:
        sys.stderr.write(f"project_docs: the {name} note was not stored in the DB ({actor}): {text[:300]}\n")
    except Exception:   # noqa: BLE001
        pass
    return False


def doc_text(name: str, repo: Optional[str] = None, db_path: Optional[str] = None) -> Optional[str]:
    """The current text of the doc `name` of the project of `repo` (default: this repo) in the DB;
    None if there is no such project or doc. Database errors raise (the caller decides)."""
    conn = store.connect(db_path)
    try:
        p = store.get_project(conn, os.path.abspath(repo or HERE))
        return _current(conn, p["id"], name) if p is not None else None
    finally:
        conn.close()


def cron_import(db_path: Optional[str] = None, repo: Optional[str] = None,
                reviews_dir: Optional[str] = None, project: Optional[str] = None) -> tuple[int, str]:
    """The scheduled import (docker/crontab): import_all, then a one-line summary (only when
    something happened; an all-quiet run prints nothing, so the log stays small). A conflict
    alerts the owner once per episode per doc (notify.py; the episode ends when the doc imports
    cleanly again). -> (exit code, the line or "")."""
    r = import_all(db_path, repo, reviews_dir, project)
    t = r["totals"]
    conflicts = {n: d.get("error", "") for n, d in r["docs"].items() if d["outcome"] == "conflict"}
    conn = store.connect(db_path)
    try:
        for name, d in r["docs"].items():
            key = f"{CONFLICT_MARK}{r['project']}/{name}"
            alerted = store.get_meta(conn, key)
            if name in conflicts and not alerted:
                _alert(f"docs import: conflict in {r['project']}/{name}",
                       f"{d['file']}: {conflicts[name]}\nThe hand edits of this file are not in the DB until "
                       "it is merged by hand (tasks.py doc show / export --check) or imported with --force.")
                with store.transaction(conn):
                    store.set_meta(conn, key, store.now_iso())
            elif name not in conflicts and alerted:
                with store.transaction(conn):
                    store.set_meta(conn, key, "")
    finally:
        conn.close()
    busy = {k: v for k, v in t.items() if v and k not in ("files", "in_sync", "db_ahead", "missing")}
    line = ""
    if busy:
        parts = [f"{n}: {d['outcome']}" + (f" +{d['appended']}" if d.get("appended") else "")
                 for n, d in r["docs"].items() if d["outcome"] in ("imported", "merged", "conflict")
                 or d.get("verified") is False]
        line = f"{store.now_iso()} docs import {r['project']}: " + ", ".join(parts)
    return (1 if t["conflict"] or t["unverified"] else 0), line


def _alert(subject: str, body: str) -> None:
    """notify.py (a push, and the alert in the DB), never raising."""
    try:
        notify = importlib.import_module("notify")
        getattr(notify, "notify")(subject, body)
    except Exception as e:   # noqa: BLE001 - an alert must never take the import down
        sys.stderr.write(f"project_docs: alert failed: {type(e).__name__}: {e}\n")


def public_findings(label: str, text: str) -> list[str]:
    """tools/check_public.py's path and line checks on an exported file (what the pre-commit hook
    would refuse). The module is loaded at run time (it reads the local identity terms and the
    denylist then)."""
    tools = os.path.join(HERE, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    cp = importlib.import_module("check_public")
    c = cp.Checker()
    c.check_path(label)
    c.check_blob(label, text.encode("utf-8"))
    return [str(f) for f in c.findings]


def _write_atomic(path: str, text: str) -> None:
    """Replace the file in one step (a temp file in the same directory, renamed over it), keeping its mode."""
    d = os.path.dirname(os.path.abspath(path))
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export(db_path: Optional[str] = None, repo: Optional[str] = None, project: Optional[str] = None,
           check: bool = False, actor: str = IMPORT_ACTOR, via: str = "runner") -> dict[str, Any]:
    """Regenerate the public build log (PROGRESS.md, GOALS.md, EXCEPTIONS.md) from the DB into `repo`.
    Per file: import it first (a hand edit since the last sync wins over nothing; a conflict stops
    this file), render the DB's text, run the public-repo check on it, and write it if it differs
    (then mark it synced). check=True: report only, write nothing. -> report; ok=False if any file
    had a conflict or a public-check finding, or has no doc."""
    repo = repo or HERE
    conn = store.connect(db_path)
    try:
        p = resolve_project(conn, project, repo)
        files: dict[str, Any] = {}
        ok = True
        for name, fname in FILES:
            if name not in BUILD_LOG:
                continue
            path = os.path.join(repo, fname)
            on_disk = read_text(path)
            rep: dict[str, Any] = {"file": path}
            if on_disk is not None and not check:
                r = _sync(conn, p["name"], name, on_disk, actor, via, False)
                rep["sync"] = r["outcome"]
                if r["outcome"] == "conflict":
                    rep.update(result="conflict", error=r["error"])
                    ok = False
                    files[fname] = rep
                    continue
            text = _current(conn, p["id"], name)
            if text is None:
                rep["result"] = "no doc (import the file first)" if on_disk is not None else "no doc"
                ok = ok and on_disk is None
                files[fname] = rep
                continue
            findings = public_findings(fname, text)
            if findings:
                rep.update(result="refused: public-repo check", findings=findings[:20])
                ok = False
            elif text == on_disk:
                rep["result"] = "unchanged"
            elif check:
                d = store.get_doc(conn, p["id"], name)
                edited = (on_disk is not None and d is not None
                          and d["file_sha256"] != store.sha256_text(on_disk))
                rep["result"] = ("the file changed since the last sync: the export imports it first" if edited
                                 else "would write")
            else:
                _write_atomic(path, text)
                actions.perform(conn, "doc.sync", {"project": p["name"], "name": name, "text": text},
                                actor=actor, via=via)          # in sync now: moves the mark
                rep["result"] = "written"
            rep["chars"] = len(text)
            files[fname] = rep
        return {"project": p["name"], "repo": os.path.abspath(repo), "check": check, "ok": ok, "files": files}
    finally:
        conn.close()


def status(db_path: Optional[str] = None) -> list[dict[str, Any]]:
    conn = store.connect(db_path)
    try:
        return [{k: d[k] for k in ("project", "name", "kind", "entries", "version", "updated_at", "updated_by",
                                   "file_entry")} for d in store.list_docs(conn)]
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AFClaude project docs in the DB (phase 2d part 2).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("import", help="import the doc files (idempotent; files untouched)")
    e = sub.add_parser("export", help="regenerate PROGRESS.md, GOALS.md, EXCEPTIONS.md from the DB (D-181)")
    for q in (i, e):
        q.add_argument("--db", default=None)
        q.add_argument("--repo", default=None, help="the project's directory (default: this repo)")
        q.add_argument("--project", default=None, help="project name (default: the project of --repo)")
    i.add_argument("--reviews-dir", default=None, help="review reports (default: <the database's dir>/reviews)")
    i.add_argument("--force", action="store_true", help="on a conflict the file wins (DB entries stay as history)")
    i.add_argument("--cron", action="store_true",
                   help="the scheduled run: a one-line summary only when something changed, alert on a conflict")
    e.add_argument("--check", action="store_true", help="report what would be written, write nothing")
    s = sub.add_parser("status", help="docs with their entry counts")
    s.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "import" and args.cron:
            code, line = cron_import(args.db, args.repo, args.reviews_dir, args.project)
            if line:
                print(line)
            return code
        if args.cmd == "import":
            r = import_all(args.db, args.repo, args.reviews_dir, args.project, force=args.force)
            print(json.dumps(r, indent=1, ensure_ascii=False))
            t = r["totals"]
            return 1 if t["conflict"] or t["unverified"] else 0
        if args.cmd == "export":
            x = export(args.db, args.repo, args.project, check=args.check)
            print(json.dumps(x, indent=1, ensure_ascii=False))
            return 0 if x["ok"] else 1
        print(json.dumps(status(args.db), indent=1, ensure_ascii=False))
        return 0
    except (store.DBError, sqlite3.Error) as err:
        print(f"error: database: {err}", file=sys.stderr)
        return 1
    except (ValueError, LookupError, UnicodeDecodeError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
