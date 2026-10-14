"""Hermetic environment for the offline tests (import it BEFORE any AFClaude module).

Points the DB ($AFCLAUDE_DB), the local config ($AFCLAUDE_CONFIG) and the data dirs
($AFCLAUDE_DATA_DIR, $DISPATCHER_DATA_DIR) at a fresh temp directory, so a test run in
the main checkout never reads the live settings or triggers the one-time settings import
on the live data/ (afclaude_config). The temp DB starts empty (every setting = its code
default); set_setting() / clear_settings() change it.
"""
import atexit
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

import store  # noqa: E402
import actions  # noqa: E402

DB = os.environ["AFCLAUDE_DB"]
store.DB_PATH = DB
store.NOTIFY = lambda subject, body="": "test: not sent"   # a DB-error alert never pushes / writes the alerts doc
import notify  # noqa: E402
notify.push = lambda text: "test: push not sent"   # an alert (failstop.fail, keepalive.alert) never runs claude
store.connect(DB, create=True).close()   # explicit, like a real install's setup: an empty DB, no
                                          # "no database" log line from the settings reads


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


def managed_project(name, session, db=None):
    """A project `name` whose task-manager is `session` (created once; projects.manager_session).
    -> a cleanup that makes it unmanaged again (so a later test's session has no project)."""
    conn = store.connect(db or store.DB_PATH)
    try:
        if store.get_project(conn, name) is None:
            store.add_project(conn, name)
        store.update_project(conn, name, manager_session=session)
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
