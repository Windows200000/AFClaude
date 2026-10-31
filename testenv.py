"""Hermetic environment for the offline tests (import it BEFORE any AFClaude module).

Points the DB ($AFCLAUDE_DB), the local config ($AFCLAUDE_CONFIG) and the data dirs
($AFCLAUDE_DATA_DIR, $DISPATCHER_DATA_DIR) at a fresh temp directory, so a test run in
the main checkout never reads the live settings or triggers the one-time settings import
on the live data/ (afclaude_config). The temp DB starts empty (every setting = its code
default); set_setting() / clear_settings() change it. $AFCLAUDE_CONFIG is an empty identity file
(SID: a synthetic task-manager session id the tests use, not a real session anywhere).
"""
import atexit
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DIR = tempfile.mkdtemp(prefix="afclaude-test-")
atexit.register(shutil.rmtree, DIR, True)
os.environ.update(AFCLAUDE_DB=os.path.join(DIR, "afclaude.db"), AFCLAUDE_CONFIG=os.path.join(DIR, "afclaude.json"),
                  AFCLAUDE_DATA_DIR=DIR, DISPATCHER_DATA_DIR=DIR,
                  KEEPALIVE_LOCK_DIR=DIR)   # the keep-alive's session locks (compaction vs start, D-238)
os.environ["AFCLAUDE_DECISIONS_FILE"] = os.path.join(DIR, "DECISIONS.md")   # no file: the repo's own is never read
os.environ["AFCLAUDE_DIR"] = HERE   # docker/at_shim.py imports from it: a worktree run never loads the live checkout
os.environ.pop("CLAUDE_GUARD_DISABLE", None)   # an unattended session running the tests is not "autonomous" in them
os.environ.pop("AFCLAUDE_SCHEDULER", None)   # a container's mode never leaks in (health, the quickview read it)

SID = "00000000-0000-4000-8000-000000000001"   # synthetic task-manager session (not a real session anywhere)
with open(os.environ["AFCLAUDE_CONFIG"], "w") as _fh:
    json.dump({}, _fh)

import store  # noqa: E402
import actions  # noqa: E402

DB = os.environ["AFCLAUDE_DB"]
store.DB_PATH = DB
store.NOTIFY = lambda subject, body="": "test: not sent"   # a DB-error alert never pushes / writes the alerts doc
import notify  # noqa: E402
notify.push = lambda text: "test: push not sent"   # an alert (failstop.fail, keepalive.alert) never runs claude
_conn = store.connect(DB, create=True)   # explicit, like a real install's setup: an empty DB, no
                                         # "no database" log line from the settings reads
# the task-manager registry (phase 3b S3, projects.manager_session) holds the synthetic identity SID, as
# a live install's AFClaude project holds its session: code that read the setting reads the registry
store.add_project(_conn, "testenv-task-manager")
store.update_project(_conn, "testenv-task-manager", manager_session=SID)
_conn.close()


def fresh_db(test):
    """A new, empty DB for one test: store.DB_PATH and $AFCLAUDE_DB point at it until the test's
    cleanup puts the shared one back. For a test that needs a table to itself (e.g. no other
    module's projects): a DELETE on the shared DB can trip a foreign key another module's rows
    hold, and a setUp that fails mid-transaction leaves its connection's write lock behind.
    -> the path."""
    d = tempfile.mkdtemp(prefix="afclaude-test-db-")
    test.addCleanup(shutil.rmtree, d, True)
    path = os.path.join(d, "afclaude.db")
    old = store.DB_PATH, os.environ["AFCLAUDE_DB"]

    def restore():
        store.DB_PATH, os.environ["AFCLAUDE_DB"] = old
    test.addCleanup(restore)
    store.DB_PATH = os.environ["AFCLAUDE_DB"] = path
    store.connect(path, create=True).close()
    return path


def set_setting(key, value, db=None):
    """Save a setting through actions.py (value None = reset to the code default)."""
    conn = store.connect(db or store.DB_PATH)
    try:
        if value is None:
            actions.perform(conn, "setting.reset", {"key": key}, actor="cli", via="cli")
        else:
            actions.perform(conn, "setting.set", {"key": key, "value": value}, actor="cli", via="cli")
    finally:
        conn.close()


def tmux_row(session_id, tmux=None, kind="task-manager", db=None, project_id=None):
    """A live driven_sessions row naming the session's tmux session (3b S2: keepalive.tmux_name
    reads it; a launcher stores it, a task-manager's comes from its project, 3b S1), written
    around actions.py as a backfill would. tmux: default afc-<id8> (a valid name; launchers use names.tmux_name); project_id: a
    task-manager row's project (needed for a later driven.set on it). -> the name."""
    name = tmux or "afc-" + session_id[:8]
    conn = store.connect(db or store.DB_PATH)
    try:
        with store.transaction(conn):
            store.upsert_driven_session(conn, session_id, kind=kind, tmux=name, ended_at=None,
                                        **({"project_id": project_id} if project_id is not None else {}))
    finally:
        conn.close()
    return name


def put_raw(key, value, db=None):
    """Write a settings row AROUND actions.py (no validation): what a broken or newer writer
    could leave behind; the runners must ignore an invalid one."""
    conn = store.connect(db or store.DB_PATH)
    try:
        with store.transaction(conn):
            store.put_setting(conn, key, value, "test")
    finally:
        conn.close()


def setcfg(**kw):
    """The settings for a test: every one at its code default, then kw (through actions.py)."""
    clear_settings()
    for k, v in kw.items():
        set_setting(k, v)


def clear_settings(db=None):
    """Every setting back to its code default (deletes the rows; tests only)."""
    conn = store.connect(db or store.DB_PATH)
    try:
        with store.transaction(conn):
            conn.execute("DELETE FROM settings")
    finally:
        conn.close()


def unpause(db=None):
    """Clear automation_paused (and its reason) and every project's pause as the owner would
    (`tasks.py automation resume --all`): a test that made a runner fail-stop (failstop.fail,
    D-234/D-235/D-244) leaves no pause behind."""
    conn = store.connect(db or store.DB_PATH)
    try:
        actions.perform(conn, "automation.set", {"paused": False}, actor="cli", via="cli")
        for p in store.project_pauses(conn):
            actions.perform(conn, "automation.set_project", {"project": p["project_id"], "paused": False},
                            actor="cli", via="cli")
    finally:
        conn.close()


def managed_project(name, session, db=None, path=None):
    """A project `name` whose task-manager is `session` (created once; projects.manager_session),
    in the directory `path` when given (the compaction's texts and notes use it, 3c S1).
    -> a cleanup that makes it unmanaged again (so a later test's session has no project)."""
    conn = store.connect(db or store.DB_PATH)
    try:
        if store.get_project(conn, name) is None:
            store.add_project(conn, name)
        store.update_project(conn, name, manager_session=session, **({"path": path} if path else {}))
    finally:
        conn.close()

    def undo():
        c = store.connect(db or store.DB_PATH)
        try:
            store.update_project(c, name, manager_session="")
        finally:
            c.close()
    return undo


def notes_project(directory, db=None):
    """A project whose directory is `directory` (created once): the runners' notes for it
    (keepalive.progress_note, notify.py; the DB, design §7.7) land in its progress doc."""
    conn = store.connect(db or store.DB_PATH)
    try:
        if store.get_project(conn, directory) is None:
            store.add_project(conn, "notes-" + os.path.basename(os.path.normpath(directory)), None, directory)
    finally:
        conn.close()


def notes(directory, name="progress", db=None):
    """The text of the doc `name` (default progress) of the project of `directory` ("" if none)."""
    conn = store.connect(db or store.DB_PATH)
    try:
        p = store.get_project(conn, directory)
        d = store.get_doc(conn, p["id"], name) if p is not None else None
        return store.doc_render(conn, d["id"]) if d is not None else ""
    finally:
        conn.close()


# ---------------------------------------------------------------- the watcher passes through the runner (phase 3a S6)

def clear_watch(db=None):
    """No acts and no log-once memory of the runner's keep-alive passes (runner state 'keepalive.watch')."""
    conn = store.connect(db or store.DB_PATH)
    try:
        with store.transaction(conn):
            conn.execute("DELETE FROM runner_state WHERE component='keepalive.watch'")
    finally:
        conn.close()


def act_of(ka, job, session=SID):
    """The stored act of the runner's job `job` for the task-manager `session` ({key, at, run, session})
    or None (phase 3b S3: acts.<job>:<sid8>)."""
    return (ka.load_watch().get("acts") or {}).get(ka.act_name(job, session))


def runner_pass(ka, job, key=None, now=None, session=SID):
    """What the runner's child `runner_jobs.py <job> [--key K]` does (keepalive.runner_pass), in a
    fresh process: the module's process-local memory reset first (a child starts without it; the
    log-once memory comes from the DB), its clock at `now`, the task-manager session `session`."""
    from datetime import datetime, timezone
    from unittest import mock

    def fresh():
        for n in ("_WATCH_LINE", "_FILLUP_LINE", "_COMPACT_LINE", "_DB_PAUSE"):
            setattr(ka, n, None)
        ka._PAUSE_LOGGED, ka._FILLUP_BUSY, ka._LINES = {}, {}, {}
        ka._COMPACT_TRIED.clear()
    fresh()
    t = now or datetime.now(timezone.utc)
    old = ka._now
    ka._now = lambda: t
    try:
        # the registry is `session` alone (phase 3b S3: the runner's passes take their sessions from it;
        # a test of the registry itself uses its own DB, test_registry.py)
        with mock.patch.object(ka, "registry_sessions", lambda: [{"session": session, "name": "test"}]):
            return ka.runner_pass(job, key)
    finally:
        ka._now = old
        fresh()                    # the child ends: nothing of it stays in this process


def runner_act(ka, job, now=None, session=SID):
    """The runner starting the stored act of `job` (its run key) at `now` -> its result, or None
    (no act stored)."""
    a = act_of(ka, job, session)
    if a is None:
        return None
    return runner_pass(ka, job, a["run"], now, session)
