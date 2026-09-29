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

Times are stored as ISO-8601 UTC strings (transcript timestamps as written,
e.g. 2026-09-25T15:40:19.679Z; computed ones as 2026-09-25T17:00:00Z; task and
decision times always with milliseconds, so they sort as strings).

Task/decision functions run in their own transaction (BEGIN IMMEDIATE, so a
read-check-write like start_task is safe between processes) and commit. If the
caller already has an open transaction they run inside a savepoint instead and
the caller commits.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("AFCLAUDE_DB", os.path.join(HERE, "data", "afclaude.db"))
SCHEMA_VERSION = 3

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
    kind               TEXT NOT NULL DEFAULT 'task',     -- task|backlog_project
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    created_by_session TEXT,
    assigned_session   TEXT,            -- last session that started it
    blocked_question   TEXT,            -- kept after the answer, so the next run sees both
    blocked_at         TEXT,
    answer             TEXT,
    answered_at        TEXT,
    result_summary     TEXT,
    done_at            TEXT
)"""

PROJECTS_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    rank        INTEGER NOT NULL,        -- 1 = top; kept contiguous by the store, reordered by hand
    description TEXT,
    path        TEXT,                    -- directory it lives in (cwd of the creating session)
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
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
    stall_ref  TEXT                     -- sessions.stall_uuid (or stalled_since) at decision time
);

-- standing "always continue" / "always ignore"
CREATE TABLE IF NOT EXISTS standing_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,           -- session|project
    match      TEXT NOT NULL,           -- session id | absolute path (cwd, incl. subdirs) | project_dir name
    decision   TEXT NOT NULL,           -- continue|ignore
    created_at TEXT NOT NULL,
    note       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS standing_rules_scope_match ON standing_rules(scope, match);
"""

# Columns added after a table first shipped: {table: [(name, decl), ...]}.
# connect() adds any that an existing database lacks.
COLUMNS = {
    "projects": [],
    "sessions": [],
    "limit_hits": [],
    "tasks": [],
    "task_events": [],
    "session_decisions": [],
    "standing_rules": [],
}

SESSION_FIELDS = (
    "project_dir", "path", "cwd", "title", "title_rank", "first_seen", "last_activity", "own",
    "last_scanned_offset", "last_scanned_size", "last_msg_uuid", "last_msg_type", "last_msg_ts",
    "stalled", "stalled_since", "stall_kind", "stall_reset_at", "stall_text", "stall_uuid",
    "updated_at",
)


def iso(dt):
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


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init(conn)
    return conn


def init(conn):
    _migrate_v3(conn)          # before SCHEMA: its tasks_order index needs the v3 columns
    conn.executescript(SCHEMA)
    for table, cols in COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # raise the stored version, never lower it (an older checkout must not downgrade the mark)
    conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value "
                 "WHERE CAST(meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
                 (str(SCHEMA_VERSION),))
    conn.commit()


V2_TASK_COLUMNS = ("id", "title", "description", "project", "status", "kind", "created_at", "updated_at",
                   "created_by_session", "assigned_session", "blocked_question", "blocked_at", "answer",
                   "answered_at", "result_summary", "done_at")


def _db_file(conn):
    r = conn.execute("PRAGMA database_list").fetchone()
    return r[2] if r and r[2] else None


def _migrate_v3(conn, backup=True):
    """v2 -> v3, in place: rebuild tasks with project_id/stage_seq and a
    high|medium|low priority. Every old integer priority becomes 'high'; each
    distinct free-text project becomes a projects row (ranked by first use; a
    path gets its basename as name), and its tasks become its stages in the old
    execution order (priority desc, created_at, id). Each migrated task gets a
    'migrated' event. A non-empty database is first copied to
    <db>.v2-<utc>.bak. No-op on a fresh or already-migrated database; True if
    it migrated."""
    def task_cols():
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
            names = {}
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
                d = {"priority": [prio, "high"]}
                if value in names:
                    d["project"] = names[value]
                _event(conn, tid, ts, "migrated", d)
            seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='tasks'").fetchone()
            conn.execute("DROP TABLE tasks")
            conn.execute("ALTER TABLE tasks_v3 RENAME TO tasks")
            if seq:                                 # ids are never reused
                conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name='tasks'", (seq[0],))
                conn.execute("INSERT INTO sqlite_sequence(name, seq) SELECT 'tasks', ? WHERE NOT EXISTS "
                             "(SELECT 1 FROM sqlite_sequence WHERE name='tasks')", (seq[0],))
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


def get_meta(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_session(conn, session_id):
    r = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return dict(r) if r else None


def upsert_session(conn, session_id, **fields):
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


def add_hit(conn, session_id, entry_uuid, ts, kind, reset_at, text):
    """Record one limit notice. Returns True if it was new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO limit_hits(session_id, entry_uuid, ts, kind, reset_at, text) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, entry_uuid, iso(ts), kind, iso(reset_at), text))
    return cur.rowcount > 0


def stalled_sessions(conn, include_resolved=False):
    """Currently stalled sessions (or, with include_resolved, every session
    that ever hit a limit), with their hit counts, newest stall first."""
    where = "s.session_id IN (SELECT session_id FROM limit_hits)" if include_resolved else "s.stalled = 1"
    rows = conn.execute(f"""
        SELECT s.*, (SELECT COUNT(*) FROM limit_hits h WHERE h.session_id = s.session_id) AS hits,
               (SELECT MAX(ts) FROM limit_hits h WHERE h.session_id = s.session_id) AS last_hit
        FROM sessions s WHERE {where}
        ORDER BY s.stalled DESC, COALESCE(s.stalled_since, last_hit) DESC""").fetchall()
    return [dict(r) for r in rows]


def find_sessions(conn, prefix):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM sessions WHERE session_id LIKE ? ORDER BY session_id", (prefix + "%",))]


def session_history(conn, session_id):
    """All limit hits of one session, oldest first."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM limit_hits WHERE session_id=? ORDER BY ts, id", (session_id,))]


def counts(conn):
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
TASK_KINDS = ("task", "backlog_project")
PRIORITIES = ("high", "medium", "low")          # order = execution order
DEFAULT_PRIORITY = "high"
DECISIONS = ("continue", "ignore")
RULE_SCOPES = ("session", "project")
TASK_EDITABLE = ("title", "description", "project", "priority", "kind")
PROJECT_EDITABLE = ("name", "description", "path")

_PRIO_SQL = "CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"
TASK_ORDER_SQL = f"{_PRIO_SQL}, p.rank IS NULL, p.rank, t.stage_seq, t.created_at, t.id"
TASK_SELECT = ("SELECT t.*, p.name AS project_name, p.rank AS project_rank "
               "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id")


class NotFound(LookupError):
    pass


class InvalidTransition(ValueError):
    """The task (or session) is not in a state that allows this change."""


def _utcnow():            # patched by tests
    return datetime.now(timezone.utc)


def now_iso():
    """Current UTC time, always with milliseconds: 2026-09-29T10:00:00.000Z."""
    dt = _utcnow().astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


@contextmanager
def transaction(conn):
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
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


def _enum(value, allowed, what):
    if value not in allowed:
        raise ValueError(f"{what} must be one of {'|'.join(allowed)}, got {value!r}")
    return value


def _text(value, what, required=False):
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


def _priority(value):
    """high|medium|low (case and surrounding blanks ignored)."""
    if not isinstance(value, str):
        raise ValueError(f"priority must be one of {'|'.join(PRIORITIES)}, got {value!r}")
    return _enum(value.strip().lower(), PRIORITIES, "priority")


def _clean_task_field(name, value):
    if name == "title":
        return _text(value, "title", required=True)
    if name == "priority":
        return _priority(value)
    if name == "kind":
        return _enum(value, TASK_KINDS, "kind")
    return _text(value, name)


def _position(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer, got {value!r}")
    return value


def _event(conn, task_id, ts, event, detail=None):
    conn.execute("INSERT INTO task_events(task_id, ts, event, detail) VALUES (?, ?, ?, ?)",
                 (task_id, ts, event, None if detail is None else json.dumps(detail, ensure_ascii=False)))


def _task_dict(r):
    """A task row as the API returns it: 'project' is the project's NAME (it
    replaces the legacy v2 free-text column; 'project_name' is the same value,
    kept for readers like export_quickview.py), plus 'project_rank'."""
    d = dict(r)
    d["project"] = d["project_name"]
    return d


def _task_row(conn, task_id):
    r = conn.execute(TASK_SELECT + " WHERE t.id=?", (task_id,)).fetchone()
    if r is None:
        raise NotFound(f"no task #{task_id}")
    return _task_dict(r)


def get_task(conn, task_id):
    r = conn.execute(TASK_SELECT + " WHERE t.id=?", (task_id,)).fetchone()
    return _task_dict(r) if r else None


# ---- projects

def _norm_path(p):
    return os.path.normpath(os.path.abspath(os.path.expanduser(p)))


def _project_by_path(conn, path, nearest=True):
    """The project whose path is `path`, or (nearest) the one with the longest
    path that is a parent of it."""
    path = _norm_path(path)
    r = conn.execute("SELECT * FROM projects WHERE path=?", (path,)).fetchone()
    if r or not nearest:
        return dict(r) if r else None
    best = None
    for r in conn.execute("SELECT * FROM projects WHERE path IS NOT NULL"):
        if path.startswith(r["path"].rstrip("/") + "/") and (best is None or len(r["path"]) > len(best["path"])):
            best = dict(r)
    return best


def get_project(conn, ref, nearest=True):
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


def _project_row(conn, ref):
    p = get_project(conn, ref)
    if p is None:
        raise NotFound(f"no project {ref!r}")
    return p


def _free_name(conn, *candidates):
    for c in candidates:
        if not conn.execute("SELECT 1 FROM projects WHERE name=?", (c,)).fetchone():
            return c
    base, i = candidates[-1], 2
    while conn.execute("SELECT 1 FROM projects WHERE name=?", (f"{base} ({i})",)).fetchone():
        i += 1
    return f"{base} ({i})"


def add_project(conn, name, description=None, path=None, rank=None):
    """New project at the bottom of the list (or at `rank`, pushing the others down)."""
    name = _text(name, "name", required=True)
    if name.startswith(("/", "~")):
        raise ValueError(f"a project name can't start with / or ~ (that's a path): {name!r}")
    path = _norm_path(path) if _text(path, "path") else None
    if rank is not None:
        _position(rank, "rank")
    with transaction(conn):
        if get_project(conn, name):
            raise ValueError(f"project {name!r} already exists")
        if path and _project_by_path(conn, path, nearest=False):
            raise ValueError(f"project {_project_by_path(conn, path, nearest=False)['name']!r} "
                             f"already has path {path}")
        ts = now_iso()
        n = conn.execute("SELECT COALESCE(MAX(rank), 0) FROM projects").fetchone()[0]
        cur = conn.execute("INSERT INTO projects(name, rank, description, path, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (name, n + 1, _text(description, "description"),
                                                         path, ts, ts))
        if rank is not None:
            move_project(conn, cur.lastrowid, rank)
        return get_project(conn, cur.lastrowid)


def _ensure_project(conn, ref, nearest=True):
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


def project_for_path(conn, path, create=True):
    """The project of a working directory: the one with that path or the
    nearest parent path; with create, a new one named after the directory."""
    return _ensure_project(conn, path) if create else _project_by_path(conn, path)


def list_projects(conn):
    """Projects by rank, each with 'open' (number of open stages) and
    'ready' {high, medium, low}: pending stages per priority."""
    out = []
    for r in conn.execute("SELECT * FROM projects ORDER BY rank, id").fetchall():
        d = dict(r)
        cnt = dict(conn.execute("SELECT status || ':' || priority, COUNT(*) FROM tasks WHERE project_id=? "
                                "GROUP BY status, priority", (d["id"],)).fetchall())
        d["open"] = sum(n for k, n in cnt.items() if k.split(":")[0] in OPEN_STATUSES)
        d["ready"] = {lvl: cnt.get(f"pending:{lvl}", 0) for lvl in PRIORITIES}
        out.append(d)
    return out


def update_project(conn, ref, **fields):
    """Rename a project or change its description/path."""
    bad = set(fields) - set(PROJECT_EDITABLE)
    if bad:
        raise ValueError(f"not editable: {sorted(bad)} (editable: {', '.join(PROJECT_EDITABLE)}; "
                         "order changes go through move_project)")
    with transaction(conn):
        p = _project_row(conn, ref)
        clean = {}
        if "name" in fields:
            clean["name"] = _text(fields["name"], "name", required=True)
            if clean["name"].startswith(("/", "~")):
                raise ValueError(f"a project name can't start with / or ~: {clean['name']!r}")
            other = get_project(conn, clean["name"])
            if other and other["id"] != p["id"]:
                raise ValueError(f"project {clean['name']!r} already exists")
        if "description" in fields:
            clean["description"] = _text(fields["description"], "description")
        if "path" in fields:
            clean["path"] = _norm_path(fields["path"]) if _text(fields["path"], "path") else None
            other = clean["path"] and _project_by_path(conn, clean["path"], nearest=False)
            if other and other["id"] != p["id"]:
                raise ValueError(f"project {other['name']!r} already has path {clean['path']}")
        clean = {k: v for k, v in clean.items() if p[k] != v}
        if clean:
            conn.execute(f"UPDATE projects SET {', '.join(f'{k}=?' for k in clean)}, updated_at=? WHERE id=?",
                         list(clean.values()) + [now_iso(), p["id"]])
        return get_project(conn, p["id"])


def _reorder(ids, item, new_pos):
    """ids without item, item re-inserted at 1-based new_pos (clamped)."""
    rest = [i for i in ids if i != item]
    k = min(max(new_pos, 1), len(rest) + 1) - 1
    return rest[:k] + [item] + rest[k:]


def move_project(conn, ref, new_rank):
    """Put a project at rank new_rank (1 = top, clamped to the list); the
    others shift. Ranks stay 1..n."""
    _position(new_rank, "rank")
    with transaction(conn):
        p = _project_row(conn, ref)
        ids = [r[0] for r in conn.execute("SELECT id FROM projects ORDER BY rank, id")]
        ts = now_iso()
        for rank, pid in enumerate(_reorder(ids, p["id"], new_rank), 1):
            conn.execute("UPDATE projects SET rank=?, updated_at=CASE WHEN id=? THEN ? ELSE updated_at END "
                         "WHERE id=? AND rank IS NOT ?", (rank, p["id"], ts, pid, rank))
        return get_project(conn, p["id"])


def set_project_priority(conn, ref, level):
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

def _next_seq(conn, project_id, table="tasks"):
    if project_id is None:
        return None
    return conn.execute(f"SELECT COALESCE(MAX(stage_seq), 0) + 1 FROM {table} WHERE project_id=?",
                        (project_id,)).fetchone()[0]


def _compact_stages(conn, project_id):
    if project_id is None:
        return
    ids = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE project_id=? ORDER BY stage_seq, id",
                                      (project_id,))]
    for seq, tid in enumerate(ids, 1):
        conn.execute("UPDATE tasks SET stage_seq=? WHERE id=? AND stage_seq IS NOT ?", (seq, tid, seq))


def task_events(conn, task_id):
    """History of one task, oldest first; detail decoded from JSON."""
    out = []
    for r in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)):
        d = dict(r)
        d["detail"] = json.loads(d["detail"]) if d["detail"] else None
        out.append(d)
    return out


def add_task(conn, title, description=None, project=None, priority=DEFAULT_PRIORITY, kind="task",
             created_by_session=None):
    """New pending task, appended as the last stage of `project` (a name or a
    directory, see get_project; an unknown one is created at the bottom of the
    project list). project=None: no project."""
    fields = {"title": _clean_task_field("title", title),
              "description": _clean_task_field("description", description),
              "priority": _priority(priority),
              "kind": _enum(kind, TASK_KINDS, "kind"),
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
        tid = cur.lastrowid
        if p:
            fields["project"] = p["name"]
        _event(conn, tid, ts, "created", {k: v for k, v in fields.items() if v is not None})
        return _task_row(conn, tid)


def list_tasks(conn, status=None, project=None, kind=None, limit=None):
    """Tasks in execution order (see above). status: one value or a list.
    project: a name, id or directory (NotFound if there is no such project)."""
    where, args = [], []
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
        args.append(_enum(kind, TASK_KINDS, "kind"))
    sql = TASK_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + TASK_ORDER_SQL
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [_task_dict(r) for r in conn.execute(sql, args)]


def execution_order(conn, kind=None, project=None, limit=None):
    """The ready queue: pending tasks in the order the dispatcher takes them."""
    return list_tasks(conn, status="pending", kind=kind, project=project, limit=limit)


def next_ready_task(conn, kind=None, project=None):
    """The pending task that should run next (or None). Read-only: claim it with start_task."""
    r = execution_order(conn, kind=kind, project=project, limit=1)
    return r[0] if r else None


def _update(conn, task_id, event, fields):
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


def update_task(conn, task_id, **fields):
    """Change title/description/project/priority/kind. Logs one 'updated'
    event with {field: [old, new]} (nothing is logged if nothing changed).
    A new project appends the task as that project's last stage."""
    bad = set(fields) - set(TASK_EDITABLE)
    if bad:
        raise ValueError(f"not editable: {sorted(bad)} (editable: {', '.join(TASK_EDITABLE)}; "
                         "status changes go through block/answer/start/finish/cancel/reopen)")
    return _update(conn, task_id, "updated", fields)


def set_stage_priority(conn, task_id, level):
    """high|medium|low for one task (stage); logs a 'priority' event."""
    return _update(conn, task_id, "priority", {"priority": level})


set_priority = set_stage_priority


def move_stage(conn, task_id, new_seq):
    """Put a task at position new_seq (1 = first, clamped) among its project's
    stages; the others shift. Logs a 'moved' event {from, to}."""
    _position(new_seq, "stage")
    with transaction(conn):
        t = _task_row(conn, task_id)
        if t["project_id"] is None:
            raise InvalidTransition(f"task #{task_id} has no project, so it has no stage order")
        ids = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE project_id=? ORDER BY stage_seq, id",
                                          (t["project_id"],))]
        order = _reorder(ids, task_id, new_seq)
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


def _transition(conn, task_id, allowed_from, to_status, event, detail=None, **fields):
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
        d = {"from": t["status"]}
        d.update(detail or {})
        _event(conn, task_id, ts, event, d)
        return _task_row(conn, task_id)



def block_task(conn, task_id, question):
    """A run needs the user: park the task with a question (clears any old answer)."""
    q = _text(question, "question", required=True)
    return _transition(conn, task_id, ("pending", "in_progress"), "blocked", "blocked", {"question": q},
                       blocked_question=q, blocked_at=NOW, answer=None, answered_at=None)


def answer_task(conn, task_id, answer):
    """The user's answer; the task is pending again (ready for the next pass).
    blocked_question and assigned_session stay, so the next run sees the Q&A
    and the dispatcher can resume the same session."""
    a = _text(answer, "answer", required=True)
    return _transition(conn, task_id, ("blocked",), "pending", "answered", {"answer": a},
                       answer=a, answered_at=NOW)


def start_task(conn, task_id, session):
    """Claim a pending task for a session. Starting it again for the same
    session is a no-op; any other state raises InvalidTransition."""
    session = _text(session, "session")
    with transaction(conn):
        t = _task_row(conn, task_id)
        if t["status"] == "in_progress" and t["assigned_session"] == session:
            return t
        return _transition(conn, task_id, ("pending",), "in_progress", "started", {"session": session},
                           assigned_session=session)


def finish_task(conn, task_id, summary=None):
    s = _text(summary, "summary")
    return _transition(conn, task_id, ("pending", "in_progress"), "done", "done", {"summary": s},
                       result_summary=s, done_at=NOW)


def cancel_task(conn, task_id, reason=None):
    r = _text(reason, "reason")
    return _transition(conn, task_id, ("pending", "in_progress", "blocked"), "cancelled", "cancelled",
                       {"reason": r} if r else None, done_at=NOW)


def reopen_task(conn, task_id, reason=None):
    """Back to pending from done/cancelled, or from in_progress (a run died)."""
    r = _text(reason, "reason")
    return _transition(conn, task_id, ("in_progress", "done", "cancelled"), "pending", "reopened",
                       {"reason": r} if r else None, done_at=None)


# ---- stalled-session decisions

def _stall_ref(sess):
    return (sess or {}).get("stall_uuid") or (sess or {}).get("stalled_since")


def decide_session(conn, session_id, decision, note=None):
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


def clear_decision(conn, session_id):
    with transaction(conn):
        return conn.execute("DELETE FROM session_decisions WHERE session_id=?", (session_id,)).rowcount > 0


def get_decision(conn, session_id):
    r = conn.execute("SELECT * FROM session_decisions WHERE session_id=?", (session_id,)).fetchone()
    return dict(r) if r else None


def _norm_match(scope, match):
    m = _text(match, "match", required=True)
    if scope == "project" and m.startswith("/"):
        m = os.path.normpath(m)
    return m


def add_rule(conn, scope, match, decision, note=None):
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


def remove_rule(conn, rule_id):
    with transaction(conn):
        if conn.execute("DELETE FROM standing_rules WHERE id=?", (rule_id,)).rowcount == 0:
            raise NotFound(f"no rule #{rule_id}")


def list_rules(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM standing_rules ORDER BY scope, match")]


def _project_rule_rank(rule, sess):
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


def _effective(sess, decision, rules):
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


def effective_decision(conn, session_id):
    """(decision, source) for a session: its own decision for the current stall
    ('session'), else a session rule ('session_rule:<id>'), else the most
    specific project rule ('project_rule:<id>'), else (None, None) = undecided."""
    sess = get_session(conn, session_id) or {"session_id": session_id}
    return _effective(sess, get_decision(conn, session_id), list_rules(conn))


def stalled_decisions(conn):
    """Every currently stalled session with 'decision' and 'decision_source'
    (None = undecided). The dispatcher resumes those with decision == 'continue'."""
    rules = list_rules(conn)
    decisions = {r["session_id"]: dict(r) for r in conn.execute("SELECT * FROM session_decisions")}
    out = []
    for s in stalled_sessions(conn):
        s["decision"], s["decision_source"] = _effective(s, decisions.get(s["session_id"]), rules)
        out.append(s)
    return out


def pending_user_input(conn, include_own=False):
    """Everything waiting for the user: blocked tasks (priority order) and
    stalled sessions with no decision (newest stall first). Undecided stalls
    never expire. AFClaude's own sessions are left out by default: the
    keep-alive already handles them."""
    return {"blocked_tasks": list_tasks(conn, status="blocked"),
            "undecided_sessions": [s for s in stalled_decisions(conn) if s["decision"] is None
                                   and (include_own or not s.get("own"))]}
