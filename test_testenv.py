#!/usr/bin/env python3
"""testenv's guard: a test can never write the live store (D-345: junk alerts from a test run reached the
owner; D-154: the live DB is written through actions.py by the real runners only). The live data dir and DB
are refused in the process (sqlite connect, writes, renames, removes), a child process without the temp DB in
its environment is refused, and the alert paths (notify.notify, project_docs.append_note, store's DB-error
alert) resolve to the temp DB. Nothing here opens a live path: the guard refuses before the call goes on."""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402  (hermetic: a temp DB; before the AFClaude imports)
import notify  # noqa: E402
import project_docs  # noqa: E402
import store  # noqa: E402

LIVE = testenv.LIVE_DIRS[0]
LIVE_DB = os.path.join(LIVE, "afclaude.db")


class Guard(unittest.TestCase):
    def test_live_dirs_cover_this_checkout_and_the_main_one(self):
        self.assertIn(os.path.join(HERE, "data"), testenv.LIVE_DIRS)
        self.assertFalse(testenv.live_path(testenv.DIR))
        self.assertFalse(testenv.live_path(os.path.join(tempfile.gettempdir(), "x.db")))

    def test_sqlite_connect_to_the_live_db_is_refused(self):
        for target in (LIVE_DB, "file:" + LIVE_DB + "?mode=ro", os.path.join(LIVE, "..", "data", "afclaude.db")):
            with self.assertRaises(testenv.LiveStoreTouched):
                sqlite3.connect(target)

    def test_store_connect_to_the_live_db_is_refused(self):
        # in a worktree there is no live DB and connect says so first (DBUnavailable); next to the live DB it is the guard
        with self.assertRaises((testenv.LiveStoreTouched, store.DBUnavailable)):
            store.connect(LIVE_DB)

    def test_writes_in_the_live_dir_are_refused_reads_are_not(self):
        probe = os.path.join(LIVE, "no-such-file-testenv-probe")
        for mode in ("w", "a", "r+", "x", "wb"):
            with self.assertRaises(testenv.LiveStoreTouched):
                open(probe, mode)
        with self.assertRaises(FileNotFoundError):   # a read passes the guard (and finds nothing)
            open(probe)
        with self.assertRaises(testenv.LiveStoreTouched):
            os.open(probe, os.O_WRONLY | os.O_CREAT)

    def test_rename_remove_mkdir_in_the_live_dir_are_refused(self):
        probe = os.path.join(LIVE, "no-such-file-testenv-probe")
        for fn, args in ((os.remove, (probe,)), (os.rename, (probe, probe + "2")), (os.mkdir, (probe,)),
                         (os.rename, (os.path.join(testenv.DIR, "a"), probe))):
            with self.assertRaises(testenv.LiveStoreTouched):
                fn(*args)

    def test_the_data_dir_itself_may_be_made(self):
        # notify.notify's os.makedirs(data/): only the directory, which holds nothing of the store
        testenv._audit("os.mkdir", (LIVE, 0o777, None))

    def test_the_temp_dir_is_free(self):
        p = os.path.join(testenv.DIR, "probe.txt")
        with open(p, "w") as fh:
            fh.write("x")
        os.remove(p)

    def test_a_child_aimed_at_the_live_data_is_refused(self):
        cmd = [sys.executable, os.path.join(HERE, "tasks.py")]
        for var in ("AFCLAUDE_DB", "AFCLAUDE_DATA_DIR"):
            with self.assertRaises(testenv.LiveStoreTouched):
                testenv._audit("subprocess.Popen", (cmd[0], cmd, None, {var: LIVE_DB if var == "AFCLAUDE_DB" else LIVE}))
        for env in (None, {"PATH": "/usr/bin"}, {"AFCLAUDE_DB": os.environ["AFCLAUDE_DB"]}):
            testenv._audit("subprocess.Popen", (cmd[0], cmd, None, env))

    def test_fd_relative_names_are_not_paths_of_the_cwd(self):
        # shutil.rmtree removes "data" relative to an open directory: not the live dir
        testenv._audit("os.rmdir", ("data", 5))
        testenv._audit("os.remove", ("data", 5))
        with self.assertRaises(testenv.LiveStoreTouched):
            testenv._audit("os.rmdir", (LIVE, None))

    def test_the_refusal_is_not_an_exception(self):
        # the runners' "never raises" blocks (except Exception) must not be able to hide a leak
        self.assertFalse(issubclass(testenv.LiveStoreTouched, Exception))


class AlertPaths(unittest.TestCase):
    """The paths a test alert takes, aimed at the live DB: each one raises instead of writing."""

    def test_testenv_aims_every_alert_at_the_temp_db(self):
        self.assertTrue(store.DB_PATH.startswith(testenv.DIR))
        self.assertEqual(notify.push("x"), "test: push not sent")
        self.assertEqual(store.NOTIFY("s", "b"), "test: not sent")

    def test_notify_to_the_live_db_is_refused(self):
        with mock.patch.object(store, "DB_PATH", LIVE_DB), mock.patch.dict(os.environ, {"AFCLAUDE_DB": LIVE_DB}):
            with self.assertRaises(testenv.LiveStoreTouched):
                notify.notify("test alert", "must not reach the live alerts doc")

    def test_append_note_to_the_live_db_is_refused(self):
        with self.assertRaises(testenv.LiveStoreTouched):
            project_docs.append_note("alerts", "x", "runner:notify", db_path=LIVE_DB)

    def test_a_db_error_alert_with_the_real_notifier_goes_nowhere_live(self):
        # store.NOTIFY back at None = the real notify.notify: still the temp DB / fallback file next to it
        d = tempfile.mkdtemp(prefix="afclaude-test-db-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        with mock.patch.object(store, "NOTIFY", None), mock.patch.object(notify, "push", lambda t: "stub"), \
                mock.patch.object(store, "DB_PATH", os.path.join(d, "afclaude.db")):
            self.assertIn("alerted", store.alert_once(os.path.join(d, "ALERTS.fallback.md"), "s", "b", "test"))


if __name__ == "__main__":
    unittest.main()
