#!/usr/bin/env python3
"""
telemetry.py: the usage telemetry in the DB (dashboard phase 2c, D-161, docs/dashboard_design.md
§7.2/§7.3). The tables are in store.py (TELEMETRY_SCHEMA), the write path in actions.py (the
registered appenders: append / append_many for the append-only kinds, put_record(s) for the
derived records); this module maps the data/ files onto them:

  file (data/)            kind                   table
  samples.jsonl           usage.sample           usage_samples          (append-only, stale marked)
  weekly_series.jsonl     usage.series           usage_weekly_series    (append-only, stale marked)
  session_windows.jsonl   usage.session_window   usage_session_windows  (append-only)
  forecast_log.jsonl      usage.forecast         forecast_log           (append-only)
  haiku.jsonl             usage.haiku            haiku_judgements       (append-only)
  usage_reports.jsonl     usage.report           usage_reports          (append-only)
  run_usage.jsonl         usage.run_reading      usage_run_readings     (append-only, stale marked)
  stage_eta_log.jsonl     stage_eta.prediction   stage_eta_log          (append-only)
  weekly_cycles.json      usage.weekly_cycle     weekly_cycles          (record per cycle)
  afclaude_runs.jsonl     usage.run              usage_runs             (record per run)
  user_model.json         usage.user_model       user_model             (one record)

The migration keeps the live system running (dual-write):
  - Writers: every producer still writes its file and also writes the same line to the DB
    (record() / put(), never raising: a failed DB write is logged, the file has the row). The
    DB copy is only written when the producer writes THE live file (the one next to the DB,
    store.DB_PATH's directory), so a test or a CLI run on another file never mixes into it.
  - The importer (import_all; `python3 store.py import-telemetry`, `python3 telemetry.py
    import`) loads the files into the tables, idempotently (a row already stored is skipped by
    its sha256), checks that every valid line of each file is in the DB and then marks the kind
    imported (meta telemetry_imported:<table>). It never deletes or changes a file. Running it
    again any time also fills rows a failed dual-write missed.
  - Readers: lines() / db_lines() read a live file's rows from the DB once its kind is marked
    imported (before that, and on any DB problem, from the file), in file order, so a reader
    gets the same rows either way. The producers' own read-before-write checks (which windows
    / runs / predictions are already recorded) keep reading their files until they are retired.

Retiring the files (the DB becoming the only copy) is a follow-up: the writers then drop the
file write and the appenders' DB errors take the full §7.8 fallback path.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402

# kind -> (file name in the data dir, format): lines = JSONL append log, records = JSONL of
# records (rewritten in place), cycles = one JSON object of records, doc = one JSON document
FILES: dict[str, tuple[str, str]] = {
    "usage.sample": ("samples.jsonl", "lines"),
    "usage.series": ("weekly_series.jsonl", "lines"),
    "usage.session_window": ("session_windows.jsonl", "lines"),
    "usage.forecast": ("forecast_log.jsonl", "lines"),
    "usage.haiku": ("haiku.jsonl", "lines"),
    "usage.report": ("usage_reports.jsonl", "lines"),
    "usage.run_reading": ("run_usage.jsonl", "lines"),
    "stage_eta.prediction": ("stage_eta_log.jsonl", "lines"),
    "usage.weekly_cycle": ("weekly_cycles.json", "cycles"),
    "usage.run": ("afclaude_runs.jsonl", "records"),
    "usage.user_model": ("user_model.json", "doc"),
}
BY_FILE = {name: kind for kind, (name, _) in FILES.items()}
MARK = "telemetry_imported:"          # meta key prefix: the kind's file was imported (JSON stats)
CHUNK = 500                           # rows per import transaction
DEFAULT_ACTOR = "runner:telemetry"

_LOGGED: set[str] = set()


def _log(msg: str) -> None:
    """Each distinct problem once per process (a 30-s loop must not flood the log)."""
    if msg in _LOGGED:
        return
    _LOGGED.add(msg)
    try:
        sys.stderr.write(f"telemetry: {msg}\n")
    except Exception:   # noqa: BLE001 - logging never takes a producer down
        pass


def _table(kind: str) -> str:
    import actions
    return actions.APPENDERS[kind].table


def _real_dir(path: str) -> str:
    return os.path.realpath(os.path.dirname(os.path.abspath(path)))


def kind_for(path: Optional[str]) -> Optional[str]:
    """The telemetry kind of `path` if it is one of THE live files (next to store.DB_PATH)."""
    if not path:
        return None
    kind = BY_FILE.get(os.path.basename(path))
    if kind is None or _real_dir(path) != _real_dir(store.DB_PATH):
        return None
    return kind


def live_path(kind: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)), FILES[kind][0])


# ---------------------------------------------------------------- reads

def _read_conn() -> Optional[sqlite3.Connection]:
    """A read-only connection to the existing DB (never creates it, no schema init); None
    without a DB file."""
    path = store.DB_PATH
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=rw", uri=True, timeout=10)
    conn.execute("PRAGMA query_only=ON")
    return conn


def imported(conn: sqlite3.Connection, kind: str) -> bool:
    r = conn.execute("SELECT 1 FROM meta WHERE key=?", (MARK + _table(kind),)).fetchone()
    return r is not None


def db_lines(path: Optional[str], max_bytes: Optional[int] = None, since: Optional[datetime | str] = None,
             ) -> Optional[list[str]]:
    """The rows of a live telemetry file from the DB (oldest first, each the line as written),
    or None when the file isn't a live one, its kind isn't imported yet, or the DB can't be read
    (the caller then reads the file). max_bytes: like reading the file's last max_bytes bytes;
    since: rows whose time is at/after it (a datetime or ISO text). Never raises."""
    kind = kind_for(path)
    if kind is None:
        return None
    try:
        conn = _read_conn()
    except sqlite3.Error as e:
        _log(f"database unreadable ({type(e).__name__}: {e}); reading the files")
        return None
    if conn is None:
        return None
    try:
        if not imported(conn, kind):
            return None
        return store.telemetry_payloads(conn, _table(kind), max_bytes=max_bytes,
                                        since=None if since is None else store.telemetry_ts(since))
    except sqlite3.Error as e:
        _log(f"{_table(kind)} unreadable ({type(e).__name__}: {e}); reading {path}")
        return None
    finally:
        conn.close()


def signature(path: Optional[str]) -> Optional[tuple[Any, ...]]:
    """A change marker of a live file's DB rows (for the readers' caches); None as db_lines."""
    kind = kind_for(path)
    if kind is None:
        return None
    try:
        conn = _read_conn()
        if conn is None:
            return None
        try:
            if not imported(conn, kind):
                return None
            return ("db",) + store.telemetry_signature(conn, _table(kind))
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def lines(path: str, since: Optional[datetime | str] = None) -> list[str]:
    """The lines of a JSONL telemetry file: from the DB when it holds them (db_lines), else the
    file's lines (an OSError if it can't be read, as open() would). `since` filters only the DB
    read (a cheap pre-filter: callers still check each row's time)."""
    got = db_lines(path, since=since)
    if got is not None:
        return got
    with open(path, errors="replace") as fh:
        return [ln.rstrip("\n") for ln in fh]


# ---------------------------------------------------------------- writes (the dual-write)

def _connect() -> sqlite3.Connection:
    return store.connect()   # never creates the DB (a missing one is a DBUnavailable)


def record(kind: str, line_or_lines: str | Iterable[str], path: Optional[str] = None,
           actor: str = DEFAULT_ACTOR, via: str = "runner", conn: Optional[sqlite3.Connection] = None) -> bool:
    """Dual-write: the line(s) a producer just appended to its file, appended to the DB too
    (actions.append_many). Only for THE live file (path None = the live one; another path is
    a test or a side copy: skipped). -> True if the DB has them now. Never raises."""
    return _write(kind, line_or_lines, path, actor, via, conn, records=False)


def put(kind: str, line_or_lines: str | Iterable[str], path: Optional[str] = None,
        actor: str = DEFAULT_ACTOR, via: str = "runner", conn: Optional[sqlite3.Connection] = None) -> bool:
    """Dual-write of derived records (actions.put_records): only the changed ones are written."""
    return _write(kind, line_or_lines, path, actor, via, conn, records=True)


def _write(kind: str, line_or_lines: str | Iterable[str], path: Optional[str], actor: str, via: str,
           conn: Optional[sqlite3.Connection], records: bool) -> bool:
    import actions
    if path is not None and kind_for(path) != kind:
        return False
    items = [line_or_lines] if isinstance(line_or_lines, str) else list(line_or_lines)
    if not items:
        return True
    try:
        own = conn is None
        c = _connect() if conn is None else conn
        try:
            if records:
                actions.put_records(c, kind, items, actor=actor, via=via)
            else:
                actions.append_many(c, kind, items, actor=actor, via=via)
        finally:
            if own:
                c.close()
        return True
    except Exception as e:   # noqa: BLE001 - the file has the row; the importer re-syncs
        _log(f"{kind}: the DB write failed ({type(e).__name__}: {e}); the file has the row, "
             "`python3 store.py import-telemetry` brings the DB up to date")
        return False


def sync_file(kind: str, path: Optional[str] = None, actor: str = DEFAULT_ACTOR,
              conn: Optional[sqlite3.Connection] = None) -> bool:
    """Dual-write of a file a producer rewrites in place (the runs, the weekly cycles, the user
    model): its records as the importer reads them, put into the DB (only changed ones are
    written). Only for THE live file. Never raises."""
    path = path or live_path(kind)
    if kind_for(path) != kind or not os.path.isfile(path):
        return False
    try:
        items, _ = _file_items(kind, path)
    except (OSError, ValueError) as e:
        _log(f"{kind}: {path} unreadable for the DB copy ({type(e).__name__}: {e})")
        return False
    return put(kind, items, path, actor=actor, conn=conn) if FILES[kind][1] != "lines" else \
        record(kind, items, path, actor=actor, conn=conn)


# ---------------------------------------------------------------- the importer

def _file_items(kind: str, path: str) -> tuple[list[str], int]:
    """-> (the file's rows as lines, unreadable/blank lines skipped). Records of a cycles file
    (a JSON object of cycles) and a doc file (one JSON document) become one line each."""
    fmt = FILES[kind][1]
    if fmt in ("lines", "records"):
        out, bad = [], 0
        with open(path, errors="replace") as fh:
            for ln in fh:
                ln = ln.rstrip("\n")
                if not ln.strip():
                    continue
                try:
                    ok = isinstance(json.loads(ln), dict)
                except ValueError:
                    ok = False
                if ok:
                    out.append(ln)
                else:
                    bad += 1
        return out, bad
    with open(path) as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        return [], 1
    if fmt == "doc":
        return [json.dumps(doc, ensure_ascii=False)], 0
    return [json.dumps(c, default=str) for _, c in sorted(doc.items()) if isinstance(c, dict)], \
        sum(1 for c in doc.values() if not isinstance(c, dict))


def _missing(conn: sqlite3.Connection, table: str, items: list[str], records: bool) -> int:
    """How many of the file's rows the DB doesn't hold (append-only: by sha256; records: by
    their key with the same payload)."""
    import hashlib
    if not records:
        keys = {hashlib.sha256(x.encode("utf-8")).hexdigest() for x in items}
        have: set[str] = set()
        ks = sorted(keys)
        for i in range(0, len(ks), 500):
            part = ks[i:i + 500]
            q = f"SELECT row_key FROM {table} WHERE account_id=? AND row_key IN ({','.join('?' * len(part))})"
            have.update(r[0] for r in conn.execute(q, [store.DEFAULT_ACCOUNT] + part))
        return len(keys - have)
    stored = {str(r[0]) for r in conn.execute(f"SELECT payload FROM {table} WHERE account_id=?",
                                                 (store.DEFAULT_ACCOUNT,))}
    return sum(1 for x in items if x not in stored)


def import_kind(conn: sqlite3.Connection, kind: str, path: str, actor: str = "runner:import") -> dict[str, Any]:
    """Import one file (idempotent). -> stats; the kind is marked imported only when every
    valid row of the file is in the DB afterwards (missing == 0)."""
    import actions
    a = actions.APPENDERS[kind]
    t0 = time.monotonic()
    res: dict[str, Any] = {"kind": kind, "table": a.table, "file": path}
    if not os.path.isfile(path):
        res.update(status="no file")
        return res
    try:
        items, bad = _file_items(kind, path)
    except (OSError, ValueError) as e:
        res.update(status=f"unreadable: {type(e).__name__}: {e}")
        return res
    totals: dict[str, int] = {}
    for i in range(0, len(items), CHUNK):
        part = items[i:i + CHUNK]
        r = (actions.put_records(conn, kind, part, actor=actor, via="runner") if a.record else
             actions.append_many(conn, kind, part, actor=actor, via="runner"))
        for k, v in r.items():
            totals[k] = totals.get(k, 0) + v
    missing = _missing(conn, a.table, items, a.record)
    db_rows = int(conn.execute(f"SELECT COUNT(*) FROM {a.table} WHERE account_id=?",
                               (store.DEFAULT_ACCOUNT,)).fetchone()[0])
    res.update(file_rows=len(items), skipped_lines=bad, db_rows=db_rows, missing=missing,
               seconds=round(time.monotonic() - t0, 3), **totals)
    if missing == 0:
        mark = {"at": store.now_iso(), "file": os.path.abspath(path), "file_rows": len(items), "db_rows": db_rows}
        with store.transaction(conn):
            store.set_meta(conn, MARK + a.table, json.dumps(mark, sort_keys=True))
        res["status"] = "ok"
    else:
        res["status"] = "incomplete: not marked imported, readers stay on the file"
    return res


def import_all(db_path: Optional[str] = None, data_dir: Optional[str] = None,
               kinds: Optional[Iterable[str]] = None, actor: str = "runner:import") -> dict[str, Any]:
    """Import every telemetry file of `data_dir` (default: the DB's directory) into the DB.
    Never deletes or changes a file. -> {"db", "kinds": [stats...], "counts", "seconds"}."""
    db = db_path or store.DB_PATH
    ddir = data_dir or os.path.dirname(os.path.abspath(db))
    t0 = time.monotonic()
    conn = store.connect(db)
    try:
        out = [import_kind(conn, k, os.path.join(ddir, FILES[k][0]), actor) for k in (kinds or FILES)]
        counts = store.telemetry_counts(conn)
    finally:
        conn.close()
    return {"db": os.path.abspath(db), "data_dir": os.path.abspath(ddir), "kinds": out, "counts": counts,
            "seconds": round(time.monotonic() - t0, 3)}


def status(db_path: Optional[str] = None) -> dict[str, Any]:
    """Per table: rows and the import mark (None = readers still on the file)."""
    conn = store.connect(db_path or store.DB_PATH)
    try:
        out: dict[str, Any] = {}
        for kind in FILES:
            t = _table(kind)
            mark = store.get_meta(conn, MARK + t)
            out[t] = {"kind": kind, "file": FILES[kind][0],
                      "rows": int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]),
                      "imported": json.loads(mark) if mark else None}
        return out
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="AFClaude telemetry in the DB (phase 2c).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("import", help="import the data/ telemetry files into the DB (idempotent; files untouched)")
    i.add_argument("--db", default=None)
    i.add_argument("--data-dir", default=None, help="the files' directory (default: the DB's)")
    i.add_argument("--kind", action="append", choices=sorted(FILES), help="only this kind (repeatable)")
    s = sub.add_parser("status", help="rows per table and the import marks")
    s.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "import":
            print(json.dumps(import_all(args.db, args.data_dir, args.kind), indent=1, ensure_ascii=False))
        else:
            print(json.dumps(status(args.db), indent=1, ensure_ascii=False))
    except store.DBError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
