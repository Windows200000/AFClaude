#!/usr/bin/env python3
"""
runner_state.py: the runners' state, scheduled jobs, own sessions and logs in the DB (dashboard
phase 2d, D-161, docs/dashboard_design/07_data_model.md §7.2). The tables are in store.py (RUNNER_SCHEMA), the
write path in actions.py (state_save, app_log_append / app_log_sync, job_put / job_started,
own_session_add); this module is the runners' one door to them:

  component / table                                   legacy file (data/, frozen)
  runner_state 'keepalive' (handled, fires, limit_hits, run_stops per entry)  keepalive/keepalive_state.json
  runner_state 'keepalive.deferred' (one row per postponed start)  keepalive/keepalive_deferred.json
  runner_state 'keepalive.fillup'                                  keepalive/keepalive_fillup.json
  runner_state 'keepalive.watch' (acts, memo per entry; phase 3a S6)  none (new in the DB era)
  runner_state 'keepalive.usage_refresh' (window_start per entry)  keepalive/usage_refresh_state.json
  runner_state 'dispatcher' (sessions, handled, starts, alerted, rc_held per entry)  dispatcher_state.json
  runner_state 'sampler' (offsets, sessions per entry)             sampler_state.json
  runner_state 'usage_review'                                      usage_review_state.json
  runner_state 'nightly_tasks' (tasks per entry; nightly_tasks.py)  none (new in the DB era)
  runner_state 'inbox' (entries per entry; phase 3a S7, inbox_worker.py)  none (new in the DB era)
  driven_sessions (kind 'own')                                     own_sessions.txt
  scheduled_jobs (a started job is marked, not deleted)            at_spool/*.json
  app_log (component = the file's stem; append-only)   keepalive/keepalive.log, dispatcher.log,
      sampler.log, keepalive/watchdog.log, host_exec.log, at_shim.log (the logs stay files:
      written by the runners / shell redirects and dual-written here; not retired yet)

The DB is the only copy of the state, the jobs and the own sessions (D-161; the files are
retired):
  - load(component) reads the state from the DB: a StateDoc that remembers the rows it came
    from; save() writes it back through actions.state_save in one transaction, only what this
    writer changed or removed since (merge), so the watcher and the cron runs, saving one after
    the other, never lose each other's entries. A plain dict (a state built anew) saves in
    'file' mode: its top-level values replace the DB's, the entries of its split parts are
    upserted but not deleted.
  - A DB failure takes the full §7.8 path: store.retrying first, then the failed save (the
    changed rows) goes to data/ALERTS.fallback.md (store.report_db_error) and the runner goes
    on; store.db_gate (keepalive before every start, the dispatcher before its pass, the
    sampler on each run) pauses the autonomous starts while the DB is broken and alerts once
    per episode. A DB that can't be read gives an empty state that saves without deleting
    anything (logged), never the frozen file.
  - Legacy files (read-only): the files of the dual-write era stay in data/ as frozen copies
    (safe to archive). A component never imported (meta state_imported:<component>), e.g. on a
    fresh install that restored old files, is read from its file until its first save, which
    writes the whole state to the DB and marks the component (also without a legacy file).
    Nothing here writes or changes a legacy file.
  - Any other path (a test, a CLI side copy) is an explicit side file, read and written as a
    file (JSON); it never touches the DB.
  - The importer (`python3 store.py import-state`, `python3 runner_state.py import`) skips the
    state components, own sessions and jobs already imported (their files are frozen:
    re-importing would revert newer DB state, or mark the DB's new jobs started); it still
    syncs the log files into app_log (by count: a line n times in the file and m < n times in
    the DB gets n - m copies), which catches the lines only a shell redirect wrote.
  - Own sessions: `python3 runner_state.py own add <session id>` / `own list` (no file).
"""
from __future__ import annotations

import copy
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

STATE_FILES: dict[str, str] = {   # component -> its legacy file, relative to the data dir
    "keepalive": "keepalive/keepalive_state.json",
    "keepalive.deferred": "keepalive/keepalive_deferred.json",
    "keepalive.fillup": "keepalive/keepalive_fillup.json",
    "keepalive.watch": "keepalive/keepalive_watch.json",   # phase 3a S6: born in the DB, never a file (the name only)
    "keepalive.usage_refresh": "keepalive/usage_refresh_state.json",
    "dispatcher": "dispatcher_state.json",
    "sampler": "sampler_state.json",
    "usage_review": "usage_review_state.json",
    "nightly_tasks": "nightly_tasks_state.json",   # D-254/D-261: born in the DB, never a file (the name only)
    "inbox": "inbox_verify_state.json",   # phase 3a S7: born in the DB, never a file (the name only)
}
LOG_FILES: dict[str, str] = {
    "keepalive": "keepalive/keepalive.log",
    "dispatcher": "dispatcher.log",
    "sampler": "sampler.log",
    "watchdog": "keepalive/watchdog.log",
    "host_exec": "host_exec.log",
    "at_shim": "at_shim.log",
    "runner": "runner.log",          # the resident runner (phase 3a, runner.py): written and dual-written by it
    "runner_shadow": "runner_shadow.log",   # its shadow of the jobs cron still runs (phase 3a S2-S7)
}
OWN_FILE = "own_sessions.txt"
SPOOL_DIR = "at_spool"
MARK = "state_imported:"          # meta: <component> | own_sessions | scheduled_jobs | app_log:<component>
IMPORT_ACTOR = "runner:import"
JOB_ACTOR = "runner:at"
BERLIN = ZoneInfo("Europe/Berlin")
FALLBACK_PAYLOAD_MAX = 1024 * 1024   # the changed rows kept in ALERTS.fallback.md for a replay (§7.8)
RETIRED = "retired: the DB is the only copy; the file is a frozen legacy copy (not re-imported)"

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
    """`path` (None = the live one) names THE live data of the component (the DB; its legacy
    file's name next to store.DB_PATH), not a side file."""
    fs = files or STATE_FILES
    if component not in fs:
        return False
    return path is None or _same(path, live_path(component, fs))


class StateDoc(dict[str, Any]):
    """A state loaded from the DB; `base` = the rows it was loaded from (the merge base of its
    save). A plain dict (e.g. a new one built from it) saves as a replacement. unavailable: the
    DB couldn't be read, this is only the default (a runner must not start anything on it)."""
    base: Optional[dict[str, str]] = None
    unavailable: bool = False


_LOAD_FAILED: dict[str, str] = {}   # component -> why its last load() couldn't read the DB (this process)


def load_failed() -> Optional[str]:
    """Why the last state load of this process couldn't read the DB (until a load reads it
    again), None if it could: the runners treat it like a broken DB (no autonomous start)."""
    return next(iter(_LOAD_FAILED.values()), None)


# ---------------------------------------------------------------- reads

def _read_conn() -> Optional[sqlite3.Connection]:
    """A read-only connection to the existing DB (never creates it, no schema init)."""
    path = store.DB_PATH
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=rw", uri=True, timeout=store.BUSY_TIMEOUT)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _read_side(path: str, default: Any) -> Any:
    """A JSON object file (a side file or a legacy file): its content, else `default`."""
    try:
        with open(path) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return copy.deepcopy(default)
    return d if isinstance(d, dict) else copy.deepcopy(default)


def _empty(component: str, default: Optional[dict[str, Any]]) -> StateDoc:
    """A state without rows behind it (`default` and the empty split parts, as a DB load gives
    them): its save upserts what it holds and deletes nothing."""
    import actions
    doc = StateDoc(copy.deepcopy(default or {}))
    for part in actions.state_component(component).split:
        doc.setdefault(part, {})
    doc.base = {}
    return doc


def load(component: str, path: Optional[str] = None, default: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The component's state: from the DB for the live path (a StateDoc; `default` when it has
    no rows yet), from the legacy file while the component was never imported (a plain dict),
    from the side file `path` otherwise. A DB that can't be read: `default` as an empty
    StateDoc (logged). Never raises."""
    import actions
    if not is_live(component, path):
        assert path is not None
        got: dict[str, Any] = _read_side(path, default or {})
        return got
    legacy = live_path(component)
    has_legacy = os.path.isfile(legacy)

    def run() -> Optional[dict[str, str]]:
        conn = _read_conn()
        if conn is None:
            raise store.DBUnavailable(f"no database at {store.DB_PATH}", "missing")
        try:
            if has_legacy and store.get_meta(conn, MARK + component) is None:
                return None
            return store.state_rows(conn, component)
        finally:
            conn.close()
    try:
        rows = store.retrying(run)
    except (store.DBError, sqlite3.Error, ValueError) as e:
        _log(f"{component}: the database can't be read ({type(e).__name__}: {e}); an empty state until it "
             "is healthy (saves delete nothing, the runners start nothing)")
        _LOAD_FAILED[component] = f"{component}: {type(e).__name__}: {e}"
        doc = _empty(component, default)
        doc.unavailable = True
        return doc
    _LOAD_FAILED.clear()   # the DB reads again (a component's bad rows mark its own doc unavailable)
    if rows is None:   # never imported: the legacy file (read-only); the first save imports it
        legacy_doc: dict[str, Any] = _read_side(legacy, default or {})
        return legacy_doc
    if not rows:
        return _empty(component, default)
    try:
        doc = StateDoc(actions.state_unflatten(component, rows))
    except (ValueError, IndexError, TypeError) as e:
        _log(f"{component}: bad rows in the DB ({type(e).__name__}: {e}); an empty state (saves delete nothing)")
        _LOAD_FAILED[component] = f"{component}: bad rows: {type(e).__name__}: {e}"
        bad = _empty(component, default)
        bad.unavailable = True
        return bad
    doc.base = rows
    return doc


def component_of(path: str) -> Optional[str]:
    for comp, rel in STATE_FILES.items():
        if os.path.basename(rel) == os.path.basename(path) and is_live(comp, path):
            return comp
    return None


def read_json(path: str) -> Any:
    """A state for a read-only reader, by its (legacy) file path: the DB for a live component
    (an OSError if the DB can't be read), else json.load of the file (OSError / ValueError as
    that raises)."""
    comp = component_of(path)
    if comp is None:
        with open(path) as fh:
            return json.load(fh)
    legacy = live_path(comp)
    has_legacy = os.path.isfile(legacy)

    def run() -> Optional[dict[str, str]]:
        conn = _read_conn()
        if conn is None:
            raise store.DBUnavailable(f"no database at {store.DB_PATH}", "missing")
        try:
            if has_legacy and store.get_meta(conn, MARK + comp) is None:
                return None
            return store.state_rows(conn, comp)
        finally:
            conn.close()
    try:
        rows = store.retrying(run)
    except (store.DBError, sqlite3.Error) as e:
        raise OSError(f"{comp}: the database can't be read ({type(e).__name__}: {e})") from e
    if rows is None:   # never imported: the legacy file (read-only)
        with open(legacy) as fh:
            return json.load(fh)
    import actions
    return actions.state_unflatten(comp, rows)


# ---------------------------------------------------------------- writes (the DB only)

def _write_side(path: str, doc: Any) -> bool:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w") as fh:
            json.dump(doc, fh, indent=1, default=str)
        os.replace(tmp, path)
        return True
    except OSError as e:
        _log(f"side file {path} not written ({type(e).__name__}: {e})")
        return False


def _mark_owned(conn: sqlite3.Connection, what: str, legacy: Optional[str]) -> None:
    """The first DB save of a component never imported: from now on the DB is its only copy."""
    if store.get_meta(conn, MARK + what) is not None:
        return
    with store.transaction(conn):
        store.set_meta(conn, MARK + what, json.dumps(
            {"at": store.now_iso(), "file": legacy if legacy and os.path.exists(legacy) else None,
             "note": "DB-owned from the first runner save"}, sort_keys=True))


def _delta(component: str, doc: Any, base: Optional[dict[str, str]]) -> dict[str, Any]:
    """What a failed save would have written (for the fallback record): the changed and the
    removed rows of a merge save, all rows of a file-mode save."""
    import actions
    try:
        rows = actions.state_flatten(component, doc)
    except (ValueError, TypeError) as e:
        return {"unflattenable": f"{type(e).__name__}: {e}"}
    if base is None:
        return {"mode": "file", "rows": rows}
    return {"mode": "merge", "set": {p: v for p, v in rows.items() if base.get(p) != v},
            "deleted": [p for p in base if p not in rows]}


def save(component: str, doc: Any, path: Optional[str] = None, actor: Optional[str] = None,
         conn: Optional[sqlite3.Connection] = None) -> bool:
    """Save a runner state: the live path -> the DB (a StateDoc merges with its base, anything
    else saves in 'file' mode), a side file -> written as JSON. -> True if stored. Never raises
    (a DB failure: the fallback file, §7.8)."""
    import actions
    if not is_live(component, path):
        assert path is not None
        return _write_side(path, doc)
    c = actions.state_component(component)
    who = actor or c.actor
    base = doc.base if isinstance(doc, StateDoc) else None
    try:
        own = conn is None
        db = store.connect() if conn is None else conn
        try:
            actions.state_save(db, component, doc, base=base, mode="file" if base is None else "merge",
                               actor=who, via="runner")
            store.retrying(lambda: _mark_owned(db, component, live_path(component)), db)
        finally:
            if own:
                db.close()
        if isinstance(doc, StateDoc):
            doc.base = actions.state_flatten(component, doc)
        return True
    except Exception as e:   # noqa: BLE001 - never sinks a runner; the save goes to the fallback file
        e.__traceback__ = None   # keep no caller frames alive (a reused exception would hold e.g. a lock open)
        err = store.as_db_error(e)
        store.report_db_error(err, actor=who, action="runner_state.save",
                              params={"component": component, **_delta(component, doc, base)},
                              payload_max=FALLBACK_PAYLOAD_MAX, runner=True)
        _log(f"{component}: the DB save failed ({type(e).__name__}: {e}); {err.not_saved}")
        return False


# ---------------------------------------------------------------- own sessions

def _side_ids(path: str) -> set[str]:
    try:
        with open(path) as fh:
            return {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    except OSError:
        return set()


def act_name(job: str, session: str) -> str:
    """The plan key of the planned act `job` of the task-manager `session` in the keep-alive's
    runner state (acts.<job>:<sid8>, phase 3b S3): the keep-alive writes it, the runner reads it."""
    return f"{job}:{session[:8]}"


def own_sessions(path: Optional[str] = None) -> set[str]:
    """The session ids listed as AFClaude's own (driven_sessions kind 'own'; the CLI `own add`
    adds one). `path`: a side list file (tests) is read as a file; the live one (None or
    data/own_sessions.txt) is the DB, its legacy file only while it was never imported. Never
    raises (an unreadable DB: an empty set, logged)."""
    live = os.path.join(data_dir(), OWN_FILE)
    if path is not None and not _same(path, live):
        return _side_ids(path)
    has_legacy = os.path.isfile(live)

    def run() -> Optional[set[str]]:
        conn = _read_conn()
        if conn is None:
            raise store.DBUnavailable(f"no database at {store.DB_PATH}", "missing")
        try:
            if has_legacy and store.get_meta(conn, MARK + "own_sessions") is None:
                return None
            return {str(r[0]) for r in conn.execute("SELECT session_id FROM driven_sessions WHERE kind='own'")}
        finally:
            conn.close()
    try:
        got = store.retrying(run)
    except (store.DBError, sqlite3.Error) as e:
        _log(f"own sessions: the database can't be read ({type(e).__name__}: {e})")
        return set()
    return _side_ids(live) if got is None else got


def own_session_add(session_id: str, actor: str = "cli", via: str = "cli") -> bool:
    """Add an own session (driven_sessions kind 'own', audited). -> added (False: known)."""
    import actions
    conn = store.connect()
    try:
        legacy = os.path.join(data_dir(), OWN_FILE)
        if os.path.isfile(legacy) and store.get_meta(conn, MARK + "own_sessions") is None:
            import_own_sessions(conn, legacy)          # the legacy list first, then it is retired
        added = actions.own_session_add(conn, session_id, actor=actor, via=via)
        store.retrying(lambda: _mark_owned(conn, "own_sessions", legacy), conn)
        return added
    finally:
        conn.close()


# ---------------------------------------------------------------- scheduled jobs (the at-shim)

def job_add(job: dict[str, Any], actor: str = JOB_ACTOR) -> bool:
    """A job the at-shim schedules (id, at, cmd, cwd) into scheduled_jobs. -> stored. Never
    raises (a DB failure: the job goes to the fallback file, §7.8)."""
    import actions
    try:
        conn = store.connect()
        try:
            actions.job_put(conn, job, actor=actor, via="runner")
            store.retrying(lambda: _mark_owned(conn, "scheduled_jobs", os.path.join(data_dir(), SPOOL_DIR)), conn)
        finally:
            conn.close()
        return True
    except Exception as e:   # noqa: BLE001 - recorded for a replay
        e.__traceback__ = None
        err = store.as_db_error(e)
        store.report_db_error(err, actor=actor, action="job.put", params={"job": job},
                              payload_max=FALLBACK_PAYLOAD_MAX, runner=True)
        _log(f"job {job.get('id')}: not stored ({type(e).__name__}: {e}); {err.not_saved}")
        return False


def jobs(status: Optional[str] = None) -> list[dict[str, Any]]:
    """The scheduled jobs (oldest run time first); an unreadable DB raises store.DBError."""
    conn = store.connect()
    try:
        return store.retrying(lambda: store.list_jobs(conn, status), conn)
    finally:
        conn.close()


def job_claim(job_id: str, actor: str = JOB_ACTOR) -> bool:
    """Claim a pending job before running it (marks it started in one UPDATE ... WHERE
    status='pending', so two runs never start it twice). -> claimed. Raises store.DBError."""
    import actions
    conn = store.connect()
    try:
        return actions.job_started(conn, job_id, actor=actor, via="runner")
    finally:
        conn.close()


def legacy_jobs(spool: Optional[str] = None) -> list[dict[str, Any]]:
    """The jobs of the legacy spool dir (read-only: never written or deleted here)."""
    d = spool or os.path.join(data_dir(), SPOOL_DIR)
    out: list[dict[str, Any]] = []
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name)) as fh:
                j = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(j, dict):
            out.append(j)
    return out


def job_spooled(job: dict[str, Any], spool: str) -> bool:
    """The dual-write call of an older at-shim copy (the image's /usr/local/bin/at until the
    image is rebuilt): its spooled job into the DB (only for the live spool). Never raises."""
    if not _same(spool, os.path.join(data_dir(), SPOOL_DIR)):
        return False
    return job_add(job)


def job_claimed(job: dict[str, Any], spool: str) -> bool:
    """The dual-write call of an older at-shim copy: a job it claimed. Never raises."""
    if not _same(spool, os.path.join(data_dir(), SPOOL_DIR)) or not job_add(job):
        return False
    try:
        job_claim(str(job.get("id")))
        return True
    except Exception as e:   # noqa: BLE001
        e.__traceback__ = None
        _log(f"job {job.get('id')}: not marked started ({type(e).__name__}: {e})")
        return False


# ---------------------------------------------------------------- logs (still files, dual-written)

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


# ---------------------------------------------------------------- the importer (legacy files)

def _mark(conn: sqlite3.Connection, what: str, stats: dict[str, Any]) -> None:
    with store.transaction(conn):
        store.set_meta(conn, MARK + what, json.dumps(dict(stats, at=store.now_iso()), sort_keys=True))


def _retired(conn: sqlite3.Connection, what: str) -> bool:
    return store.get_meta(conn, MARK + what) is not None


def import_state(conn: sqlite3.Connection, component: str, path: str, actor: str = IMPORT_ACTOR) -> dict[str, Any]:
    """One legacy state file: a component already imported (or saved by a runner since) is
    skipped (RETIRED); else the DB copy becomes the file, checked row by row, and the component
    is marked imported."""
    import actions
    res: dict[str, Any] = {"component": component, "file": path}
    if _retired(conn, component):
        res.update(status=RETIRED, db_rows=len(store.state_rows(conn, component)))
        return res
    if not os.path.isfile(path):
        res["status"] = "no file"
        return res
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
    res.update(actions.state_save(conn, component, doc, mode="replace", actor=actor, via="runner"))
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
    if _retired(conn, "own_sessions"):
        res["status"] = RETIRED
        return res
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
    """The legacy spool: its jobs into scheduled_jobs, pending DB jobs whose spool file is gone
    marked started (the old at-shim claimed them by deleting the file). Only before the jobs
    are retired: afterwards new jobs live in the DB alone (they have no spool file) and this
    would mark them started, so an imported spool is skipped (RETIRED)."""
    import actions
    res: dict[str, Any] = {"what": "scheduled_jobs", "dir": spool}
    if _retired(conn, "scheduled_jobs"):
        res["status"] = RETIRED
        return res
    found = legacy_jobs(spool)
    names = [n for n in os.listdir(spool) if n.endswith(".json")] if os.path.isdir(spool) else []
    bad = len(names) - len(found)
    counts = {"inserted": 0, "updated": 0, "same": 0}
    for j in found:
        try:
            counts[actions.job_put(conn, j, actor=actor, via="runner")] += 1
        except ValueError:
            bad += 1
    spooled = {str(j.get("id")) for j in found}
    gone = 0
    for row in store.list_jobs(conn, "pending"):
        if row["id"] not in spooled and actions.job_started(conn, row["id"], actor=actor, via="runner"):
            gone += 1
    missing = sum(1 for j in found if store.get_job(conn, str(j.get("id"))) is None)
    res.update(files=len(found), invalid=bad, marked_started=gone, missing=missing, **counts)
    if missing == 0:
        _mark(conn, "scheduled_jobs", {"dir": os.path.abspath(spool), "rows": len(found)})
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
    """Import the legacy state files, own sessions and spooled jobs of `ddir` (default: the
    DB's directory) not imported yet, and sync the logs. Never changes or deletes a file."""
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
    i = sub.add_parser("import", help="import the legacy data/ state files, jobs and own sessions not imported "
                                      "yet, and sync the logs (idempotent; files untouched)")
    i.add_argument("--db", default=None)
    i.add_argument("--data-dir", default=None, help="the files' directory (default: the DB's)")
    s = sub.add_parser("status", help="rows per component and the import marks")
    s.add_argument("--db", default=None)
    sh = sub.add_parser("show", help="print a component's state (the DB) as JSON")
    sh.add_argument("component", choices=sorted(STATE_FILES))
    o = sub.add_parser("own", help="AFClaude's own sessions (driven_sessions kind 'own')")
    o.add_argument("op", choices=("list", "add"))
    o.add_argument("session_id", nargs="?")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "import":
            print(json.dumps(import_all(args.db, args.data_dir), indent=1, ensure_ascii=False))
        elif args.cmd == "status":
            print(json.dumps(status(args.db), indent=1, ensure_ascii=False))
        elif args.cmd == "show":
            print(json.dumps(dict(load(args.component)), indent=1, ensure_ascii=False, default=str))
        elif args.op == "list":
            for sid in sorted(own_sessions()):
                print(sid)
        else:
            if not args.session_id:
                ap.error("own add needs a session id")
            print("added" if own_session_add(args.session_id) else "already listed")
    except store.DBError as e:
        print(store.handback_text(e) if e.not_saved else f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
