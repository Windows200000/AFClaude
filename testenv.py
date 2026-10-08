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
                  AFCLAUDE_DATA_DIR=DIR, DISPATCHER_DATA_DIR=DIR)

import store  # noqa: E402
import actions  # noqa: E402

DB = os.environ["AFCLAUDE_DB"]
store.DB_PATH = DB
store.NOTIFY = lambda subject, body="": "test: not sent"   # a DB-error alert never pushes / writes ALERTS.md
store.connect(DB).close()          # an empty DB: no "no database" log line from the settings reads


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
