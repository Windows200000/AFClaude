#!/usr/bin/env python3
"""
Local SQLite store for AFClaude: data/afclaude.db (data/ is not committed).

Schema creation is idempotent: every table is CREATE TABLE IF NOT EXISTS, and
columns added later go into COLUMNS so `connect()` ALTERs older databases in
place. New tables go into SCHEMA the same way (CREATE ... IF NOT EXISTS), so
an older database gains them on its next connect(); bump SCHEMA_VERSION.

  v1 (goal 2): sessions, limit_hits, meta
  v2 (goal 3): tasks, task_events (append-only), session_decisions, standing_rules
  v3 (goal 4): projects, an ordered list (rank 1 = top); tasks become the stages
               of a project (project_id, stage_seq) with priority high|medium|low.
               A v2 tasks table is rebuilt in place by _migrate_v3 (after a backup
               copy): integer priorities -> 'high', free-text project -> projects.
               Later column (COLUMNS, no version bump): projects.manager_session
               (goal 5: a managed project's stages are worked by that session,
               not by per-task dispatcher sessions).
  v4 (dashboard phase 1, docs/dashboard_design.md §4): settings, prompt_overrides,
               audit_log (append-only), idempotency_keys, run_log, driven_sessions,
               action_requests; a `version` column on projects, tasks,
               standing_rules and session_decisions, bumped by a trigger on every
               UPDATE (optimistic concurrency, whoever writes); task kind 'question'
               (a manager question, born blocked; answering it closes it). Purely
               additive: a v3 database is first copied to <db>.v3-<utc>.bak, then
               gains the tables/columns/triggers in place (_backup_v3, init).
               Writes from the CLI, the MCP server and the dashboard go through
               actions.py (validation, audit row, idempotency, version checks).

Schema guard (docs/dashboard_design.md §7.1, review A1): this code writes only to a
database whose schema_version it knows. A newer one (written by newer code) is left
untouched: connect() opens it read-only (reads go on; transaction() raises
SchemaTooNew naming both versions; any other write fails in SQLite). Non-additive
changes (a table rebuild, a dropped or renamed column) never run on connect(): they
go into MIGRATIONS, and a database that needs one stays read-only (MigrationNeeded)
until the explicit migrate step, which backs it up first:
    python3 store.py migrate [--db PATH]
Additive changes (new tables, COLUMNS, triggers) still apply on connect(). The v2 -> v3
rebuild (_migrate_v3) predates this rule and still runs on connect(), after a backup.

Times are stored as ISO-8601 UTC strings (transcript timestamps as written,
e.g. 2026-09-25T15:40:19.679Z; computed ones as 2026-09-25T17:00:00Z; task and
decision times always with milliseconds, so they sort as strings).

Task/decision functions run in their own transaction (BEGIN IMMEDIATE, so a
read-check-write like start_task is safe between processes) and commit. If the
caller already has an open transaction they run inside a savepoint instead and
the caller commits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Literal, TypeVar, overload

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("AFCLAUDE_DB", os.path.join(HERE, "data", "afclaude.db"))
SCHEMA_VERSION = 4

Row = dict[str, Any]        # a table row as a dict (what the read functions return)
_T = TypeVar("_T")

# Non-additive migrations: {the schema_version they produce: fn(conn)}. connect() never
# runs them; `python3 store.py migrate` does, after a backup, each in one transaction
# with foreign keys off (migrate()). A step gets the database as the previous version
# left it. Additive changes need no entry (SCHEMA, COLUMNS, VERSION_TRIGGERS).
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}

# The tasks table, v3. {name} so _migrate_v3 can build it as tasks_v3 and rename it.
TASKS_DDL = """
CREATE TABLE IF NOT EXISTS {name} (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title              TEXT NOT NULL,
    description        TEXT,
    project_id         INTEGER REFERENCES projects(id),   -- NULL = no project (runs after all projects)
    stage_seq          INTEGER,         -- order within the project, 1 = first (NULL without a project)
    project            TEXT,            -- legacy v2 free text, kept only as the migration's source
    priority           TEXT NOT NULL DEFAULT 'high',     -- high|medium|low
    status             TEXT NOT NULL DEFAULT 'pending',  -- pending|in_progress|blocked|done|cancelled
    kind               TEXT NOT NULL DEFAULT 'task',     -- task|backlog_project|question
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    created_by_session TEXT,
    assigned_session   TEXT,            -- last session that started it
    blocked_question   TEXT,            -- kept after the answer, so the next run sees both
    blocked_at         TEXT,
    answer             TEXT,
    answered_at        TEXT,
    result_summary     TEXT,
    done_at            TEXT,
    version            INTEGER NOT NULL DEFAULT 0       -- v4: bumped by tasks_version on every UPDATE
)"""

PROJECTS_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    rank        INTEGER NOT NULL,        -- 1 = top; kept contiguous by the store, reordered by hand
    description TEXT,
    path        TEXT,                    -- directory it lives in (cwd of the creating session)
    manager_session TEXT,                -- managed project: this session works its stages (goal 5)
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 0   -- v4: bumped by projects_version on every UPDATE
)"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- one row per top-level Claude Code session transcript on this host
CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT PRIMARY KEY,
    project_dir         TEXT,            -- directory name under ~/.claude/projects
    path                TEXT,            -- transcript path
    cwd                 TEXT,            -- latest cwd seen in entries
    title               TEXT,            -- custom-title > agent-name > ai-title
    title_rank          INTEGER,         -- 3 custom, 2 agent, 1 ai
    first_seen          TEXT,            -- first entry timestamp
    last_activity       TEXT,            -- latest entry timestamp
    own                 INTEGER NOT NULL DEFAULT 0,
    last_scanned_offset INTEGER NOT NULL DEFAULT 0,  -- bytes consumed (complete lines)
    last_scanned_size   INTEGER NOT NULL DEFAULT 0,  -- file size at last scan
    last_msg_uuid       TEXT,            -- last user/assistant entry seen so far
    last_msg_type       TEXT,
    last_msg_ts         TEXT,
    stalled             INTEGER NOT NULL DEFAULT 0,  -- last user/assistant entry is a limit notice
    stalled_since       TEXT,            -- timestamp of that notice
    stall_kind          TEXT,            -- session | weekly | ...
    stall_reset_at      TEXT,            -- parsed reset time (UTC)
    stall_text          TEXT,
    stall_uuid          TEXT,
    updated_at          TEXT
);

-- every synthetic usage-limit notice ever seen (hit history)
CREATE TABLE IF NOT EXISTS limit_hits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    entry_uuid TEXT UNIQUE,
    ts         TEXT,
    kind       TEXT,
    reset_at   TEXT,
    text       TEXT
);
CREATE INDEX IF NOT EXISTS limit_hits_session ON limit_hits(session_id, ts);
CREATE INDEX IF NOT EXISTS sessions_stalled ON sessions(stalled);

-- v3: projects (ordered) and tasks = the stages of a project. status/kind/
-- priority/decision/scope values are validated in Python (no CHECK
-- constraints: SQLite can't ALTER those, a new value would need a table rebuild).
""" + PROJECTS_DDL + """;
CREATE UNIQUE INDEX IF NOT EXISTS projects_path ON projects(path) WHERE path IS NOT NULL;
""" + TASKS_DDL.format(name="tasks") + """;
CREATE INDEX IF NOT EXISTS tasks_order ON tasks(status, priority, project_id, stage_seq);

-- every change to a task, oldest first; detail is JSON
CREATE TABLE IF NOT EXISTS task_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    ts      TEXT NOT NULL,
    event   TEXT NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS task_events_task ON task_events(task_id, id);
CREATE TRIGGER IF NOT EXISTS task_events_no_update BEFORE UPDATE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS task_events_no_delete BEFORE DELETE ON task_events
BEGIN SELECT RAISE(ABORT, 'task_events is append-only'); END;

-- one-off continue/ignore for a stalled session; valid for the stall it was
-- made for (stall_ref), a later stall asks again (that's what rules are for)
CREATE TABLE IF NOT EXISTS session_decisions (
    session_id TEXT PRIMARY KEY,
    decision   TEXT NOT NULL,           -- continue|ignore
    decided_at TEXT NOT NULL,
    note       TEXT,
    stall_ref  TEXT,                    -- sessions.stall_uuid (or stalled_since) at decision time
    version    INTEGER NOT NULL DEFAULT 0
);

-- standing "always continue" / "always ignore"
CREATE TABLE IF NOT EXISTS standing_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,           -- session|project
    match      TEXT NOT NULL,           -- session id | absolute path (cwd, incl. subdirs) | project_dir name
    decision   TEXT NOT NULL,           -- continue|ignore
    created_at TEXT NOT NULL,
    note       TEXT,
    version    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS standing_rules_scope_match ON standing_rules(scope, match);

-- v4 (dashboard phase 1). Values are validated in actions.py, not with CHECKs.
-- settings: key -> JSON value; no row or a JSON null = the code default (actions.SETTINGS).
-- A reset stores null instead of deleting, so a key's version never repeats (no row = 0,
-- the first save = 1).
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,           -- JSON
    version    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

-- an edited prompt; the file prompts/<name> stays the default. A reset sets body NULL
-- (the row stays, so its version never repeats; no row = version 0).
CREATE TABLE IF NOT EXISTS prompt_overrides (
    name        TEXT PRIMARY KEY,       -- file name under prompts/, e.g. continue.md
    body        TEXT,                   -- NULL = reset to the default
    base_sha256 TEXT,                   -- sha256 of the default file when the edit was made
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);

-- every write through actions.py, oldest first (task_events stays the per-task history)
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,          -- owner | cli | mcp[:<session>] | dispatcher | keepalive
    via         TEXT NOT NULL,          -- dashboard | mcp | cli | runner
    action      TEXT NOT NULL,          -- actions.ACTIONS name, e.g. task.answer
    target_type TEXT,
    target_id   TEXT,
    before      TEXT,                   -- JSON: the changed fields before (NULL = created)
    after       TEXT,                   -- JSON: the changed fields after (NULL = deleted)
    request_id  TEXT                    -- the idempotency key, if any
);
CREATE INDEX IF NOT EXISTS audit_log_target ON audit_log(target_type, target_id, id);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

-- a replayed key returns the stored response without acting again (pruned after 7 days)
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key            TEXT PRIMARY KEY,
    actor          TEXT NOT NULL,
    action         TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,       -- the action + parameters it was first used for
    response       TEXT,                -- JSON
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idempotency_keys_created ON idempotency_keys(created_at);

-- one row per keep-alive / dispatcher decision (FIRE, HOLD, WAIT_WINDOW, skip, start, cleanup)
CREATE TABLE IF NOT EXISTS run_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    component  TEXT NOT NULL,           -- keepalive | dispatcher | ...
    session_id TEXT,
    task_id    INTEGER,
    decision   TEXT NOT NULL,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS run_log_component ON run_log(component, id);

-- every session AFClaude started or continued (replaces dispatcher_state.json's list
-- and data/own_sessions.txt once the runners fill it, phase 3)
CREATE TABLE IF NOT EXISTS driven_sessions (
    session_id  TEXT PRIMARY KEY,
    tmux        TEXT,
    kind        TEXT NOT NULL,          -- keepalive | task | stall | review
    task_id     INTEGER,
    started_at  TEXT NOT NULL,
    last_seen   TEXT,
    ended_at    TEXT,
    holder_kind TEXT,
    rc_url      TEXT
);

-- the dashboard's only way to ask for execution; the dispatcher consumes them (phase 2)
CREATE TABLE IF NOT EXISTS action_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    kind       TEXT NOT NULL,           -- continue_now | review_now
    target     TEXT,                    -- session id (continue_now), NULL (review_now)
    status     TEXT NOT NULL DEFAULT 'open',   -- open | done | failed | cancelled
    result     TEXT,
    handled_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS action_requests_one_open ON action_requests(kind, COALESCE(target, ''))
    WHERE status = 'open';
"""

# v4: every UPDATE bumps `version` (unless the statement set it itself), so a stale
# read is detectable whoever wrote: actions.py, the dispatcher, or an older checkout.
# Created after COLUMNS (the column must exist). recursive_triggers is off, so the
# trigger's own UPDATE doesn't fire it again.
VERSIONED = {"projects": "id", "tasks": "id", "standing_rules": "id", "session_decisions": "session_id",
             "settings": "key", "prompt_overrides": "name"}
VERSION_TRIGGERS = [
    f"CREATE TRIGGER IF NOT EXISTS {t}_version AFTER UPDATE ON {t} FOR EACH ROW "
    f"WHEN NEW.version IS OLD.version BEGIN "
    f"UPDATE {t} SET version = OLD.version + 1 WHERE {pk} = NEW.{pk}; END"
    for t, pk in VERSIONED.items()]

# Columns added after a table first shipped: {table: [(name, decl), ...]}.
# connect() adds any that an existing database lacks.
VERSION_COL = ("version", "INTEGER NOT NULL DEFAULT 0")
COLUMNS = {
    "projects": [("manager_session", "TEXT"), VERSION_COL],
    "sessions": [],
    "limit_hits": [],
    "tasks": [VERSION_COL],
    "task_events": [],
    "session_decisions": [VERSION_COL],
    "standing_rules": [VERSION_COL],
}

SESSION_FIELDS = (
    "project_dir", "path", "cwd", "title", "title_rank", "first_seen", "last_activity", "own",
    "last_scanned_offset", "last_scanned_size", "last_msg_uuid", "last_msg_type", "last_msg_ts",
    "stalled", "stalled_since", "stall_kind", "stall_reset_at", "stall_text", "stall_uuid",
    "updated_at",
)


def iso(dt: datetime | str | None) -> str | None:
    """datetime -> '2026-09-25T17:00:00Z' (None passes through)."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    dt = dt.astimezone(timezone.utc)
    s = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        s += ".%03d" % (dt.microsecond // 1000)
    return s + "Z"


def parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def connect(path: str | None = None, create: bool = False) -> sqlite3.Connection:
    """Open the database (WAL, foreign keys on) and bring its schema up to date (init), on
    the DB error path (§7.8): a transient error is retried, a persistent one is a DBError.
    A database this process can't write to (read-only file or file system) still opens,
    read-only (PRAGMA query_only): reads go on, every write is a DBError (read_only).

    Creating a new database is an explicit act, never a side effect of an ordinary connect
    (D-171, D-187): a missing data volume must stop writes and escalate, not quietly start
    empty and pass every later health check. So when the file doesn't exist, connect() raises
    DBUnavailable (kind "missing") unless the caller passes create=True -- the explicit
    init/setup path (`python3 store.py init`) and tests (testenv.py), which stand in for a
    real first install. Every other caller (the runners, tasks.py, the MCP server,
    export_quickview.py) keeps the default: on a live install the database already exists, so
    this is a no-op change for them, and a lost or mis-mounted data directory is reported
    instead of silently recreated."""
    db = path or DB_PATH
    if not create and not os.path.isfile(db):
        raise DBUnavailable(f"the database {db} does not exist: nothing creates it implicitly "
                            "(a lost or mis-mounted data directory must not come back as a fresh, "
                            "empty one); create it explicitly with `python3 store.py init` or "
                            "restore/mount the real one", "missing")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(db)), exist_ok=True)
    except OSError as e:
        raise DBUnavailable(f"the database directory of {db} can't be created ({e})", "missing") from e

    def open_() -> sqlite3.Connection:
        conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            init(conn)
        except sqlite3.Error as e:
            if classify(e) == ("persistent", "read_only"):
                if conn.in_transaction:
                    conn.rollback()
                conn.execute("PRAGMA query_only=ON")
                return conn
            conn.close()
            raise
        except BaseException:
            conn.close()
            raise
        return conn
    return retrying(open_)


def init(conn: sqlite3.Connection) -> None:
    """Create or (additively) upgrade the schema. Schema guard: a database that is newer
    than this code, or needs a non-additive migration first, is left untouched and the
    connection becomes read-only (PRAGMA query_only); see the module doc."""
    if schema_problem(conn) is not None:
        conn.execute("PRAGMA query_only=ON")
        return
    if not _migrate_v3(conn):  # before SCHEMA: its tasks_order index needs the v3 columns
        _backup_v3(conn)       # (a v2 database was just backed up by _migrate_v3)
    before = _stored_version(conn)
    conn.executescript(SCHEMA)
    with transaction(conn):    # BEGIN IMMEDIATE: concurrent connects add each column once
        for table, cols in COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        for ddl in VERSION_TRIGGERS:
            conn.execute(ddl)
        if before is not None and before < 4 and get_meta(conn, "migrated_v4_at") is None:
            set_meta(conn, "migrated_v4_at", now_iso())
        if before is None:   # a brand-new database (no meta table yet): mark it as one install's own
            set_meta(conn, "install_id", uuid.uuid4().hex)
            set_meta(conn, "installed_at", now_iso())
        # raise the stored version, never lower it (an older checkout must not downgrade the mark)
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                     "WHERE CAST(meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
                     (str(SCHEMA_VERSION),))


def _stored_version(conn: sqlite3.Connection) -> int | None:
    """meta.schema_version as an int; None for a fresh database (no meta table yet)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
        return None
    try:
        return int(get_meta(conn, "schema_version", 0))
    except (TypeError, ValueError):
        return 0


# ---- schema guard (§7.1) and the explicit migrate step

ErrorClass = Literal["transient", "caller", "persistent"]


class DBError(RuntimeError):
    """A database failure that retrying didn't fix (§7.8; the DB error path section below).
    error_class: persistent (transient: a raw error on a path that didn't retry); kind:
    locked | io | read_only | disk_full | corrupt | missing | schema | error. report_db_error()
    adds what was not saved and the escalation (the hand-back, handback())."""
    def __init__(self, msg: str, kind: str = "error", error_class: ErrorClass = "persistent") -> None:
        super().__init__(msg)
        self.kind: str = kind
        self.error_class: ErrorClass = error_class
        self.not_saved: str | None = None
        self.escalation: str | None = None


class SchemaMismatch(DBError):
    """The database's schema_version doesn't fit this code, so it refuses to write
    (reads go on). db_version / code_version: the two versions."""
    def __init__(self, msg: str, db_version: int, code_version: int) -> None:
        super().__init__(msg, "schema")
        self.db_version, self.code_version = db_version, code_version


class SchemaTooNew(SchemaMismatch):
    """Written by newer code: this code must not write to it."""


class MigrationNeeded(SchemaMismatch):
    """Needs a non-additive migration (MIGRATIONS) first: `python3 store.py migrate`."""


def pending_migrations(version: int) -> list[int]:
    """The non-additive steps a database at `version` needs, in order."""
    return sorted(v for v in MIGRATIONS if version < v <= SCHEMA_VERSION)


def schema_problem(conn: sqlite3.Connection) -> SchemaMismatch | None:
    """Why this code must not write to the database (None = it may). A database without
    a schema_version is a fresh one (init creates the current schema)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
        return None
    raw = get_meta(conn, "schema_version")
    if raw is None:
        return None
    try:
        stored = int(raw)
    except ValueError:
        stored = 0
    where = _db_file(conn) or "(in memory)"
    if stored > SCHEMA_VERSION:
        return SchemaTooNew(f"the database {where} has schema_version {stored}, newer than this code's "
                            f"{SCHEMA_VERSION}: refusing to write (reads still work); update the code",
                            stored, SCHEMA_VERSION)
    steps = pending_migrations(stored)
    if steps:
        return MigrationNeeded(f"the database {where} has schema_version {stored}; this code "
                               f"({SCHEMA_VERSION}) needs the non-additive migration to "
                               f"{', '.join(map(str, steps))} first: refusing to write until "
                               "`python3 store.py migrate` (it backs the database up first)",
                               stored, SCHEMA_VERSION)
    return None


def check_writable(conn: sqlite3.Connection) -> None:
    """Raise SchemaMismatch if this code must not write to the database (schema guard)."""
    problem = schema_problem(conn)
    if problem is not None:
        raise problem


def _backup_copy(conn: sqlite3.Connection, path: str, tag: str) -> str:
    """Copy the database to <path>.<tag>-<utc>.bak (SQLite online backup). -> its path."""
    dst_path = f"{path}.{tag}-{_utcnow():%Y%m%dT%H%M%SZ}.bak"
    dst = sqlite3.connect(dst_path)
    try:
        conn.backup(dst)
    finally:
        dst.close()
    return dst_path


def migrate(path: str | None = None) -> dict[str, Any]:
    """The explicit migrate step (§7.1): if the database needs non-additive migrations,
    copy it to <db>.v<old>-<utc>.bak first, then run each (MIGRATIONS, in order; one
    transaction each, foreign keys off, foreign_key_check before the commit), then the
    additive part (init). Refuses a database newer than this code (SchemaTooNew).
    -> {db, from, to, steps, backup}."""
    path = path or DB_PATH
    if not os.path.isfile(path):
        raise NotFound(f"no database at {path}")
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        stored = _stored_version(conn)
        problem = schema_problem(conn)
        if isinstance(problem, SchemaTooNew):
            raise problem
        steps = pending_migrations(stored) if stored is not None else []
        backup = _backup_copy(conn, path, f"v{stored}") if steps else None
        for v in steps:
            _run_migration(conn, v)
        init(conn)
        return {"db": path, "from": stored, "to": SCHEMA_VERSION, "steps": steps, "backup": backup}
    finally:
        conn.close()


def _run_migration(conn: sqlite3.Connection, version: int) -> None:
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")        # no effect inside a transaction: set it first
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            have = _stored_version(conn)
            if have is not None and have >= version:   # another migrate ran while we waited
                conn.rollback()
                return
            MIGRATIONS[version](conn)
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise sqlite3.IntegrityError(f"migration to v{version} left dangling references: "
                                             f"{[tuple(b) for b in bad]}")
            set_meta(conn, "schema_version", version)
            set_meta(conn, f"migrated_v{version}_at", now_iso())
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _backup_v3(conn: sqlite3.Connection) -> str | None:
    """v3 -> v4 is additive (new tables, a version column, triggers; init does it in
    place), but like the v3 rebuild it first copies a non-empty database to
    <db>.v3-<utc>.bak. Only for a stored schema_version of 3: v4+ needs nothing, a
    v2 one was backed up by _migrate_v3. -> the backup path or None."""
    if _stored_version(conn) != 3:
        return None
    path = _db_file(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    rows = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
               for t in ("sessions", "tasks", "projects", "standing_rules", "session_decisions") if t in tables)
    if not path or not rows:
        return None
    conn.commit()
    return _backup_copy(conn, path, "v3")


V2_TASK_COLUMNS = ("id", "title", "description", "project", "status", "kind", "created_at", "updated_at",
                   "created_by_session", "assigned_session", "blocked_question", "blocked_at", "answer",
                   "answered_at", "result_summary", "done_at")


def _db_file(conn: sqlite3.Connection) -> str | None:
    r = conn.execute("PRAGMA database_list").fetchone()
    path: str | None = r[2] if r and r[2] else None
    return path


def _migrate_v3(conn: sqlite3.Connection, backup: bool = True) -> bool:
    """v2 -> v3, in place: rebuild tasks with project_id/stage_seq and a
    high|medium|low priority. Every old integer priority becomes 'high'; each
    distinct free-text project becomes a projects row (ranked by first use; a
    path gets its basename as name), and its tasks become its stages in the old
    execution order (priority desc, created_at, id). Each migrated task gets a
    'migrated' event. A non-empty database is first copied to
    <db>.v2-<utc>.bak. No-op on a fresh or already-migrated database; True if
    it migrated."""
    def task_cols() -> set[str]:
        return {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if not task_cols() or "project_id" in task_cols():
        return False
    conn.commit()
    path = _db_file(conn)
    if backup and path and conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]:
        dst = sqlite3.connect(f"{path}.v2-{_utcnow():%Y%m%dT%H%M%SZ}.bak")
        conn.backup(dst)
        dst.close()
    conn.execute("PRAGMA foreign_keys=OFF")        # no effect inside a transaction: set it first
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "project_id" in task_cols():        # another process migrated while we waited
                conn.rollback()
                return False
            conn.execute(PROJECTS_DDL)
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS projects_path ON projects(path) WHERE path IS NOT NULL")
            conn.execute("DROP TABLE IF EXISTS tasks_v3")
            conn.execute(TASKS_DDL.format(name="tasks_v3"))
            cols = ", ".join(V2_TASK_COLUMNS)
            conn.execute(f"INSERT INTO tasks_v3 ({cols}, priority) SELECT {cols}, 'high' FROM tasks")
            ts = now_iso()
            names: dict[str, str] = {}
            for (value,) in conn.execute("SELECT project FROM tasks WHERE TRIM(COALESCE(project, '')) != '' "
                                         "GROUP BY project ORDER BY MIN(created_at), MIN(id)").fetchall():
                p = _ensure_project(conn, value, nearest=False)
                names[value] = p["name"]
                seq = _next_seq(conn, p["id"], table="tasks_v3")
                for i, (tid,) in enumerate(conn.execute(
                        "SELECT id FROM tasks WHERE project=? ORDER BY priority DESC, created_at, id",
                        (value,)).fetchall()):
                    conn.execute("UPDATE tasks_v3 SET project_id=?, stage_seq=? WHERE id=?", (p["id"], seq + i, tid))
            for tid, prio, value in conn.execute("SELECT id, priority, project FROM tasks ORDER BY id").fetchall():
                d: dict[str, Any] = {"priority": [prio, "high"]}
                if value in names:
                    d["project"] = names[value]
                _event(conn, tid, ts, "migrated", d)
            last = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='tasks'").fetchone()
            conn.execute("DROP TABLE tasks")
            conn.execute("ALTER TABLE tasks_v3 RENAME TO tasks")
            if last:                                # ids are never reused
                conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name='tasks'", (last[0],))
                conn.execute("INSERT INTO sqlite_sequence(name, seq) SELECT 'tasks', ? WHERE NOT EXISTS "
                             "(SELECT 1 FROM sqlite_sequence WHERE name='tasks')", (last[0],))
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise sqlite3.IntegrityError(f"v3 migration left dangling references: {[tuple(b) for b in bad]}")
            set_meta(conn, "migrated_v3_at", ts)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    return True


@overload
def get_meta(conn: sqlite3.Connection, key: str) -> str | None: ...
@overload
def get_meta(conn: sqlite3.Connection, key: str, default: _T) -> str | _T: ...
def get_meta(conn: sqlite3.Connection, key: str, default: object = None) -> object:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_session(conn: sqlite3.Connection, session_id: str) -> Row | None:
    r = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return dict(r) if r else None


def upsert_session(conn: sqlite3.Connection, session_id: str, **fields: Any) -> None:
    """Insert the session or update only the given fields."""
    bad = set(fields) - set(SESSION_FIELDS)
    if bad:
        raise ValueError(f"unknown session fields: {sorted(bad)}")
    fields = {k: iso(v) if isinstance(v, datetime) else v for k, v in fields.items()}
    cols = ["session_id"] + list(fields)
    ph = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({ph})"
    if fields:
        sql += " ON CONFLICT(session_id) DO UPDATE SET " + ", ".join(f"{k}=excluded.{k}" for k in fields)
    else:
        sql += " ON CONFLICT(session_id) DO NOTHING"
    conn.execute(sql, [session_id] + list(fields.values()))


def add_hit(conn: sqlite3.Connection, session_id: str, entry_uuid: str | None, ts: datetime | str | None,
            kind: str | None, reset_at: datetime | str | None, text: str | None) -> bool:
    """Record one limit notice. Returns True if it was new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO limit_hits(session_id, entry_uuid, ts, kind, reset_at, text) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, entry_uuid, iso(ts), kind, iso(reset_at), text))
    return cur.rowcount > 0


def stalled_sessions(conn: sqlite3.Connection, include_resolved: bool = False) -> list[Row]:
    """Currently stalled sessions (or, with include_resolved, every session
    that ever hit a limit), with their hit counts, newest stall first."""
    where = "s.session_id IN (SELECT session_id FROM limit_hits)" if include_resolved else "s.stalled = 1"
    rows = conn.execute(f"""
        SELECT s.*, (SELECT COUNT(*) FROM limit_hits h WHERE h.session_id = s.session_id) AS hits,
               (SELECT MAX(ts) FROM limit_hits h WHERE h.session_id = s.session_id) AS last_hit
        FROM sessions s WHERE {where}
        ORDER BY s.stalled DESC, COALESCE(s.stalled_since, last_hit) DESC""").fetchall()
    return [dict(r) for r in rows]


def find_sessions(conn: sqlite3.Connection, prefix: str) -> list[Row]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM sessions WHERE session_id LIKE ? ORDER BY session_id", (prefix + "%",))]


def session_history(conn: sqlite3.Connection, session_id: str) -> list[Row]:
    """All limit hits of one session, oldest first."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM limit_hits WHERE session_id=? ORDER BY ts, id", (session_id,))]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {"sessions": one("SELECT COUNT(*) FROM sessions"),
            "hits": one("SELECT COUNT(*) FROM limit_hits"),
            "sessions_with_hits": one("SELECT COUNT(DISTINCT session_id) FROM limit_hits"),
            "stalled": one("SELECT COUNT(*) FROM sessions WHERE stalled=1")}


# ---------------------------------------------------------------- projects, tasks & decisions (v2/v3)
#
# Execution order (list_tasks, next_ready_task): priority level first (all high
# stages, then all medium, then all low); within a level by project rank, then
# stage_seq. Tasks without a project come after every project of their level.
# Only pending tasks are ready; blocked/in_progress/done ones are skipped (they
# do not hold back the later stages of their project).

TASK_STATUSES = ("pending", "in_progress", "blocked", "done", "cancelled")
OPEN_STATUSES = ("pending", "in_progress", "blocked")
TASK_KINDS = ("task", "backlog_project")       # what add_task / update_task accept
QUESTION_KIND = "question"   # v4: a manager question (ask_question), born blocked, closed by its answer
ALL_TASK_KINDS = TASK_KINDS + (QUESTION_KIND,)
PRIORITIES = ("high", "medium", "low")          # order = execution order
DEFAULT_PRIORITY = "high"
DECISIONS = ("continue", "ignore")
RULE_SCOPES = ("session", "project")
TASK_EDITABLE = ("title", "description", "project", "priority", "kind")
PROJECT_EDITABLE = ("name", "description", "path", "manager_session")

_PRIO_SQL = "CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"
TASK_ORDER_SQL = f"{_PRIO_SQL}, p.rank IS NULL, p.rank, t.stage_seq, t.created_at, t.id"
TASK_SELECT = ("SELECT t.*, p.name AS project_name, p.rank AS project_rank "
               "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id")


class NotFound(LookupError):
    pass


class InvalidTransition(ValueError):
    """The task (or session) is not in a state that allows this change."""


def _utcnow() -> datetime:            # patched by tests
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """Current UTC time, always with milliseconds: 2026-09-29T10:00:00.000Z."""
    dt = _utcnow().astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT (a savepoint inside a caller's transaction). A new
    transaction checks the schema guard first and again under the write lock (a newer
    process may have upgraded the database meanwhile): SchemaMismatch, nothing written."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT afclaude_tx")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK TO afclaude_tx")
            conn.execute("RELEASE afclaude_tx")
            raise
        conn.execute("RELEASE afclaude_tx")
    else:
        check_writable(conn)        # the clear error, before BEGIN fails on a read-only connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            check_writable(conn)
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _enum(value: object, allowed: Sequence[str], what: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{what} must be one of {'|'.join(allowed)}, got {value!r}")
    return value


@overload
def _text(value: object, what: str, required: Literal[True]) -> str: ...
@overload
def _text(value: object, what: str, required: bool = ...) -> str | None: ...
def _text(value: object, what: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{what} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string, got {type(value).__name__}")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{what} must not be empty")
    return value or None


def _priority(value: object) -> str:
    """high|medium|low (case and surrounding blanks ignored)."""
    if not isinstance(value, str):
        raise ValueError(f"priority must be one of {'|'.join(PRIORITIES)}, got {value!r}")
    return _enum(value.strip().lower(), PRIORITIES, "priority")


def _clean_task_field(name: str, value: object) -> str | None:
    if name == "title":
        return _text(value, "title", required=True)
    if name == "priority":
        return _priority(value)
    if name == "kind":
        if value == QUESTION_KIND:
            raise ValueError("kind 'question' is only for questions (ask_question), not an edit")
        return _enum(value, TASK_KINDS, "kind")
    return _text(value, name)


def _position(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer, got {value!r}")
    return value


def _event(conn: sqlite3.Connection, task_id: int, ts: str, event: str, detail: object = None) -> None:
    conn.execute("INSERT INTO task_events(task_id, ts, event, detail) VALUES (?, ?, ?, ?)",
                 (task_id, ts, event, None if detail is None else json.dumps(detail, ensure_ascii=False)))


def _task_dict(r: sqlite3.Row) -> Row:
    """A task row as the API returns it: 'project' is the project's NAME (it
    replaces the legacy v2 free-text column; 'project_name' is the same value,
    kept for readers like export_quickview.py), plus 'project_rank'."""
    d = dict(r)
    d["project"] = d["project_name"]
    return d


def _task_row(conn: sqlite3.Connection, task_id: int) -> Row:
    r = conn.execute(TASK_SELECT + " WHERE t.id=?", (task_id,)).fetchone()
    if r is None:
        raise NotFound(f"no task #{task_id}")
    return _task_dict(r)


def get_task(conn: sqlite3.Connection, task_id: int) -> Row | None:
    r = conn.execute(TASK_SELECT + " WHERE t.id=?", (task_id,)).fetchone()
    return _task_dict(r) if r else None


# ---- projects

def _norm_path(p: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(p)))


def _project_by_path(conn: sqlite3.Connection, path: str, nearest: bool = True) -> Row | None:
    """The project whose path is `path`, or (nearest) the one with the longest
    path that is a parent of it."""
    path = _norm_path(path)
    r = conn.execute("SELECT * FROM projects WHERE path=?", (path,)).fetchone()
    if r or not nearest:
        return dict(r) if r else None
    best: Row | None = None
    for r in conn.execute("SELECT * FROM projects WHERE path IS NOT NULL"):
        if path.startswith(r["path"].rstrip("/") + "/") and (best is None or len(r["path"]) > len(best["path"])):
            best = dict(r)
    return best


def get_project(conn: sqlite3.Connection, ref: object, nearest: bool = True) -> Row | None:
    """By id (int), by path (a string starting with / or ~: that directory's
    project, or with nearest the closest parent's) or by name. None if unknown."""
    if isinstance(ref, int) and not isinstance(ref, bool):
        r = conn.execute("SELECT * FROM projects WHERE id=?", (ref,)).fetchone()
        return dict(r) if r else None
    ref = _text(ref, "project", required=True)
    if ref.startswith(("/", "~")):
        return _project_by_path(conn, ref, nearest)
    r = conn.execute("SELECT * FROM projects WHERE name=?", (ref,)).fetchone()
    return dict(r) if r else None


def _project_row(conn: sqlite3.Connection, ref: object) -> Row:
    p = get_project(conn, ref)
    if p is None:
        raise NotFound(f"no project {ref!r}")
    return p


def _free_name(conn: sqlite3.Connection, *candidates: str) -> str:
    for c in candidates:
        if not conn.execute("SELECT 1 FROM projects WHERE name=?", (c,)).fetchone():
            return c
    base, i = candidates[-1], 2
    while conn.execute("SELECT 1 FROM projects WHERE name=?", (f"{base} ({i})",)).fetchone():
        i += 1
    return f"{base} ({i})"


def _rowid(cur: sqlite3.Cursor) -> int:
    """The id an INSERT just created."""
    rid = cur.lastrowid
    if rid is None:             # never after a successful INSERT
        raise sqlite3.InterfaceError("the INSERT returned no row id")
    return rid


def add_project(conn: sqlite3.Connection, name: object, description: object = None, path: object = None,
                rank: object = None) -> Row:
    """New project at the bottom of the list (or at `rank`, pushing the others down)."""
    name = _text(name, "name", required=True)
    if name.startswith(("/", "~")):
        raise ValueError(f"a project name can't start with / or ~ (that's a path): {name!r}")
    path = _norm_path(path) if _text(path, "path") and isinstance(path, str) else None
    if rank is not None:
        _position(rank, "rank")
    with transaction(conn):
        if get_project(conn, name):
            raise ValueError(f"project {name!r} already exists")
        other = _project_by_path(conn, path, nearest=False) if path else None
        if other:
            raise ValueError(f"project {other['name']!r} already has path {path}")
        ts = now_iso()
        n = conn.execute("SELECT COALESCE(MAX(rank), 0) FROM projects").fetchone()[0]
        cur = conn.execute("INSERT INTO projects(name, rank, description, path, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (name, n + 1, _text(description, "description"),
                                                         path, ts, ts))
        pid = _rowid(cur)
        if rank is not None:
            move_project(conn, pid, rank)
        return _project_row(conn, pid)


def _ensure_project(conn: sqlite3.Connection, ref: object, nearest: bool = True) -> Row:
    """The project `ref` names; a new one (at the bottom) if there is none. A
    path creates a project named after its last component (if that name is
    taken: parent/last, then the whole path without the leading /)."""
    p = get_project(conn, ref, nearest)
    if p:
        return p
    ref = _text(ref, "project", required=True)
    if ref.startswith(("/", "~")):
        path = _norm_path(ref)
        parts = [x for x in path.split("/") if x] or ["root"]
        return add_project(conn, _free_name(conn, parts[-1], "/".join(parts[-2:]), "/".join(parts)), path=path)
    return add_project(conn, ref)


def project_for_path(conn: sqlite3.Connection, path: str, create: bool = True) -> Row | None:
    """The project of a working directory: the one with that path or the
    nearest parent path; with create, a new one named after the directory."""
    return _ensure_project(conn, path) if create else _project_by_path(conn, path)


def list_projects(conn: sqlite3.Connection) -> list[Row]:
    """Projects by rank, each with 'open' (number of open stages) and
    'ready' {high, medium, low}: pending stages per priority."""
    out: list[Row] = []
    for r in conn.execute("SELECT * FROM projects ORDER BY rank, id").fetchall():
        d = dict(r)
        cnt = dict(conn.execute("SELECT status || ':' || priority, COUNT(*) FROM tasks WHERE project_id=? "
                                "GROUP BY status, priority", (d["id"],)).fetchall())
        d["open"] = sum(n for k, n in cnt.items() if k.split(":")[0] in OPEN_STATUSES)
        d["ready"] = {lvl: cnt.get(f"pending:{lvl}", 0) for lvl in PRIORITIES}
        out.append(d)
    return out


def update_project(conn: sqlite3.Connection, ref: object, **fields: object) -> Row:
    """Rename a project or change its description/path/manager_session. A
    manager_session (full session id; empty = none) makes the project managed:
    the dispatcher keeps that session alive and starts no task sessions for it."""
    bad = set(fields) - set(PROJECT_EDITABLE)
    if bad:
        raise ValueError(f"not editable: {sorted(bad)} (editable: {', '.join(PROJECT_EDITABLE)}; "
                         "order changes go through move_project)")
    with transaction(conn):
        p = _project_row(conn, ref)
        clean: dict[str, str | None] = {}
        if "name" in fields:
            name = _text(fields["name"], "name", required=True)
            if name.startswith(("/", "~")):
                raise ValueError(f"a project name can't start with / or ~: {name!r}")
            other = get_project(conn, name)
            if other and other["id"] != p["id"]:
                raise ValueError(f"project {name!r} already exists")
            clean["name"] = name
        if "description" in fields:
            clean["description"] = _text(fields["description"], "description")
        if "path" in fields:
            raw = fields["path"]
            path = _norm_path(raw) if _text(raw, "path") and isinstance(raw, str) else None
            clean["path"] = path
            other = _project_by_path(conn, path, nearest=False) if path else None
            if other and other["id"] != p["id"]:
                raise ValueError(f"project {other['name']!r} already has path {path}")
        if "manager_session" in fields:
            clean["manager_session"] = _text(fields["manager_session"], "manager_session")
        clean = {k: v for k, v in clean.items() if p[k] != v}
        if clean:
            conn.execute(f"UPDATE projects SET {', '.join(f'{k}=?' for k in clean)}, updated_at=? WHERE id=?",
                         list(clean.values()) + [now_iso(), p["id"]])
        return _project_row(conn, p["id"])


def _reorder(ids: list[int], item: int, new_pos: int) -> list[int]:
    """ids without item, item re-inserted at 1-based new_pos (clamped)."""
    rest = [i for i in ids if i != item]
    k = min(max(new_pos, 1), len(rest) + 1) - 1
    return rest[:k] + [item] + rest[k:]


def move_project(conn: sqlite3.Connection, ref: object, new_rank: object) -> Row:
    """Put a project at rank new_rank (1 = top, clamped to the list); the
    others shift. Ranks stay 1..n."""
    pos = _position(new_rank, "rank")
    with transaction(conn):
        p = _project_row(conn, ref)
        ids = [r[0] for r in conn.execute("SELECT id FROM projects ORDER BY rank, id")]
        ts = now_iso()
        for rank, pid in enumerate(_reorder(ids, p["id"], pos), 1):
            conn.execute("UPDATE projects SET rank=?, updated_at=CASE WHEN id=? THEN ? ELSE updated_at END "
                         "WHERE id=? AND rank IS NOT ?", (rank, p["id"], ts, pid, rank))
        return _project_row(conn, p["id"])


def set_project_priority(conn: sqlite3.Connection, ref: object, level: object) -> dict[str, Any]:
    """Bulk: give every OPEN stage of a project the same priority (e.g. the
    whole project -> medium, so none of it runs before the high stages of the
    other projects). Logs a 'priority' event (via: project) per changed stage.
    Returns {project, priority, changed: [task ids]}."""
    level = _priority(level)
    with transaction(conn):
        p = _project_row(conn, ref)
        rows = conn.execute(f"SELECT id, priority FROM tasks WHERE project_id=? AND priority != ? "
                            f"AND status IN ({', '.join('?' for _ in OPEN_STATUSES)}) ORDER BY stage_seq, id",
                            (p["id"], level) + OPEN_STATUSES).fetchall()
        ts = now_iso()
        for tid, old in rows:
            conn.execute("UPDATE tasks SET priority=?, updated_at=? WHERE id=?", (level, ts, tid))
            _event(conn, tid, ts, "priority", {"from": old, "to": level, "via": "project"})
        return {"project": p["name"], "priority": level, "changed": [r[0] for r in rows]}


# ---- tasks (the stages of a project)

@overload
def _next_seq(conn: sqlite3.Connection, project_id: int, table: str = ...) -> int: ...
@overload
def _next_seq(conn: sqlite3.Connection, project_id: int | None, table: str = ...) -> int | None: ...
def _next_seq(conn: sqlite3.Connection, project_id: int | None, table: str = "tasks") -> int | None:
    if project_id is None:
        return None
    seq: int = conn.execute(f"SELECT COALESCE(MAX(stage_seq), 0) + 1 FROM {table} WHERE project_id=?",
                            (project_id,)).fetchone()[0]
    return seq


def _compact_stages(conn: sqlite3.Connection, project_id: int | None) -> None:
    if project_id is None:
        return
    ids = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE project_id=? ORDER BY stage_seq, id",
                                      (project_id,))]
    for seq, tid in enumerate(ids, 1):
        conn.execute("UPDATE tasks SET stage_seq=? WHERE id=? AND stage_seq IS NOT ?", (seq, tid, seq))


def task_events(conn: sqlite3.Connection, task_id: int) -> list[Row]:
    """History of one task, oldest first; detail decoded from JSON."""
    out: list[Row] = []
    for r in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)):
        d = dict(r)
        d["detail"] = json.loads(d["detail"]) if d["detail"] else None
        out.append(d)
    return out


def add_task(conn: sqlite3.Connection, title: object, description: object = None, project: object = None,
             priority: object = DEFAULT_PRIORITY, kind: object = "task", created_by_session: object = None) -> Row:
    """New pending task, appended as the last stage of `project` (a name or a
    directory, see get_project; an unknown one is created at the bottom of the
    project list). project=None: no project."""
    if kind == QUESTION_KIND:
        raise ValueError("kind 'question' is created with ask_question (born blocked)")
    return _add_task(conn, title, description, project, priority, kind, created_by_session)


def _add_task(conn: sqlite3.Connection, title: object, description: object, project: object, priority: object,
              kind: object, created_by_session: object) -> Row:
    fields: dict[str, str | None] = {"title": _clean_task_field("title", title),
              "description": _clean_task_field("description", description),
              "priority": _priority(priority),
              "kind": _enum(kind, ALL_TASK_KINDS, "kind"),
              "created_by_session": _text(created_by_session, "created_by_session")}
    ref = _text(project, "project")
    with transaction(conn):
        p = _ensure_project(conn, ref) if ref else None
        pid = p["id"] if p else None
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO tasks(title, description, project_id, stage_seq, priority, status, kind, created_at, "
            "updated_at, created_by_session) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (fields["title"], fields["description"], pid, _next_seq(conn, pid), fields["priority"],
             fields["kind"], ts, ts, fields["created_by_session"]))
        tid = _rowid(cur)
        if p:
            fields["project"] = p["name"]
        _event(conn, tid, ts, "created", {k: v for k, v in fields.items() if v is not None})
        return _task_row(conn, tid)


def list_tasks(conn: sqlite3.Connection, status: str | Iterable[str] | None = None, project: object = None,
               kind: object = None, limit: int | None = None) -> list[Row]:
    """Tasks in execution order (see above). status: one value or a list.
    project: a name, id or directory (NotFound if there is no such project)."""
    where: list[str] = []
    args: list[Any] = []
    if status is not None:
        sts = [status] if isinstance(status, str) else list(status)
        for s in sts:
            _enum(s, TASK_STATUSES, "status")
        where.append(f"t.status IN ({', '.join('?' for _ in sts)})")
        args += sts
    if project is not None:
        where.append("t.project_id = ?")
        args.append(_project_row(conn, project)["id"])
    if kind is not None:
        where.append("t.kind = ?")
        args.append(_enum(kind, ALL_TASK_KINDS, "kind"))
    sql = TASK_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + TASK_ORDER_SQL
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [_task_dict(r) for r in conn.execute(sql, args)]


def execution_order(conn: sqlite3.Connection, kind: object = None, project: object = None,
                    limit: int | None = None) -> list[Row]:
    """The ready queue: pending tasks in the order the dispatcher takes them."""
    return list_tasks(conn, status="pending", kind=kind, project=project, limit=limit)


def next_ready_task(conn: sqlite3.Connection, kind: object = None, project: object = None) -> Row | None:
    """The pending task that should run next (or None). Read-only: claim it with start_task."""
    r = execution_order(conn, kind=kind, project=project, limit=1)
    return r[0] if r else None


def _update(conn: sqlite3.Connection, task_id: int, event: str, fields: Mapping[str, object]) -> Row:
    clean = {k: _clean_task_field(k, v) for k, v in fields.items() if k != "project"}
    with transaction(conn):
        t = _task_row(conn, task_id)
        changes = {k: [t[k], v] for k, v in clean.items() if t[k] != v}
        sets = {k: v[1] for k, v in changes.items()}
        if "project" in fields:        # moves the task to the end of the other project
            ref = _text(fields["project"], "project")
            new = _ensure_project(conn, ref) if ref else None
            new_id = new["id"] if new else None
            if new_id != t["project_id"]:
                changes["project"] = [t["project"], new["name"] if new else None]
                sets.update(project_id=new_id, stage_seq=_next_seq(conn, new_id))
        if not changes:
            return t
        ts = now_iso()
        conn.execute(f"UPDATE tasks SET {', '.join(f'{k}=?' for k in sets)}, updated_at=? WHERE id=?",
                     list(sets.values()) + [ts, task_id])
        if "project_id" in sets:
            _compact_stages(conn, t["project_id"])
        if event == "priority":
            _event(conn, task_id, ts, "priority", {"from": changes["priority"][0], "to": changes["priority"][1]})
        else:
            _event(conn, task_id, ts, event, changes)
        return _task_row(conn, task_id)


def update_task(conn: sqlite3.Connection, task_id: int, **fields: object) -> Row:
    """Change title/description/project/priority/kind. Logs one 'updated'
    event with {field: [old, new]} (nothing is logged if nothing changed).
    A new project appends the task as that project's last stage."""
    bad = set(fields) - set(TASK_EDITABLE)
    if bad:
        raise ValueError(f"not editable: {sorted(bad)} (editable: {', '.join(TASK_EDITABLE)}; "
                         "status changes go through block/answer/start/finish/cancel/reopen)")
    return _update(conn, task_id, "updated", fields)


def set_stage_priority(conn: sqlite3.Connection, task_id: int, level: object) -> Row:
    """high|medium|low for one task (stage); logs a 'priority' event."""
    return _update(conn, task_id, "priority", {"priority": level})


set_priority = set_stage_priority


def move_stage(conn: sqlite3.Connection, task_id: int, new_seq: object) -> Row:
    """Put a task at position new_seq (1 = first, clamped) among its project's
    stages; the others shift. Logs a 'moved' event {from, to}."""
    pos = _position(new_seq, "stage")
    with transaction(conn):
        t = _task_row(conn, task_id)
        if t["project_id"] is None:
            raise InvalidTransition(f"task #{task_id} has no project, so it has no stage order")
        ids = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE project_id=? ORDER BY stage_seq, id",
                                          (t["project_id"],))]
        order = _reorder(ids, task_id, pos)
        if order == ids:
            _compact_stages(conn, t["project_id"])
            return _task_row(conn, task_id)
        for seq, tid in enumerate(order, 1):
            conn.execute("UPDATE tasks SET stage_seq=? WHERE id=?", (seq, tid))
        ts = now_iso()
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (ts, task_id))
        _event(conn, task_id, ts, "moved", {"from": t["stage_seq"], "to": order.index(task_id) + 1})
        return _task_row(conn, task_id)


_VERB = {"blocked": "block", "answered": "answer", "started": "start", "done": "finish",
         "cancelled": "cancel", "reopened": "reopen"}
NOW = object()   # placeholder for "the event timestamp" in _transition fields


def _transition(conn: sqlite3.Connection, task_id: int, allowed_from: Sequence[str], to_status: str, event: str,
                detail: Mapping[str, object] | None = None, **fields: object) -> Row:
    """Status change with a state check. Field values of NOW become the event timestamp."""
    with transaction(conn):
        t = _task_row(conn, task_id)
        if t["status"] not in allowed_from:
            raise InvalidTransition(f"task #{task_id} is {t['status']}; {_VERB.get(event, event)} "
                                    f"needs {' or '.join(allowed_from)}")
        ts = now_iso()
        fields = {k: (ts if v is NOW else v) for k, v in fields.items()}
        fields.update(status=to_status, updated_at=ts)
        conn.execute(f"UPDATE tasks SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                     list(fields.values()) + [task_id])
        d: dict[str, Any] = {"from": t["status"]}
        d.update(detail or {})
        _event(conn, task_id, ts, event, d)
        return _task_row(conn, task_id)



def block_task(conn: sqlite3.Connection, task_id: int, question: object) -> Row:
    """A run needs the user: park the task with a question (clears any old answer)."""
    q = _text(question, "question", required=True)
    return _transition(conn, task_id, ("pending", "in_progress"), "blocked", "blocked", {"question": q},
                       blocked_question=q, blocked_at=NOW, answer=None, answered_at=None)


def answer_task(conn: sqlite3.Connection, task_id: int, answer: object) -> Row:
    """The user's answer; the task is pending again (ready for the next pass).
    blocked_question and assigned_session stay, so the next run sees the Q&A
    and the dispatcher can resume the same session. A question (kind
    'question') is done once answered: there is nothing to run, the asking
    session reads the answer."""
    a = _text(answer, "answer", required=True)
    with transaction(conn):
        if _task_row(conn, task_id)["kind"] == QUESTION_KIND:
            return _transition(conn, task_id, ("blocked",), "done", "answered", {"answer": a},
                               answer=a, answered_at=NOW, done_at=NOW)
        return _transition(conn, task_id, ("blocked",), "pending", "answered", {"answer": a},
                           answer=a, answered_at=NOW)


def ask_question(conn: sqlite3.Connection, question: object, project: object = None,
                 created_by_session: object = None, title: object = None) -> Row:
    """v4: a question for the user from a (manager) session, as a task of kind
    'question' that is born blocked, so it is in the inbox like any blocked task.
    Its answer closes it (done); the dispatcher never starts it (it only takes
    kind 'task'). title defaults to the question's first line, cut at 120 chars."""
    q = _text(question, "question", required=True)
    if title is None:
        first = q.splitlines()[0].strip()
        title = first if len(first) <= 120 else first[:119] + "…"
    with transaction(conn):
        t = _add_task(conn, title, None, project, DEFAULT_PRIORITY, QUESTION_KIND, created_by_session)
        return block_task(conn, t["id"], q)


def start_task(conn: sqlite3.Connection, task_id: int, session: object) -> Row:
    """Claim a pending task for a session. Starting it again for the same
    session is a no-op; any other state raises InvalidTransition."""
    session = _text(session, "session")
    with transaction(conn):
        t = _task_row(conn, task_id)
        if t["kind"] == QUESTION_KIND:
            raise InvalidTransition(f"task #{task_id} is a question: answer it, it doesn't run")
        if t["status"] == "in_progress" and t["assigned_session"] == session:
            return t
        return _transition(conn, task_id, ("pending",), "in_progress", "started", {"session": session},
                           assigned_session=session)


def finish_task(conn: sqlite3.Connection, task_id: int, summary: object = None) -> Row:
    s = _text(summary, "summary")
    return _transition(conn, task_id, ("pending", "in_progress"), "done", "done", {"summary": s},
                       result_summary=s, done_at=NOW)


def cancel_task(conn: sqlite3.Connection, task_id: int, reason: object = None) -> Row:
    r = _text(reason, "reason")
    return _transition(conn, task_id, ("pending", "in_progress", "blocked"), "cancelled", "cancelled",
                       {"reason": r} if r else None, done_at=NOW)


def reopen_task(conn: sqlite3.Connection, task_id: int, reason: object = None) -> Row:
    """Back to pending from done/cancelled, or from in_progress (a run died). A
    question goes back to blocked (asked again, the old answer cleared)."""
    r = _text(reason, "reason")
    with transaction(conn):
        if _task_row(conn, task_id)["kind"] == QUESTION_KIND:
            return _transition(conn, task_id, ("done", "cancelled"), "blocked", "reopened",
                               {"reason": r} if r else None, done_at=None, answer=None, answered_at=None,
                               blocked_at=NOW)
        return _transition(conn, task_id, ("in_progress", "done", "cancelled"), "pending", "reopened",
                           {"reason": r} if r else None, done_at=None)


# ---- stalled-session decisions

def _stall_ref(sess: Mapping[str, Any] | None) -> str | None:
    ref: str | None = (sess or {}).get("stall_uuid") or (sess or {}).get("stalled_since")
    return ref


def decide_session(conn: sqlite3.Connection, session_id: str, decision: object, note: object = None) -> Row | None:
    """One-off continue/ignore for the session's CURRENT stall. A later stall
    of the same session is undecided again unless a standing rule covers it."""
    _enum(decision, DECISIONS, "decision")
    note = _text(note, "note")
    with transaction(conn):
        s = get_session(conn, session_id)
        if s is None:
            raise NotFound(f"unknown session {session_id}")
        if not s.get("stalled"):
            raise InvalidTransition(f"session {session_id[:8]} is not stalled; "
                                    "use a session-scoped standing rule for future stalls")
        conn.execute(
            "INSERT INTO session_decisions(session_id, decision, decided_at, note, stall_ref) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET decision=excluded.decision, "
            "decided_at=excluded.decided_at, note=excluded.note, stall_ref=excluded.stall_ref",
            (session_id, decision, now_iso(), note, _stall_ref(s)))
    return get_decision(conn, session_id)


def clear_decision(conn: sqlite3.Connection, session_id: str) -> bool:
    with transaction(conn):
        return conn.execute("DELETE FROM session_decisions WHERE session_id=?", (session_id,)).rowcount > 0


def get_decision(conn: sqlite3.Connection, session_id: str) -> Row | None:
    r = conn.execute("SELECT * FROM session_decisions WHERE session_id=?", (session_id,)).fetchone()
    return dict(r) if r else None


def _norm_match(scope: object, match: object) -> str:
    m = _text(match, "match", required=True)
    if scope == "project" and m.startswith("/"):
        m = os.path.normpath(m)
    return m


def add_rule(conn: sqlite3.Connection, scope: object, match: object, decision: object, note: object = None) -> Row:
    """Standing rule. The same scope+match again replaces the old rule."""
    _enum(scope, RULE_SCOPES, "scope")
    _enum(decision, DECISIONS, "decision")
    m = _norm_match(scope, match)
    with transaction(conn):
        conn.execute(
            "INSERT INTO standing_rules(scope, match, decision, created_at, note) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(scope, match) DO UPDATE SET decision=excluded.decision, "
            "created_at=excluded.created_at, note=excluded.note",
            (scope, m, decision, now_iso(), _text(note, "note")))
        return dict(conn.execute("SELECT * FROM standing_rules WHERE scope=? AND match=?", (scope, m)).fetchone())


def remove_rule(conn: sqlite3.Connection, rule_id: object) -> None:
    with transaction(conn):
        if conn.execute("DELETE FROM standing_rules WHERE id=?", (rule_id,)).rowcount == 0:
            raise NotFound(f"no rule #{rule_id}")


def list_rules(conn: sqlite3.Connection) -> list[Row]:
    return [dict(r) for r in conn.execute("SELECT * FROM standing_rules ORDER BY scope, match")]


def _project_rule_rank(rule: Mapping[str, Any], sess: Mapping[str, Any]) -> tuple[int, int, int] | None:
    """None if the project rule doesn't match; else a sort key (higher = more specific).
    Path rules match the session's cwd and its subdirectories (longest path wins)
    and beat project_dir-name rules."""
    m = rule["match"]
    if m.startswith("/"):
        cwd = sess.get("cwd") or ""
        if cwd == m or cwd.startswith(m.rstrip("/") + "/"):
            return (1, len(m), rule["id"])
        return None
    return (0, 0, rule["id"]) if m == sess.get("project_dir") else None


def _effective(sess: Mapping[str, Any], decision: Mapping[str, Any] | None,
               rules: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    if decision and decision.get("stall_ref") == _stall_ref(sess):
        return decision["decision"], "session"
    for r in rules:
        if r["scope"] == "session" and r["match"] == sess["session_id"]:
            return r["decision"], f"session_rule:{r['id']}"
    ranked = [(k, r) for r in rules if r["scope"] == "project"
              for k in [_project_rule_rank(r, sess)] if k is not None]
    if ranked:
        r = max(ranked, key=lambda kr: kr[0])[1]
        return r["decision"], f"project_rule:{r['id']}"
    return None, None


def effective_decision(conn: sqlite3.Connection, session_id: str) -> tuple[str | None, str | None]:
    """(decision, source) for a session: its own decision for the current stall
    ('session'), else a session rule ('session_rule:<id>'), else the most
    specific project rule ('project_rule:<id>'), else (None, None) = undecided."""
    sess = get_session(conn, session_id) or {"session_id": session_id}
    return _effective(sess, get_decision(conn, session_id), list_rules(conn))


def stalled_decisions(conn: sqlite3.Connection) -> list[Row]:
    """Every currently stalled session with 'decision' and 'decision_source'
    (None = undecided). The dispatcher resumes those with decision == 'continue'."""
    rules = list_rules(conn)
    decisions = {r["session_id"]: dict(r) for r in conn.execute("SELECT * FROM session_decisions")}
    out: list[Row] = []
    for s in stalled_sessions(conn):
        s["decision"], s["decision_source"] = _effective(s, decisions.get(s["session_id"]), rules)
        out.append(s)
    return out


def pending_user_input(conn: sqlite3.Connection, include_own: bool = False) -> dict[str, list[Row]]:
    """Everything waiting for the user: blocked tasks (priority order) and
    stalled sessions with no decision (newest stall first). Undecided stalls
    never expire. AFClaude's own sessions are left out by default: the
    keep-alive already handles them."""
    return {"blocked_tasks": list_tasks(conn, status="blocked"),
            "undecided_sessions": [s for s in stalled_decisions(conn) if s["decision"] is None
                                   and (include_own or not s.get("own"))]}


# ---------------------------------------------------------------- v4: settings, prompts, audit, runner tables
#
# Low-level rows only. Validation, defaults, version checks, the audit row and
# idempotency are actions.py's job: every write from the CLI, the MCP server and
# the dashboard goes through actions.perform().

def _json_or_none(v: object) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, sort_keys=True)


def _loads(s: str | None) -> Any:
    return None if s is None else json.loads(s)


def get_setting_row(conn: sqlite3.Connection, key: str) -> Row | None:
    """{key, value (decoded; None = the code default), version, updated_at, updated_by},
    or None if the key was never saved (version 0)."""
    r = conn.execute("SELECT * FROM settings WHERE key=?", (key,)).fetchone()
    return dict(r, value=json.loads(r["value"])) if r else None


def setting_rows(conn: sqlite3.Connection) -> dict[str, Row]:
    return {r["key"]: dict(r, value=json.loads(r["value"]))
            for r in conn.execute("SELECT * FROM settings ORDER BY key")}


def put_setting(conn: sqlite3.Connection, key: str, value: object, updated_by: str | None = None) -> Row | None:
    conn.execute("INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
                 "updated_by=excluded.updated_by", (key, json.dumps(value, ensure_ascii=False), now_iso(),
                                                    updated_by))
    return get_setting_row(conn, key)


def reset_setting(conn: sqlite3.Connection, key: str, updated_by: str | None = None) -> Row | None:
    """Back to the code default: the value becomes JSON null (the row and its version stay)."""
    return put_setting(conn, key, None, updated_by)


def get_prompt_override(conn: sqlite3.Connection, name: str) -> Row | None:
    r = conn.execute("SELECT * FROM prompt_overrides WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def list_prompt_overrides(conn: sqlite3.Connection) -> list[Row]:
    """The prompts that currently have an override (body not NULL)."""
    return [dict(r) for r in conn.execute("SELECT * FROM prompt_overrides WHERE body IS NOT NULL ORDER BY name")]


def put_prompt_override(conn: sqlite3.Connection, name: str, body: str, base_sha256: str | None,
                        updated_by: str | None = None) -> Row | None:
    conn.execute("INSERT INTO prompt_overrides(name, body, base_sha256, updated_at, updated_by) "
                 "VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET body=excluded.body, "
                 "base_sha256=excluded.base_sha256, updated_at=excluded.updated_at, "
                 "updated_by=excluded.updated_by", (name, body, base_sha256, now_iso(), updated_by))
    return get_prompt_override(conn, name)


def reset_prompt_override(conn: sqlite3.Connection, name: str, updated_by: str | None = None) -> Row | None:
    """Back to the default file: body and base_sha256 NULL (the row and its version stay)."""
    conn.execute("UPDATE prompt_overrides SET body=NULL, base_sha256=NULL, updated_at=?, updated_by=? "
                 "WHERE name=?", (now_iso(), updated_by, name))
    return get_prompt_override(conn, name)


def add_audit(conn: sqlite3.Connection, actor: str, via: str, action: str, target_type: str | None = None,
              target_id: object = None, before: object = None, after: object = None,
              request_id: str | None = None, ts: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO audit_log(ts, actor, via, action, target_type, target_id, before, after, request_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts or now_iso(), actor, via, action, target_type, None if target_id is None else str(target_id),
         _json_or_none(before), _json_or_none(after), request_id))
    return _rowid(cur)


def audit_log(conn: sqlite3.Connection, limit: int = 50, target_type: str | None = None,
              target_id: object = None) -> list[Row]:
    """Newest first; before/after decoded."""
    where: list[str] = []
    args: list[Any] = []
    if target_type is not None:
        where.append("target_type=?")
        args.append(target_type)
    if target_id is not None:
        where.append("target_id=?")
        args.append(str(target_id))
    sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "")
    sql += f" ORDER BY id DESC LIMIT {int(limit)}"
    return [dict(r, before=_loads(r["before"]), after=_loads(r["after"])) for r in conn.execute(sql, args)]


def get_idempotency(conn: sqlite3.Connection, key: str) -> Row | None:
    r = conn.execute("SELECT * FROM idempotency_keys WHERE key=?", (key,)).fetchone()
    return dict(r) if r else None


def put_idempotency(conn: sqlite3.Connection, key: str, actor: str, action: str, request_sha256: str,
                    response: object) -> None:
    conn.execute("INSERT INTO idempotency_keys(key, actor, action, request_sha256, response, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (key, actor, action, request_sha256, json.dumps(response, ensure_ascii=False), now_iso()))


def prune_idempotency(conn: sqlite3.Connection, older_than: datetime) -> int:
    """Delete keys created before `older_than` (a datetime). -> number deleted."""
    return conn.execute("DELETE FROM idempotency_keys WHERE created_at < ?", (iso(older_than),)).rowcount


def add_run_log(conn: sqlite3.Connection, component: object, decision: object, reason: str | None = None,
                session_id: str | None = None, task_id: int | None = None, ts: datetime | str | None = None) -> int:
    """One keep-alive/dispatcher decision (written by the runners from phase 2 on)."""
    cur = conn.execute("INSERT INTO run_log(ts, component, session_id, task_id, decision, reason) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (iso(ts) if ts else now_iso(), _text(component, "component", required=True), session_id,
                        task_id, _text(decision, "decision", required=True), reason))
    return _rowid(cur)


def run_log(conn: sqlite3.Connection, component: str | None = None, limit: int = 50) -> list[Row]:
    if component is None:
        rows = conn.execute(f"SELECT * FROM run_log ORDER BY id DESC LIMIT {int(limit)}")
    else:
        rows = conn.execute(f"SELECT * FROM run_log WHERE component=? ORDER BY id DESC LIMIT {int(limit)}",
                            (component,))
    return [dict(r) for r in rows]


DRIVEN_FIELDS = ("tmux", "kind", "task_id", "started_at", "last_seen", "ended_at", "holder_kind", "rc_url")


def upsert_driven_session(conn: sqlite3.Connection, session_id: str, **fields: Any) -> Row | None:
    """Insert (kind and started_at needed then) or update the given fields."""
    bad = set(fields) - set(DRIVEN_FIELDS)
    if bad:
        raise ValueError(f"unknown driven_sessions fields: {sorted(bad)}")
    fields = {k: iso(v) if isinstance(v, datetime) else v for k, v in fields.items()}
    if get_driven_session(conn, session_id) is None:
        fields.setdefault("started_at", now_iso())
        if not fields.get("kind"):
            raise ValueError("a new driven session needs kind")
        cols = ["session_id"] + list(fields)
        conn.execute(f"INSERT INTO driven_sessions ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                     [session_id] + list(fields.values()))
    elif fields:
        conn.execute(f"UPDATE driven_sessions SET {', '.join(f'{k}=?' for k in fields)} WHERE session_id=?",
                     list(fields.values()) + [session_id])
    return get_driven_session(conn, session_id)


def get_driven_session(conn: sqlite3.Connection, session_id: str) -> Row | None:
    r = conn.execute("SELECT * FROM driven_sessions WHERE session_id=?", (session_id,)).fetchone()
    return dict(r) if r else None


def driven_sessions(conn: sqlite3.Connection, include_ended: bool = True) -> list[Row]:
    sql = "SELECT * FROM driven_sessions" + ("" if include_ended else " WHERE ended_at IS NULL")
    return [dict(r) for r in conn.execute(sql + " ORDER BY started_at DESC, session_id")]


def open_request(conn: sqlite3.Connection, kind: str, target: str | None = None) -> Row | None:
    r = conn.execute("SELECT * FROM action_requests WHERE status='open' AND kind=? AND COALESCE(target, '')=?",
                     (kind, target or "")).fetchone()
    return dict(r) if r else None


def add_request(conn: sqlite3.Connection, actor: str, kind: str, target: str | None = None) -> Row:
    cur = conn.execute("INSERT INTO action_requests(ts, actor, kind, target) VALUES (?, ?, ?, ?)",
                       (now_iso(), actor, kind, target))
    return dict(conn.execute("SELECT * FROM action_requests WHERE id=?", (cur.lastrowid,)).fetchone())


def list_requests(conn: sqlite3.Connection, status: str | None = None, limit: int = 50) -> list[Row]:
    if status is None:
        rows = conn.execute(f"SELECT * FROM action_requests ORDER BY id DESC LIMIT {int(limit)}")
    else:
        rows = conn.execute(f"SELECT * FROM action_requests WHERE status=? ORDER BY id DESC LIMIT {int(limit)}",
                            (status,))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- the DB error path (§7.8, D-171)
#
# Every DB access goes through one error path: retrying() (connect() and actions.perform use
# it). Transient errors (SQLITE_BUSY/LOCKED, a short I/O error) are retried with backoff
# (RETRY_DELAYS: 5 tries, each waiting up to BUSY_TIMEOUT for a lock), then count as
# persistent. Caller errors (a constraint, validation, a version conflict) are never retried
# and reach the caller as they are (an IntegrityError as a ValueError). Persistent ones (disk
# full, read-only, corrupt, schema newer than the code or needing a migration, DB missing)
# raise DBError: nothing is written.
#
# A missing database is persistent too, by design (D-171, D-187): connect() never creates the
# file as a side effect (kind "missing", raised unless the caller passes create=True, which
# only the explicit `python3 store.py init` / first-install path and the tests use); a lost or
# mis-mounted data volume must stop writes and escalate, not quietly start over empty and pass
# every later health check. init() also stamps a one-time meta.install_id the moment a database
# is truly new, so health() can tell that from a database that came back empty after really
# holding data (kind "replaced": no install_id, no rows, but the data directory still has a
# schema .bak or PRIOR_INSTALL_FILES left over from before).
# Escalation can't depend on the DB: report_db_error() records a failed action (actor, action,
# payload hash and payload, time) in the fallback file ALERTS.fallback.md next to the DB (the
# data volume; "never lose the write": it can be replayed or dropped, and is imported into the
# DB later) and alerts the owner (notify.py) only when the same request failed again (the
# session's own retry, D-171: escalate only if trying again doesn't fix it), once per episode.
# The runners call db_gate() before autonomous starts: a broken DB pauses them and alerts once
# per episode; the first check that finds it healthy again closes the episode with an alert
# that automation resumes.

# 5 tries with 7.5 s of backoff; each try waits up to BUSY_TIMEOUT for a lock (SQLite's busy
# handler), so a lock held throughout fails after ~30 s, as the single 30-s wait did before
# (the stalled-session scan holds the write lock for its whole pass).
BUSY_TIMEOUT = 5.0
RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)
MIN_FREE_BYTES = 4 * 1024 * 1024                         # less free space = disk full (health())
MISSING_RECHECK_DELAY = 0.2   # health(): one re-check after this short wait before reporting
                              # kind "missing"/"unable to open" (owner D-171 addendum: escalate
                              # only if trying again doesn't fix it) -- a one-off open failure
                              # (e.g. a momentary mount hiccup) must not by itself trigger an alert
FALLBACK_NAME = "ALERTS.fallback.md"
FALLBACK_MARK = "<!-- afclaude-fallback "
FALLBACK_HEADER = ("# AFClaude: database failures recorded outside the database (§7.8)\n\n"
                   "Written only while the database is broken: failed actions (replay or drop them), "
                   "the alerts sent and when it was healthy again. Imported into the database later.\n\n")
PAYLOAD_MAX = 4096                                       # characters of a payload kept for a replay
NOTIFY: Callable[[str, str], object] | None = None       # None = notify.notify (tests set a recorder)
NEXT_STEP = ("Wait about 30 seconds and retry this once. If it fails again, stop the work that needs the "
             "AFClaude database, don't work around it (no hand-edited files or other stores instead), and "
             "tell the user it needs their intervention (they are alerted outside the database).")
_sleep = time.sleep                                      # patched by tests

# (sqlite error name prefixes, message fragments, class, kind): the first match wins
_SQLITE_CLASSES: tuple[tuple[tuple[str, ...], tuple[str, ...], ErrorClass, str], ...] = (
    (("SQLITE_READONLY",), ("readonly database", "read-only"), "persistent", "read_only"),
    (("SQLITE_BUSY", "SQLITE_LOCKED"), ("is locked", "database is busy"), "transient", "locked"),
    (("SQLITE_FULL",), ("disk is full",), "persistent", "disk_full"),
    (("SQLITE_CORRUPT", "SQLITE_NOTADB"), ("malformed", "not a database"), "persistent", "corrupt"),
    (("SQLITE_CANTOPEN",), ("unable to open database",), "persistent", "missing"),
    (("SQLITE_IOERR",), ("disk i/o error",), "transient", "io"),
    (("SQLITE_CONSTRAINT",), ("constraint failed",), "caller", "constraint"),
)


class DBUnavailable(DBError):
    """A persistent SQLite failure (or a transient one that outlasted the retries)."""


def classify(exc: BaseException) -> tuple[ErrorClass, str] | None:
    """(class, kind) of an error on the DB error path; None = neither a DB nor a caller
    error (a bug: let it raise)."""
    if isinstance(exc, DBError):
        return exc.error_class, exc.kind
    if isinstance(exc, sqlite3.Error):
        name = str(getattr(exc, "sqlite_errorname", "") or "")   # Python >= 3.11
        msg = str(exc).lower()
        for names, frags, cls, kind in _SQLITE_CLASSES:
            if name.startswith(names) or any(f in msg for f in frags):
                return cls, kind
        if isinstance(exc, sqlite3.IntegrityError):
            return "caller", "constraint"
        return "persistent", "error"
    if isinstance(exc, (ValueError, LookupError)):   # validation, NotFound, InvalidTransition, Conflict
        return "caller", "invalid"
    return None


def as_db_error(exc: BaseException) -> DBError:
    """exc as a DBError (a raw sqlite3.Error from a path that didn't retry keeps its class)."""
    if isinstance(exc, DBError):
        return exc
    found: tuple[ErrorClass, str] = classify(exc) or ("persistent", "error")
    cls, kind = found
    err = DBUnavailable(f"database {kind.replace('_', ' ')}: {exc}", kind, cls)
    err.__cause__ = exc
    return err


def retrying(fn: Callable[[], _T], conn: sqlite3.Connection | None = None) -> _T:
    """Run fn() on the DB error path: a transient error is retried with backoff, then
    persistent; a caller error raises at once (an IntegrityError as a ValueError); a
    persistent one is a DBError (DBUnavailable). Inside a caller's open transaction there
    is one try only (the caller's transaction is the unit to repeat). -> fn()'s result."""
    tries = 1 if conn is not None and conn.in_transaction else len(RETRY_DELAYS) + 1
    attempt, waited = 0, 0.0
    while True:
        try:
            return fn()
        except DBError:
            raise
        except sqlite3.Error as e:
            found: tuple[ErrorClass, str] = classify(e) or ("persistent", "error")
            cls, kind = found
            if cls == "caller":
                raise ValueError(f"rejected by the database: {e}") from e
            attempt += 1
            if cls == "transient" and attempt < tries:
                _sleep(RETRY_DELAYS[attempt - 1])
                waited += RETRY_DELAYS[attempt - 1]
                continue
            if cls == "transient" and tries > 1:
                raise DBUnavailable(f"database {kind.replace('_', ' ')}: {e} (still failing after {attempt} "
                                    f"tries over {waited:g} s of backoff)", kind) from e
            raise DBUnavailable(f"database {kind.replace('_', ' ')}: {e}", kind,
                                "transient" if cls == "transient" else "persistent") from e


PRIOR_INSTALL_FILES = ("samples.jsonl",)   # a plain name in the data dir; a schema .bak is matched by prefix


def _prior_install_evidence(path: str) -> str | None:
    """A file in the data directory that only a previous install would have left behind (a
    `<db>.*.bak` schema backup, or one of PRIOR_INSTALL_FILES, e.g. the usage sampler's own
    history) -> its name, or None. Used to tell a freshly restored/mounted data directory
    (no such files) from one where the database itself went missing or was replaced while
    everything else survived."""
    d = os.path.dirname(path)
    base = os.path.basename(path)
    try:
        names = os.listdir(d)
    except OSError:
        return None
    baks = sorted(n for n in names if n.startswith(base + ".") and n.endswith(".bak"))
    if baks:
        return baks[0]
    return next((n for n in PRIOR_INSTALL_FILES if n in names), None)


def _looks_freshly_created(conn: sqlite3.Connection) -> bool:
    """No install marker (meta.install_id, stamped once by init() the moment a database is
    created, §init) and no real data in the tables a working install would have written to:
    this connection's database could be the current process's own first-ever init, or it
    could be a stand-in that quietly took the real one's place."""
    has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    if has_meta and get_meta(conn, "install_id") is not None:
        return False
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return not any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                   for t in ("sessions", "tasks", "projects", "settings") if t in tables)


def health(path: str | None = None) -> DBError | None:
    """Can this code write to the database? None = healthy, else the problem (persistent).
    Never creates or writes the database: missing; the file or its directory not writable
    (read-only file or file system); less than MIN_FREE_BYTES free; unreadable; PRAGMA
    quick_check failing (corruption); the schema guard (newer schema, migration needed);
    "replaced" (D-171): no install marker, no real data, yet the data directory has evidence
    of a previous install (a .bak backup or one of PRIOR_INSTALL_FILES) -- the database file
    itself looks like it was lost or swapped out and came back empty (a mis-mounted or
    restored-too-early data volume), so it is reported instead of accepted as healthy.
    "missing" (the file isn't there) or "unable to open" (SQLITE_CANTOPEN while connecting)
    gets one re-check after MISSING_RECHECK_DELAY before being reported: a one-off open
    failure (e.g. a momentary mount hiccup) must not by itself page the owner (D-171 addendum,
    "only if trying again doesn't fix it")."""
    path = os.path.abspath(path or DB_PATH)
    if not os.path.isfile(path):
        _sleep(MISSING_RECHECK_DELAY)
        if not os.path.isfile(path):
            return DBUnavailable(f"the database {path} is missing", "missing")
    d = os.path.dirname(path)
    if not (os.access(path, os.W_OK) and os.access(d, os.W_OK)):
        return DBUnavailable(f"the database {path} is read-only (the file or its file system is not writable)",
                             "read_only")
    try:
        fs = os.statvfs(d)
        if fs.f_bavail * fs.f_frsize < MIN_FREE_BYTES:
            return DBUnavailable(f"disk full: {fs.f_bavail * fs.f_frsize} bytes free for {path}", "disk_full")
    except OSError:
        pass

    def check() -> DBError | None:
        conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=BUSY_TIMEOUT)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            bad = [str(r[0]) for r in conn.execute("PRAGMA quick_check(5)")]
            if bad != ["ok"]:
                return DBUnavailable(f"the database {path} is corrupt (PRAGMA quick_check: "
                                     f"{'; '.join(bad)[:300]})", "corrupt")
            problem = schema_problem(conn)
            if problem is not None:
                return problem
            if _looks_freshly_created(conn):
                evidence = _prior_install_evidence(path)
                if evidence:
                    return DBUnavailable(
                        f"the database {path} has no install marker and no data, but the data "
                        f"directory has {evidence} (evidence of a previous install): it looks like "
                        "the database was lost or replaced and silently recreated empty", "replaced")
            return None
        finally:
            conn.close()

    def run() -> DBError | None:
        try:
            return retrying(check)
        except DBError as e:
            return _detached(e)
        except ValueError as e:
            return DBUnavailable(str(e), "error")
    result = run()
    if result is not None and result.kind == "missing":
        _sleep(MISSING_RECHECK_DELAY)
        result = run()
    return result


def _detached(e: DBError) -> DBError:
    """e without its tracebacks, so a returned (not raised) error doesn't keep the caller's
    frames alive (e.g. a runner's lock file) through a reference cycle."""
    for x in (e, e.__cause__, e.__context__):
        if x is not None:
            x.__traceback__ = None
    return e


# ---- the fallback alert file (escalation without the DB)

def fallback_path(db_path: str | None = None) -> str:
    """ALERTS.fallback.md next to the database (data/, the data volume)."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path or DB_PATH)), FALLBACK_NAME)


def _spare_dir() -> str:
    """A per-user directory for the spare fallback copy (store.py ~1891: not a bare predictable
    name directly in a shared /tmp): tempfile.gettempdir()/afclaude-<uid>/, created 0700 and
    verified to be a plain directory owned by us alone before any file under it is touched."""
    uid = os.getuid()
    d = os.path.join(tempfile.gettempdir(), f"afclaude-{uid}")
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"refusing to use {d}: not a plain directory")
    if st.st_uid != uid:
        raise OSError(f"refusing to use {d}: owned by uid {st.st_uid}, not us ({uid})")
    if stat.S_IMODE(st.st_mode) & 0o077:         # umask may have loosened a freshly created dir
        os.chmod(d, 0o700)
    return d


def _spare_path(path: str) -> str:
    """Where records go when the data volume itself can't be written (read-only, full): named
    by a hash of the real path, under the per-user spare directory (_spare_dir)."""
    return os.path.join(_spare_dir(), f"{hashlib.sha256(path.encode()).hexdigest()[:12]}-{FALLBACK_NAME}")


def _spare_or_none(path: str) -> str | None:
    """_spare_path(path), or None if the per-user spare directory can't be safely used."""
    try:
        return _spare_path(path)
    except OSError:
        return None


def _open_spare(p: str, mode: str) -> Any:
    """Open the spare fallback copy (a predictable name under a shared /tmp): O_NOFOLLOW
    refuses a symlink planted at that path rather than following it, and a freshly created
    file is 0600 (store.py ~1891)."""
    flags = os.O_NOFOLLOW | (os.O_RDONLY if mode == "r" else os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    fd = os.open(p, flags, 0o600)
    return os.fdopen(fd, mode, encoding="utf-8")


def fallback_events(path: str) -> list[Row]:
    """The records of a fallback file (and its spare copy), oldest first."""
    out: list[Row] = []
    candidates: list[tuple[str, bool]] = [(path, False)]
    spare = _spare_or_none(path)
    if spare is not None:
        candidates.append((spare, True))
    for p, is_spare in candidates:
        try:
            fh = _open_spare(p, "r") if is_spare else open(p, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                i = line.find(FALLBACK_MARK)
                if i < 0:
                    continue
                try:
                    rec = json.loads(line[i + len(FALLBACK_MARK):].rsplit("-->", 1)[0])
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    return sorted(out, key=lambda r: str(r.get("time", "")))


def _append_record(path: str, headline: str, rec: Mapping[str, Any]) -> str | None:
    """Append one record: a readable line plus its JSON (for the import / a replay).
    -> the file it went to (the spare copy if the data volume can't be written), else None
    (then it goes to stderr, the runner's log)."""
    js = json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str).replace("-->", "--\\u003e")
    text = f"- **{rec.get('time')}** {' '.join(headline.split())}\n  {FALLBACK_MARK}{js} -->\n"
    candidates: list[tuple[str, bool]] = [(path, False)]
    spare = _spare_or_none(path)
    if spare is not None:
        candidates.append((spare, True))
    for p, is_spare in candidates:
        try:
            new = not os.path.exists(p)
            if is_spare:
                with _open_spare(p, "a") as fh:
                    fh.write((FALLBACK_HEADER if new else "") + text)
            else:
                with open(p, "a", encoding="utf-8") as fh:
                    fh.write((FALLBACK_HEADER if new else "") + text)
            return p
        except OSError:
            continue
    sys.stderr.write(text)
    return None


def _open_alert(events: Sequence[Mapping[str, Any]], scope: str) -> Mapping[str, Any] | None:
    """The alert that opened the current episode within `scope` ("runner": db_gate's
    before-a-start pause/resume alerts; "request": report_db_error's failed-request alerts,
    store.py ~2021/2039); None = no open episode. Events of the other scope are ignored, so an
    interactive request failure can't open or close a runner-pause episode, and vice versa --
    one real episode can no longer suppress or wrongly "resume" the other. An event recorded
    before scopes existed has no "scope" field and is treated as "request" (report_db_error's
    longstanding behavior, the older and more common of the two)."""
    opened: Mapping[str, Any] | None = None
    for e in events:
        if e.get("event") not in ("alert", "recovered") or e.get("scope", "request") != scope:
            continue
        if e.get("event") == "alert" and opened is None:
            opened = e
        elif e.get("event") == "recovered":
            opened = None
    return opened


def _notify(subject: str, body: str) -> str:
    """notify.py (ALERTS.md, PROGRESS.md, a push), never raising."""
    try:
        fn = NOTIFY
        if fn is None:
            import importlib
            fn = getattr(importlib.import_module("notify"), "notify")
        return str(fn(subject, body))
    except Exception as e:   # noqa: BLE001 - an alert must never take the caller down
        return f"notify failed: {type(e).__name__}: {e}"


def _alert(path: str, subject: str, body: str, who: str, scope: str) -> str:
    """Alert the owner once per episode within `scope` ("runner" or "request", see
    _open_alert) -- the first alert in that scope opens it. -> what happened."""
    opened = _open_alert(fallback_events(path), scope)
    if opened is not None:
        return f"the owner was already alerted at {opened.get('time')} (one alert per episode)"
    _append_record(path, f"ALERT ({who}): {subject}",
                   {"event": "alert", "time": now_iso(), "who": who, "subject": subject, "scope": scope})
    _notify(subject, body)
    return "the owner has been alerted (notify.py)"


def report_db_error(err: DBError, *, actor: str, action: str, params: Mapping[str, Any] | None = None,
                    write: bool = True, db_path: str | None = None, payload_sha: str | None = None) -> DBError:
    """The escalation of a failed DB action (§7.8): record it in the fallback file (a write with
    its payload, so nothing is lost), and alert the owner when the same request by the same
    actor already failed in this episode (its retry failed too, D-171), once per episode.
    Sets err.not_saved and err.escalation (the hand-back). Never raises. -> err"""
    try:
        path = fallback_path(db_path)
        payload = json.dumps({"action": action, "params": dict(params or {})}, sort_keys=True,
                             ensure_ascii=False, default=str)
        sha = payload_sha or hashlib.sha256(payload.encode()).hexdigest()
        events = fallback_events(path)
        since = max((i for i, e in enumerate(events) if e.get("event") == "recovered"), default=-1)
        repeat = any(e.get("event") == "failed" and e.get("payload_sha256") == sha and e.get("actor") == actor
                     for e in events[since + 1:])
        rec: Row = {"event": "failed", "time": now_iso(), "actor": actor, "action": action, "write": write,
                    "payload_sha256": sha, "error_class": err.error_class, "kind": err.kind,
                    "message": str(err)[:500]}
        rec["params"] = dict(params or {}) if len(payload) <= PAYLOAD_MAX else None   # None: too big
        where = _append_record(path, f"failed {'write' if write else 'read'}: {action} by {actor} "
                                     f"({err.kind}: {str(err)[:200]})", rec)
        err.not_saved = ("nothing was changed by this call" if not write else
                         f"{action} was not applied; recorded in {where} (payload sha256 {sha[:12]}) for a replay"
                         if where else f"{action} was not applied")
        if repeat:
            err.escalation = _alert(path, f"AFClaude DB error ({err.kind}): {action} failed again",
                                    f"{actor}: {action} failed twice: {err}\nRecorded in {where or 'stderr'}; "
                                    "the database needs the owner's intervention.", actor, scope="request")
        else:
            opened = _open_alert(events, "request")
            err.escalation = (f"the owner was already alerted at {opened.get('time')}" if opened is not None else
                              "not escalated yet: if a retry of the same request fails too, the owner is alerted")
    except Exception as e:   # noqa: BLE001 - the escalation must never hide the error itself
        err.escalation = f"the escalation failed ({type(e).__name__}: {e})"
    return err


def handback(err: DBError) -> dict[str, str]:
    """What the calling session gets back (the MCP error object, the CLI's text): class, kind,
    message, what was not saved, the escalation and the suggested next step."""
    return {"error_class": err.error_class, "kind": err.kind, "message": str(err),
            "not_saved": err.not_saved or "unknown", "escalation": err.escalation or "none",
            "next_step": NEXT_STEP}


def handback_text(err: DBError) -> str:
    h = handback(err)
    return (f"error: database {h['kind']} ({h['error_class']}): {h['message']}\n"
            f"not saved: {h['not_saved']}\nescalation: {h['escalation']}\nnext step: {h['next_step']}")


def db_gate(component: str, db_path: str | None = None) -> DBError | None:
    """For the runners, before any autonomous start: None = the DB is healthy, go on; else the
    problem: pause (start nothing). The owner is alerted once per episode; the first healthy
    check after an alerted episode closes it with an alert that automation resumes. Never raises."""
    path = db_path or DB_PATH
    try:
        problem = health(path)
        fb = fallback_path(path)
        if problem is None:
            opened = _open_alert(fallback_events(fb), "runner")
            if opened is not None:
                _append_record(fb, f"RECOVERED ({component}): the database is healthy again; automation resumes",
                               {"event": "recovered", "time": now_iso(), "who": component, "scope": "runner"})
                _notify("AFClaude DB healthy again: automation resumes",
                        f"{component}: {path} passed its health check (problem since {opened.get('time')}: "
                        f"{opened.get('subject')}). Actions that failed meanwhile are recorded in {fb} "
                        "(replay or drop them).")
            return None
        problem.escalation = _alert(fb, f"AFClaude DB problem ({problem.kind}): automation paused",
                                    f"{component}: {problem}\nNo autonomous starts until the database is healthy "
                                    "again; AFClaude resumes on its own then (another alert says so). Failed "
                                    f"actions are recorded in {fb}.", component, scope="runner")
        return problem
    except Exception as e:   # noqa: BLE001 - a broken check pauses, it never crashes the runner
        return DBUnavailable(f"the database health check failed: {type(e).__name__}: {e}", "error")


# ---------------------------------------------------------------- CLI: the explicit migrate/init steps

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AFClaude store maintenance.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="create a new database (the explicit act connect() itself never "
                                    "performs: a first install, or a deliberate reset of a lost one)")
    i.add_argument("--db", default=None, help="database path (default: $AFCLAUDE_DB or data/afclaude.db)")
    m = sub.add_parser("migrate", help="back up the database, then run the migrations this code needs "
                                       "(the non-additive ones only run here)")
    m.add_argument("--db", default=None, help="database path (default: $AFCLAUDE_DB or data/afclaude.db)")
    args = ap.parse_args(argv)
    if args.cmd == "init":
        db = args.db or DB_PATH
        existed = os.path.isfile(db)
        try:
            connect(db, create=True).close()
        except DBError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"db": os.path.abspath(db), "schema_version": SCHEMA_VERSION,
                          "created": not existed}, ensure_ascii=False))
        return 0
    try:
        r = migrate(args.db)
    except (SchemaMismatch, NotFound) as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
