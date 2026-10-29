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
               Later column (COLUMNS, no version bump): tasks.estimated_sessions
               (D-213: how many full session windows the stage still needs, set by
               the task-manager or the stage-ETA review; NULL = stage_eta.py's default).
               Later columns (COLUMNS, no version bump): projects.manager_model and
               projects.manager_effort (D-260: the task-manager session's launch; NULL = the
               default; manager_launch.py, set only by actions.project.manager_launch).
  v4 (dashboard phase 1, docs/dashboard_design/04_architecture.md §4): settings, prompt_overrides,
               audit_log (append-only), idempotency_keys, run_log, driven_sessions,
               action_requests; a `version` column on projects, tasks,
               standing_rules and session_decisions, bumped by a trigger on every
               UPDATE (optimistic concurrency, whoever writes); task kind 'question'
               (a manager question, born blocked; answering it closes it). Purely
               additive: a v3 database is first copied to <db>.v3-<utc>.bak, then
               gains the tables/columns/triggers in place (_backup_v3, init).
               Writes from the CLI, the MCP server and the dashboard go through
               actions.py (validation, audit row, idempotency, version checks).
               Later tables (SCHEMA/TELEMETRY_SCHEMA, no version bump: purely additive, so
               a running older checkout keeps writing): accounts (one 'default' row, D-156)
               and the telemetry tables of dashboard phase 2c (D-161; telemetry.py, the
               actions.py appenders, `python3 store.py import-telemetry`), and the
               runner state of phase 2d (RUNNER_SCHEMA: runner_state, scheduled_jobs,
               app_log; runner_state.py, `python3 store.py import-state`), and the
               project docs of phase 2d part 2 (DOCS_SCHEMA: docs, doc_entries;
               project_docs.py, `python3 store.py import-docs`).
               The handoffs table (compaction hand-offs, D-144/D-155, design §5.5)
               is additive the same way: no version bump, which would make every
               process still running older code (the keep-alive watcher) see the
               database as newer than itself, read-only, and pause all autonomous
               starts until it is restarted.
               prompt_defaults (D-227, additive the same way): the code's current prompts/*.md
               (except README.md) imported on every deploy (`python3 store.py import-prompts`,
               docker/entrypoint.sh, after the DB is reachable; the audited action is
               actions.prompt_defaults_sync, actor runner:deploy). Readers still read the files
               for now (D-169; the prompts.py loader is phase 3); prompt_overrides.base_outdated
               (COLUMNS) is refreshed by the same sync, flagging an override whose base_sha256 no
               longer matches the current default.
               inbox_messages (D-178/D-241, additive the same way): the owner's messages to the
               task-manager from the dashboard, verbatim, with the Haiku triage and the delivery
               state (inbox.py, inbox_api.py; written only through actions.py's inbox.* actions);
               its retry_after (COLUMNS, no version bump) holds a triage that hit the account's
               usage limit until the limit's reset (D-244, inbox_worker.triage).
               project_pauses (D-244, additive the same way): the automation pause of one project
               (an inbox error pauses the project its message was for); the install-wide pause
               stays the settings automation_paused / automation_paused_reason (failstop.py).
               runner_jobs, runner_runs (phase 3a, additive the same way, RUNNER_RUNS_SCHEMA): the
               resident runner's due times and its run records (runner.py; actions.py writes).
               status_snapshot (phase 3a S9(b), additive the same way, STATUS_SNAPSHOT_SCHEMA): the
               latest usage / next-run / pause / running-runs sections the runner writes each minute.
               Phase 3b (COLUMNS, no version bump): driven_sessions.project_id (a task-manager
               row's project) and action_requests.run_id / updated_at (run-now, D-153); the
               driven_sessions backfill is `python3 store.py import-driven` (driven.py).
               prompt_vars (phase 4a, D-197, additive the same way): a project's values of the
               prompt variables prompts.PROJECT_VARS declares (prompts.py; actions.py writes).
               auth_sessions (phase 6a W5, additive the same way, AUTH_SCHEMA): the dashboard's
               browser sessions (dashboard/sessions.py; actions.py's auth.session_* writes).
               notices (D-306, additive the same way): the owner's dashboard notices (title, text,
               dismissed_at; actions.py's notice.* writes, inbox.view shows them).
               setup_codes (phase 6a W7, additive the same way, SETUP_SCHEMA): the one-time setup
               code (first owner) and admin reset-auth codes (dashboard/setup.py; actions.py's
               setup.mint_code / setup.claim_code write it; only a code's hash is ever stored).

Schema guard (docs/dashboard_design/07_data_model.md §7.1, review A1): this code writes only to a
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
import re
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from types import FrameType
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
-- and data/own_sessions.txt once the runners fill it, phase 3). Phase 3b: the tmux name is
-- looked up here, never computed from the id; written by actions.driven_set (own_session_add
-- for kind 'own'); project_id (COLUMNS) is the project a task-manager row runs.
CREATE TABLE IF NOT EXISTS driven_sessions (
    session_id  TEXT PRIMARY KEY,
    tmux        TEXT,
    kind        TEXT NOT NULL,          -- task-manager | review | stall | request | own (legacy: keepalive | task)
    task_id     INTEGER,
    started_at  TEXT NOT NULL,
    last_seen   TEXT,
    ended_at    TEXT,
    holder_kind TEXT,
    rc_url      TEXT,
    project_id  INTEGER REFERENCES projects(id)
);

-- the dashboard's only way to ask for execution; the runner consumes them (phase 3b S5)
CREATE TABLE IF NOT EXISTS action_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    kind       TEXT NOT NULL,           -- continue_now | review_now | work_on_now
    target     TEXT,                    -- session id (continue_now), project id (work_on_now), NULL (review_now)
    status     TEXT NOT NULL DEFAULT 'open',   -- open | started | refused | failed (legacy: done | cancelled)
    result     TEXT,                    -- JSON: {tmux, session_id} (started) | {reason} (refused, failed)
    handled_at TEXT,
    run_id     INTEGER,                 -- the runner_runs row that handled it (no FK: runs are pruned)
    updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS action_requests_one_open ON action_requests(kind, COALESCE(target, ''))
    WHERE status = 'open';

-- the Claude accounts (D-156): one row ('default') now; telemetry rows carry account_id so the
-- future account pool (§14) needs no migration
CREATE TABLE IF NOT EXISTS accounts (
    id         TEXT PRIMARY KEY,
    label      TEXT,
    created_at TEXT NOT NULL
);

-- compaction hand-offs (D-144, D-155, D-191; design §5.5): what a long-running session writes for
-- itself before the keep-alive compacts it; the next start injects the latest unconsumed one into
-- its continue message and marks it consumed. Additive, no schema_version bump (see the module doc).
CREATE TABLE IF NOT EXISTS handoffs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL,
    project_id        INTEGER REFERENCES projects(id),
    created_at        TEXT NOT NULL,
    created_by        TEXT,                -- the writing actor (mcp:<session>, cli, ...)
    body              TEXT NOT NULL,       -- JSON: the fields of handoff_body (D-144, D-305)
    consumed_at       TEXT,
    consumed_by_start TEXT                 -- the start's dedup key (window-start-..., last-mile-...)
);
CREATE INDEX IF NOT EXISTS handoffs_session ON handoffs(session_id, id);

-- the owner's messages to the task-manager from the dashboard (D-178/D-241; inbox.py): the text as
-- typed is kept only here. Haiku sorts each one: "now" is typed into the session at once (D-186),
-- "note" waits for the next message the keep-alive sends into it. Written only through the
-- inbox.* actions. Additive, no schema_version bump (see the module doc).
CREATE TABLE IF NOT EXISTS inbox_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL,         -- the actor (owner via the dashboard, cli)
    session_id      TEXT NOT NULL,         -- the task-manager session it is for
    text            TEXT NOT NULL,         -- verbatim (D-241: the only copy)
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending | delivered | noted | included | answered | error
    verdict         TEXT,                  -- now | note (the triage), NULL until triaged
    verdict_reason  TEXT,                  -- the triage's one-line reason
    triaged_at      TEXT,
    triage_cost_usd REAL,                  -- the triage call's own cost and usage (claude -p JSON)
    triage_usage    TEXT,
    hold            TEXT,                  -- why a "now" message waits (owner typing, a compaction, ...)
    hold_since      TEXT,                  -- (retry_after, COLUMNS: a triage held at a usage limit)
    delivered_at    TEXT,
    delivered_via   TEXT,                  -- the send plan (send-keys, resume, ...)
    included_at     TEXT,                  -- a note sent along with a message into the session ...
    included_by     TEXT,                  -- ... this one (a start's key, a compaction, inbox #<id>)
    answer          TEXT,                  -- the task-manager's answer (tasks.py inbox reply)
    answered_at     TEXT,
    error           TEXT,
    error_at        TEXT,
    version         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS inbox_messages_status ON inbox_messages(status, session_id, id);

-- the automation pause of one project (D-244; failstop.py): its runner checks hold while it is set,
-- on top of the install-wide one (settings automation_paused). One row per project ever paused; a
-- resume keeps the row (paused 0, reason NULL), so its version never repeats. Written only through
-- actions.py's automation.set_project. Additive, no schema_version bump (see the module doc).
CREATE TABLE IF NOT EXISTS project_pauses (
    project_id  INTEGER PRIMARY KEY REFERENCES projects(id),
    paused      INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,                  -- why (a runner's failure says what failed); NULL when resumed
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);

-- notices for the owner (D-306): a title and a short text on the dashboard's "Needs your input"
-- section until the owner dismisses it. Posted by the task-manager (`tasks.py notice post`, MCP
-- afclaude_notice) or written with every review report doc (review:*, source = '<project>/<doc
-- name>': one notice per report, refreshed while it is shown). Written only through actions.py's
-- notice.* actions. Additive, no schema_version bump (see the module doc).
CREATE TABLE IF NOT EXISTS notices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL,         -- the actor
    project_id   INTEGER REFERENCES projects(id),   -- NULL: not about one project
    title        TEXT NOT NULL,
    text         TEXT NOT NULL,         -- the full content (D-279: no pointer to a file)
    source       TEXT,                  -- '<project>/review:<name>' for a review's notice, else NULL
    updated_at   TEXT,
    dismissed_at TEXT,                  -- the owner dismissed it (notice.dismiss)
    version      INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS notices_source ON notices(source) WHERE source IS NOT NULL;
CREATE INDEX IF NOT EXISTS notices_open ON notices(dismissed_at, id);
"""

# ---- telemetry (dashboard phase 2c, D-161, §7.2/§7.3): the usage files' rows in the DB.
# Append-only tables (TELEMETRY_APPEND): one row per file line, written only through the
# registered actions.py appenders (actions.APPENDERS); each row carries ts, actor, via and
# recorded_at and is immutable by trigger, so the row is its own audit record. `payload` is
# the JSON line exactly as the producer wrote it to its file (byte-identical during the
# dual-write period, so a reader gets the same rows from either), `row_key` its sha256 (the
# importer and a dual-write dedupe on it). `stale` marks rows whose usage meters must not be
# used (D-130: never drop data, mark it).
# Record tables (TELEMETRY_DOCS): one row per derived record that is recomputed in place (a
# run's fill-time row until it is final, a weekly cycle, the user model, a usage review's launch
# record until it is final, the context cost model of compact_timing.py, D-237/D-238); they carry the same ts/actor/via plus updated_at and a trigger-bumped
# version, and can't be deleted.
TELEMETRY_APPEND = ("usage_samples", "usage_weekly_series", "usage_session_windows", "forecast_log",
                    "haiku_judgements", "usage_reports", "usage_run_readings", "stage_eta_log")
TELEMETRY_DOCS = ("weekly_cycles", "usage_runs", "user_model", "usage_reviews", "cost_model")
DEFAULT_ACCOUNT = "default"
_APPEND_DDL = """
CREATE TABLE IF NOT EXISTS {t} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT NOT NULL DEFAULT 'default' REFERENCES accounts(id),
    ts          TEXT NOT NULL,           -- the row's own time, normalised UTC ISO (sorts as text)
    stale       INTEGER NOT NULL DEFAULT 0,
    row_key     TEXT NOT NULL,           -- sha256 of payload
    actor       TEXT NOT NULL,
    via         TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload     TEXT NOT NULL,           -- the JSON line as written to the file
    UNIQUE (account_id, row_key)
);
CREATE INDEX IF NOT EXISTS {t}_ts ON {t}(account_id, ts, id);
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
"""
_DOC_DDL = """
CREATE TABLE IF NOT EXISTS {t} (
    account_id TEXT NOT NULL DEFAULT 'default' REFERENCES accounts(id),
    doc_key    TEXT NOT NULL,            -- run_id | the cycle's reset | 'model' | a review's session
    ts         TEXT NOT NULL,            -- the record's own time (run start, reset), normalised
    final      INTEGER NOT NULL DEFAULT 0,
    actor      TEXT NOT NULL,
    via        TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload    TEXT NOT NULL,            -- the record's JSON
    version    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, doc_key)
);
CREATE INDEX IF NOT EXISTS {t}_ts ON {t}(account_id, ts);
CREATE TRIGGER IF NOT EXISTS {t}_version AFTER UPDATE ON {t} FOR EACH ROW WHEN NEW.version IS OLD.version
BEGIN UPDATE {t} SET version = OLD.version + 1 WHERE account_id = NEW.account_id AND doc_key = NEW.doc_key; END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} rows are never deleted'); END;
"""
TELEMETRY_SCHEMA = ("".join(_APPEND_DDL.format(t=t) for t in TELEMETRY_APPEND)
                    + "".join(_DOC_DDL.format(t=t) for t in TELEMETRY_DOCS))

# ---- runner state (dashboard phase 2d, D-161, §7.2): the runners' state files, the at-shim's
# spooled jobs and the log files in the DB (runner_state.py maps the files; actions.py writes).
# runner_state: one row per top-level key of a state file, or per entry of a "split" part (a
# map the runners update entry by entry, e.g. keepalive_state.json's handled dedup keys), so
# concurrent writers merge per entry in one transaction instead of overwriting each other.
# `path` is the JSON array of keys ([top] or [part, entry]), `payload` the value's JSON. Rows
# are deleted only where the runner deletes the entry from its state (actions.state_save).
# scheduled_jobs: the at-shim jobs (data/at_spool/*.json); a started job is marked, not deleted.
# app_log: the log lines, append-only (immutable by trigger, ts/actor/via on each row).
RUNNER_SCHEMA = """
CREATE TABLE IF NOT EXISTS runner_state (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL DEFAULT 'default' REFERENCES accounts(id),
    component  TEXT NOT NULL,            -- keepalive | keepalive.deferred | dispatcher | sampler | ...
    path       TEXT NOT NULL,            -- JSON array: [top-level key] | [split part, entry key]
    payload    TEXT NOT NULL,            -- the value's JSON
    actor      TEXT NOT NULL,
    via        TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (account_id, component, path)
);
CREATE INDEX IF NOT EXISTS runner_state_component ON runner_state(account_id, component, id);
CREATE TRIGGER IF NOT EXISTS runner_state_version AFTER UPDATE ON runner_state FOR EACH ROW
WHEN NEW.version IS OLD.version
BEGIN UPDATE runner_state SET version = OLD.version + 1 WHERE id = NEW.id; END;

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id         TEXT PRIMARY KEY,         -- the at-shim job id
    run_at     TEXT NOT NULL,            -- normalised UTC
    cmd        TEXT NOT NULL,
    cwd        TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending | started
    payload    TEXT NOT NULL,            -- the spool file's JSON
    created_at TEXT NOT NULL,
    started_at TEXT,
    actor      TEXT NOT NULL,
    via        TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS scheduled_jobs_due ON scheduled_jobs(status, run_at);
CREATE TRIGGER IF NOT EXISTS scheduled_jobs_version AFTER UPDATE ON scheduled_jobs FOR EACH ROW
WHEN NEW.version IS OLD.version
BEGIN UPDATE scheduled_jobs SET version = OLD.version + 1 WHERE id = NEW.id; END;
CREATE TRIGGER IF NOT EXISTS scheduled_jobs_no_delete BEFORE DELETE ON scheduled_jobs
BEGIN SELECT RAISE(ABORT, 'scheduled_jobs rows are marked started, not deleted'); END;

CREATE TABLE IF NOT EXISTS app_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,           -- the line's own time (normalised UTC), else the previous line's
    component   TEXT NOT NULL,           -- keepalive | dispatcher | sampler | watchdog | host_exec | at_shim
    level       TEXT NOT NULL,           -- info | warn | error
    line        TEXT NOT NULL,           -- the log line as written to the file
    line_hash   TEXT NOT NULL,           -- sha256 of line (the importer counts copies by it)
    actor       TEXT NOT NULL,
    via         TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS app_log_ts ON app_log(component, ts, id);
CREATE INDEX IF NOT EXISTS app_log_hash ON app_log(component, line_hash);
CREATE TRIGGER IF NOT EXISTS app_log_no_update BEFORE UPDATE ON app_log
BEGIN SELECT RAISE(ABORT, 'app_log is append-only'); END;

-- Retention (phase 2d part 2, D-161/D-130): app_log rows may be deleted ONLY by
-- prune_app_log(), through the gate row below -- never by hand, and UPDATE stays fully
-- blocked (trigger above). A trigger on app_log can't reference a TEMP table ("trigger ...
-- cannot reference objects in database temp"), so the gate is one row of a real table; it is
-- opened and closed inside the SAME transaction as the deletes (BEGIN IMMEDIATE holds SQLite's
-- one write lock for its duration, and a crash mid-prune rolls the whole transaction -- gate
-- included -- back), so no other writer can ever see it open.
CREATE TABLE IF NOT EXISTS app_log_prune_gate (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    allowed INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO app_log_prune_gate(id, allowed) VALUES (1, 0);
DROP TRIGGER IF EXISTS app_log_no_delete;
CREATE TRIGGER IF NOT EXISTS app_log_guarded_delete BEFORE DELETE ON app_log
WHEN (SELECT allowed FROM app_log_prune_gate WHERE id = 1) = 0
BEGIN SELECT RAISE(ABORT, 'app_log deletes only through store.prune_app_log (retention)'); END;
"""
RUNNER_TABLES = ("runner_state", "scheduled_jobs", "app_log", "runner_jobs", "runner_runs", "status_snapshot")

# ---- the status snapshot (phase 3a S9(b), design 04_architecture.md: the web container reads usage and
# the next run from a row the runner writes, it imports no model code). Additive, no version bump. One
# row per (section, account), always the latest value (no history, so no retention: a row is replaced);
# the shape is the one docs/phase6c_plan.md §3.3 names, so 6c R1 adds sections, not a table.
# state 'error' = the section could not be built (error says why, payload is then NULL); builder = the
# payload format's version. Written only through actions.snapshot_put (runner actors).
STATUS_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS status_snapshot (
    section     TEXT NOT NULL,
    account_id  TEXT NOT NULL DEFAULT 'default' REFERENCES accounts(id),
    computed_at TEXT NOT NULL,           -- UTC, when the builder read the values
    state       TEXT NOT NULL CHECK (state IN ('ok', 'error')),
    payload     TEXT,                    -- JSON object; NULL when state is 'error'
    error       TEXT,
    builder     INTEGER NOT NULL,
    version     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (section, account_id)
);
CREATE TRIGGER IF NOT EXISTS status_snapshot_version AFTER UPDATE ON status_snapshot FOR EACH ROW
WHEN NEW.version IS OLD.version
BEGIN UPDATE status_snapshot SET version = OLD.version + 1 WHERE section = NEW.section AND account_id = NEW.account_id; END;
"""

# ---- the resident runner (phase 3a, docs/phase3a_plan.md §3.3; runner.py, written only through
# actions.py: runner_job_set, run_begin, run_pid, run_end, runner.prune). Additive, no version bump.
# runner_jobs: one row per job (its next due time and last outcome) plus the row 'runner' (the
# loop's heartbeat). runner_runs: one row per run, actor runner:<job> and via on the row (no
# audit_log row per run, D-256). A keyed run (a session-window start, an at-job, ...) can't start
# twice, also across a restart: UNIQUE (job, key) over the rows that were not interrupted (an
# interrupted run whose job reruns gets a new row with the same key; one whose job fails never runs
# again: actions.run_begin(rerun=False)). A run leaves
# 'running' once: a finished row is immutable (trigger). Deletes only through prune_runner_runs()
# and its gate row (retention, runner_runs_retention_days), the way app_log does it.
RUNNER_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runner_jobs (
    name                 TEXT PRIMARY KEY,   -- a job of runner.JOBS, or 'runner' (the loop's heartbeat)
    next_due             TEXT,               -- UTC; NULL = not planned yet
    last_run_id          INTEGER,
    last_status          TEXT,
    last_ok_at           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    heartbeat_at         TEXT,
    updated_at           TEXT NOT NULL,
    version              INTEGER NOT NULL DEFAULT 0
);
CREATE TRIGGER IF NOT EXISTS runner_jobs_version AFTER UPDATE ON runner_jobs FOR EACH ROW
WHEN NEW.version IS OLD.version
BEGIN UPDATE runner_jobs SET version = OLD.version + 1 WHERE name = NEW.name; END;

CREATE TABLE IF NOT EXISTS runner_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    boot_id     TEXT NOT NULL,           -- the runner process that ran it
    job         TEXT NOT NULL,
    key         TEXT,                    -- NULL for a tick
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'timeout', 'interrupted', 'skipped')),
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    pid         INTEGER,                 -- the child (the leader of its own process group)
    timeout_s   REAL,
    result      TEXT,                    -- the handler's JSON result line
    error       TEXT,                    -- why it failed or was skipped; a failed child's stderr tail (<= 2 kB)
    actor       TEXT NOT NULL,           -- runner:<job>
    via         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS runner_runs_key ON runner_runs(job, key)
    WHERE key IS NOT NULL AND status != 'interrupted';
CREATE INDEX IF NOT EXISTS runner_runs_job ON runner_runs(job, id);
CREATE INDEX IF NOT EXISTS runner_runs_running ON runner_runs(status) WHERE status = 'running';
CREATE TRIGGER IF NOT EXISTS runner_runs_finished BEFORE UPDATE ON runner_runs
WHEN OLD.status != 'running'
BEGIN SELECT RAISE(ABORT, 'runner_runs: a finished run is immutable (status leaves running once)'); END;

CREATE TABLE IF NOT EXISTS runner_runs_prune_gate (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    allowed INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO runner_runs_prune_gate(id, allowed) VALUES (1, 0);
CREATE TRIGGER IF NOT EXISTS runner_runs_guarded_delete BEFORE DELETE ON runner_runs
WHEN (SELECT allowed FROM runner_runs_prune_gate WHERE id = 1) = 0 OR OLD.status = 'running'
BEGIN SELECT RAISE(ABORT, 'runner_runs deletes only through store.prune_runner_runs (retention)'); END;
"""

# ---- project docs (dashboard phase 2d part 2, D-161, D-152, D-181; §7.2, §7.7): PROGRESS, GOALS,
# EXCEPTIONS, ALERTS and the review reports per project (project_docs.py maps the files; actions.py
# writes: doc.append / doc.set / doc.sync). One table of documents and ONE append-only table of
# their entries, for both kinds:
#   kind 'log'  (progress, exceptions, alerts): the text is the concatenation of its entries; a
#               writer appends ('append'). An in-place edit of the file (dual-write period) is a
#               'replace' entry: keep the first `keep` entries of the current version, then this body
#               (the changed tail follows as appends), so history stays append-only and small.
#   kind 'body' (goals, review:<name>): every change is one 'replace' (keep 0) holding the whole new
#               body: the current version is the newest one, the earlier ones are its history.
# The current text (doc_render) is byte-identical to the file it came from, so the public build log
# (PROGRESS.md, GOALS.md, EXCEPTIONS.md) can be regenerated from the DB with no diff (§7.7).
# docs.file_sha256/file_entry: the dual-write sync mark (the file as the last import or export saw
# or wrote it, and the newest entry id then), so the importer can tell "the file changed" from "the
# DB moved on" and never drops a DB entry silently (project_docs.py, actions.doc_sync).
DOCS_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    name        TEXT NOT NULL,           -- progress | goals | exceptions | alerts | review:<name>
    kind        TEXT NOT NULL CHECK (kind IN ('log', 'body')),
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL,
    file_sha256 TEXT,                    -- sync mark: sha256 of the file at the last import/export
    file_entry  INTEGER,                 -- sync mark: the newest doc_entries.id at that time
    version     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (project_id, name)
);
CREATE TRIGGER IF NOT EXISTS docs_version AFTER UPDATE ON docs FOR EACH ROW WHEN NEW.version IS OLD.version
BEGIN UPDATE docs SET version = OLD.version + 1 WHERE id = NEW.id; END;
CREATE TRIGGER IF NOT EXISTS docs_no_delete BEFORE DELETE ON docs
BEGIN SELECT RAISE(ABORT, 'docs are never deleted'); END;

CREATE TABLE IF NOT EXISTS doc_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL REFERENCES docs(id),
    op          TEXT NOT NULL CHECK (op IN ('append', 'replace')),
    keep        INTEGER NOT NULL DEFAULT 0 CHECK (keep >= 0),   -- replace: current entries kept before body
    ts          TEXT NOT NULL,
    author      TEXT NOT NULL,           -- the actor (cli, mcp:<session>, runner:import, ...)
    via         TEXT NOT NULL,
    body        TEXT NOT NULL,           -- the exact text (no newline added or stripped)
    body_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS doc_entries_doc ON doc_entries(doc_id, id);
CREATE TRIGGER IF NOT EXISTS doc_entries_no_update BEFORE UPDATE ON doc_entries
BEGIN SELECT RAISE(ABORT, 'doc_entries is append-only'); END;
CREATE TRIGGER IF NOT EXISTS doc_entries_no_delete BEFORE DELETE ON doc_entries
BEGIN SELECT RAISE(ABORT, 'doc_entries is append-only'); END;
"""

# ---- prompt defaults (D-227): the code's current prompts/*.md imported into the DB, every
# deploy (`python3 store.py import-prompts`, run by docker/entrypoint.sh after the DB volume is
# mounted; the action is actions.prompt_defaults_sync, actor runner:deploy). Defaults still ship
# as files (D-169) and readers still read the files for now (the prompts.py loader is phase 3);
# this table exists so the DB always has a record of what is currently deployed, for later
# phases and so an override's `base_sha256` (prompt_overrides) can be checked against the
# current default without re-reading the file. One row per prompt name (the latest default
# only); `previous_sha256` is what it replaced (NULL the first time a name is seen). Additive,
# no schema_version bump (same treatment as accounts/handoffs/telemetry/runner-state/docs: a
# still-running older checkout must not see the DB as newer than itself).
PROMPT_DEFAULTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_defaults (
    name            TEXT PRIMARY KEY,     -- file name under prompts/, e.g. continue.md
    body            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    deployed_at     TEXT NOT NULL,        -- when this body was imported (changed, not every sync)
    code_version    TEXT,                 -- git commit or image build id, when known
    previous_sha256 TEXT                  -- the sha256 this replaced (NULL the first time)
);
"""

# ---- per-project prompt variables (D-197; phase 4a, prompts.py, docs/phase4_plan.md P1): prompts stay
# system-wide (D-193), a project only fills the variables prompts.PROJECT_VARS declares. One row per
# (project, variable) ever set; a reset sets value NULL (the row and its version stay, as in
# prompt_overrides), so the variable's declared default applies again. Written only through
# actions.py (prompt_var.set / prompt_var.reset / prompt_var.migrate). Additive, no schema_version bump.
PROMPT_VARS_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_vars (
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    name        TEXT NOT NULL,          -- a prompts.PROJECT_VARS name
    value       TEXT,                   -- NULL = reset to the variable's default
    version     INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT,
    PRIMARY KEY (project_id, name)
);
"""

# ---- the dashboard's browser sessions (phase 6a W5, docs/phase6a_plan.md §3.5; design §9.5, D-162): one
# row per LOGGED-IN session, server-side so it is revocable and listable (F11). Only the sha256 of the
# cookie's 256-bit id is stored, and the sha256 of its CSRF token. A pre-session (an anonymous browser's
# cookie before login) is stateless (dashboard.sessions: a server-key-signed, expiring token) since a
# W5 review found the earlier design -- a row per cookieless GET /me -- let an unauthenticated flood
# hold write locks (D-235: runners fail-stop on DB errors); it never reaches this table. A login, a
# step-up and a factor change start a new row (the id rotates) and end the old one ('rotated').
# created_at is the login (a rotated row keeps it: the absolute lifetime counts from the login),
# authenticated_at the last full authentication (the step-up window). user_id is 5a's users.id (no
# foreign key until 5a's users table exists). Written only through actions.py (auth.session_* audited;
# the last_seen touch is the row's own record, no audit row). `kind` stays for a future non-'full' row
# kind; every row written today is 'full'. Additive, no schema_version bump.
AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    id_sha256        TEXT NOT NULL UNIQUE,
    user_id          INTEGER,
    kind             TEXT NOT NULL CHECK (kind IN ('pre', 'full')),
    created_at       TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,         -- written at most once a minute
    authenticated_at TEXT,
    factors          TEXT NOT NULL DEFAULT '[]',   -- JSON list of dashboard.auth.factors names
    csrf_sha256      TEXT NOT NULL,
    user_agent       TEXT,                  -- a short summary for F11, never used for a decision
    ended_at         TEXT,
    end_reason       TEXT CHECK (end_reason IN ('logout', 'rotated', 'revoked', 'evicted', 'idle', 'expired')),
    CHECK ((kind = 'pre') = (user_id IS NULL)),
    CHECK ((ended_at IS NULL) = (end_reason IS NULL))
);
CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id, ended_at);
CREATE INDEX IF NOT EXISTS auth_sessions_ended ON auth_sessions(ended_at);
"""

# ---- the setup code (phase 6a W7, docs/phase6a_plan.md §3.7; design §9.3, §10.4): a one-time code,
# 'first_owner' (user_id NULL: minted while no person exists yet) or 'reset' (admin reset-auth,
# user_id = whose login it resets). Only the sha256 of the normalised code is stored; dashboard/setup.py
# does the constant-time compare. Single use (used_at), expiring; minting a new code of the same
# (kind, user_id) first supersedes any other open one (supersede_open_setup_codes), so there is at most
# one live code per kind/user. Written only through actions.py (setup.mint_code / setup.claim_code,
# audited: the hash and timestamps, never the raw code). Superseded in part by D-293 / 6b P10 (a setup
# URL with a token replaces the first_owner flow; this table's 'reset' codes are unaffected).
# Additive, no schema_version bump.
SETUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS setup_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('first_owner', 'reset')),
    user_id     INTEGER,                 -- 'reset': whose code (5a's users.id, no FK yet); else NULL
    code_hash   TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    CHECK ((kind = 'first_owner') = (user_id IS NULL))
);
CREATE INDEX IF NOT EXISTS setup_codes_open ON setup_codes(kind, user_id, used_at);
"""

# ---- the decision store (D-242; phase 4b of §7.6, D-107/D-151/D-158/D-176; decisions.py): one row per
# owner / task-manager decision, mirrored from DECISIONS.md (the source the task-manager edits for
# now; actions.decision_import, audited) with the full entry text and its fields. keywords is the
# source's Keywords line; keywords_added / keywords_removed are the task-manager's own changes
# (actions.decision_keywords, D-242), kept apart so a re-import never undoes them (the effective
# set: decision_keywords()). decisions_fts (FTS5, when the SQLite build has it: has_decisions_fts) is
# the full-text index over it, kept in step by the write functions below; without FTS5 a lookup
# uses LIKE. Additive, no schema_version bump (as prompt_defaults).
DECISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id               TEXT PRIMARY KEY,      -- D-NNN | M-NNN, as in the source
    title            TEXT NOT NULL,
    type             TEXT,                  -- owner | manager decision | task-manager (as written)
    date             TEXT,                  -- as written
    status           TEXT,                  -- the Status line as written
    state            TEXT NOT NULL CHECK (state IN ('active', 'partial', 'done', 'superseded')),
    supersedes       TEXT,
    keywords         TEXT NOT NULL DEFAULT '[]',   -- JSON list: the source's Keywords line
    keywords_added   TEXT NOT NULL DEFAULT '[]',   -- JSON list: added by the task-manager
    keywords_removed TEXT NOT NULL DEFAULT '[]',   -- JSON list (lower case): source keywords it dropped
    summary          TEXT,
    words            TEXT,                  -- the owner's words, verbatim
    source           TEXT,
    scope            TEXT,
    interpretation   TEXT,                  -- the (task-)manager's reading, not binding
    section          TEXT,                  -- the source's "## " heading above the entry
    body             TEXT NOT NULL,         -- the full entry text, heading line included
    body_sha256      TEXT NOT NULL,
    ord              INTEGER NOT NULL,      -- position in the source
    imported_at      TEXT NOT NULL,         -- when this body was imported (changed, not every run)
    updated_at       TEXT NOT NULL,
    updated_by       TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS decisions_ord ON decisions(ord);
"""
DECISIONS_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(id UNINDEXED, title, keywords, "
                 "summary, words, body, tokenize = 'porter unicode61')")
DECISION_STATES = ("active", "partial", "done", "superseded")
DECISION_ID_RE = re.compile(r"^[DM]-\d{1,5}$")

# v4: every UPDATE bumps `version` (unless the statement set it itself), so a stale
# read is detectable whoever wrote: actions.py, the dispatcher, or an older checkout.
# Created after COLUMNS (the column must exist). recursive_triggers is off, so the
# trigger's own UPDATE doesn't fire it again.
VERSIONED = {"projects": "id", "tasks": "id", "standing_rules": "id", "session_decisions": "session_id",
             "settings": "key", "prompt_overrides": "name", "inbox_messages": "id", "project_pauses": "project_id",
             "decisions": "id", "notices": "id"}
VERSION_TRIGGERS = [
    f"CREATE TRIGGER IF NOT EXISTS {t}_version AFTER UPDATE ON {t} FOR EACH ROW "
    f"WHEN NEW.version IS OLD.version BEGIN "
    f"UPDATE {t} SET version = OLD.version + 1 WHERE {pk} = NEW.{pk}; END"
    for t, pk in VERSIONED.items()] + [
    # prompt_vars has a two-column key, so its trigger is spelled out
    "CREATE TRIGGER IF NOT EXISTS prompt_vars_version AFTER UPDATE ON prompt_vars FOR EACH ROW "
    "WHEN NEW.version IS OLD.version BEGIN "
    "UPDATE prompt_vars SET version = OLD.version + 1 WHERE project_id = NEW.project_id AND name = NEW.name; END"]

# Columns added after a table first shipped: {table: [(name, decl), ...]}.
# connect() adds any that an existing database lacks.
VERSION_COL = ("version", "INTEGER NOT NULL DEFAULT 0")
COLUMNS = {
    "projects": [("manager_session", "TEXT"), VERSION_COL,
                 ("manager_model", "TEXT"), ("manager_effort", "TEXT")],   # D-260: manager_launch.py
    "sessions": [],
    "limit_hits": [],
    "tasks": [VERSION_COL, ("estimated_sessions", "REAL")],   # D-213: stage_eta.py
    "task_events": [],
    "session_decisions": [VERSION_COL],
    "standing_rules": [VERSION_COL],
    # D-227: base_sha256 vs the current prompt_defaults.sha256, refreshed by prompt.defaults_sync
    # on every import -- NULL until the first sync after this column exists, or when there's no
    # override (no row) / no prompt_defaults row yet for that name; else 0 (matches) or 1 (the
    # default moved on since this override was made).
    "prompt_overrides": [("base_outdated", "INTEGER")],
    # §7.7 switch: the file's text at the sync mark, the base of the 3-way merge when the file
    # (hand edits) and the DB (doc.append/doc.set) both changed since (actions.doc_sync).
    "docs": [("file_text", "TEXT")],
    # phase 3b S1 (docs/phase3b_plan.md §3.1): the project of a task-manager row; a request's run
    "driven_sessions": [("project_id", "INTEGER REFERENCES projects(id)")],
    "action_requests": [("run_id", "INTEGER"), ("updated_at", "TEXT")],
    # D-244: a message whose triage call hit the account's usage limit waits until this time (the
    # limit's reset), then is triaged again once (inbox_worker.triage)
    "inbox_messages": [("retry_after", "TEXT"),
                       # D-304: the owner dismissed the answered thread on the dashboard (inbox.dismiss);
                       # until then it stays in the questions section; a later reply clears it again
                       ("dismissed_at", "TEXT")],
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


# ---- long write transactions, attributable (incident 16.10.: a ~40-s lock with no audit row)
#
# Every connection connect() opens is a TimedConnection: it notes when a transaction opens
# (after the first statement that leaves in_transaction true: BEGIN IMMEDIATE, or a DML
# statement with Python's implicit BEGIN, so the write lock is held from then on) and where
# (the first frames outside store.py), and when it ends (commit, rollback, close, `with conn:`,
# or a statement that ended it). One held longer than SLOW_TX_SECONDS is a WARN line on stderr
# (the process's own log) and in app_log (component "db", actions.app_log_append), naming the
# process, its pid, how it ended and the code that opened it. The cost is one Python call and
# an attribute check per statement; the frame walk runs once per transaction.
SLOW_TX_SECONDS = 3.0
_STORE_FILE = os.path.abspath(__file__)


def _tx_origin() -> str:
    """The code that opened a transaction: up to 6 frames outside store.py and contextlib,
    innermost first ('stalled.py:212 scan < export_quickview.py:315 build')."""
    out: list[str] = []
    f: FrameType | None = sys._getframe(1)
    while f is not None and len(out) < 6:
        fn = f.f_code.co_filename
        if os.path.basename(fn) != "contextlib.py" and os.path.abspath(fn) != _STORE_FILE:
            out.append(f"{os.path.basename(fn)}:{f.f_lineno} {f.f_code.co_name}")
        f = f.f_back
    return " < ".join(out) or "?"


def _report_slow_tx(conn: sqlite3.Connection, seconds: float, how: str, origin: str) -> None:
    """One slow transaction: the WARN line on stderr and in app_log ("db"). The transaction
    itself is over (committed or rolled back), so a failure to store the line doesn't fail
    it: the failure goes to stderr next to the line instead (never silent)."""
    proc = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "python"
    line = (f"WARN: slow write transaction: held the database write lock {seconds:.1f} s "
            f"(ended by {how}) in {proc} pid {os.getpid()}, opened at {origin}")[:2000]
    print(f"{now_iso()} {line}", file=sys.stderr, flush=True)
    try:
        import actions
        actions.app_log_append(conn, "db", [(now_iso(), "warn", line)], actor="runner:db", via="runner")
    except Exception as e:   # noqa: BLE001 - reported on stderr; the transaction is already over
        print(f"{now_iso()} ERROR: the slow-transaction warning above could not be stored in "
              f"app_log ({type(e).__name__}: {e})", file=sys.stderr, flush=True)


class TimedConnection(sqlite3.Connection):
    """sqlite3.Connection that reports write transactions held longer than SLOW_TX_SECONDS
    (see above). Behaves exactly like its base otherwise."""
    _tx_start: float | None = None
    _tx_origin: str = ""
    _tx_reporting: bool = False

    def _tx_mark(self, how: str) -> None:
        if self._tx_reporting:
            return
        try:
            open_ = self.in_transaction
        except sqlite3.ProgrammingError:   # closed / wrong thread: the statement itself raised that
            return
        if open_:
            if self._tx_start is None:
                self._tx_start, self._tx_origin = time.monotonic(), _tx_origin()
        elif self._tx_start is not None:
            held, self._tx_start = time.monotonic() - self._tx_start, None
            if held > SLOW_TX_SECONDS:
                self._tx_reporting = True
                try:
                    _report_slow_tx(self, held, how, self._tx_origin)
                finally:
                    self._tx_reporting = False

    def execute(self, *args: Any) -> sqlite3.Cursor:
        try:
            return super().execute(*args)
        finally:
            self._tx_mark("a statement")

    def executemany(self, *args: Any) -> sqlite3.Cursor:
        try:
            return super().executemany(*args)
        finally:
            self._tx_mark("a statement")

    def executescript(self, *args: Any) -> sqlite3.Cursor:
        try:
            return super().executescript(*args)
        finally:
            self._tx_mark("a script")

    def commit(self) -> None:
        try:
            super().commit()
        finally:
            self._tx_mark("commit")

    def rollback(self) -> None:
        try:
            super().rollback()
        finally:
            self._tx_mark("rollback")

    def __exit__(self, *args: Any) -> Literal[False]:
        try:
            return super().__exit__(*args)
        finally:
            self._tx_mark("the with block")

    def close(self) -> None:
        try:
            if self._tx_start is not None and self.in_transaction:
                self.rollback()      # what close() does anyway; measured (and reported) first
        finally:
            super().close()


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
        conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT, factory=TimedConnection)
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
    conn.executescript(TELEMETRY_SCHEMA)
    conn.executescript(RUNNER_SCHEMA)
    conn.executescript(RUNNER_RUNS_SCHEMA)
    conn.executescript(STATUS_SNAPSHOT_SCHEMA)
    conn.executescript(DOCS_SCHEMA)
    conn.executescript(PROMPT_DEFAULTS_SCHEMA)
    conn.executescript(PROMPT_VARS_SCHEMA)
    conn.executescript(DECISIONS_SCHEMA)
    conn.executescript(AUTH_SCHEMA)
    conn.executescript(SETUP_SCHEMA)
    with transaction(conn):    # BEGIN IMMEDIATE: concurrent connects add each column once
        _init_decisions_fts(conn)
        conn.execute("INSERT OR IGNORE INTO accounts(id, label, created_at) VALUES (?, ?, ?)",
                     (DEFAULT_ACCOUNT, "this installation's Claude account", now_iso()))
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
    def one(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])
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
TASK_EDITABLE = ("title", "description", "project", "priority", "kind", "estimated_sessions")
ESTIMATE_MAX = 100.0          # estimated_sessions: full session windows a stage still needs (D-213)
PROJECT_EDITABLE = ("name", "description", "path", "manager_session")
PROJECT_LAUNCH = ("manager_model", "manager_effort")   # D-260: only through set_project_launch (owner-only)

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
    return iso_ms(_utcnow())


def iso_ms(dt: datetime) -> str:
    """`dt` as now_iso() writes it (always milliseconds), so the strings compare in time order."""
    dt = dt.astimezone(timezone.utc)
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


def _estimate(value: object) -> float | None:
    """estimated_sessions (D-213): a number of full session windows in 0..ESTIMATE_MAX, or
    None (cleared: stage_eta.py uses its default from history / the fallback)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= ESTIMATE_MAX:
        raise ValueError(f"estimated_sessions must be a number of session windows in 0..{ESTIMATE_MAX:g} "
                         f"or null, got {value!r}")
    return float(value)


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


def session_project(conn: sqlite3.Connection, session_id: str) -> Row | None:
    """The project this session is the task-manager of (projects.manager_session; the top-ranked
    one if several), or None."""
    r = conn.execute("SELECT * FROM projects WHERE manager_session=? ORDER BY rank, id LIMIT 1",
                     (session_id,)).fetchone()
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


def task_managers(conn: sqlite3.Connection) -> list[Row]:
    """The task-manager registry (phase 3b §2), read-only: EVERY project, by rank (D-064), as
    {project_id, name, rank, path, session, tmux, model, effort, paused, pause_reason, open, ready}.
    Each project is its task-manager session (D-281); session None = not created yet (it is
    created at the project's first start). tmux: the live driven_sessions task-manager row's
    name, or None. paused: the project's own pause (D-244), not the install-wide one."""
    rows = {int(r["project_id"]): str(r["tmux"]) for r in conn.execute(
        "SELECT project_id, tmux FROM driven_sessions WHERE kind='task-manager' AND ended_at IS NULL "
        "AND project_id IS NOT NULL AND tmux IS NOT NULL")}
    out: list[Row] = []
    for p in list_projects(conn):
        pause = get_project_pause(conn, int(p["id"]))
        paused = bool(pause and pause["paused"])
        out.append({"project_id": p["id"], "name": p["name"], "rank": p["rank"], "path": p["path"],
                    "session": p["manager_session"] or None, "tmux": rows.get(int(p["id"])),
                    "model": p.get("manager_model"), "effort": p.get("manager_effort"),
                    "paused": paused, "pause_reason": pause["reason"] if pause and paused else None,
                    "open": p["open"], "ready": p["ready"]})
    return out


def manager_sessions(conn: sqlite3.Connection) -> list[Row]:
    """The registry rows that have a task-manager session, in rank order: what every per-session
    runner job acts on (phase 3b S3). A project whose session is not created yet (D-281: at its
    first start) is skipped here, not an error."""
    return [t for t in task_managers(conn) if t["session"]]


def start_manager(conn: sqlite3.Connection) -> Row:
    """The task-manager a session-window start is anchored on (its `--session`, the deferred
    entry's and the start's pause check): the first of the registry (rank order, D-064) that is not
    paused (D-244), else the first (its own pause then holds the start, as every other one is paused
    too). The project the start continues is chosen later, after the gate and the timing
    (pick_project, phase 3b S4). No task-manager at all is an error (D-235)."""
    tms = manager_sessions(conn)
    if not tms:
        raise ValueError("no project has a task-manager session (projects.manager_session): nothing to start")
    return next((t for t in tms if not t["paused"]), tms[0])


# Phase 3b S4: the open work a start picks a project by. A pending stage is ready; an in_progress one
# is the task-manager's unfinished work (a run that ended mid-stage). blocked waits for the owner.
READY_STATUSES = ("pending", "in_progress")


def pick_project(conn: sqlite3.Connection, chained: object = None) -> Row:
    """The projects a start (a session-window start, a postponed one, a last-stretch slot) may
    continue, best first (phase 3b S4, docs/phase3b_plan.md §2), read-only:
    {"candidates": [registry row + ready, best, why], "skipped": [(name, why)]}.
      - a paused project (D-244) is skipped; so is one with no task-manager session yet AND no ready
        stage (D-281 creates its session at its first start, which needs work to start for);
      - `chained` (a project id: the run before is still going, D-274) comes first (D-281);
      - then the projects with ready stages (READY_STATUSES): all whose best stage is high, by
        project rank, then medium, then low (D-064; inside the project its task-manager decides);
      - then the task-managers without a ready stage, by rank (each still has its own work, as
        the only task-manager always had before S4).
    The caller passes over a candidate the owner holds or that is at a limit (D-204, D-239.1)
    while a later one exists. No candidate: the start holds ("no project to run"), not an error."""
    work: dict[int, tuple[int, int]] = {}
    marks = ",".join("?" * len(READY_STATUSES))
    for pid, n, best in conn.execute(
            f"SELECT project_id, COUNT(*), MIN(CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END) "
            f"FROM tasks WHERE status IN ({marks}) AND project_id IS NOT NULL GROUP BY project_id",
            READY_STATUSES):
        work[int(pid)] = (int(n), int(best))
    first: list[Row] = []
    ready: list[Row] = []
    rest: list[Row] = []
    skipped: list[tuple[str, str]] = []
    for t in task_managers(conn):
        n, best = work.get(int(t["project_id"]), (0, len(PRIORITIES)))
        if t["paused"]:
            skipped.append((str(t["name"]), f"paused ({t['pause_reason']})"))
            continue
        if not t["session"] and not n:
            skipped.append((str(t["name"]), "no task-manager session yet and no ready stage"))
            continue
        c = dict(t, ready=n, best=PRIORITIES[best] if n else None)
        if chained is not None and int(t["project_id"]) == chained:
            c["why"] = "chained: the run before is still going, so the start keeps its project (D-274, D-281)"
            first.append(c)
        elif n:
            c["why"] = f"{n} ready stage(s), best priority {c['best']}, project rank {t['rank']} (D-064)"
            ready.append(c)
        else:
            c["why"] = f"no ready stage, project rank {t['rank']}: its task-manager continues its own work"
            rest.append(c)
    ready.sort(key=lambda c: (PRIORITIES.index(str(c["best"])), int(c["rank"]), int(c["project_id"])))
    return {"candidates": first + ready + rest, "skipped": skipped}


def check_manager_setting(conn: sqlite3.Connection, setting: str) -> None:
    """The boot check of phase 3b S3 (docs/phase3b_plan.md §3.3), while the machine setting
    manager_session (or a legacy --session) still exists: it must be a registered task-manager
    session, else ValueError (the caller fail-stops, D-235)."""
    tms = manager_sessions(conn)
    if setting not in {str(t["session"]) for t in tms}:
        have = ", ".join(f"{t['name']} {str(t['session'])[:8]}" for t in tms) or "none"
        raise ValueError(f"the session {setting[:8]} (setting manager_session / --session) is no project's "
                         f"task-manager in the registry (projects.manager_session: {have})")


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


def set_project_launch(conn: sqlite3.Connection, ref: object, **fields: str | None) -> Row:
    """Set a project's task-manager launch (D-260): manager_model / manager_effort, already
    checked by the caller (actions.project.manager_launch, manager_launch.py); None = the default."""
    bad = set(fields) - set(PROJECT_LAUNCH)
    if bad:
        raise ValueError(f"not a launch field: {sorted(bad)} (fields: {', '.join(PROJECT_LAUNCH)})")
    with transaction(conn):
        p = _project_row(conn, ref)
        clean = {k: v for k, v in fields.items() if p[k] != v}
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
    clean: dict[str, str | float | None] = {
        k: _estimate(v) if k == "estimated_sessions" else _clean_task_field(k, v)
        for k, v in fields.items() if k != "project"}
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
    """Change title/description/project/priority/kind/estimated_sessions. Logs one 'updated'
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


def get_project_pause(conn: sqlite3.Connection, project_id: int) -> Row | None:
    """{project_id, paused (bool), reason, version, updated_at, updated_by}, or None if the project
    was never paused (version 0)."""
    r = conn.execute("SELECT * FROM project_pauses WHERE project_id=?", (project_id,)).fetchone()
    return dict(r, paused=bool(r["paused"])) if r else None


def project_pauses(conn: sqlite3.Connection) -> list[Row]:
    """The projects paused now, by project rank: the get_project_pause rows plus 'name'."""
    return [dict(r, paused=True) for r in conn.execute(
        "SELECT pp.*, p.name FROM project_pauses pp JOIN projects p ON p.id = pp.project_id "
        "WHERE pp.paused ORDER BY p.rank, p.id")]


def put_project_pause(conn: sqlite3.Connection, project_id: int, paused: bool, reason: str | None,
                      updated_by: str | None = None) -> Row | None:
    conn.execute("INSERT INTO project_pauses(project_id, paused, reason, updated_at, updated_by) "
                 "VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET paused=excluded.paused, "
                 "reason=excluded.reason, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                 (project_id, int(bool(paused)), reason, now_iso(), updated_by))
    return get_project_pause(conn, project_id)


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


def get_prompt_default(conn: sqlite3.Connection, name: str) -> Row | None:
    r = conn.execute("SELECT * FROM prompt_defaults WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def list_prompt_defaults(conn: sqlite3.Connection) -> list[Row]:
    return [dict(r) for r in conn.execute("SELECT * FROM prompt_defaults ORDER BY name")]


def put_prompt_default(conn: sqlite3.Connection, name: str, body: str, sha256: str,
                       code_version: str | None) -> tuple[Row, bool]:
    """Upsert prompts/<name>'s current default text (D-227). Idempotent: when the stored
    sha256 already matches, this writes nothing (an unchanged file is a no-op on every deploy)
    and returns the existing row with False. Otherwise inserts/updates with deployed_at = now
    and previous_sha256 = what it replaced (NULL the first time this name is seen).
    -> (the row after, True if it was inserted or changed)."""
    cur = get_prompt_default(conn, name)
    if cur is not None and cur["sha256"] == sha256:
        return cur, False
    previous = cur["sha256"] if cur is not None else None
    conn.execute(
        "INSERT INTO prompt_defaults(name, body, sha256, deployed_at, code_version, previous_sha256) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET body=excluded.body, "
        "sha256=excluded.sha256, deployed_at=excluded.deployed_at, code_version=excluded.code_version, "
        "previous_sha256=excluded.previous_sha256", (name, body, sha256, now_iso(), code_version, previous))
    row = get_prompt_default(conn, name)
    assert row is not None     # just written, in the same connection
    return row, True


def mark_override_base_outdated(conn: sqlite3.Connection, name: str, outdated: bool | None) -> None:
    """Set prompt_overrides.base_outdated for `name`, only if that column exists (it is added
    additively by COLUMNS on connect(), but this stays defensive: a caller against an older-
    shaped connection must not crash a deploy over a cosmetic flag). No-op if there's no
    override row for `name` (nothing to flag)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(prompt_overrides)")}
    if "base_outdated" not in cols:
        return
    conn.execute("UPDATE prompt_overrides SET base_outdated=? WHERE name=?",
                 (None if outdated is None else int(outdated), name))


def get_prompt_var(conn: sqlite3.Connection, project_id: int, name: str) -> Row | None:
    r = conn.execute("SELECT * FROM prompt_vars WHERE project_id=? AND name=?", (project_id, name)).fetchone()
    return dict(r) if r else None


def list_prompt_vars(conn: sqlite3.Connection, project_id: int | None = None) -> list[Row]:
    """Every prompt_vars row (incl. reset ones, value NULL), of one project or all, by project and name."""
    if project_id is None:
        return [dict(r) for r in conn.execute("SELECT * FROM prompt_vars ORDER BY project_id, name")]
    return [dict(r) for r in conn.execute("SELECT * FROM prompt_vars WHERE project_id=? ORDER BY name",
                                          (project_id,))]


def put_prompt_var(conn: sqlite3.Connection, project_id: int, name: str, value: str | None,
                   updated_by: str | None = None) -> Row:
    """Set one project's variable (value None = reset: the row and its version stay)."""
    conn.execute("INSERT INTO prompt_vars(project_id, name, value, updated_at, updated_by) VALUES (?, ?, ?, ?, ?) "
                 "ON CONFLICT(project_id, name) DO UPDATE SET value=excluded.value, "
                 "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                 (project_id, name, value, now_iso(), updated_by))
    row = get_prompt_var(conn, project_id, name)
    assert row is not None     # just written, in the same connection
    return row


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


AUTH_SESSION_END_REASONS = ("logout", "rotated", "revoked", "evicted", "idle", "expired")


def _auth_session(r: sqlite3.Row | None) -> Row | None:
    return dict(r, factors=_loads(r["factors"])) if r else None


def add_auth_session(conn: sqlite3.Connection, id_sha256: str, user_id: int | None, csrf_sha256: str, *,
                     factors: Sequence[str] = (), user_agent: str | None = None, created_at: str | None = None,
                     authenticated_at: str | None = None) -> int:
    """A new session row (kind 'full' with a user, else 'pre'). created_at: the login's time for a
    rotated row (default now); authenticated_at: the last full authentication (None for a pre-session)."""
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO auth_sessions(id_sha256, user_id, kind, created_at, last_seen_at, authenticated_at, "
        "factors, csrf_sha256, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (id_sha256, user_id, "pre" if user_id is None else "full", created_at or now, now,
         authenticated_at, json.dumps(list(factors)), csrf_sha256, user_agent))
    return _rowid(cur)


def get_auth_session(conn: sqlite3.Connection, id_sha256: str) -> Row | None:
    return _auth_session(conn.execute("SELECT * FROM auth_sessions WHERE id_sha256=?", (id_sha256,)).fetchone())


def get_auth_session_by_id(conn: sqlite3.Connection, session_id: int) -> Row | None:
    return _auth_session(conn.execute("SELECT * FROM auth_sessions WHERE id=?", (session_id,)).fetchone())


def open_auth_sessions(conn: sqlite3.Connection, user_id: int) -> list[Row]:
    """A user's sessions not ended yet (some may be past a lifetime), oldest first."""
    rows = conn.execute("SELECT * FROM auth_sessions WHERE user_id=? AND ended_at IS NULL ORDER BY created_at, id",
                        (user_id,))
    return [r for r in (_auth_session(x) for x in rows) if r is not None]


def end_auth_session(conn: sqlite3.Connection, session_id: int, reason: str) -> bool:
    """End a session (once: an ended one keeps its first end). -> whether it was open."""
    if reason not in AUTH_SESSION_END_REASONS:
        raise ValueError(f"unknown session end reason {reason!r}")
    return conn.execute("UPDATE auth_sessions SET ended_at=?, end_reason=? WHERE id=? AND ended_at IS NULL",
                        (now_iso(), reason, session_id)).rowcount == 1


def touch_auth_session(conn: sqlite3.Connection, session_id: int, seen_before: str) -> bool:
    """Set last_seen_at to now when the stored one is `seen_before` or older (the once-a-minute
    rule, kept in the UPDATE so two racing requests write once). -> whether it wrote."""
    return conn.execute("UPDATE auth_sessions SET last_seen_at=? WHERE id=? AND ended_at IS NULL "
                        "AND last_seen_at <= ?", (now_iso(), session_id, seen_before)).rowcount == 1


def prune_ended_sessions(conn: sqlite3.Connection, ended_before: str, abandoned_before: str) -> int:
    """Delete sessions past their end plus a short retention (finding 2 of the W5 review): ended
    (logout, rotation, revocation, idle or expiry having been noticed) with ended_at < `ended_before`,
    or never explicitly ended but created before `abandoned_before` (a browser that expired and never
    came back to be noticed; `abandoned_before` is the caller's now - absolute_lifetime - retention, so
    only a session that is certainly past its lifetime is swept this way). -> number deleted."""
    return conn.execute(
        "DELETE FROM auth_sessions WHERE (ended_at IS NOT NULL AND ended_at < ?) "
        "OR (ended_at IS NULL AND created_at < ?)", (ended_before, abandoned_before)).rowcount


SETUP_CODE_KINDS = ("first_owner", "reset")


def supersede_open_setup_codes(conn: sqlite3.Connection, kind: str, user_id: int | None) -> int:
    """Mark every open (unused) code of this (kind, user_id) used 'superseded': a fresh mint always
    leaves at most one live code per kind/user. -> number superseded."""
    if kind not in SETUP_CODE_KINDS:
        raise ValueError(f"unknown setup code kind {kind!r}")
    return conn.execute(
        "UPDATE setup_codes SET used_at=? WHERE kind=? AND user_id IS ? AND used_at IS NULL",
        (now_iso(), kind, user_id)).rowcount


def add_setup_code(conn: sqlite3.Connection, kind: str, user_id: int | None, code_hash: str,
                   expires_at: str) -> int:
    if kind not in SETUP_CODE_KINDS:
        raise ValueError(f"unknown setup code kind {kind!r}")
    if (kind == "first_owner") != (user_id is None):
        raise ValueError("first_owner codes have no user_id; reset codes need one")
    cur = conn.execute("INSERT INTO setup_codes(kind, user_id, code_hash, created_at, expires_at) "
                       "VALUES (?, ?, ?, ?, ?)", (kind, user_id, code_hash, now_iso(), expires_at))
    return _rowid(cur)


def open_setup_code(conn: sqlite3.Connection, kind: str, user_id: int | None) -> Row | None:
    """The one live (unused, not necessarily unexpired) code of this (kind, user_id), if any.
    dashboard.setup compares the submitted code against it in constant time."""
    r = conn.execute("SELECT * FROM setup_codes WHERE kind=? AND user_id IS ? AND used_at IS NULL "
                     "ORDER BY id DESC LIMIT 1", (kind, user_id)).fetchone()
    return dict(r) if r else None


def get_setup_code_by_id(conn: sqlite3.Connection, code_id: int) -> Row | None:
    r = conn.execute("SELECT * FROM setup_codes WHERE id=?", (code_id,)).fetchone()
    return dict(r) if r else None


def claim_setup_code(conn: sqlite3.Connection, code_id: int, now: str) -> bool:
    """Spend a setup code (single-use, not expired): the atomic guard against two racing claims
    (SQLite serialises writers; only one UPDATE matches used_at IS NULL). -> whether this call won."""
    return conn.execute("UPDATE setup_codes SET used_at=? WHERE id=? AND used_at IS NULL AND expires_at > ?",
                        (now, code_id, now)).rowcount == 1


def add_run_log(conn: sqlite3.Connection, component: object, decision: object, reason: str | None = None,
                session_id: str | None = None, task_id: int | None = None, ts: datetime | str | None = None) -> int:
    """One keep-alive/dispatcher decision (written by the runners from phase 2 on)."""
    cur = conn.execute("INSERT INTO run_log(ts, component, session_id, task_id, decision, reason) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (iso(ts) if ts else now_iso(), _text(component, "component", required=True), session_id,
                        task_id, _text(decision, "decision", required=True), reason))
    return _rowid(cur)


def has_run_log(conn: sqlite3.Connection, component: str, decision: str, prefix: str) -> bool:
    """A run_log row of `component` and `decision` whose reason starts with `prefix`."""
    return conn.execute("SELECT 1 FROM run_log WHERE component=? AND decision=? AND substr(reason, 1, ?)=? LIMIT 1",
                        (component, decision, len(prefix), prefix)).fetchone() is not None


def run_log(conn: sqlite3.Connection, component: str | None = None, limit: int = 50) -> list[Row]:
    if component is None:
        rows = conn.execute(f"SELECT * FROM run_log ORDER BY id DESC LIMIT {int(limit)}")
    else:
        rows = conn.execute(f"SELECT * FROM run_log WHERE component=? ORDER BY id DESC LIMIT {int(limit)}",
                            (component,))
    return [dict(r) for r in rows]


DRIVEN_FIELDS = ("tmux", "kind", "task_id", "started_at", "last_seen", "ended_at", "holder_kind", "rc_url",
                 "project_id")


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


def live_driven_with(conn: sqlite3.Connection, tmux: str | None = None, task_manager_of: int | None = None,
                     other_than: str | None = None) -> list[Row]:
    """The live rows (ended_at NULL) holding the tmux name `tmux`, or the live task-manager
    rows of project `task_manager_of`; `other_than`: not this session's row."""
    if (tmux is None) == (task_manager_of is None):
        raise ValueError("live_driven_with: tmux or task_manager_of, exactly one")
    args: list[object] = [tmux if tmux is not None else task_manager_of]
    where = "tmux=?" if tmux is not None else "kind='task-manager' AND project_id=?"
    sql = f"SELECT * FROM driven_sessions WHERE ended_at IS NULL AND {where}"
    if other_than is not None:
        sql += " AND session_id<>?"
        args.append(other_than)
    return [dict(r) for r in conn.execute(sql + " ORDER BY session_id", args)]


def open_request(conn: sqlite3.Connection, kind: str, target: str | None = None) -> Row | None:
    r = conn.execute("SELECT * FROM action_requests WHERE status='open' AND kind=? AND COALESCE(target, '')=?",
                     (kind, target or "")).fetchone()
    return dict(r) if r else None


def add_request(conn: sqlite3.Connection, actor: str, kind: str, target: str | None = None) -> Row:
    ts = now_iso()
    cur = conn.execute("INSERT INTO action_requests(ts, actor, kind, target, updated_at) VALUES (?, ?, ?, ?, ?)",
                       (ts, actor, kind, target, ts))
    return dict(conn.execute("SELECT * FROM action_requests WHERE id=?", (cur.lastrowid,)).fetchone())


def get_request(conn: sqlite3.Connection, request_id: int) -> Row | None:
    r = conn.execute("SELECT * FROM action_requests WHERE id=?", (request_id,)).fetchone()
    return dict(r) if r else None


def set_request_state(conn: sqlite3.Connection, request_id: int, status: str, result: str,
                      run_id: int | None) -> Row:
    """A request's outcome (actions.request_start / request_finish check the transition): the
    status, its JSON result, the run that handled it (kept when None), handled_at and updated_at."""
    ts = now_iso()
    conn.execute("UPDATE action_requests SET status=?, result=?, run_id=COALESCE(?, run_id), "
                 "handled_at=COALESCE(handled_at, ?), updated_at=? WHERE id=?",
                 (status, result, run_id, ts, ts, request_id))
    r = get_request(conn, request_id)
    if r is None:
        raise NotFound(f"no request #{request_id}")
    return r


def list_requests(conn: sqlite3.Connection, status: str | None = None, limit: int = 50) -> list[Row]:
    if status is None:
        rows = conn.execute(f"SELECT * FROM action_requests ORDER BY id DESC LIMIT {int(limit)}")
    else:
        rows = conn.execute(f"SELECT * FROM action_requests WHERE status=? ORDER BY id DESC LIMIT {int(limit)}",
                            (status,))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- telemetry (phase 2c, D-161)
#
# Written only through actions.append / actions.append_many / actions.put_record (the
# registered appenders, §7.3); these are their storage helpers and the readers.

def telemetry_ts(value: object) -> str:
    """A row's own time as sortable UTC ISO text (iso(), milliseconds); a value that isn't a
    parsable time is kept as its text ('' for none), so nothing is dropped for it."""
    if isinstance(value, datetime):
        return iso(value) or ""
    if not isinstance(value, str) or not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return iso(dt) or value


def _telemetry_table(table: str, kinds: Sequence[str]) -> str:
    if table not in kinds:
        raise ValueError(f"unknown telemetry table {table!r}")
    return table


def insert_telemetry(conn: sqlite3.Connection, table: str, payload: str, ts: str, stale: bool, actor: str,
                     via: str, account_id: str = DEFAULT_ACCOUNT) -> bool:
    """One append-only row; a payload already stored (same sha256) is skipped. -> inserted."""
    t = _telemetry_table(table, TELEMETRY_APPEND)
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    cur = conn.execute(f"INSERT OR IGNORE INTO {t}(account_id, ts, stale, row_key, actor, via, recorded_at, "
                       "payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (account_id, ts, 1 if stale else 0, key, actor, via, now_iso(), payload))
    return cur.rowcount > 0


def put_telemetry_record(conn: sqlite3.Connection, table: str, doc_key: str, payload: str, ts: str, final: bool,
                         actor: str, via: str, account_id: str = DEFAULT_ACCOUNT) -> str:
    """Insert or update one derived record. -> 'inserted' | 'updated' | 'same' (unchanged)."""
    t = _telemetry_table(table, TELEMETRY_DOCS)
    old = conn.execute(f"SELECT payload, ts, final FROM {t} WHERE account_id=? AND doc_key=?",
                       (account_id, doc_key)).fetchone()
    if old is None:
        conn.execute(f"INSERT INTO {t}(account_id, doc_key, ts, final, actor, via, updated_at, payload) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (account_id, doc_key, ts, 1 if final else 0, actor, via, now_iso(), payload))
        return "inserted"
    if (old["payload"], bool(old["final"])) == (payload, bool(final)):   # ts derives from the payload
        return "same"
    conn.execute(f"UPDATE {t} SET ts=?, final=?, actor=?, via=?, updated_at=?, payload=? "
                 "WHERE account_id=? AND doc_key=?",
                 (ts, 1 if final else 0, actor, via, now_iso(), payload, account_id, doc_key))
    return "updated"


def telemetry_payloads(conn: sqlite3.Connection, table: str, account_id: str = DEFAULT_ACCOUNT,
                       max_bytes: int | None = None, since: str | None = None) -> list[str]:
    """The payloads of an append-only or record table, oldest first (ts, then insertion).
    max_bytes: only the newest rows whose payloads (one newline each, as in the file) end within
    the last max_bytes bytes and start after its first byte -- what reading the end of the file
    (seek, drop the first, partial line) gives; since: rows with ts >= since."""
    t = _telemetry_table(table, TELEMETRY_APPEND + TELEMETRY_DOCS)
    order = "ts, id" if table in TELEMETRY_APPEND else "ts, doc_key"
    where, args = "account_id=?", [account_id]
    if since is not None:
        where += " AND ts >= ?"
        args.append(since)
    if max_bytes is None:
        rows = conn.execute(f"SELECT payload FROM {t} WHERE {where} ORDER BY {order}", args)
        return [str(r[0]) for r in rows]
    desc = ", ".join(f"{c.strip()} DESC" for c in order.split(","))
    rows = conn.execute(f"SELECT payload FROM (SELECT payload, {order}, SUM(length(CAST(payload AS BLOB)) + 1) "
                        f"OVER (ORDER BY {desc}) AS cum FROM {t} WHERE {where}) WHERE cum < ? "
                        f"ORDER BY {order}", args + [int(max_bytes)])
    return [str(r[0]) for r in rows]


def telemetry_signature(conn: sqlite3.Connection, table: str, account_id: str = DEFAULT_ACCOUNT) -> tuple[Any, ...]:
    """A cheap change marker of a table (rows, last row/update) for the readers' caches."""
    t = _telemetry_table(table, TELEMETRY_APPEND + TELEMETRY_DOCS)
    if table in TELEMETRY_APPEND:
        r = conn.execute(f"SELECT COUNT(*), MAX(id) FROM {t} WHERE account_id=?", (account_id,)).fetchone()
    else:
        r = conn.execute(f"SELECT COUNT(*), MAX(updated_at), SUM(version) FROM {t} WHERE account_id=?",
                         (account_id,)).fetchone()
    return tuple(r)


def telemetry_counts(conn: sqlite3.Connection, account_id: str | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in TELEMETRY_APPEND + TELEMETRY_DOCS:
        if account_id is None:
            out[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        else:
            out[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE account_id=?", (account_id,)).fetchone()[0])
    return out


# ---------------------------------------------------------------- runner state (phase 2d, D-161)
#
# Written only through actions.state_save / app_log_append / app_log_sync / job_put / job_started /
# own_session_add (§7.3); these are their storage helpers and the readers. runner_state.py maps
# the files.

def state_rows(conn: sqlite3.Connection, component: str, account_id: str = DEFAULT_ACCOUNT) -> dict[str, str]:
    """path -> payload of a component's rows, in insertion order."""
    rows = conn.execute("SELECT path, payload FROM runner_state WHERE account_id=? AND component=? ORDER BY id",
                        (account_id, component))
    return {str(r[0]): str(r[1]) for r in rows}


def state_set(conn: sqlite3.Connection, component: str, path: str, payload: str, actor: str, via: str,
              account_id: str = DEFAULT_ACCOUNT) -> str:
    """Insert or update one row. -> 'inserted' | 'updated' | 'same'."""
    old = conn.execute("SELECT payload FROM runner_state WHERE account_id=? AND component=? AND path=?",
                       (account_id, component, path)).fetchone()
    if old is None:
        conn.execute("INSERT INTO runner_state(account_id, component, path, payload, actor, via, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (account_id, component, path, payload, actor, via, now_iso()))
        return "inserted"
    if str(old[0]) == payload:
        return "same"
    conn.execute("UPDATE runner_state SET payload=?, actor=?, via=?, updated_at=? "
                 "WHERE account_id=? AND component=? AND path=?",
                 (payload, actor, via, now_iso(), account_id, component, path))
    return "updated"


def state_delete(conn: sqlite3.Connection, component: str, path: str, account_id: str = DEFAULT_ACCOUNT) -> bool:
    cur = conn.execute("DELETE FROM runner_state WHERE account_id=? AND component=? AND path=?",
                       (account_id, component, path))
    return cur.rowcount > 0


def runner_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Rows per runner_state component, per scheduled-job status and per app_log component."""
    def grouped(sql: str) -> dict[str, int]:
        return {str(r[0]): int(r[1]) for r in conn.execute(sql)}
    return {"runner_state": grouped("SELECT component, COUNT(*) FROM runner_state GROUP BY component"),
            "scheduled_jobs": grouped("SELECT status, COUNT(*) FROM scheduled_jobs GROUP BY status"),
            "app_log": grouped("SELECT component, COUNT(*) FROM app_log GROUP BY component")}


def insert_app_log(conn: sqlite3.Connection, ts: str, component: str, level: str, line: str, actor: str,
                   via: str) -> None:
    conn.execute("INSERT INTO app_log(ts, component, level, line, line_hash, actor, via, recorded_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (ts, component, level, line, hashlib.sha256(line.encode("utf-8")).hexdigest(), actor, via,
                  now_iso()))


def app_log_hash_counts(conn: sqlite3.Connection, component: str) -> dict[str, int]:
    """line_hash -> how many copies of that line the component's log holds."""
    rows = conn.execute("SELECT line_hash, COUNT(*) FROM app_log WHERE component=? GROUP BY line_hash", (component,))
    return {str(r[0]): int(r[1]) for r in rows}


def app_log_tail(conn: sqlite3.Connection, component: str, limit: int = 100) -> list[str]:
    """The component's last `limit` lines, oldest first."""
    rows = conn.execute("SELECT line FROM (SELECT line, ts, id FROM app_log WHERE component=? "
                        "ORDER BY ts DESC, id DESC LIMIT ?) ORDER BY ts, id", (component, int(limit)))
    return [str(r[0]) for r in rows]


def app_log_prune_counts(conn: sqlite3.Connection, cutoffs: Mapping[str, str]) -> dict[str, int]:
    """Read-only: how many rows prune_app_log(conn, cutoffs) would delete, per component (only
    components with at least one row due; D-130 -- this is app_log's own lines, never telemetry
    or usage data). No gate, no transaction, nothing changes (the dry-run path)."""
    out: dict[str, int] = {}
    for component, cutoff in cutoffs.items():
        n = conn.execute("SELECT COUNT(*) FROM app_log WHERE component=? AND ts<?",
                         (component, cutoff)).fetchone()[0]
        if n:
            out[component] = int(n)
    return out


def prune_app_log(conn: sqlite3.Connection, cutoffs: Mapping[str, str], *, batch_size: int = 1000) -> dict[str, int]:
    """Delete app_log rows with ts < their component's cutoff (ISO, exclusive), in batches of at
    most `batch_size` rows per DELETE so one prune never holds the write lock on one huge
    statement. The append-only trigger (app_log_guarded_delete) blocks every DELETE on app_log
    except the ones made here, through app_log_prune_gate -- opened and closed in THIS
    transaction only (see the trigger's comment in RUNNER_SCHEMA): no other connection can ever
    see the gate open, and a crash mid-prune rolls it (and any partial deletes) back shut with
    everything else. -> {component: rows deleted}, only components that lost at least one row.

    Deleting rows frees pages to SQLite's internal freelist, not back to the OS: the file itself
    doesn't shrink unless the database runs in incremental-auto_vacuum mode (PRAGMA
    incremental_vacuum, attempted below, a no-op in the default mode this database still uses)
    or is VACUUMed (a one-time, full-file rewrite under an exclusive lock -- an explicit,
    separate step for the owner to run by hand, not done automatically here)."""
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size!r}")

    def run() -> dict[str, int]:
        deleted: dict[str, int] = {}
        with transaction(conn):
            conn.execute("UPDATE app_log_prune_gate SET allowed=1 WHERE id=1")
            try:
                for component, cutoff in cutoffs.items():
                    total = 0
                    while True:
                        cur = conn.execute(
                            "DELETE FROM app_log WHERE id IN (SELECT id FROM app_log WHERE component=? "
                            "AND ts<? ORDER BY id LIMIT ?)", (component, cutoff, batch_size))
                        total += cur.rowcount
                        if cur.rowcount < batch_size:
                            break
                    if total:
                        deleted[component] = total
            finally:
                conn.execute("UPDATE app_log_prune_gate SET allowed=0 WHERE id=1")
        return deleted
    out = retrying(run, conn)
    if any(out.values()):
        try:
            conn.execute("PRAGMA incremental_vacuum")
        except sqlite3.Error:
            pass
    return out


def get_job(conn: sqlite3.Connection, job_id: str) -> Row | None:
    r = conn.execute("SELECT * FROM scheduled_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def put_job(conn: sqlite3.Connection, job_id: str, run_at: str, cmd: str, cwd: str | None, payload: str,
            actor: str, via: str) -> str:
    """Insert a spooled job or update its fields. -> 'inserted' | 'updated' | 'same'."""
    old = get_job(conn, job_id)
    if old is None:
        conn.execute("INSERT INTO scheduled_jobs(id, run_at, cmd, cwd, payload, created_at, actor, via, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (job_id, run_at, cmd, cwd, payload, now_iso(), actor, via, now_iso()))
        return "inserted"
    if (old["run_at"], old["cmd"], old["cwd"], old["payload"]) == (run_at, cmd, cwd, payload):
        return "same"
    conn.execute("UPDATE scheduled_jobs SET run_at=?, cmd=?, cwd=?, payload=?, actor=?, via=?, updated_at=? "
                 "WHERE id=?", (run_at, cmd, cwd, payload, actor, via, now_iso(), job_id))
    return "updated"


def mark_job_started(conn: sqlite3.Connection, job_id: str, actor: str, via: str, at: str | None = None) -> bool:
    cur = conn.execute("UPDATE scheduled_jobs SET status='started', started_at=?, actor=?, via=?, updated_at=? "
                       "WHERE id=? AND status='pending'", (at or now_iso(), actor, via, now_iso(), job_id))
    return cur.rowcount > 0


def list_jobs(conn: sqlite3.Connection, status: str | None = None) -> list[Row]:
    sql = "SELECT * FROM scheduled_jobs" + (" WHERE status=?" if status else "") + " ORDER BY run_at, id"
    return [dict(r) for r in conn.execute(sql, (status,) if status else ())]


# ---- the resident runner's jobs and runs (RUNNER_RUNS_SCHEMA; written only through actions.py)

RUNNER_JOB_FIELDS = ("next_due", "last_run_id", "last_status", "last_ok_at", "consecutive_failures",
                     "heartbeat_at")


def get_runner_job(conn: sqlite3.Connection, name: str) -> Row | None:
    r = conn.execute("SELECT * FROM runner_jobs WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def runner_job_rows(conn: sqlite3.Connection) -> dict[str, Row]:
    return {str(r["name"]): dict(r) for r in conn.execute("SELECT * FROM runner_jobs ORDER BY name")}


def put_runner_job(conn: sqlite3.Connection, name: str, fields: Mapping[str, object]) -> Row:
    """Insert the job's row or update the given fields (RUNNER_JOB_FIELDS). -> the row."""
    bad = set(fields) - set(RUNNER_JOB_FIELDS)
    if bad:
        raise ValueError(f"unknown runner_jobs field(s) {sorted(bad)}")
    now = now_iso()
    conn.execute("INSERT OR IGNORE INTO runner_jobs(name, updated_at) VALUES (?, ?)", (name, now))
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE runner_jobs SET {cols}, updated_at=? WHERE name=?", (*fields.values(), now, name))
    row = get_runner_job(conn, name)
    assert row is not None     # inserted above
    return row


# ---- the status snapshot (STATUS_SNAPSHOT_SCHEMA; written only through actions.snapshot_put)

def put_snapshot(conn: sqlite3.Connection, section: str, account_id: str, *, state: str, payload: str | None,
                 error: str | None, builder: int, computed_at: str) -> None:
    """Insert or replace the section's row (its version counts the replacements)."""
    conn.execute("INSERT INTO status_snapshot(section, account_id, computed_at, state, payload, error, builder) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(section, account_id) DO UPDATE SET "
                 "computed_at=excluded.computed_at, state=excluded.state, payload=excluded.payload, "
                 "error=excluded.error, builder=excluded.builder",
                 (section, account_id, computed_at, state, payload, error, builder))


def snapshot_rows(conn: sqlite3.Connection, account_id: str = DEFAULT_ACCOUNT) -> dict[str, Row]:
    """{section: row} of the account's status snapshot (payload still the JSON text)."""
    return {str(r["section"]): dict(r) for r in conn.execute(
        "SELECT * FROM status_snapshot WHERE account_id=? ORDER BY section", (account_id,))}


def get_run(conn: sqlite3.Connection, run_id: int) -> Row | None:
    r = conn.execute("SELECT * FROM runner_runs WHERE id=?", (run_id,)).fetchone()
    return dict(r) if r else None


def key_runs(conn: sqlite3.Connection, job: str, key: str) -> list[Row]:
    """Every run of the keyed run (job, key), oldest first (interrupted ones included)."""
    return [dict(r) for r in conn.execute("SELECT * FROM runner_runs WHERE job=? AND key=? ORDER BY id", (job, key))]


def insert_run(conn: sqlite3.Connection, boot_id: str, job: str, key: str | None, status: str, timeout_s: float | None,
               actor: str, via: str, error: str | None = None) -> int:
    """A new run row: 'running' (finished_at NULL) or an already finished one ('skipped')."""
    now = now_iso()
    cur = conn.execute("INSERT INTO runner_runs(boot_id, job, key, status, started_at, finished_at, timeout_s, error, "
                       "actor, via) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (boot_id, job, key, status, now, None if status == "running" else now, timeout_s, error,
                        actor, via))
    return _rowid(cur)


def set_run_pid(conn: sqlite3.Connection, run_id: int, pid: int) -> bool:
    return conn.execute("UPDATE runner_runs SET pid=? WHERE id=? AND status='running'", (pid, run_id)).rowcount > 0


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, result: str | None, error: str | None) -> bool:
    """running -> status (the trigger refuses a second change). -> False: it was not running."""
    cur = conn.execute("UPDATE runner_runs SET status=?, finished_at=?, result=?, error=? WHERE id=? AND status='running'",
                       (status, now_iso(), result, error, run_id))
    return cur.rowcount > 0


def running_runs(conn: sqlite3.Connection) -> list[Row]:
    return [dict(r) for r in conn.execute("SELECT * FROM runner_runs WHERE status='running' ORDER BY id")]


def recent_runs(conn: sqlite3.Connection, limit: int = 20, job: str | None = None) -> list[Row]:
    sql = "SELECT * FROM runner_runs" + (" WHERE job=?" if job else "") + " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, ((job,) if job else ()) + (int(limit),))]


def runner_runs_prune_count(conn: sqlite3.Connection, cutoff: str) -> int:
    """Read-only: how many runs prune_runner_runs(conn, cutoff) would delete."""
    return int(conn.execute("SELECT COUNT(*) FROM runner_runs WHERE status != 'running' AND started_at < ?",
                            (cutoff,)).fetchone()[0])


def prune_runner_runs(conn: sqlite3.Connection, cutoff: str, *, batch_size: int = 1000) -> int:
    """Delete the finished runs started before `cutoff` (ISO, exclusive), in batches, through
    runner_runs_prune_gate opened and closed in THIS transaction only (as prune_app_log does: no
    other connection ever sees it open; a running row is never deleted, gate or not). -> rows
    deleted. Inside a caller's transaction (actions.perform) it runs in that one."""
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size!r}")
    total = 0
    with transaction(conn):
        conn.execute("UPDATE runner_runs_prune_gate SET allowed=1 WHERE id=1")
        try:
            while True:
                cur = conn.execute("DELETE FROM runner_runs WHERE id IN (SELECT id FROM runner_runs WHERE "
                                   "status != 'running' AND started_at < ? ORDER BY id LIMIT ?)", (cutoff, batch_size))
                total += cur.rowcount
                if cur.rowcount < batch_size:
                    break
        finally:
            conn.execute("UPDATE runner_runs_prune_gate SET allowed=0 WHERE id=1")
    return total


# ---- compaction hand-offs (D-144, D-155, D-191; design §5.5)
#
# A long-running session (the task-manager) writes a short, structured hand-off for its
# compacted self (MCP afclaude_handoff or `tasks.py handoff write`, through actions.py); the
# keep-alive injects the latest unconsumed one into the continue message after the compaction
# and consumes it. One open hand-off per session counts: a newer one supersedes the older ones
# (consume_handoff closes them too).

# D-305: owner_messages, in_flight, lessons and working_notes carry what only the compaction
# summary used to hold (the owner's words, exact branch/commit names, errors and their lessons,
# the know-how used afterwards). Optional on write and read: older hand-offs lack them.
HANDOFF_LISTS = ("done", "in_progress", "next_steps", "owner_messages", "in_flight", "lessons", "working_notes")
HANDOFF_KEYS = ("summary",) + HANDOFF_LISTS + ("open_question_ids", "notes")
HANDOFF_MAX = 12000            # characters of the whole body as JSON (~3k tokens, well below a summary)
HANDOFF_TEXT_MAX = 2000        # summary, notes
HANDOFF_ITEM_MAX = 500         # one list item
HANDOFF_OWNER_ITEM_MAX = 1500  # one owner message, verbatim
HANDOFF_ITEMS_MAX = 30         # items per list (open_question_ids: 50)
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _session_id(value: object) -> str:
    sid = _text(value, "session_id", required=True)
    if not SESSION_ID_RE.match(sid):
        raise ValueError(f"session_id must be a full session UUID, got {sid!r}")
    return sid


def _str_list(value: object, what: str, item_max: int = HANDOFF_ITEM_MAX) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list of strings, got {type(value).__name__}")
    if len(value) > HANDOFF_ITEMS_MAX:
        raise ValueError(f"{what} has too many items ({len(value)} > {HANDOFF_ITEMS_MAX}); keep it short")
    out: list[str] = []
    for x in value:
        t = _text(x, f"an item of {what}")
        if t is None:
            continue
        if len(t) > item_max:
            raise ValueError(f"an item of {what} is too long ({len(t)} > {item_max} characters)")
        out.append(t)
    return out


def handoff_body(value: object) -> dict[str, Any]:
    """Validate + normalize a hand-off body (a dict or its JSON text): summary (required),
    done / in_progress / next_steps / owner_messages (verbatim) / in_flight / lessons /
    working_notes (lists of short strings; a single string is one item; missing = empty),
    open_question_ids (task ids), notes. Unknown keys and an oversized body are a ValueError."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as e:
            raise ValueError(f"the hand-off is not valid JSON: {e}") from None
    if not isinstance(value, dict):
        raise ValueError(f"the hand-off must be a JSON object, got {type(value).__name__}")
    unknown = sorted(set(value) - set(HANDOFF_KEYS))
    if unknown:
        raise ValueError(f"unknown hand-off field(s) {unknown} (known: {', '.join(HANDOFF_KEYS)})")
    summary = _text(value.get("summary"), "summary", required=True)
    notes = _text(value.get("notes"), "notes")
    for what, t in (("summary", summary), ("notes", notes)):
        if t is not None and len(t) > HANDOFF_TEXT_MAX:
            raise ValueError(f"{what} is too long ({len(t)} > {HANDOFF_TEXT_MAX} characters)")
    ids_raw = value.get("open_question_ids")
    if ids_raw is None:
        ids_raw = []
    if not isinstance(ids_raw, list) or len(ids_raw) > 50:
        raise ValueError("open_question_ids must be a list of at most 50 task ids")
    ids = [_position(x, "an open question id") for x in ids_raw]
    body: dict[str, Any] = {"summary": summary}
    for k in HANDOFF_LISTS:
        body[k] = _str_list(value.get(k), k, HANDOFF_OWNER_ITEM_MAX if k == "owner_messages" else HANDOFF_ITEM_MAX)
    body["open_question_ids"] = ids
    body["notes"] = notes
    if len(json.dumps(body, ensure_ascii=False)) > HANDOFF_MAX:
        raise ValueError(f"the hand-off is too long (> {HANDOFF_MAX} characters as JSON); keep it short")
    return body


def _handoff_dict(r: sqlite3.Row) -> Row:
    return dict(r, body=json.loads(r["body"]))


def get_handoff(conn: sqlite3.Connection, handoff_id: int) -> Row | None:
    r = conn.execute("SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
    return _handoff_dict(r) if r else None


def add_handoff(conn: sqlite3.Connection, session_id: object, body: object, project: object = None,
                created_by: str | None = None) -> Row:
    """A new hand-off for the session. project: a name, path or id (unknown = NotFound); default
    the project this session is the task-manager of (projects.manager_session), else none."""
    sid = _session_id(session_id)
    clean = handoff_body(body)
    if project is not None:
        project_id: int | None = _project_row(conn, project)["id"]
    else:
        mp = session_project(conn, sid)
        project_id = mp["id"] if mp else None
    with transaction(conn):
        cur = conn.execute("INSERT INTO handoffs(session_id, project_id, created_at, created_by, body) "
                           "VALUES (?, ?, ?, ?, ?)",
                           (sid, project_id, now_iso(), created_by, json.dumps(clean, ensure_ascii=False)))
        h = get_handoff(conn, _rowid(cur))
    assert h is not None
    return h


def latest_handoff(conn: sqlite3.Connection, session_id: str, unconsumed: bool = True) -> Row | None:
    """The session's newest hand-off (only an unconsumed one if `unconsumed`), or None."""
    sql = "SELECT * FROM handoffs WHERE session_id=?" + (" AND consumed_at IS NULL" if unconsumed else "")
    r = conn.execute(sql + " ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
    return _handoff_dict(r) if r else None


def list_handoffs(conn: sqlite3.Connection, session_id: str | None = None, limit: int = 20) -> list[Row]:
    """Newest first."""
    if session_id is None:
        rows = conn.execute(f"SELECT * FROM handoffs ORDER BY id DESC LIMIT {int(limit)}")
    else:
        rows = conn.execute(f"SELECT * FROM handoffs WHERE session_id=? ORDER BY id DESC LIMIT {int(limit)}",
                            (session_id,))
    return [_handoff_dict(r) for r in rows]


def consume_handoff(conn: sqlite3.Connection, handoff_id: object, start_key: object) -> Row:
    """Mark the hand-off consumed by the start `start_key` (its dedup key), together with every
    older unconsumed hand-off of the same session (superseded by it). Already consumed:
    InvalidTransition."""
    hid = _position(handoff_id, "handoff id")
    key = _text(start_key, "start_key", required=True)
    with transaction(conn):
        h = get_handoff(conn, hid)
        if h is None:
            raise NotFound(f"no hand-off #{hid}")
        if h["consumed_at"] is not None:
            raise InvalidTransition(f"hand-off #{hid} was already consumed ({h['consumed_by_start']})")
        conn.execute("UPDATE handoffs SET consumed_at=?, consumed_by_start=? "
                     "WHERE session_id=? AND id<=? AND consumed_at IS NULL", (now_iso(), key, h["session_id"], hid))
        out = get_handoff(conn, hid)
    assert out is not None
    return out


HANDOFF_LABELS = (("owner_messages", "The owner's messages (verbatim)"), ("in_flight", "In flight"),
                  ("done", "Done"), ("in_progress", "In progress"), ("next_steps", "Next steps"),
                  ("lessons", "Lessons from this run"), ("working_notes", "Working notes"))


def render_handoff(h: Mapping[str, Any]) -> str:
    """A hand-off as one line of text (the continue message is typed with tmux send-keys). A
    field an older hand-off lacks is left out."""
    b = h.get("body") or {}
    parts = [f"Hand-off #{h.get('id')} (written {h.get('created_at')})."]
    parts.append(f"Summary: {b.get('summary')}")
    for k, label in HANDOFF_LABELS:
        items = b.get(k) or []
        if items:
            q = '"' if k == "owner_messages" else ""
            parts.append(f"{label}: " + " ".join(f"({i}) {q}{x}{q}" for i, x in enumerate(items, 1)))
    ids = b.get("open_question_ids") or []
    if ids:
        parts.append("Open questions (AFClaude tasks): " + ", ".join(f"#{i}" for i in ids))
    if b.get("notes"):
        parts.append(f"Notes: {b['notes']}")
    return " ".join(" ".join(p.split()) for p in parts)


# ---- the owner's inbox to the task-manager (D-178, D-241; inbox.py)
#
# One row per message: the verbatim text (the only copy), the Haiku verdict, the delivery state and
# the answer. The raw reads and writes; actions.py (inbox.*) is the only caller of the writes.

INBOX_STATUSES = ("pending", "delivered", "noted", "included", "answered", "error")
INBOX_VERDICTS = ("now", "note")
INBOX_TEXT_MAX = 4000          # characters (a "now" message is typed into tmux in one go)
INBOX_FIELDS = ("status", "verdict", "verdict_reason", "triaged_at", "triage_cost_usd", "triage_usage",
                "hold", "hold_since", "retry_after", "delivered_at", "delivered_via", "included_at", "included_by",
                "answer", "answered_at", "error", "error_at", "dismissed_at")


def inbox_text(value: object) -> str:
    """The message as typed (not stripped: D-241 keeps it verbatim); empty or too long: ValueError."""
    if not isinstance(value, str):
        raise ValueError(f"text must be a string, got {type(value).__name__}")
    if not value.strip():
        raise ValueError("text must not be empty")
    if len(value) > INBOX_TEXT_MAX:
        raise ValueError(f"text is too long ({len(value)} > {INBOX_TEXT_MAX} characters)")
    if "\0" in value:
        raise ValueError("text must not contain NUL characters")
    return value


def _inbox_dict(r: sqlite3.Row) -> Row:
    return dict(r, triage_usage=_loads(r["triage_usage"]))


def get_inbox(conn: sqlite3.Connection, msg_id: int) -> Row | None:
    r = conn.execute("SELECT * FROM inbox_messages WHERE id=?", (msg_id,)).fetchone()
    return _inbox_dict(r) if r else None


def inbox_row(conn: sqlite3.Connection, msg_id: object) -> Row:
    mid = _position(msg_id, "message id")
    m = get_inbox(conn, mid)
    if m is None:
        raise NotFound(f"no inbox message #{mid}")
    return m


def add_inbox(conn: sqlite3.Connection, text: object, session_id: object, created_by: str) -> Row:
    clean = inbox_text(text)
    sid = _session_id(session_id)
    with transaction(conn):
        cur = conn.execute("INSERT INTO inbox_messages(created_at, created_by, session_id, text) VALUES (?, ?, ?, ?)",
                           (now_iso(), created_by, sid, clean))
        m = get_inbox(conn, _rowid(cur))
    assert m is not None
    return m


def list_inbox(conn: sqlite3.Connection, status: str | Iterable[str] | None = None, session_id: str | None = None,
               limit: int = 50) -> list[Row]:
    """Newest first."""
    where, args = [], []
    if status is not None:
        sts = [status] if isinstance(status, str) else list(status)
        for s in sts:
            _enum(s, INBOX_STATUSES, "status")
        where.append(f"status IN ({','.join('?' * len(sts))})")
        args += sts
    if session_id is not None:
        where.append("session_id=?")
        args.append(session_id)
    sql = "SELECT * FROM inbox_messages" + (" WHERE " + " AND ".join(where) if where else "")
    rows = conn.execute(sql + f" ORDER BY id DESC LIMIT {int(limit)}", args)
    return [_inbox_dict(r) for r in rows]


def inbox_threads(conn: sqlite3.Connection, limit: int = 200) -> list[Row]:
    """The answered messages the owner has not dismissed (D-304), oldest first."""
    rows = conn.execute("SELECT * FROM inbox_messages WHERE status='answered' AND dismissed_at IS NULL "
                        f"ORDER BY id LIMIT {int(limit)}")
    return [_inbox_dict(r) for r in rows]


def inbox_posted_since(conn: sqlite3.Connection, since: str) -> int:
    r = conn.execute("SELECT COUNT(*) FROM inbox_messages WHERE created_at >= ?", (since,)).fetchone()
    return int(r[0])


def update_inbox(conn: sqlite3.Connection, msg_id: int, allowed_from: Sequence[str], **fields: object) -> Row:
    """Set `fields` on the message if its status is one of `allowed_from` (else InvalidTransition)."""
    unknown = set(fields) - set(INBOX_FIELDS)
    if unknown:
        raise ValueError(f"unknown inbox field(s) {sorted(unknown)}")
    with transaction(conn):
        m = inbox_row(conn, msg_id)
        if m["status"] not in allowed_from:
            raise InvalidTransition(f"inbox message #{msg_id} is {m['status']}, not {'|'.join(allowed_from)}")
        vals = [json.dumps(v, ensure_ascii=False) if k == "triage_usage" and v is not None else v
                for k, v in fields.items()]
        conn.execute(f"UPDATE inbox_messages SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                     (*vals, msg_id))
        out = get_inbox(conn, msg_id)
    assert out is not None
    return out



# ---------------------------------------------------------------- notices (D-306)
#
# The notices table above; actions.py (notice.post / notice.dismiss and the review docs' notices)
# is the only caller of the writes.

NOTICE_TITLE_MAX = 200
NOTICE_TEXT_MAX = 4000
NOTICE_FIELDS = ("title", "text", "updated_at", "dismissed_at")


def notice_title(value: object) -> str:
    t = _text(value, "title", required=True)
    if "\n" in t or len(t) > NOTICE_TITLE_MAX:
        raise ValueError(f"title must be one line of at most {NOTICE_TITLE_MAX} characters")
    return t


def notice_text(value: object) -> str:
    t = _text(value, "text", required=True)
    if len(t) > NOTICE_TEXT_MAX:
        raise ValueError(f"text is too long ({len(t)} > {NOTICE_TEXT_MAX} characters)")
    if "\0" in t:
        raise ValueError("text must not contain NUL characters")
    return t


def get_notice(conn: sqlite3.Connection, notice_id: int) -> Row | None:
    r = conn.execute("SELECT n.*, p.name AS project_name FROM notices n LEFT JOIN projects p ON p.id = n.project_id "
                     "WHERE n.id=?", (notice_id,)).fetchone()
    return dict(r) if r else None


def notice_row(conn: sqlite3.Connection, notice_id: object) -> Row:
    nid = _position(notice_id, "notice id")
    n = get_notice(conn, nid)
    if n is None:
        raise NotFound(f"no notice #{nid}")
    return n


def notice_by_source(conn: sqlite3.Connection, source: str) -> Row | None:
    r = conn.execute("SELECT id FROM notices WHERE source=?", (source,)).fetchone()
    return get_notice(conn, int(r[0])) if r else None


def add_notice(conn: sqlite3.Connection, title: object, text: object, project_id: int | None, created_by: str,
               source: str | None = None) -> Row:
    t, body = notice_title(title), notice_text(text)
    with transaction(conn):
        cur = conn.execute("INSERT INTO notices(created_at, created_by, project_id, title, text, source) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (now_iso(), created_by, project_id, t, body, source))
        n = get_notice(conn, _rowid(cur))
    assert n is not None
    return n


def list_notices(conn: sqlite3.Connection, include_dismissed: bool = False, limit: int = 200) -> list[Row]:
    """The notices the owner has not dismissed (all of them with include_dismissed), oldest first."""
    where = "" if include_dismissed else " WHERE n.dismissed_at IS NULL"
    rows = conn.execute("SELECT n.*, p.name AS project_name FROM notices n LEFT JOIN projects p ON p.id = n.project_id"
                        f"{where} ORDER BY n.id LIMIT {int(limit)}")
    return [dict(r) for r in rows]


def update_notice(conn: sqlite3.Connection, notice_id: int, **fields: object) -> Row:
    unknown = set(fields) - set(NOTICE_FIELDS)
    if unknown:
        raise ValueError(f"unknown notice field(s) {sorted(unknown)}")
    with transaction(conn):
        notice_row(conn, notice_id)
        conn.execute(f"UPDATE notices SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                     (*fields.values(), notice_id))
        out = get_notice(conn, notice_id)
    assert out is not None
    return out


# ---------------------------------------------------------------- project docs (phase 2d part 2)
#
# DOCS_SCHEMA above: docs + the append-only doc_entries. These are the reads and the raw writes;
# actions.py (doc.append, doc.set, doc.sync) is the only caller of the writes.

DOC_KINDS = ("log", "body")
DOC_OPS = ("append", "replace")
DOC_NAMES = {"progress": "log", "exceptions": "log", "alerts": "log", "goals": "body"}
DOC_REVIEW_RE = re.compile(r"^review:[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")   # data/reviews/<name>.md: 'body'
DOC_BODY_MAX = 4 * 1024 * 1024        # characters of one entry (PROGRESS.md is ~70 KB today)
# Where an imported text is cut into entries (presentation only: the entries' concatenation is the
# text, byte for byte): before a heading, a top-level list item or a table row.
DOC_CHUNK_RE = re.compile(r"^(?:#{1,6} |- |\* |\| )")


def doc_kind(name: object) -> str:
    """The kind of a doc name ('log' | 'body'); ValueError for an unknown name."""
    if isinstance(name, str) and name in DOC_NAMES:
        return DOC_NAMES[name]
    if isinstance(name, str) and DOC_REVIEW_RE.match(name):
        return "body"
    raise ValueError(f"doc name must be one of {'|'.join(DOC_NAMES)} or review:<name>, got {name!r}")


def doc_text(value: object, what: str = "text", allow_empty: bool = False) -> str:
    """A doc body exactly as given (nothing stripped): a string without NUL, at most DOC_BODY_MAX."""
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string, got {type(value).__name__}")
    if "\0" in value:
        raise ValueError(f"{what} must not contain NUL characters")
    if len(value) > DOC_BODY_MAX:
        raise ValueError(f"{what} is too long ({len(value)} > {DOC_BODY_MAX} characters)")
    if not allow_empty and not value.strip():
        raise ValueError(f"{what} must not be empty")
    return value


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_chunks(text: str) -> list[str]:
    """Cut a text into entries at DOC_CHUNK_RE line starts; "".join(result) == text."""
    out: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if cur and DOC_CHUNK_RE.match(line):
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out


def get_doc(conn: sqlite3.Connection, project_id: int, name: str) -> Row | None:
    r = conn.execute("SELECT * FROM docs WHERE project_id=? AND name=?", (project_id, name)).fetchone()
    return dict(r) if r else None


def ensure_doc(conn: sqlite3.Connection, project_id: int, name: str, actor: str) -> Row:
    """The doc (created empty if missing; its kind follows from the name)."""
    d = get_doc(conn, project_id, name)
    if d is not None:
        return d
    now = now_iso()
    conn.execute("INSERT INTO docs(project_id, name, kind, created_at, created_by, updated_at, updated_by) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (project_id, name, doc_kind(name), now, actor, now, actor))
    d = get_doc(conn, project_id, name)
    assert d is not None
    return d


def doc_chunks(conn: sqlite3.Connection, doc_id: int, upto: int | None = None) -> list[str]:
    """The current version as its entries' bodies, in order (append: add one; replace: keep the
    first `keep`, then add its body); upto: the version as of that entry id (its entries up to it)."""
    chunks: list[str] = []
    for r in conn.execute("SELECT op, keep, body FROM doc_entries WHERE doc_id=? AND id <= ? ORDER BY id",
                          (doc_id, upto if upto is not None else 2 ** 62)):
        if r["op"] == "replace":
            del chunks[int(r["keep"]):]
        chunks.append(r["body"])
    return chunks


def doc_render(conn: sqlite3.Connection, doc_id: int) -> str:
    """The doc's current text (byte-identical to the file it was imported from)."""
    return "".join(doc_chunks(conn, doc_id))


def doc_last_entry(conn: sqlite3.Connection, doc_id: int) -> int:
    """The newest entry id of the doc (0: none)."""
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM doc_entries WHERE doc_id=?", (doc_id,)).fetchone()[0])


def add_doc_entry(conn: sqlite3.Connection, doc_id: int, op: str, body: str, actor: str, via: str,
                  keep: int = 0) -> int:
    """Append one entry (and touch the doc's updated_at/by). -> its id. Run inside a transaction."""
    _enum(op, DOC_OPS, "op")
    if op == "append" and keep:
        raise ValueError("keep applies to a replace only")
    now = now_iso()
    cur = conn.execute("INSERT INTO doc_entries(doc_id, op, keep, ts, author, via, body, body_sha256) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (doc_id, op, keep, now, actor, via, body, sha256_text(body)))
    conn.execute("UPDATE docs SET updated_at=?, updated_by=? WHERE id=?", (now, actor, doc_id))
    return _rowid(cur)


def set_doc_mark(conn: sqlite3.Connection, doc_id: int, file_text: str, entry: int) -> bool:
    """Record the sync mark: the file's text (and its sha256) as the import/export saw or wrote
    it, and the newest entry id then (only if it changed). -> whether it changed."""
    sha = sha256_text(file_text)
    cur = conn.execute("UPDATE docs SET file_sha256=?, file_entry=?, file_text=? WHERE id=? AND "
                       "(file_sha256 IS NOT ? OR file_entry IS NOT ? OR file_text IS NULL)",
                       (sha, entry, file_text, doc_id, sha, entry))
    return cur.rowcount > 0


def list_docs(conn: sqlite3.Connection, project_id: int | None = None) -> list[Row]:
    """Docs (of one project, or all) with their entry counts, by project and name."""
    sql = ("SELECT d.*, p.name AS project, (SELECT COUNT(*) FROM doc_entries e WHERE e.doc_id = d.id) AS entries "
           "FROM docs d JOIN projects p ON p.id = d.project_id")
    if project_id is None:
        rows = conn.execute(sql + " ORDER BY p.name, d.name")
    else:
        rows = conn.execute(sql + " WHERE d.project_id=? ORDER BY d.name", (project_id,))
    return [{k: r[k] for k in r.keys() if k != "file_text"} for r in rows]   # the merge base stays internal


def doc_entries(conn: sqlite3.Connection, doc_id: int, limit: int = 20) -> list[Row]:
    """The doc's newest entries (all ops), oldest first."""
    rows = conn.execute("SELECT * FROM (SELECT * FROM doc_entries WHERE doc_id=? ORDER BY id DESC LIMIT ?) "
                        "ORDER BY id", (doc_id, int(limit)))
    return [dict(r) for r in rows]


def doc_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Entries per doc, as 'project/name'."""
    return {f"{r[0]}/{r[1]}": int(r[2]) for r in conn.execute(
        "SELECT p.name, d.name, COUNT(e.id) FROM docs d JOIN projects p ON p.id = d.project_id "
        "LEFT JOIN doc_entries e ON e.doc_id = d.id GROUP BY d.id ORDER BY p.name, d.name")}


# ---------------------------------------------------------------- the decision store (D-242)
#
# DECISIONS_SCHEMA above. The reads, the lookup (find_decision_entries) and the raw writes;
# actions.py (decision.import, decision.keywords) is the only caller of the writes.

DECISION_FIELDS = ("title", "type", "date", "status", "state", "supersedes", "summary", "words", "source",
                   "scope", "interpretation", "section", "body")


def _init_decisions_fts(conn: sqlite3.Connection) -> None:
    """Create decisions_fts if this SQLite build has FTS5 (else lookups use LIKE), and rebuild it
    when it is out of step with the table (first created on a database that already had rows)."""
    try:
        conn.execute(DECISIONS_FTS)
    except sqlite3.OperationalError as e:
        if "fts5" in str(e):        # "no such module: fts5": this build has no FTS5
            return
        raise
    n = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    if conn.execute("SELECT COUNT(*) FROM decisions_fts").fetchone()[0] != n:
        conn.execute("DELETE FROM decisions_fts")
        for r in conn.execute("SELECT * FROM decisions").fetchall():
            _decision_fts_put(conn, _decision_dict(r))


def has_decisions_fts(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'decisions_fts'").fetchone() is not None


def _decision_dict(r: sqlite3.Row) -> Row:
    d = dict(r)
    for k in ("keywords", "keywords_added", "keywords_removed"):
        d[k] = json.loads(d[k] or "[]")
    d["effective_keywords"] = decision_keywords(d)
    return d


def decision_keywords(d: Mapping[str, Any]) -> list[str]:
    """The effective keywords: the source's, minus the ones the task-manager removed, plus the ones
    it added (case-insensitive, no duplicates)."""
    removed = {str(k).lower() for k in d.get("keywords_removed") or []}
    out: list[str] = []
    seen: set[str] = set()
    for k in list(d.get("keywords") or []) + list(d.get("keywords_added") or []):
        kl = str(k).lower()
        if kl not in removed and kl not in seen:
            seen.add(kl)
            out.append(str(k))
    return out


def decision_id(value: object) -> str:
    """'d-012' -> 'D-012' (upper-cased); ValueError unless it looks like D-NNN / M-NNN."""
    s = _text(value, "decision id", required=True).strip().upper()
    if not DECISION_ID_RE.match(s):
        raise ValueError(f"not a decision id: {value!r} (D-NNN or M-NNN)")
    return s


def get_decision_entry(conn: sqlite3.Connection, decision: object) -> Row | None:
    r = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id(decision),)).fetchone()
    return _decision_dict(r) if r is not None else None


def list_decision_entries(conn: sqlite3.Connection, states: Iterable[str] | None = None) -> list[Row]:
    """Every decision (or those in `states`), in source order."""
    rows = [_decision_dict(r) for r in conn.execute("SELECT * FROM decisions ORDER BY ord, id")]
    if states is None:
        return rows
    want = set(states)
    return [r for r in rows if r["state"] in want]


def _decision_fts_put(conn: sqlite3.Connection, d: Mapping[str, Any]) -> None:
    conn.execute("DELETE FROM decisions_fts WHERE id = ?", (d["id"],))
    conn.execute("INSERT INTO decisions_fts(id, title, keywords, summary, words, body) VALUES (?, ?, ?, ?, ?, ?)",
                 (d["id"], d["title"], ", ".join(decision_keywords(d)), d.get("summary") or "",
                  d.get("words") or "", d["body"]))


# fields that can change without the entry's text changing -- derived from the text by code that
# can itself change (e.g. state_of's title-marker inference, D-242 follow-up): body and section
# are excluded (body changing is 'changed'; section alone is 'moved', with ord).
DECISION_DERIVED_FIELDS = tuple(k for k in DECISION_FIELDS if k not in ("body", "section"))


def put_decision_entry(conn: sqlite3.Connection, e: Mapping[str, Any], by: str) -> tuple[str, Row | None]:
    """Insert or update one decision from the source (fields DECISION_FIELDS + id, keywords, ord),
    leaving the task-manager's keyword changes alone. -> (outcome, the row before): 'added',
    'changed' (the entry text differs), 'restated' (the text is the same but a derived field, e.g.
    state, comes out different -- the derivation logic changed, not the source), 'moved' (only its
    position/section), 'unchanged'."""
    did = decision_id(e["id"])
    if e["state"] not in DECISION_STATES:
        raise ValueError(f"{did}: state must be one of {'|'.join(DECISION_STATES)}")
    body = str(e["body"])
    sha = sha256_text(body)
    kw = json.dumps(list(e.get("keywords") or []), ensure_ascii=False)
    cur = get_decision_entry(conn, did)
    now = now_iso()
    vals = [e.get(k) for k in DECISION_FIELDS]
    if cur is None:
        conn.execute(f"INSERT INTO decisions(id, {', '.join(DECISION_FIELDS)}, keywords, body_sha256, ord, "
                     f"imported_at, updated_at, updated_by) VALUES ({', '.join('?' * (len(DECISION_FIELDS) + 7))})",
                     [did, *vals, kw, sha, int(e["ord"]), now, now, by])
        outcome = "added"
    elif cur["body_sha256"] != sha or cur["keywords"] != list(e.get("keywords") or []):
        conn.execute(f"UPDATE decisions SET {', '.join(k + ' = ?' for k in DECISION_FIELDS)}, keywords = ?, "
                     "body_sha256 = ?, ord = ?, imported_at = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                     [*vals, kw, sha, int(e["ord"]), now, now, by, did])
        outcome = "changed"
    elif any(cur.get(k) != e.get(k) for k in DECISION_DERIVED_FIELDS):
        conn.execute(f"UPDATE decisions SET {', '.join(k + ' = ?' for k in DECISION_FIELDS)}, keywords = ?, "
                     "body_sha256 = ?, ord = ?, imported_at = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                     [*vals, kw, sha, int(e["ord"]), now, now, by, did])
        outcome = "restated"
    elif cur["ord"] != int(e["ord"]) or cur["section"] != e.get("section"):
        conn.execute("UPDATE decisions SET ord = ?, section = ? WHERE id = ?", (int(e["ord"]), e.get("section"), did))
        return "moved", cur
    else:
        return "unchanged", cur
    if has_decisions_fts(conn):
        new = get_decision_entry(conn, did)
        assert new is not None
        _decision_fts_put(conn, new)
    return outcome, cur


def delete_decision_entry(conn: sqlite3.Connection, decision: object) -> Row:
    """Remove a decision that left the source. -> the row as it was."""
    cur = get_decision_entry(conn, decision)
    if cur is None:
        raise NotFound(f"no decision {decision}")
    conn.execute("DELETE FROM decisions WHERE id = ?", (cur["id"],))
    if has_decisions_fts(conn):
        conn.execute("DELETE FROM decisions_fts WHERE id = ?", (cur["id"],))
    return cur


def set_decision_keywords(conn: sqlite3.Connection, decision: object, added: Sequence[str],
                          removed: Sequence[str], by: str) -> Row:
    """Store the task-manager's keyword changes of one decision. -> the row after."""
    cur = get_decision_entry(conn, decision)
    if cur is None:
        raise NotFound(f"no decision {decision}")
    conn.execute("UPDATE decisions SET keywords_added = ?, keywords_removed = ?, updated_at = ?, updated_by = ? "
                 "WHERE id = ?", (json.dumps(list(added), ensure_ascii=False),
                                  json.dumps(sorted({k.lower() for k in removed}), ensure_ascii=False),
                                  now_iso(), by, cur["id"]))
    after = get_decision_entry(conn, cur["id"])
    assert after is not None
    if has_decisions_fts(conn):
        _decision_fts_put(conn, after)
    return after


def _kw_forms(s: str) -> set[str]:
    """'Night-Windows' -> {'night windows', 'night window'}: lower case, - and _ as spaces, and
    without a plural s on the last word."""
    words = [w for w in re.split(r"[-_\s]+", s.lower()) if w]
    if not words:
        return set()
    out = {" ".join(words)}
    if len(words[-1]) > 3 and words[-1].endswith("s"):
        out.add(" ".join(words[:-1] + [words[-1][:-1]]))
    return out


def _kw_match(term: str, keyword: str) -> bool:
    """A term matches a keyword when it is the keyword or a whole-word part of it (forms as _kw_forms)."""
    for t in _kw_forms(term):
        for k in _kw_forms(keyword):
            if t == k or f" {t} " in f" {k} ":
                return True
    return False


def find_decision_entries(conn: sqlite3.Connection, terms: Sequence[str],
                          states: Iterable[str] | None = None) -> list[Row]:
    """The decisions matching any of `terms`, best first: those with a matching keyword (most
    matched terms first), then those whose text matches (FTS5 rank when the index exists, else
    LIKE, most matched terms first). Each row gets "match" ('keyword' | 'text') and "matched"
    (the terms that hit its keywords, for a keyword match)."""
    terms = [" ".join(str(t).split()) for t in terms if str(t).strip()]
    if not terms:
        raise ValueError("find needs at least one keyword")
    rows = list_decision_entries(conn, states)
    text_rank: dict[str, float] = {}
    if has_decisions_fts(conn):
        q = " OR ".join('"' + t.replace('"', " ") + '"' for t in terms)
        for i, (did,) in enumerate(conn.execute(
                "SELECT id FROM decisions_fts WHERE decisions_fts MATCH ? ORDER BY bm25(decisions_fts)", (q,))):
            text_rank.setdefault(did, float(i))
    else:
        for r in rows:
            hay = " ".join(str(r.get(k) or "") for k in ("title", "summary", "words", "body")).lower()
            hay += " " + " ".join(r["effective_keywords"]).lower()
            n = sum(1 for t in terms if t.lower() in hay)
            if n:
                text_rank[r["id"]] = -n + r["ord"] / 1e6
    hits: list[tuple[tuple[float, ...], Row]] = []
    for r in rows:
        matched = [t for t in terms if any(_kw_match(t, k) for k in r["effective_keywords"])]
        rank = text_rank.get(r["id"])
        if matched:
            hits.append(((0, -len(matched), rank if rank is not None else 1e9, r["ord"]),
                         dict(r, match="keyword", matched=matched)))
        elif rank is not None:
            hits.append(((1, 0, rank, r["ord"]), dict(r, match="text", matched=[])))
    hits.sort(key=lambda h: h[0])
    return [h[1] for h in hits]


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
# handler), so a lock held throughout fails after ~30 s, as the single 30-s wait did before.
# No writer should come near that: the stalled-session scan commits in short batches
# (stalled.FLUSH_SECONDS), and a transaction held longer than SLOW_TX_SECONDS is logged with
# its origin (TimedConnection).
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


# plain names in the data dir (a schema .bak is matched by prefix): the sampler's legacy history (frozen since
# the files were retired, D-161) and its cron log (still written)
PRIOR_INSTALL_FILES = ("samples.jsonl", "sampler.log")


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


def fallback_note(headline: str, rec: Mapping[str, Any], db_path: str | None = None) -> str | None:
    """Record something that could not be written to the DB in the fallback file (§7.8), e.g. a
    runner's note for a project the DB doesn't know (project_docs.append_note). -> the file it
    went to, None (then it went to stderr). Never raises."""
    try:
        return _append_record(fallback_path(db_path), headline, dict(rec, time=rec.get("time") or now_iso()))
    except Exception:   # noqa: BLE001 - the fallback path must never take its caller down
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
    """notify.py (a push; the alert in the DB, or this fallback file while the DB is broken), never raising."""
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
                    write: bool = True, db_path: str | None = None, payload_sha: str | None = None,
                    payload_max: int = PAYLOAD_MAX, runner: bool = False) -> DBError:
    """The escalation of a failed DB action (§7.8): record it in the fallback file (a write with
    its payload, so nothing is lost), and alert the owner when the same request by the same
    actor already failed in this episode (its retry failed too, D-171), once per episode.
    payload_max: the biggest payload kept for a replay (the runners' telemetry rows and state
    saves keep more: the DB is their only copy). runner: a runner's own write (telemetry rows,
    state saves, jobs; their payloads differ every time): "the same request" is the same action
    by the same actor, and no alert while db_gate's pause episode is open (it alerted already).
    Sets err.not_saved and err.escalation (the hand-back). Never raises. -> err"""
    try:
        path = fallback_path(db_path)
        payload = json.dumps({"action": action, "params": dict(params or {})}, sort_keys=True,
                             ensure_ascii=False, default=str)
        sha = payload_sha or hashlib.sha256(payload.encode()).hexdigest()
        events = fallback_events(path)
        since = max((i for i, e in enumerate(events) if e.get("event") == "recovered"), default=-1)
        repeat = any(e.get("event") == "failed" and e.get("actor") == actor
                     and (e.get("action") == action if runner else e.get("payload_sha256") == sha)
                     for e in events[since + 1:])
        paused = _open_alert(events, "runner") if runner else None
        rec: Row = {"event": "failed", "time": now_iso(), "actor": actor, "action": action, "write": write,
                    "payload_sha256": sha, "error_class": err.error_class, "kind": err.kind,
                    "message": str(err)[:500]}
        rec["params"] = dict(params or {}) if len(payload) <= payload_max else None   # None: too big
        where = _append_record(path, f"failed {'write' if write else 'read'}: {action} by {actor} "
                                     f"({err.kind}: {str(err)[:200]})", rec)
        err.not_saved = ("nothing was changed by this call" if not write else
                         f"{action} was not applied; recorded in {where} (payload sha256 {sha[:12]}) for a replay"
                         if where else f"{action} was not applied")
        if paused is not None:
            err.escalation = (f"the runners are paused (store.db_gate) and the owner was alerted at "
                              f"{paused.get('time')}")
        elif repeat:
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


def runner_pause_open(db_path: str | None = None) -> str | None:
    """The time of the alert that paused the runners (db_gate), if that episode is still open
    (the fallback file, so it works while the DB is broken); None otherwise. Never raises."""
    try:
        opened = _open_alert(fallback_events(fallback_path(db_path)), "runner")
    except Exception:   # noqa: BLE001 - a status read
        return None
    return None if opened is None else str(opened.get("time"))


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
    t = sub.add_parser("import-telemetry", help="import the legacy data/ usage telemetry files of the kinds not "
                                                 "imported yet into the DB (idempotent, the files are not changed; "
                                                 "telemetry.py)")
    t.add_argument("--db", default=None, help="database path (default: $AFCLAUDE_DB or data/afclaude.db)")
    t.add_argument("--data-dir", default=None, help="the files' directory (default: the database's)")
    t.add_argument("--catch-up", action="store_true", help="re-import the append-only kinds already imported "
                                                           "(rows the DB has are skipped)")
    rs = sub.add_parser("import-state", help="import the legacy runner state files, scheduled jobs and own "
                                             "sessions not imported yet, and sync the logs into the DB "
                                             "(idempotent, the files are not changed; runner_state.py)")
    rs.add_argument("--db", default=None, help="database path (default: $AFCLAUDE_DB or data/afclaude.db)")
    rs.add_argument("--data-dir", default=None, help="the files' directory (default: the database's)")
    dc = sub.add_parser("import-docs", help="import the project docs (PROGRESS.md, GOALS.md, EXCEPTIONS.md, "
                                            "ALERTS.md, data/reviews/*.md) into the DB (idempotent, the files "
                                            "are not changed; project_docs.py)")
    dc.add_argument("--db", default=None, help="database path (default: $AFCLAUDE_DB or data/afclaude.db)")
    dc.add_argument("--repo", default=None, help="the project's directory holding the files (default: this repo)")
    dc.add_argument("--reviews-dir", default=None, help="the review reports (default: <the database's dir>/reviews)")
    pl = sub.add_parser("prune-logs", help="delete app_log rows past their retention (log_retention_days / "
                                           "log_retention_overrides, actions.SETTINGS); batched and audited "
                                           "(actor runner:retention); meant to run once a day from cron")
    pl.add_argument("--db", default=None, help="database path (default: $AFCLAUDE_DB or data/afclaude.db)")
    pl.add_argument("--dry-run", action="store_true", help="count what is due, change nothing")
    pl.add_argument("--batch-size", type=int, default=1000, help="rows per DELETE statement (default 1000)")
    pp = sub.add_parser("import-prompts", help="import prompts/*.md (except README.md) into prompt_defaults, "
                                               "the code's current defaults (D-227; idempotent, the files are "
                                               "not changed); run at every container start/deploy "
                                               "(docker/entrypoint.sh), audited as prompt.defaults_sync")
    pp.add_argument("--db", default=None, help="database path (default: $AFCLAUDE_DB or data/afclaude.db)")
    pp.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    dv = sub.add_parser("import-driven", help="the driven_sessions backfill of phase 3b (idempotent; driven.py): "
                                              "a row per task-manager and per running review / continued "
                                              "session, then `tasks.py driven check`")
    dv.add_argument("--db", default=None, help="database path (default: $AFCLAUDE_DB or data/afclaude.db)")
    dv.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = ap.parse_args(argv)
    if args.cmd == "import-driven":
        import importlib
        driven = importlib.import_module("driven")
        return int(driven.main(["import"] + (["--db", args.db] if args.db else [])
                               + (["--dry-run"] if args.dry_run else [])))
    if args.cmd == "import-prompts":
        import importlib
        actions = importlib.import_module("actions")
        try:
            conn = connect(args.db)
        except DBError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        try:
            if args.dry_run:
                print(json.dumps(actions.prompt_defaults_preview(conn), ensure_ascii=False))
                return 0
            out = actions.perform(conn, "prompt.defaults_sync", {}, actor="runner:deploy", via="runner")
            print(json.dumps(out, ensure_ascii=False))
            return 0
        except (DBError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        finally:
            conn.close()
    if args.cmd == "prune-logs":
        import importlib
        actions = importlib.import_module("actions")
        try:
            conn = connect(args.db)
        except DBError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        try:
            if args.dry_run:
                print(json.dumps(actions.log_prune_preview(conn), ensure_ascii=False))
                return 0
            out = actions.perform(conn, "log.prune", {"batch_size": args.batch_size},
                                  actor="runner:retention", via="runner")
            print(json.dumps(out, ensure_ascii=False))
            return 0
        except (DBError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        finally:
            conn.close()
    if args.cmd == "import-docs":
        import importlib
        project_docs = importlib.import_module("project_docs")
        argv4 = ["import"] + (["--db", args.db] if args.db else []) + \
            (["--repo", args.repo] if args.repo else []) + \
            (["--reviews-dir", args.reviews_dir] if args.reviews_dir else [])
        return int(project_docs.main(argv4))
    if args.cmd == "import-state":
        import importlib
        runner_state = importlib.import_module("runner_state")
        argv3 = ["import"] + (["--db", args.db] if args.db else []) + \
            (["--data-dir", args.data_dir] if args.data_dir else [])
        return int(runner_state.main(argv3))
    if args.cmd == "import-telemetry":
        import importlib
        telemetry = importlib.import_module("telemetry")
        argv2 = ["import"] + (["--db", args.db] if args.db else []) + \
            (["--data-dir", args.data_dir] if args.data_dir else []) + (["--catch-up"] if args.catch_up else [])
        return int(telemetry.main(argv2))
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
