#!/usr/bin/env python3
"""
telemetry.py: the usage telemetry in the DB (dashboard phase 2c, D-161, docs/dashboard_design.md
§7.2/§7.3). The tables are in store.py (TELEMETRY_SCHEMA), the write path in actions.py (the
registered appenders: append / append_many for the append-only kinds, put_record(s) for the
derived records); this module is the producers' and readers' one door to them:

  kind                   table                  legacy file (data/, frozen)
  usage.sample           usage_samples          samples.jsonl          (append-only, stale marked)
  usage.series           usage_weekly_series    weekly_series.jsonl    (append-only, stale marked)
  usage.session_window   usage_session_windows  session_windows.jsonl  (append-only)
  usage.forecast         forecast_log           forecast_log.jsonl     (append-only)
  usage.haiku            haiku_judgements       haiku.jsonl            (append-only)
  usage.report           usage_reports          usage_reports.jsonl    (append-only)
  usage.run_reading      usage_run_readings     run_usage.jsonl        (append-only, stale marked)
  stage_eta.prediction   stage_eta_log          stage_eta_log.jsonl    (append-only)
  usage.weekly_cycle     weekly_cycles          weekly_cycles.json     (record per cycle)
  usage.run              usage_runs             afclaude_runs.jsonl    (record per run)
  usage.user_model       user_model             user_model.json        (one record)

The DB is the only copy (D-161, "put EVERYTHING in the db"; the files are retired):
  - A producer names its data by the live path (the file name next to store.DB_PATH, i.e. in
    data/) or by None; record() / write_records() / write_cycles() write the DB only, through
    the actions.py appenders. A DB failure takes the full §7.8 path: store.retrying first, then
    the failed rows go to data/ALERTS.fallback.md (store.report_db_error, with the rows, so
    nothing is lost silently) and the producer goes on; the runners' store.db_gate pauses the
    autonomous starts while the DB is broken and alerts once per episode.
  - Readers (lines(), db_lines(), read_records(), read_cycles(), read_user_model()) read the DB.
    A DB that can't be read gives no rows (logged), never the frozen file.
  - Legacy files (read-only): the files of the dual-write era stay in data/ as frozen copies
    (safe to archive). A kind that was never imported (meta telemetry_imported:<table>), e.g.
    on a fresh install that restored old files, is read from its file until its first DB
    write, which imports the file first (import_kind) and marks the kind; a kind without a
    legacy file is marked by its first write. No file is ever written or changed here.
  - Any other path (a test fixture, a tool's --file, a CLI's --out) is an explicit side file,
    read and written as a file (the format of FILES); it never touches the DB.
  - The importer (`python3 store.py import-telemetry`, `python3 telemetry.py import`) skips a
    kind already imported (its file is frozen; re-importing a record kind would revert newer DB
    records); `--catch-up` re-imports the append-only kinds (idempotent, rows deduped by
    sha256) to pick up rows the last dual-write might have missed.
  - CLI for sessions and tools (no file to read any more): `python3 telemetry.py rows <kind>`
    prints the rows as JSONL, `python3 telemetry.py user-model get|set` reads / replaces the
    user model (set: the audited action user_model.set).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterable, Mapping
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
    "usage.review": ("usage_reviews.jsonl", "records"),     # never a file: born in the DB (usage_review.py)
}
DB_ONLY = ("usage.review",)           # kinds that never had a legacy file: the importer skips them
BY_FILE = {name: kind for kind, (name, _) in FILES.items()}
MARK = "telemetry_imported:"          # meta key prefix: the kind is DB-owned (JSON stats)
CHUNK = 500                           # rows per import transaction
DEFAULT_ACTOR = "runner:telemetry"
FALLBACK_PAYLOAD_MAX = 1024 * 1024    # rows kept in ALERTS.fallback.md for a replay (§7.8)

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
    """The telemetry kind of `path` if it is one of THE live paths (next to store.DB_PATH)."""
    if not path:
        return None
    kind = BY_FILE.get(os.path.basename(path))
    if kind is None or _real_dir(path) != _real_dir(store.DB_PATH):
        return None
    return kind


def live_path(kind: str) -> str:
    """The kind's live path: its legacy file's name in the DB's directory."""
    return os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)), FILES[kind][0])


def is_live(kind: str, path: Optional[str]) -> bool:
    """`path` (None = the live one) names the kind's live data (the DB), not a side file."""
    return path is None or kind_for(path) == kind


# ---------------------------------------------------------------- reads

def _read_conn() -> Optional[sqlite3.Connection]:
    """A read-only connection to the existing DB (never creates it, no schema init); None
    without a DB file."""
    path = store.DB_PATH
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=rw", uri=True, timeout=store.BUSY_TIMEOUT)
    conn.execute("PRAGMA query_only=ON")
    return conn


def imported(conn: sqlite3.Connection, kind: str) -> bool:
    r = conn.execute("SELECT 1 FROM meta WHERE key=?", (MARK + _table(kind),)).fetchone()
    return r is not None


def _live_rows(kind: str, max_bytes: Optional[int] = None, since: Optional[datetime | str] = None,
               ) -> Optional[list[str]]:
    """The kind's rows from the DB (oldest first, each the line as stored). None = read the
    legacy file (the kind was never imported and its file exists). A DB that is missing or
    can't be read: [] (logged; the runners' db_gate alerts), never the file."""
    legacy = os.path.isfile(live_path(kind))

    def run() -> Optional[list[str]]:
        conn = _read_conn()
        if conn is None:
            raise store.DBUnavailable(f"no database at {store.DB_PATH}", "missing")
        try:
            if legacy and not imported(conn, kind):
                return None
            return store.telemetry_payloads(conn, _table(kind), max_bytes=max_bytes,
                                            since=None if since is None else store.telemetry_ts(since))
        finally:
            conn.close()
    try:
        return store.retrying(run)
    except (store.DBError, sqlite3.Error, ValueError) as e:
        _log(f"{_table(kind)} unreadable ({type(e).__name__}: {e}); no rows until the database is healthy")
        return []


def db_lines(path: Optional[str], max_bytes: Optional[int] = None, since: Optional[datetime | str] = None,
             ) -> Optional[list[str]]:
    """The rows of a live path from the DB (oldest first, each the line as written), or None
    when `path` is a side file or a never-imported legacy file (the caller reads the file).
    max_bytes: like reading the file's last max_bytes bytes; since: rows whose time is at/after
    it (a datetime or ISO text). Never raises (an unreadable DB gives [])."""
    kind = kind_for(path)
    if kind is None:
        return None
    return _live_rows(kind, max_bytes=max_bytes, since=since)


def signature(path: Optional[str]) -> Optional[tuple[Any, ...]]:
    """A change marker of a live path's DB rows (for the readers' caches); None as db_lines."""
    kind = kind_for(path)
    if kind is None:
        return None
    legacy = os.path.isfile(live_path(kind))
    try:
        conn = _read_conn()
        if conn is None:
            return ("db", "missing", time.monotonic())   # never cached
        try:
            if legacy and not imported(conn, kind):
                return None
            return ("db",) + store.telemetry_signature(conn, _table(kind))
        finally:
            conn.close()
    except sqlite3.Error:
        return ("db", "unreadable", time.monotonic())   # never cached: retried on the next read


def _file_lines(path: str) -> list[str]:
    with open(path, errors="replace") as fh:
        return [ln.rstrip("\n") for ln in fh]


def lines(path: str, since: Optional[datetime | str] = None) -> list[str]:
    """The lines of a JSONL telemetry path: the DB's rows for a live path (db_lines), else the
    file's lines (an OSError if it can't be read, as open() would). `since` filters only the DB
    read (a cheap pre-filter: callers still check each row's time)."""
    got = db_lines(path, since=since)
    if got is not None:
        return got
    return _file_lines(path)


def rows(kind: str, path: Optional[str] = None, since: Optional[datetime | str] = None) -> list[str]:
    """The kind's rows as lines, for any format: the DB for the live path, else the side file
    (records of a cycles file / a doc file one line each). Never raises (unreadable: [])."""
    if is_live(kind, path):
        got = _live_rows(kind, since=since)
        if got is not None:
            return got
        path = live_path(kind)
    assert path is not None
    try:
        return _file_items(kind, path)[0]
    except (OSError, ValueError):
        return []


def _parsed(items: Iterable[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for x in items:
        try:
            r = json.loads(x)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def read_records(kind: str, path: Optional[str] = None) -> list[dict[str, Any]]:
    """The records of a record kind (or the rows of any kind) as dicts, oldest first."""
    return _parsed(rows(kind, path))


def read_cycles(path: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """The weekly cycles {reset_at: cycle} (usage_sampler.track_cycle)."""
    return {str(c["reset_at"]): c for c in read_records("usage.weekly_cycle", path) if c.get("reset_at")}


def read_user_model(path: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The user model (the usage review's user-specific parameters), None if there is none."""
    got = read_records("usage.user_model", path)
    return got[-1] if got else None


# ---------------------------------------------------------------- writes (the DB only)

def _connect() -> sqlite3.Connection:
    return store.connect()   # never creates the DB (a missing one is a DBUnavailable)


def _ensure_owned(conn: sqlite3.Connection, kind: str) -> None:
    """Before the first DB write of a kind that was never imported: import its legacy file
    (if any) so the DB holds everything the readers saw, then mark it (the readers use the DB
    from then on). A legacy file that can't be imported completely raises (the write then
    takes the fallback path; the readers stay on the file)."""
    if store.get_meta(conn, MARK + _table(kind)) is not None:
        return
    legacy = live_path(kind)
    if os.path.isfile(legacy):
        r = import_kind(conn, kind, legacy)
        if r.get("status") != "ok":
            raise RuntimeError(f"{kind}: the legacy file {legacy} could not be imported first ({r.get('status')})")
        _log(f"{kind}: imported the legacy file {legacy} before the first DB write")
        return
    mark = {"at": store.now_iso(), "file": None, "note": "no legacy file: the DB is the only copy from its first write"}
    with store.transaction(conn):
        store.set_meta(conn, MARK + _table(kind), json.dumps(mark, sort_keys=True))


def _db_write(kind: str, items: list[str], actor: str, via: str, conn: Optional[sqlite3.Connection],
              records: bool) -> bool:
    import actions
    if not items:
        return True
    try:
        own = conn is None
        c = _connect() if conn is None else conn
        try:
            store.retrying(lambda: _ensure_owned(c, kind), c)
            if records:
                actions.put_records(c, kind, items, actor=actor, via=via)
            else:
                actions.append_many(c, kind, items, actor=actor, via=via)
        finally:
            if own:
                c.close()
        return True
    except Exception as e:   # noqa: BLE001 - never sinks a producer; the rows go to the fallback file
        e.__traceback__ = None
        err = store.as_db_error(e)
        store.report_db_error(err, actor=actor, action=f"telemetry.{'put' if records else 'append'}",
                              params={"kind": kind, "lines": items}, payload_max=FALLBACK_PAYLOAD_MAX,
                              runner=True)
        _log(f"{kind}: the DB write failed ({type(e).__name__}: {e}); {err.not_saved}")
        return False


def _side_append(path: str, items: list[str]) -> bool:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a") as fh:
            fh.write("".join(x.rstrip("\n") + "\n" for x in items))
        return True
    except OSError as e:
        _log(f"side file {path} not written ({type(e).__name__}: {e})")
        return False


def _side_replace(path: str, text: str) -> bool:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
        return True
    except OSError as e:
        _log(f"side file {path} not written ({type(e).__name__}: {e})")
        return False


def record(kind: str, line_or_lines: str | Iterable[str], path: Optional[str] = None,
           actor: str = DEFAULT_ACTOR, via: str = "runner", conn: Optional[sqlite3.Connection] = None) -> bool:
    """Append row line(s) of an append-only kind: to the DB for the live path (None or the
    live file's name; actions.append_many, rows already stored are skipped), else to the side
    file `path`. -> True if stored. Never raises (a DB failure: the fallback file, §7.8)."""
    items = [line_or_lines] if isinstance(line_or_lines, str) else list(line_or_lines)
    if not is_live(kind, path):
        assert path is not None
        return _side_append(path, items)
    return _db_write(kind, [x.rstrip("\n") for x in items], actor, via, conn, records=False)


def put(kind: str, line_or_lines: str | Iterable[str], actor: str = DEFAULT_ACTOR, via: str = "runner",
        conn: Optional[sqlite3.Connection] = None) -> bool:
    """Insert or update records of a record kind in the DB (actions.put_records: only changed
    ones are written). Never raises (a DB failure: the fallback file, §7.8)."""
    items = [line_or_lines] if isinstance(line_or_lines, str) else list(line_or_lines)
    return _db_write(kind, items, actor, via, conn, records=True)


def _record_line(kind: str, rec: Mapping[str, Any]) -> str:
    """A record as its stored line (the serialisation its legacy file / the importer used)."""
    fmt = FILES[kind][1]
    if fmt == "records":
        return json.dumps(dict(rec), ensure_ascii=False, default=str)
    if fmt == "doc":
        return json.dumps(dict(rec), ensure_ascii=False)
    return json.dumps(dict(rec), default=str)


def write_records(kind: str, recs: Iterable[Mapping[str, Any]], path: Optional[str] = None,
                  actor: str = DEFAULT_ACTOR, via: str = "runner") -> bool:
    """Write the records of a record kind: the live path -> the DB (only the changed ones are
    written; records are never deleted), a side file -> rewritten in its format. -> stored."""
    recs = list(recs)
    if is_live(kind, path):
        return put(kind, [_record_line(kind, r) for r in recs], actor=actor, via=via)
    assert path is not None
    fmt = FILES[kind][1]
    if fmt == "cycles":
        text = json.dumps({str(r.get("reset_at")): dict(r) for r in recs}, indent=1, default=str)
    elif fmt == "doc":
        text = json.dumps(dict(recs[-1]) if recs else {}, indent=2, ensure_ascii=False) + "\n"
    else:
        text = "".join(_record_line(kind, r) + "\n" for r in recs)
    return _side_replace(path, text)


def write_cycles(cycles: Mapping[str, Mapping[str, Any]], path: Optional[str] = None,
                 actor: str = "runner:sampler") -> bool:
    """The weekly cycles {reset_at: cycle} (only the changed ones are written to the DB)."""
    return write_records("usage.weekly_cycle", [cycles[k] for k in sorted(cycles)], path, actor=actor)


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


RETIRED = "retired: the DB is the only copy; the file is a frozen legacy copy (not re-imported)"


def import_all(db_path: Optional[str] = None, data_dir: Optional[str] = None,
               kinds: Optional[Iterable[str]] = None, actor: str = "runner:import",
               catch_up: bool = False) -> dict[str, Any]:
    """Import the legacy telemetry files of `data_dir` (default: the DB's directory) of the
    kinds not imported yet. An imported kind is skipped (RETIRED); with catch_up, the
    append-only ones are imported again (idempotent: rows the DB holds are skipped), the record
    kinds never (their frozen file would revert newer records). Never changes a file.
    -> {"db", "kinds": [stats...], "counts", "seconds"}."""
    import actions
    db = db_path or store.DB_PATH
    ddir = data_dir or os.path.dirname(os.path.abspath(db))
    t0 = time.monotonic()
    conn = store.connect(db)
    try:
        out = []
        for k in (kinds or [k for k in FILES if k not in DB_ONLY]):
            path = os.path.join(ddir, FILES[k][0])
            mark = store.get_meta(conn, MARK + actions.APPENDERS[k].table)
            if mark is not None and not (catch_up and not actions.APPENDERS[k].record):
                out.append({"kind": k, "table": actions.APPENDERS[k].table, "file": path, "status": RETIRED})
                continue
            out.append(import_kind(conn, k, path, actor))
        counts = store.telemetry_counts(conn)
    finally:
        conn.close()
    return {"db": os.path.abspath(db), "data_dir": os.path.abspath(ddir), "kinds": out, "counts": counts,
            "seconds": round(time.monotonic() - t0, 3)}


def status(db_path: Optional[str] = None) -> dict[str, Any]:
    """Per table: rows and the import mark (None = never imported: readers on the legacy file
    if there is one)."""
    conn = store.connect(db_path or store.DB_PATH)
    try:
        out: dict[str, Any] = {}
        for kind in FILES:
            t = _table(kind)
            mark = store.get_meta(conn, MARK + t)
            out[t] = {"kind": kind, "legacy_file": FILES[kind][0],
                      "rows": int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]),
                      "imported": json.loads(mark) if mark else None}
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------- CLI

def set_user_model(doc: Any, actor: str = "cli", via: str = "cli") -> dict[str, Any]:
    """Replace the user model in the DB (the audited action user_model.set; its legacy file is
    imported first if the kind never was). Raises store.DBError / ValueError."""
    import actions
    conn = store.connect()
    try:
        store.retrying(lambda: _ensure_owned(conn, "usage.user_model"), conn)
        out: dict[str, Any] = actions.perform(conn, "user_model.set", {"doc": doc}, actor=actor, via=via)
        return out
    finally:
        conn.close()


def _user_model_set(text: str) -> dict[str, Any]:
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise ValueError(f"the user model is not JSON ({e})") from e
    return set_user_model(doc)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="AFClaude telemetry in the DB (phase 2c).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("import", help="import the legacy data/ telemetry files of the kinds not imported yet "
                                      "(idempotent; files untouched)")
    i.add_argument("--db", default=None)
    i.add_argument("--data-dir", default=None, help="the files' directory (default: the DB's)")
    i.add_argument("--kind", action="append", choices=sorted(FILES), help="only this kind (repeatable)")
    i.add_argument("--catch-up", action="store_true",
                   help="re-import the append-only kinds already imported (rows the DB has are skipped)")
    s = sub.add_parser("status", help="rows per table and the import marks")
    s.add_argument("--db", default=None)
    r = sub.add_parser("rows", help="print a kind's rows (the DB) as JSONL, oldest first")
    r.add_argument("kind", choices=sorted(FILES))
    r.add_argument("--since", default=None, help="only rows at/after this ISO time")
    r.add_argument("--last", type=int, default=None, help="only the last N rows")
    u = sub.add_parser("user-model", help="the user model in the DB (the usage review's user-specific parameters)")
    u.add_argument("op", choices=("get", "set"))
    u.add_argument("file", nargs="?", default="-", help="set: the new document (a JSON file, - = stdin)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "import":
            print(json.dumps(import_all(args.db, args.data_dir, args.kind, catch_up=args.catch_up),
                             indent=1, ensure_ascii=False))
        elif args.cmd == "status":
            print(json.dumps(status(args.db), indent=1, ensure_ascii=False))
        elif args.cmd == "rows":
            got = rows(args.kind, since=args.since)
            for x in got[-args.last:] if args.last else got:
                print(x)
        elif args.op == "get":
            doc = read_user_model()
            if doc is None:
                print("error: no user model in the database", file=sys.stderr)
                return 1
            print(json.dumps(doc, indent=2, ensure_ascii=False))
        else:
            if args.file == "-":
                text = sys.stdin.read()
            else:
                with open(args.file) as fh:
                    text = fh.read()
            print(json.dumps(_user_model_set(text), indent=1, ensure_ascii=False))
    except store.DBError as e:
        print(store.handback_text(e) if e.not_saved else f"error: {e}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
