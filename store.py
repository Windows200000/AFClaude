#!/usr/bin/env python3
"""
Local SQLite store for AFClaude: data/afclaude.db (data/ is not committed).

Schema creation is idempotent: every table is CREATE TABLE IF NOT EXISTS, and
columns added later go into COLUMNS so `connect()` ALTERs older databases in
place. New tables go into SCHEMA the same way (CREATE ... IF NOT EXISTS), so
an older database gains them on its next connect(); bump SCHEMA_VERSION.

  v1 (goal 2): sessions, limit_hits, meta
  v2 (goal 3): tasks, task_events (append-only), session_decisions, standing_rules

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
SCHEMA_VERSION = 2

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

-- v2: tasks. status/kind/decision/scope values are validated in Python (no
-- CHECK constraints: SQLite can't ALTER those, a new value would need a
-- table rebuild).
CREATE TABLE IF NOT EXISTS tasks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title              TEXT NOT NULL,
    description        TEXT,
    project            TEXT,            -- free text, usually the cwd of the adding session
    priority           INTEGER NOT NULL DEFAULT 0,   -- manual only; higher runs first
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
);
CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(status, priority DESC, created_at);

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


# ---------------------------------------------------------------- tasks & decisions (v2)

TASK_STATUSES = ("pending", "in_progress", "blocked", "done", "cancelled")
OPEN_STATUSES = ("pending", "in_progress", "blocked")
TASK_KINDS = ("task", "backlog_project")
DECISIONS = ("continue", "ignore")
RULE_SCOPES = ("session", "project")
TASK_EDITABLE = ("title", "description", "project", "priority", "kind")


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
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"priority must be an integer, got {value!r}")
    return value


def _clean_task_field(name, value):
    if name == "title":
        return _text(value, "title", required=True)
    if name == "priority":
        return _priority(value)
    if name == "kind":
        return _enum(value, TASK_KINDS, "kind")
    return _text(value, name)


def _event(conn, task_id, ts, event, detail=None):
    conn.execute("INSERT INTO task_events(task_id, ts, event, detail) VALUES (?, ?, ?, ?)",
                 (task_id, ts, event, None if detail is None else json.dumps(detail, ensure_ascii=False)))


def _task_row(conn, task_id):
    r = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if r is None:
        raise NotFound(f"no task #{task_id}")
    return dict(r)


def get_task(conn, task_id):
    r = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(r) if r else None


def task_events(conn, task_id):
    """History of one task, oldest first; detail decoded from JSON."""
    out = []
    for r in conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)):
        d = dict(r)
        d["detail"] = json.loads(d["detail"]) if d["detail"] else None
        out.append(d)
    return out


def add_task(conn, title, description=None, project=None, priority=0, kind="task",
             created_by_session=None):
    fields = {"title": _clean_task_field("title", title),
              "description": _clean_task_field("description", description),
              "project": _clean_task_field("project", project),
              "priority": _priority(priority),
              "kind": _enum(kind, TASK_KINDS, "kind"),
              "created_by_session": _text(created_by_session, "created_by_session")}
    with transaction(conn):
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO tasks(title, description, project, priority, status, kind, created_at, updated_at, "
            "created_by_session) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (fields["title"], fields["description"], fields["project"], fields["priority"], fields["kind"],
             ts, ts, fields["created_by_session"]))
        tid = cur.lastrowid
        _event(conn, tid, ts, "created", {k: v for k, v in fields.items() if v is not None})
        return _task_row(conn, tid)


def list_tasks(conn, status=None, project=None, kind=None, limit=None):
    """Tasks by priority (high first), then oldest first. status: one value or a list."""
    where, args = [], []
    if status is not None:
        sts = [status] if isinstance(status, str) else list(status)
        for s in sts:
            _enum(s, TASK_STATUSES, "status")
        where.append(f"status IN ({', '.join('?' for _ in sts)})")
        args += sts
    if project is not None:
        where.append("project = ?")
        args.append(project)
    if kind is not None:
        where.append("kind = ?")
        args.append(_enum(kind, TASK_KINDS, "kind"))
    sql = "SELECT * FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY priority DESC, created_at, id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, args)]


def next_ready_task(conn, kind=None, project=None):
    """The pending task that should run next (or None). Read-only: claim it with start_task."""
    r = list_tasks(conn, status="pending", kind=kind, project=project, limit=1)
    return r[0] if r else None


def _update(conn, task_id, event, fields):
    clean = {k: _clean_task_field(k, v) for k, v in fields.items()}
    with transaction(conn):
        t = _task_row(conn, task_id)
        changes = {k: [t[k], v] for k, v in clean.items() if t[k] != v}
        if not changes:
            return t
        ts = now_iso()
        sets = ", ".join(f"{k}=?" for k in changes)
        conn.execute(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
                     [v[1] for v in changes.values()] + [ts, task_id])
        if event == "priority":
            _event(conn, task_id, ts, "priority", {"from": changes["priority"][0], "to": changes["priority"][1]})
        else:
            _event(conn, task_id, ts, event, changes)
        return _task_row(conn, task_id)


def update_task(conn, task_id, **fields):
    """Change title/description/project/priority/kind. Logs one 'updated'
    event with {field: [old, new]} (nothing is logged if nothing changed)."""
    bad = set(fields) - set(TASK_EDITABLE)
    if bad:
        raise ValueError(f"not editable: {sorted(bad)} (editable: {', '.join(TASK_EDITABLE)}; "
                         "status changes go through block/answer/start/finish/cancel/reopen)")
    return _update(conn, task_id, "updated", fields)


def set_priority(conn, task_id, priority):
    return _update(conn, task_id, "priority", {"priority": priority})


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
