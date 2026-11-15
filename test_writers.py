#!/usr/bin/env python3
"""K5 part 2b Brief A (D-344): meta.writers = container. store.connect refuses every write outside the container
on a marked database (reads go on, an unmarked database is untouched), the container's `writers` action is
audited, the host's `store.py writers host` rollback is the one write allowed there, and the CLI defaults
("here") come from $CLAUDE_PROJECT_DIR. Temp databases only.

    python3 -m unittest test_writers
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic env; before the AFClaude imports)
import actions  # noqa: E402
import host  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402

IN = {"AFCLAUDE_IN_CONTAINER": "1"}
OUT = {"AFCLAUDE_IN_CONTAINER": ""}


def env(**kw):
    return mock.patch.dict(os.environ, kw)


class Marked(unittest.TestCase):
    """A temp database with a task-manager project; `mark()` sets writers=container from inside the container."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "afclaude.db")
        with env(**OUT):
            conn = store.connect(self.db, create=True)
            actions.perform(conn, "project.add", {"name": "p1"}, actor="cli", via="cli")
            conn.close()

    def mark(self, value="container"):
        with env(**IN):
            conn = store.connect(self.db)
            try:
                return actions.perform(conn, "writers.set", {"value": value}, actor="cli", via="cli")
            finally:
                conn.close()

    def audit(self):
        c = sqlite3.connect(self.db)
        try:
            return [r[0] for r in c.execute("SELECT action FROM audit_log WHERE action LIKE 'writers.%'")]
        finally:
            c.close()

    def projects(self):
        c = sqlite3.connect(self.db)
        try:
            return [r[0] for r in c.execute("SELECT name FROM projects ORDER BY id")]
        finally:
            c.close()


class Refusal(Marked):
    def test_an_unmarked_database_is_written_from_anywhere(self):
        for flag in IN, OUT:
            with env(**flag):
                conn = store.connect(self.db)
                actions.perform(conn, "project.add", {"name": "p" + flag["AFCLAUDE_IN_CONTAINER"] + "x"}, actor="cli", via="cli")
                conn.close()
        self.assertEqual(len(self.projects()), 3)

    def test_the_mark_is_set_in_the_container_with_an_audit_row(self):
        self.assertEqual(self.mark()["writers"], "container")
        self.assertEqual(self.audit(), ["writers.set"])
        self.mark()                           # the same value again: nothing changes
        with env(**OUT):
            conn = store.connect(self.db)
            self.assertEqual(store.writers_mark(conn), "container")
            conn.close()

    def test_container_cannot_be_set_from_the_host(self):
        with env(**OUT):
            conn = store.connect(self.db)
            with self.assertRaisesRegex(ValueError, "inside the container"):
                actions.perform(conn, "writers.set", {"value": "container"}, actor="cli", via="cli")
            conn.close()
        self.assertEqual(self.audit(), [])

    def test_a_bad_value_is_refused(self):
        with env(**IN):
            conn = store.connect(self.db)
            with self.assertRaisesRegex(ValueError, "writers must be"):
                actions.perform(conn, "writers.set", {"value": "both"}, actor="cli", via="cli")
            conn.close()

    def test_the_host_reads_but_every_write_is_refused_naming_the_rollback(self):
        self.mark()
        with env(**OUT):
            conn = store.connect(self.db)
            self.assertEqual([p["name"] for p in store.list_projects(conn)], ["p1"])    # reads go on
            attempts = [
                lambda: actions.perform(conn, "project.add", {"name": "p2"}, actor="cli", via="cli"),
                lambda: conn.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')"),
                lambda: conn.execute("UPDATE projects SET name='z'"),
                lambda: conn.execute("DELETE FROM projects"),
                lambda: conn.executescript("CREATE TABLE evil(a)"),
                lambda: conn.execute("DROP TABLE meta"),
                lambda: conn.execute("PRAGMA user_version=99"),
                lambda: conn.execute("PRAGMA writable_schema=ON"),
                lambda: conn.execute("ATTACH DATABASE ':memory:' AS other"),
                lambda: store.set_meta(conn, "k", "v"),
                lambda: conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", [("a", "b")]),
            ]
            for i, attempt in enumerate(attempts):
                with self.assertRaises(store.WriterRefused, msg=str(i)) as cm:
                    attempt()
                self.assertIn(store.WRITERS_ROLLBACK, str(cm.exception), i)
                self.assertEqual(cm.exception.kind, "writers")
            with self.assertRaises(store.WriterRefused):
                with store.transaction(conn):
                    pass
            conn.close()
        self.assertEqual(self.projects(), ["p1"])

    def test_the_container_still_writes(self):
        self.mark()
        with env(**IN):
            conn = store.connect(self.db)
            actions.perform(conn, "project.add", {"name": "p2"}, actor="cli", via="cli")
            conn.close()
        self.assertEqual(self.projects(), ["p1", "p2"])

    def test_zero_counts_as_the_host(self):
        self.mark()
        with env(AFCLAUDE_IN_CONTAINER="0"):
            conn = store.connect(self.db)
            with self.assertRaises(store.WriterRefused):
                store.set_meta(conn, "k", "v")
            conn.close()

    def test_read_pragmas_and_the_connections_own_settings_still_work(self):
        self.mark()
        with env(**OUT):
            conn = store.connect(self.db)
            conn.execute("PRAGMA busy_timeout=500")       # what actions.shadow does
            conn.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            conn.close()

    def test_migrate_is_refused_on_the_host(self):
        self.mark()
        with env(**OUT), self.assertRaisesRegex(store.WriterRefused, "the container migrates"):
            store.migrate(self.db)

    def test_a_refused_write_is_a_clear_hand_back_not_an_escalation(self):
        self.mark()
        with env(**OUT):
            conn = store.connect(self.db)
            with self.assertRaises(store.WriterRefused) as cm:
                actions.perform(conn, "project.add", {"name": "p2"}, actor="cli", via="cli")
            conn.close()
        err = cm.exception
        h = store.handback(err)
        self.assertEqual(h["kind"], "writers")
        self.assertIn("host wrapper", h["next_step"])
        self.assertNotIn("Wait about 30 seconds", h["next_step"])
        self.assertTrue(err.not_saved and err.escalation)       # filled by perform's report path
        self.assertFalse(os.path.exists(store.fallback_path(self.db)))   # no replay record, no alert


class Rollback(Marked):
    def run_store(self, *argv, flag=OUT):
        out, err = io.StringIO(), io.StringIO()
        with env(**flag), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = store.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_show_and_the_host_rollback(self):
        self.mark()
        rc, out, _ = self.run_store("writers", "show", "--db", self.db)
        self.assertEqual((rc, json.loads(out)), (0, {"writers": "container"}))
        rc, out, err = self.run_store("writers", "host", "--db", self.db)
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["writers"], "host")
        self.assertEqual(self.audit(), ["writers.set", "writers.set"])       # the container's mark, the rollback
        with env(**OUT):
            conn = store.connect(self.db)                                    # the host writes again
            actions.perform(conn, "project.add", {"name": "p2"}, actor="cli", via="cli")
            conn.close()
        rc, out, _ = self.run_store("writers", "show", "--db", self.db)
        self.assertEqual(json.loads(out), {"writers": "host"})

    def test_no_database_is_an_error_not_a_new_one(self):
        rc, _, err = self.run_store("writers", "host", "--db", os.path.join(self.tmp.name, "none.db"))
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", err)

    def test_the_tasks_cli_on_the_host_names_the_rollback_and_reads_still_work(self):
        self.mark()
        for argv, expect_rc in ((["list"], 0), (["writers"], 0), (["writers", "host"], 1)):
            out, err = io.StringIO(), io.StringIO()
            with env(**OUT), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = tasks.main(["--db", self.db] + argv)
            self.assertEqual(rc, expect_rc, (argv, out.getvalue(), err.getvalue()))
        self.assertIn("python3 store.py writers host", out.getvalue() + err.getvalue())

    def test_the_tasks_cli_in_the_container_sets_and_shows_it(self):
        out = io.StringIO()
        with env(**IN), contextlib.redirect_stdout(out):
            self.assertEqual(tasks.main(["--db", self.db, "writers", "container"]), 0)
            self.assertEqual(tasks.main(["--db", self.db, "writers"]), 0)
        self.assertEqual(out.getvalue().split("\n")[:2], ["writers: container", "writers: container"])
        with env(**IN):                      # and the container rolls back too
            self.assertEqual(tasks.main(["--db", self.db, "writers", "host"]), 0)


class CallerDir(unittest.TestCase):
    """The CLI's "here" is $CLAUDE_PROJECT_DIR (the wrapper passes the caller's cwd through docker exec)."""

    def test_the_env_wins_when_absolute(self):
        with env(CLAUDE_PROJECT_DIR="/srv/x/../proj"):
            self.assertEqual(host.caller_dir(), "/srv/proj")
            self.assertEqual(tasks.project_ref("."), "/srv/proj")
            self.assertEqual(tasks.project_ref("./sub"), "/srv/proj/sub")
            self.assertEqual(tasks.project_ref("../sib"), "/srv/sib")
            self.assertEqual(tasks.project_ref("named"), "named")

    def test_unset_empty_or_relative_means_the_cwd(self):
        for value in (None, "", "rel/dir"):
            with mock.patch.dict(os.environ):
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
                if value is not None:
                    os.environ["CLAUDE_PROJECT_DIR"] = value
                self.assertEqual(host.caller_dir(), os.getcwd(), value)
                self.assertEqual(tasks.project_ref("."), os.path.abspath("."), value)

    def test_doc_project_defaults_to_it(self):
        conn = mock.Mock()
        with env(CLAUDE_PROJECT_DIR="/srv/proj"), mock.patch.object(tasks.store, "get_project", return_value={"name": "p"}) as g:
            tasks.doc_project(conn, None)
        g.assert_called_once_with(conn, "/srv/proj")

    def test_the_session_rules_hook_defaults_to_it(self):
        import keepalive
        with env(CLAUDE_PROJECT_DIR="/srv/proj"), mock.patch.object(keepalive, "rules_hooked", return_value=False) as h:
            keepalive.session_rules_hook(1, {})
        h.assert_called_once_with("/srv/proj")


if __name__ == "__main__":
    unittest.main()
