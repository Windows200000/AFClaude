#!/usr/bin/env python3
"""
runner_state.py: the runners' state, scheduled jobs, own sessions and logs in the DB (dashboard
phase 2d, D-161, docs/dashboard_design.md §7.2). The tables are in store.py (RUNNER_SCHEMA), the
write path in actions.py (state_save, app_log_append / app_log_sync, job_put / job_started,
own_session_add); this module maps the data/ files onto them:

  file (data/)                          component / table
  keepalive/keepalive_state.json        runner_state 'keepalive' (handled, fires, limit_hits per entry)
  keepalive/keepalive_deferred.json     runner_state 'keepalive.deferred' (one row per postponed start)
  keepalive/keepalive_fillup.json       runner_state 'keepalive.fillup'
  keepalive/usage_refresh_state.json    runner_state 'keepalive.usage_refresh' (window_start per entry)
  keepalive/keepalive_handoff.json      runner_state 'keepalive.handoff' (if present)
  dispatcher_state.json                 runner_state 'dispatcher' (sessions, handled, starts, alerted, rc_held per entry)
  sampler_state.json                    runner_state 'sampler' (offsets, sessions per entry)
  usage_review_state.json               runner_state 'usage_review'
  own_sessions.txt                      driven_sessions (kind 'own')
  at_spool/*.json                       scheduled_jobs (a started job is marked, not deleted)
  keepalive/keepalive.log, dispatcher.log, sampler.log, keepalive/watchdog.log, host_exec.log,
  at_shim.log                           app_log (component = the file's stem; append-only)

The migration keeps the live system running (dual-write, as telemetry.py):
  - Writers: every runner still writes its file and then saves the same state to the DB (save(),
    never raising: a failed DB write is logged, the file has it). Only for THE live file (the
    one in the DB's directory). The DB save records the file's mark (inode:mtime_ns:size;
    meta state_file:<component> keeps the last few) in the same transaction.
  - Readers: db_load() returns the state from the DB once the component is imported AND the
    file carries the mark of one of the last DB saves; a file changed outside the DB (a runner
    on older code such as the long-running watcher until its restart, a failed DB write, a hand
    edit) or any DB problem -> None, and the caller reads the file as before. read_json() is the
    same for read-only readers of a state file.
  - Concurrency (actions.state_save, one transaction per save): a state loaded from the DB is a
    StateDoc that remembers the rows it came from; its save writes only what this writer
    changed or removed since (merge), so the watcher and the cron run, saving one after the
    other, no longer lose each other's entries in the DB. A state read from the file (or built
    anew, e.g. the deferrals minus the older ones) saves in 'file' mode: its top-level values
    replace the DB's (the file's semantics), the entries of its split parts (handled, fires,
    sessions, offsets, ...) are upserted but not deleted, so a writer that fell back to the file
    can't drop another writer's entries. The file write stays last-writer-wins for now.
  - The importer (import_all; `python3 store.py import-state`, `python3 runner_state.py import`)
    is idempotent and never changes or deletes a file: a state file whose mark the DB already
    has is in sync and skipped, otherwise the DB copy becomes the file (checked row by row) and
    the component is marked imported (meta state_imported:<component>); own sessions and jobs
    are added if missing (jobs whose spool file is gone are marked started); log lines are added
    by count (a line n times in the file and m < n times in the DB gets n - m copies), so the
    dual-written lines aren't doubled. Running it again any time re-syncs what a failed
    dual-write or a log written only by a shell redirect (sampler.log tracebacks, host_exec.log,
    watchdog.log, at_shim.log) left out.

Retiring the files (the DB as the only copy) and moving the log readers to app_log is a
follow-up.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Optional, TextIO
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402

STATE_FILES: dict[str, str] = {   # component -> path relative to the data dir
    "keepalive": "keepalive/keepalive_state.json",
    "keepalive.deferred": "keepalive/keepalive_deferred.json",
    "keepalive.fillup": "keepalive/keepalive_fillup.json",
    "keepalive.usage_refresh": "keepalive/usage_refresh_state.json",
    "keepalive.handoff": "keepalive/keepalive_handoff.json",
    "dispatcher": "dispatcher_state.json",
    "sampler": "sampler_state.json",
    "usage_review": "usage_review_state.json",
}
LOG_FILES: dict[str, str] = {
    "keepalive": "keepalive/keepalive.log",
    "dispatcher": "dispatcher.log",
    "sampler": "sampler.log",
    "watchdog": "keepalive/watchdog.log",
    "host_exec": "host_exec.log",
    "at_shim": "at_shim.log",
}
OWN_FILE = "own_sessions.txt"
SPOOL_DIR = "at_spool"
MARK = "state_imported:"          # meta: <component> | own_sessions | scheduled_jobs | app_log:<component>
IMPORT_ACTOR = "runner:import"
BERLIN = ZoneInfo("Europe/Berlin")

_LOGGED: set[str] = set()


def _log(msg: str) -> None:
    """Each distinct problem once per process (a 30-s loop must not flood the log)."""
    if msg in _LOGGED:
        return
    _LOGGED.add(msg)
    try:
        sys.stderr.write(f"runner_state: {msg}\n")
    except Exception:   # noqa: BLE001 - logging never takes a runner down
        pass


def data_dir() -> str:
    return os.path.dirname(os.path.abspath(store.DB_PATH))


def live_path(component: str, files: Optional[dict[str, str]] = None) -> str:
    return os.path.join(data_dir(), (files or STATE_FILES)[component])


def _same(a: str, b: str) -> bool:
    return os.path.realpath(os.path.abspath(a)) == os.path.realpath(os.path.abspath(b))


def is_live(component: str, path: Optional[str], files: Optional[dict[str, str]] = None) -> bool:
    """`path` (None = the live one) is THE live file of the component (next to store.DB_PATH)."""
    fs = files or STATE_FILES
    if component not in fs:
        return False
    return path is None or _same(path, live_path(component, fs))


def file_mark(path: str) -> Optional[str]:
    """The file's change mark (inode:mtime_ns:size; the runners replace their files, so each
    write is a new inode), None if it doesn't exist."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_ino}:{st.st_mtime_ns}:{st.st_size}"


class StateDoc(dict[str, Any]):
    """A state loaded from the DB; `base` = the rows it was loaded from (the merge base of its
    save). A plain dict (e.g. a new one built from it) saves as a replacement."""
    base: Optional[dict[str, str]] = None


# ---------------------------------------------------------------- reads

def _read_conn() -> Optional[sqlite3.Connection]:
    """A read-only connection to the existing DB (never creates it, no schema init)."""
    path = store.DB_PATH
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=rw", uri=True, timeout=10)
    conn.execute("PRAGMA query_only=ON")
    return conn


def db_load(component: str, path: Optional[str] = None) -> Optional[StateDoc]:
    """The component's state from the DB, or None when `path` isn't the live file, the
    component isn't imported, the file changed since the last DB save, or the DB can't be read
    (the caller then reads the file). Never raises."""
    import actions
    if not is_live(component, path):
        return None
    fpath = path or live_path(component)
    try:
        conn = _read_conn()
    except sqlite3.Error as e:
        _log(f"database unreadable ({type(e).__name__}: {e}); reading the files")
        return None
    if conn is None:
        return None
    try:
        if store.get_meta(conn, MARK + component) is None:
            return None
        mark = file_mark(fpath)
        if mark is None or mark not in actions.state_marks(conn, component):
            _log(f"{component}: {fpath} changed outside the DB; reading the file until the next save "
                 "(or `python3 store.py import-state`)")
            return None
        rows = store.state_rows(conn, component)
    except sqlite3.Error as e:
        _log(f"{component} unreadable ({type(e).__name__}: {e}); reading {fpath}")
        return None
    finally:
        conn.close()
    try:
        doc = StateDoc(actions.state_unflatten(component, rows))
    except (ValueError, IndexError, TypeError) as e:
        _log(f"{component}: bad rows in the DB ({type(e).__name__}: {e}); reading {fpath}")
        return None
    doc.base = rows
    return doc


def component_of(path: str) -> Optional[str]:
    for comp, rel in STATE_FILES.items():
        if os.path.basename(rel) == os.path.basename(path) and is_live(comp, path):
            return comp
    return None


def read_json(path: str) -> Any:
    """A state file's content for a read-only reader: from the DB when it holds the live file's
    current state (db_load), else json.load of the file (OSError / ValueError as that raises)."""
    comp = component_of(path)
    if comp is not None:
        got = db_load(comp, path)
        if got is not None:
            return dict(got)
    with open(path) as fh:
        return json.load(fh)


# ---------------------------------------------------------------- writes (the dual-write)

def save(component: str, doc: Any, path: Optional[str] = None, actor: Optional[str] = None,
         conn: Optional[sqlite3.Connection] = None) -> bool:
    """Dual-write: call right after the runner wrote `doc` to its state file. Only for THE live
    file. A StateDoc merges (its base), anything else replaces the DB copy. -> True if the DB
    has it now. Never raises."""
    import actions
    if not is_live(component, path):
        return False
    fpath = path or live_path(component)
    base = doc.base if isinstance(doc, StateDoc) else None
    try:
        c = actions.state_component(component)
        own = conn is None
        db = store.connect() if conn is None else conn
        try:
            actions.state_save(db, component, doc, base=base, mode="file" if base is None else "merge",
                               actor=actor or c.actor, via="runner", file_mark=file_mark(fpath))
        finally:
            if own:
                db.close()
        if isinstance(doc, StateDoc):
            doc.base = actions.state_flatten(component, doc)
        return True
    except Exception as e:   # noqa: BLE001 - the file has the state; the importer re-syncs
        e.__traceback__ = None   # keep no caller frames alive (a reused exception would hold e.g. a lock open)
        _log(f"{component}: the DB save failed ({type(e).__name__}: {e}); the file has the state, "
             "`python3 store.py import-state` brings the DB up to date")
        return False


_BRACKET_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (CEST|CET|UTC)\]")
_ISO_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?)")
_ERROR = re.compile(r"\b(ERROR|Error|Traceback|FAILED|failed|exception)\b")
_WARN = re.compile(r"\b(ALERT|WARN|WARNING|warning|DENY|unreadable|skipped)\b")


def line_ts(line: str) -> Optional[str]:
    """A log line's own time as normalised UTC ISO: `[YYYY-MM-DD HH:MM:SS CEST|CET|UTC] ...`
    (Berlin time) or a leading ISO time (without an offset: UTC, the host's time zone); None
    for a line without one (a traceback, a continuation)."""
    m = _BRACKET_TS.match(line)
    if m:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        if m.group(2) == "UTC":
            return store.telemetry_ts(dt.replace(tzinfo=timezone.utc))
        return store.telemetry_ts(dt.replace(tzinfo=BERLIN, fold=1 if m.group(2) == "CET" else 0))
    m = _ISO_TS.match(line)
    if m:
        ts = store.telemetry_ts(m.group(1))
        return ts if ts != m.group(1) or ts.endswith("Z") else None
    return None


def line_level(line: str) -> str:
    if _ERROR.search(line):
        return "error"
    if _WARN.search(line):
        return "warn"
    return "info"


def _entries(lines: Iterable[str], first_ts: str) -> list[tuple[object, object, object]]:
    out: list[tuple[object, object, object]] = []
    prev = first_ts
    for ln in lines:
        ts = line_ts(ln) or prev
        prev = ts
        out.append((ts, line_level(ln), ln))
    return out


def log_lines(component: str, text: str, path: Optional[str] = None, actor: Optional[str] = None) -> bool:
    """Dual-write of log text a runner just appended to its log file (one or more lines). Only
    for THE live log. -> True if the DB has them. Never raises."""
    import actions
    if not is_live(component, path, LOG_FILES):
        return False
    lines = [ln for ln in text.rstrip("\n").split("\n")]
    if not lines or lines == [""]:
        return True
    try:
        conn = store.connect()
        try:
            actions.app_log_append(conn, component, _entries(lines, store.now_iso()),
                                   actor=actor or f"runner:{component}", via="runner")
        finally:
            conn.close()
        return True
    except Exception as e:   # noqa: BLE001 - the file has the line; the importer re-syncs
        e.__traceback__ = None
        _log(f"{component} log: the DB write failed ({type(e).__name__}: {e}); the file has the lines")
        return False


def tee(component: str, text: str, stream: Optional[TextIO] = None) -> bool:
    """Dual-write of what a runner printed when its stdout IS the live log file (the shell
    redirect of the watcher or a cron line). Never raises."""
    try:
        s = stream if stream is not None else sys.stdout
        a = os.fstat(s.fileno())
        b = os.stat(live_path(component, LOG_FILES))
    except (AttributeError, OSError, ValueError):   # no fd (StringIO), closed, no log yet
        return False
    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
        return False
    return log_lines(component, text)


def job_spooled(job: dict[str, Any], spool: str) -> bool:
    """Dual-write of the at-shim: a job it just spooled (only for the live spool). Never raises."""
    return _job(job, spool, started=False)


def job_claimed(job: dict[str, Any], spool: str) -> bool:
    """Dual-write of the at-shim: a job it just claimed and starts. Never raises."""
    return _job(job, spool, started=True)


def _job(job: dict[str, Any], spool: str, started: bool) -> bool:
    import actions
    if not _same(spool, os.path.join(data_dir(), SPOOL_DIR)):
        return False
    try:
        conn = store.connect()
        try:
            actions.job_put(conn, job, actor="runner:at", via="runner")
            if started:
                actions.job_started(conn, str(job.get("id")), actor="runner:at", via="runner")
        finally:
            conn.close()
        return True
    except Exception as e:   # noqa: BLE001 - the spool file is the job; the importer re-syncs
        e.__traceback__ = None
        _log(f"job {job.get('id')}: the DB write failed ({type(e).__name__}: {e})")
        return False


# ---------------------------------------------------------------- the importer

def _mark(conn: sqlite3.Connection, what: str, stats: dict[str, Any]) -> None:
    with store.transaction(conn):
        store.set_meta(conn, MARK + what, json.dumps(dict(stats, at=store.now_iso()), sort_keys=True))


def import_state(conn: sqlite3.Connection, component: str, path: str, actor: str = IMPORT_ACTOR) -> dict[str, Any]:
    """One state file (idempotent): in sync (the file carries the mark of a DB save) ->
    skipped; else (first import, or the file was changed outside the DB) the DB copy becomes the
    file, checked row by row, then the component is marked imported."""
    import actions
    res: dict[str, Any] = {"component": component, "file": path}
    if not os.path.isfile(path):
        res["status"] = "no file"
        return res
    mark = file_mark(path)
    try:
        with open(path) as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        res["status"] = f"unreadable: {type(e).__name__}: {e}"
        return res
    rows = actions.state_flatten(component, doc)
    res["file_rows"] = len(rows)
    if store.get_meta(conn, MARK + component) is not None and mark in actions.state_marks(conn, component):
        res.update(status="in sync", db_rows=len(store.state_rows(conn, component)))
        return res
    res.update(actions.state_save(conn, component, doc, mode="replace", actor=actor, via="runner", file_mark=mark))
    stored = store.state_rows(conn, component)
    missing = sum(1 for p, v in rows.items() if stored.get(p) != v) + sum(1 for p in stored if p not in rows)
    res.update(db_rows=len(stored), missing=missing)
    if missing == 0:
        _mark(conn, component, {"file": os.path.abspath(path), "rows": len(rows)})
        res["status"] = "ok"
    else:
        res["status"] = "incomplete: not marked imported, readers stay on the file"
    return res


def import_own_sessions(conn: sqlite3.Connection, path: str, actor: str = IMPORT_ACTOR) -> dict[str, Any]:
    import actions
    res: dict[str, Any] = {"what": "own_sessions", "file": path}
    if not os.path.isfile(path):
        res["status"] = "no file"
        return res
    with open(path) as fh:
        ids = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    at = store.telemetry_ts(datetime.fromtimestamp(os.path.getmtime(path), timezone.utc))
    added, bad = 0, 0
    for sid in ids:
        try:
            added += actions.own_session_add(conn, sid, actor=actor, via="runner", started_at=at)
        except ValueError:
            bad += 1
    missing = sum(1 for sid in ids if store.get_driven_session(conn, sid) is None) - bad
    res.update(file_rows=len(ids), added=added, invalid=bad, missing=missing)
    if missing == 0:
        _mark(conn, "own_sessions", {"file": os.path.abspath(path), "rows": len(ids)})
        res["status"] = "ok"
    else:
        res["status"] = "incomplete"
    return res


def import_jobs(conn: sqlite3.Connection, spool: str, actor: str = IMPORT_ACTOR) -> dict[str, Any]:
    import actions
    res: dict[str, Any] = {"what": "scheduled_jobs", "dir": spool}
    jobs: list[dict[str, Any]] = []
    bad = 0
    for name in sorted(os.listdir(spool)) if os.path.isdir(spool) else []:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(spool, name)) as fh:
                j = json.load(fh)
            if not isinstance(j, dict):
                raise ValueError("not a JSON object")
            jobs.append(j)
        except (OSError, ValueError):
            bad += 1
    counts = {"inserted": 0, "updated": 0, "same": 0}
    for j in jobs:
        try:
            counts[actions.job_put(conn, j, actor=actor, via="runner")] += 1
        except ValueError:
            bad += 1
    spooled = {str(j.get("id")) for j in jobs}
    gone = 0   # pending in the DB, spool file gone: the at-shim claimed (ran) it
    for row in store.list_jobs(conn, "pending"):
        if row["id"] not in spooled and actions.job_started(conn, row["id"], actor=actor, via="runner"):
            gone += 1
    missing = sum(1 for j in jobs if store.get_job(conn, str(j.get("id"))) is None)
    res.update(files=len(jobs), invalid=bad, marked_started=gone, missing=missing, **counts)
    if missing == 0:
        _mark(conn, "scheduled_jobs", {"dir": os.path.abspath(spool), "rows": len(jobs)})
        res["status"] = "ok"
    else:
        res["status"] = "incomplete"
    return res


def import_log(conn: sqlite3.Connection, component: str, path: str, actor: str = IMPORT_ACTOR) -> dict[str, Any]:
    import actions
    import hashlib
    res: dict[str, Any] = {"component": component, "file": path}
    if not os.path.isfile(path):
        res["status"] = "no file"
        return res
    with open(path, errors="replace") as fh:
        lines = [ln.rstrip("\n") for ln in fh]
    first = store.telemetry_ts(datetime.fromtimestamp(os.path.getmtime(path), timezone.utc))
    entries = _entries(lines, first)
    r = actions.app_log_sync(conn, component, entries, actor=actor, via="runner")
    have = store.app_log_hash_counts(conn, component)
    want: dict[str, int] = {}
    for ln in lines:
        h = hashlib.sha256(ln.encode("utf-8")).hexdigest()
        want[h] = want.get(h, 0) + 1
    missing = sum(max(0, n - have.get(h, 0)) for h, n in want.items())
    res.update(file_lines=len(lines), missing=missing, **r)
    if missing == 0:
        _mark(conn, "app_log:" + component, {"file": os.path.abspath(path), "lines": len(lines)})
        res["status"] = "ok"
    else:
        res["status"] = "incomplete"
    return res


def import_all(db_path: Optional[str] = None, ddir: Optional[str] = None,
               actor: str = IMPORT_ACTOR) -> dict[str, Any]:
    """Import the runner state files, own sessions, spooled jobs and logs of `ddir` (default:
    the DB's directory). Never changes or deletes a file."""
    db = db_path or store.DB_PATH
    d = ddir or os.path.dirname(os.path.abspath(db))
    t0 = time.monotonic()
    conn = store.connect(db)
    try:
        out: dict[str, Any] = {"db": os.path.abspath(db), "data_dir": os.path.abspath(d)}
        out["state"] = [import_state(conn, c, os.path.join(d, rel), actor) for c, rel in STATE_FILES.items()]
        out["own_sessions"] = import_own_sessions(conn, os.path.join(d, OWN_FILE), actor)
        out["scheduled_jobs"] = import_jobs(conn, os.path.join(d, SPOOL_DIR), actor)
        out["logs"] = [import_log(conn, c, os.path.join(d, rel), actor) for c, rel in LOG_FILES.items()]
        out["counts"] = store.runner_counts(conn)
    finally:
        conn.close()
    out["seconds"] = round(time.monotonic() - t0, 3)
    return out


def status(db_path: Optional[str] = None) -> dict[str, Any]:
    """Rows per component and the import marks."""
    conn = store.connect(db_path or store.DB_PATH)
    try:
        marks = {str(r[0])[len(MARK):]: json.loads(r[1])
                 for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (MARK + "%",))}
        return {"counts": store.runner_counts(conn), "imported": marks}
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="AFClaude runner state in the DB (phase 2d).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("import", help="import the data/ state files, jobs, own sessions and logs (idempotent; "
                                      "files untouched)")
    i.add_argument("--db", default=None)
    i.add_argument("--data-dir", default=None, help="the files' directory (default: the DB's)")
    s = sub.add_parser("status", help="rows per component and the import marks")
    s.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "import":
            print(json.dumps(import_all(args.db, args.data_dir), indent=1, ensure_ascii=False))
        else:
            print(json.dumps(status(args.db), indent=1, ensure_ascii=False))
    except store.DBError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
