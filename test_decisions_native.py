#!/usr/bin/env python3
"""Tests for the native decision store (phase 4b Q1; D-104, D-158, D-176, D-284, D-285, D-286): the v4 -> v5
migration on a copy (backup made, rows equal), decision.add|edit|expire|delete|confirm|link|topics (audit
before/after, version, overwrite, who may), the DECISIONS.md import leaving native rows alone, find over
topics, the TLDR under the hook cap, the expand notice of show / find (CLI and MCP) and the topic nudge.
Synthetic data only.

    python3 -m unittest test_decisions_native
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import testenv  # noqa: F401  (hermetic DB/config; before any AFClaude module)
import actions
import decisions as dec
import store
import tasks
from test_decisions import SOURCE

# the decisions table as schema v4 had it (before the rebuild), for the migration test
V4_DDL = """
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

OWNER_WORDS = "keep the titles, add topics"


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-decnative-")
        self.db = os.path.join(self.tmp, "afclaude.db")
        self.src = os.path.join(self.tmp, "DECISIONS.md")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write(SOURCE)
        self.conn = store.connect(self.db, create=True)
        patch = mock.patch.object(dec, "DEFAULT_SOURCE", os.path.join(self.tmp, "none.md"))   # no file: ids from D-001
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def do(self, name, *, actor="cli", via="cli", autonomous=False, **params):
        return actions.perform(self.conn, name, params, actor=actor, via=via, autonomous=autonomous)

    def audit(self, action=None, target=None):
        rows = store.audit_log(self.conn, 200, target_type="decision", target_id=target)
        return [r for r in rows if action is None or r["action"] == action]

    def add(self, title="Show titles", topics=("lists",), **kw):
        kw.setdefault("words", OWNER_WORDS)
        kw.setdefault("author", "owner")
        return self.do("decision.add", title=title, topics=list(topics), **kw)

    def imp(self):
        return dec.import_file(self.src, self.db, actor="cli", via="cli", conn=self.conn)

    def drop_from_source(self, *ids):
        """The synthetic source without the entries `ids` (a native entry took their numbers)."""
        with open(self.src, encoding="utf-8") as fh:
            parts = fh.read().split("\n### ")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("\n### ".join(p for p in parts if not any(p.startswith(i + " ") for i in ids)))

    def floor(self, n):
        """The next native id is D-(n+1): a test that needs a particular number (ids come from the DB alone)."""
        store.set_meta(self.conn, "decision_id_floor", n)
        self.conn.commit()


@contextlib.contextmanager
def patch_source(path):
    with mock.patch.object(dec, "DEFAULT_SOURCE", path):
        yield


class V4Env(unittest.TestCase):
    """A v4 database (the old decisions table, schema_version 4) under v5 code."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-decmig-")
        self.path = os.path.join(self.tmp, "afclaude.db")
        src = os.path.join(self.tmp, "DECISIONS.md")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(SOURCE)
        conn = store.connect(self.path, create=True)
        dec.import_file(src, self.path, actor="cli", via="cli", conn=conn)
        actions.perform(conn, "decision.keywords", {"decision": "D-001", "op": "add", "keywords": ["fill-up"]},
                        actor="cli", via="cli")
        # make it a v4 database: the old table shape, the old version mark, no new tables
        rows = [dict(r) for r in conn.execute("SELECT * FROM decisions")]
        self.old_cols = [c for c in rows[0] if c not in ("slug", "project_id", "author", "origin")]
        conn.executescript("DROP TABLE decision_topics; DROP TABLE decision_links; DROP TABLE decisions;")
        conn.executescript(V4_DDL)
        conn.execute("CREATE TRIGGER decisions_version AFTER UPDATE ON decisions FOR EACH ROW WHEN NEW.version IS "
                     "OLD.version BEGIN UPDATE decisions SET version = OLD.version + 1 WHERE id = NEW.id; END")
        for r in rows:
            conn.execute(f"INSERT INTO decisions ({', '.join(self.old_cols)}) VALUES "
                         f"({', '.join('?' * len(self.old_cols))})", [r[c] for c in self.old_cols])
        conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
        conn.commit()
        self.before = [tuple(r) for r in conn.execute(
            f"SELECT {', '.join(self.old_cols)} FROM decisions ORDER BY id")]
        self.fts_before = conn.execute("SELECT COUNT(*) FROM decisions_fts").fetchone()[0]
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, True)


class Migration(V4Env):
    """v4 -> v5 on a copy: `store.py migrate` backs up, rebuilds decisions, keeps every row."""
    def test_v5_code_refuses_to_write_a_v4_database_until_migrated(self):
        conn = store.connect(self.path)
        try:
            with self.assertRaises(store.MigrationNeeded):
                actions.perform(conn, "decision.keywords", {"decision": "D-001", "op": "add", "keywords": ["x"]},
                                actor="cli", via="cli")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], len(self.before))   # reads work
        finally:
            conn.close()

    def test_migrate_backs_up_and_keeps_every_row(self):
        r = store.migrate(self.path)
        self.assertEqual((r["from"], r["to"], r["steps"]), (4, 5, [5]))
        bak = sqlite3.connect(r["backup"])
        try:
            self.assertEqual(bak.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "4")
            self.assertEqual([tuple(x) for x in bak.execute(
                f"SELECT {', '.join(self.old_cols)} FROM decisions ORDER BY id")], self.before)   # the copy is v4's
            self.assertNotIn("origin", {c[1] for c in bak.execute("PRAGMA table_info(decisions)")})
        finally:
            bak.close()
        conn = store.connect(self.path)
        try:
            self.assertEqual(store.get_meta(conn, "schema_version"), "5")
            self.assertEqual([tuple(x) for x in conn.execute(
                f"SELECT {', '.join(self.old_cols)} FROM decisions ORDER BY id")], self.before)   # rows equal, versions too
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decisions_fts").fetchone()[0], self.fts_before)
            got = {r["id"]: (r["author"], r["origin"], r["slug"], r["project_id"])
                   for r in conn.execute("SELECT * FROM decisions")}
            self.assertEqual(got["D-001"], ("owner", "file", None, None))
            self.assertEqual(got["M-001"], ("task-manager", "file", None, None))
            for t in ("decision_topics", "decision_links"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], 0)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'decisions'")}
            self.assertIn("decisions_version", names)                       # the trigger is back
            # writable now, and the new state is allowed
            r = actions.perform(conn, "decision.add", {"title": "T", "topics": ["a"], "author": "task-manager"},
                                actor="cli", via="cli")
            self.assertEqual(r["state"], "proposed")
            before = conn.execute("SELECT version FROM decisions WHERE id = 'D-001'").fetchone()[0]
            actions.perform(conn, "decision.keywords", {"decision": "D-001", "op": "add", "keywords": ["y"]},
                            actor="cli", via="cli")
            self.assertEqual(conn.execute("SELECT version FROM decisions WHERE id = 'D-001'").fetchone()[0], before + 1)
        finally:
            conn.close()

    def test_migrate_twice_is_a_noop(self):
        store.migrate(self.path)
        n = len(os.listdir(self.tmp))
        r = store.migrate(self.path)
        self.assertEqual((r["from"], r["steps"], r["backup"]), (5, [], None))
        self.assertEqual(len(os.listdir(self.tmp)), n)                      # no second backup

    def test_failed_migration_changes_nothing(self):
        with mock.patch.dict(store.MIGRATIONS, {5: lambda c: (_ for _ in ()).throw(sqlite3.OperationalError("boom"))}):
            with self.assertRaises(sqlite3.OperationalError):
                store.migrate(self.path)
        raw = sqlite3.connect(self.path)
        try:
            self.assertEqual(raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "4")
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], len(self.before))
        finally:
            raw.close()

    def baks(self):
        return sorted(f for f in os.listdir(self.tmp) if f.endswith(".bak"))

    def failing(self):
        return mock.patch.dict(store.MIGRATIONS, {5: lambda c: (_ for _ in ()).throw(sqlite3.OperationalError("boom"))})

    def test_a_retried_migrate_reuses_the_backup_of_an_unchanged_database(self):
        for _ in range(3):                                   # a restarting container
            with self.failing(), self.assertRaises(sqlite3.OperationalError):
                store.migrate(self.path)
        self.assertEqual(len(self.baks()), 1)                # not one ~whole-DB copy per start
        r = store.migrate(self.path)                         # fixed: the same copy is the backup
        self.assertEqual(os.path.basename(r["backup"]), self.baks()[0])
        self.assertEqual(len(self.baks()), 1)

    def test_a_changed_database_gets_its_own_backup(self):
        with self.failing(), self.assertRaises(sqlite3.OperationalError):
            store.migrate(self.path)
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE decisions SET title = 'edited meanwhile' WHERE id = 'D-001'")
        raw.commit()
        raw.close()
        time.sleep(1.1)                                      # the backup's name has second resolution
        with self.failing(), self.assertRaises(sqlite3.OperationalError):
            store.migrate(self.path)
        self.assertEqual(len(self.baks()), 2)

    def test_the_migrate_command_alerts_once_and_says_so_in_its_exit_status(self):
        err = io.StringIO()
        for _ in range(2):
            with self.failing(), contextlib.redirect_stderr(err):
                self.assertEqual(store.main(["migrate", "--db", self.path]), 1)
        self.assertIn("boom", err.getvalue())
        events = store.fallback_events(store.fallback_path(self.path))
        self.assertEqual([e["event"] for e in events if e.get("event") == "alert"], ["alert"])   # one per episode
        self.assertIn("migration failed", events[0]["subject"])
        self.assertEqual(len(self.baks()), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(store.main(["migrate", "--db", self.path]), 0)                       # fixed
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(store.main(["migrate", "--db", self.path + ".none"]), store.MIGRATE_NO_DB)

    def test_an_orphan_row_elsewhere_does_not_block_the_migration(self):
        raw = sqlite3.connect(self.path)
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("INSERT INTO tasks(title, project_id, created_at, updated_at) VALUES ('orphan', 999, 'x', 'x')")
        raw.commit()
        raw.close()
        self.assertEqual(store.migrate(self.path)["to"], 5)

    def test_the_tldr_is_built_read_only_while_the_database_cant_be_written(self):
        src = os.path.join(self.tmp, "DECISIONS.md")
        for version, kind in (("4", "MigrationNeeded"), ("6", "SchemaTooNew")):
            raw = sqlite3.connect(self.path)
            raw.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (version,))
            raw.commit()
            raw.close()
            for part in (1, 2):
                out = dec.hook_output(part, 2, {"source": "compact", "cwd": dec.HERE}, src, self.path)
                text = out["hookSpecificOutput"]["additionalContext"] if out else ""
                if part == 1:
                    self.assertIn("D-001 Night runs fill the week", text, kind)
                    self.assertIn(f"was not imported, the database's schema doesn't fit this code ({kind})", text)
                    self.assertNotIn("could NOT be built", text)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(dec.main(["--db", self.path, "tldr"]), 0)

    def test_a_fresh_database_is_v5_with_the_new_columns(self):
        conn = store.connect(os.path.join(self.tmp, "new.db"), create=True)
        try:
            self.assertEqual(store.get_meta(conn, "schema_version"), "5")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
            self.assertTrue({"slug", "project_id", "author", "origin"} <= cols)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decision_topics").fetchone()[0], 0)
        finally:
            conn.close()


class DeployGap(V4Env):
    """The deploy gap: v5 code against the live v4 database, while the old container still runs on v4
    code. Everything but the decision tables stays writable (the host's deploy.py drains and appends
    to a doc before the rebuild); only the new container's entrypoint migrates."""
    def open(self):
        conn = store.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def decision_tables(self, conn):
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("decisions", "decisions_fts", "session_decisions")}

    def test_the_drain_and_a_doc_append_write_while_decisions_wait(self):
        conn = self.open()
        before = self.decision_tables(conn)
        p = actions.perform(conn, "project.add", {"name": "P"}, actor="cli", via="cli")
        actions.perform(conn, "doc.append", {"project": "P", "name": "progress", "text": "- deploy\n"},
                        actor="cli", via="cli")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'doc.append'").fetchone()[0], 1)
        self.assertIsNotNone(p)
        for name, params in (("decision.add", {"title": "T", "topics": ["x"], "author": "owner", "words": "w"}),
                             ("decision.keywords", {"decision": "D-001", "op": "add", "keywords": ["x"]}),
                             ("decision.import", {"path": os.path.join(self.tmp, "DECISIONS.md")})):
            with self.assertRaises(store.MigrationNeeded, msg=name) as cm:
                actions.perform(conn, name, params, actor="cli", via="cli")
            self.assertIn("python3 store.py migrate", str(cm.exception))
        self.assertEqual(self.decision_tables(conn), before)
        self.assertEqual(store.get_meta(conn, "schema_version"), "4")      # connect() raised nothing
        self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'decision_topics'").fetchone())

    def test_reads_degrade_without_the_new_tables(self):
        conn = self.open()
        d = store.get_decision_entry(conn, "D-001")
        self.assertEqual((d["id"], d["topics"], d["links"]), ("D-001", [], []))
        self.assertIn("fill-up", d["effective_keywords"])
        self.assertEqual(len(store.list_decision_entries(conn)), len(self.before))
        self.assertIn("D-001", [h["id"] for h in store.find_decision_entries(conn, ["fill-up"], None)])
        cli = CliEnv.cli
        env = type("E", (), {"db": self.path})()
        for argv, want in ((("show", "D-001"), "D-001"), (("find", "fill-up"), "D-001"), (("tldr",), "D-001")):
            code, out, err = cli(env, *argv)
            self.assertEqual(code, 0, (argv, err))
            self.assertIn(want, out)
        with self.assertRaises(store.MigrationNeeded):        # the writes stay refused
            actions.perform(conn, "decision.topics", {"decision": "D-001", "op": "add", "topics": ["x"]},
                            actor="cli", via="cli")

    def test_raw_sql_cant_change_the_pending_tables(self):
        conn = self.open()
        for sql in ("INSERT INTO decisions(id) VALUES ('D-999')", "UPDATE decisions SET title = 'x'",
                    "DELETE FROM decisions", "DROP TABLE decisions", "ALTER TABLE decisions ADD COLUMN z TEXT",
                    "DELETE FROM decisions_fts", "CREATE INDEX zz ON decisions(title)",
                    "CREATE TABLE decision_topics(a)", "DROP TRIGGER decisions_version"):
            try:
                with self.assertRaisesRegex(sqlite3.DatabaseError, "not authorized", msg=sql):
                    conn.execute(sql)
            finally:
                conn.rollback()
        with self.assertRaises(store.MigrationNeeded):        # inside a store transaction: the clear error
            with store.transaction(conn):
                conn.execute("UPDATE decisions SET title = 'x'")
        conn.execute("SELECT * FROM decisions").fetchall()    # reads are fine
        with store.transaction(conn):
            store.set_meta(conn, "gap_probe", "1")             # so is every other table
        self.assertEqual(store.get_meta(conn, "gap_probe"), "1")

    def test_a_newer_database_stays_read_only(self):
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
        raw.commit()
        raw.close()
        conn = self.open()
        with self.assertRaises(store.SchemaTooNew):
            actions.perform(conn, "project.add", {"name": "P"}, actor="cli", via="cli")
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO meta(key, value) VALUES ('k', 'v')")

    def test_every_migration_declares_its_tables(self):
        self.assertEqual(set(store.MIGRATIONS), set(store.MIGRATION_TABLES))
        self.assertEqual(set(store._gap_tables(4)), {"decisions", "decisions_fts", "decision_topics", "decision_links",
                                                     "decisions_fts_data", "decisions_fts_idx", "decisions_fts_content",
                                                     "decisions_fts_docsize", "decisions_fts_config"})
        self.assertEqual(store._gap_tables(5), frozenset())
        self.assertEqual((store._in_gap(4), store._in_gap(3), store._in_gap(5)), (True, False, False))

    def test_a_connection_opened_in_the_gap_writes_decisions_once_migrated(self):
        conn = self.open()
        with self.assertRaises(store.MigrationNeeded):
            actions.perform(conn, "decision.expire", {"decision": "D-001"}, actor="cli", via="cli")
        store.migrate(self.path)                                  # the container's entrypoint, meanwhile
        actions.perform(conn, "decision.keywords", {"decision": "D-001", "op": "add", "keywords": ["x"]},
                        actor="cli", via="cli")                    # the authorizer is lifted, no reconnect

    def test_the_entrypoint_migrate_runs_after_the_gap_and_twice(self):
        conn = self.open()
        actions.perform(conn, "project.add", {"name": "P"}, actor="cli", via="cli")
        conn.close()
        first = store.migrate(self.path)
        self.assertEqual((first["from"], first["to"], first["steps"]), (4, 5, [5]))
        self.assertTrue(os.path.isfile(first["backup"]))
        second = store.migrate(self.path)
        self.assertEqual((second["from"], second["to"], second["steps"], second["backup"]), (5, 5, [], None))
        conn = self.open()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM projects WHERE name = 'P'").fetchone()[0], 1)
        actions.perform(conn, "decision.keywords", {"decision": "D-001", "op": "add", "keywords": ["y"]},
                        actor="cli", via="cli")

    def test_deploy_py_gets_through_its_first_writes(self):
        """tools/deploy.py on the host with v5 code against the v4 database: connect, drain
        (runner_state write through actions) and the rebuild call, nothing refused."""
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
        try:
            import deploy
        finally:
            sys.path.pop(0)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(store, "DB_PATH", self.path), \
                mock.patch.object(deploy, "check_start_guard", return_value=True), \
                mock.patch.object(deploy, "wait_clear", return_value=True), \
                mock.patch.object(deploy, "code_version", return_value="abc1234"), \
                mock.patch.object(deploy.runner, "wake", return_value=True), \
                mock.patch.object(deploy.subprocess, "run", side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(deploy.main([]), 0)
        self.assertIn("drained", out.getvalue())
        self.assertEqual(calls[-1][:2], ["docker", "compose"])
        conn = self.open()
        self.assertIsNotNone(actions.runner_drain(conn))           # the drain mark is in the v4 database
        self.assertEqual(store.get_meta(conn, "schema_version"), "4")


class Add(Env):
    def test_owner_entry_is_active_at_once_with_the_exact_words(self):
        r = self.add(title="Keep titles in lists", topics=["Lists", " decision list ", "lists"], keywords=["tldr"],
                     scope="decision store", source="inbox #31")
        self.assertEqual((r["id"], r["state"], r["author"], r["origin"], r["slug"]),
                         ("D-001", "active", "owner", "db", "keep-titles-in-lists"))
        self.assertEqual(r["topics"], ["lists", "decision list"])           # normalised, no duplicates
        self.assertEqual(r["new_topics"], ["lists", "decision list"])
        d = store.get_decision_entry(self.conn, "D-001")
        self.assertIn(f'- **Owner\'s words:** "{OWNER_WORDS}"', d["body"])
        self.assertTrue(d["body"].startswith("### D-001 Keep titles in lists\n"))
        self.assertEqual((d["effective_keywords"], d["date"] is not None, d["version"]), (["tldr"], True, 0))
        a = self.audit("decision.add")[0]
        self.assertEqual((a["target_id"], a["before"], a["after"]["state"], a["after"]["topics"]),
                         ("D-001", None, "active", ["lists", "decision list"]))

    def test_task_manager_entry_is_proposed(self):
        r = self.do("decision.add", title="Rename X", topics=["naming"], author="task-manager")
        self.assertEqual((r["state"], r["status"].startswith("proposed")), ("proposed", True))
        self.assertIn("**Status:** proposed", store.get_decision_entry(self.conn, r["id"])["body"])

    def test_refusals(self):
        for kw, msg in (({"words": None}, "exact words"), ({"topics": []}, "at least one"),
                        ({"topics": ["a,b"]}, "without commas"), ({"title": " "}, "must not be empty"),
                        ({"author": "me"}, "author"), ({"project": "nope"}, "no project"),
                        ({"slug": "Bad Slug"}, "slug is lower-case")):
            with self.assertRaisesRegex((ValueError, LookupError), msg, msg=str(kw)):
                self.add(**kw)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_autonomous_session_records_only_its_own(self):
        with self.assertRaises(actions.Forbidden):
            self.add(actor="mcp:abc", via="mcp", autonomous=True)
        r = self.do("decision.add", title="Mine", topics=["x"], author="task-manager", actor="mcp:abc", via="mcp",
                    autonomous=True)
        self.assertEqual(r["state"], "proposed")

    def test_ids_slugs_and_projects(self):
        self.floor(299)
        self.assertEqual(self.add()["id"], "D-300")
        self.assertEqual(self.add(title="Second")["id"], "D-301")             # above the highest one
        self.do("decision.delete", decision="D-301", reason="test")
        self.assertEqual(self.add(title="Third")["id"], "D-302")              # a deleted number isn't reused
        with self.assertRaisesRegex(ValueError, "unknown parameter"):          # the DB alone gives out ids (Q2)
            self.add(id="D-500")
        self.assertEqual(self.add(title="Same title", slug=None)["slug"], "same-title")
        self.assertEqual(self.add(title="Same title")["slug"], "same-title-2")
        with self.assertRaisesRegex(ValueError, "already"):
            self.add(title="Other", slug="same-title")
        p = store.add_project(self.conn, "Alpha")
        self.conn.commit()
        r = self.add(title="Same title", project="Alpha")
        self.assertEqual((r["slug"], r["project_id"]), ("same-title", p["id"]))   # unique per project

    def test_topic_nudge_names_new_topics_and_a_high_total(self):
        self.add(topics=["a"])
        r = self.add(title="Two", topics=["a", "b"])
        self.assertEqual(r["new_topics"], ["b"])                              # a is reused
        with mock.patch.object(actions, "DECISION_TOPICS_SOFT_MAX", 1):
            r = self.add(title="Three", topics=["c"])
        self.assertIn("keep the total low", r["topic_note"])
        self.assertEqual(list(store.decision_topic_counts(self.conn)), ["a", "b", "c"])


class Edit(Env):
    def setUp(self):
        super().setUp()
        self.d = self.add(title="Old title", summary="old summary", scope="all")

    def test_overwrites_and_audits_old_and_new(self):
        r = self.do("decision.edit", decision="d-001", title="New title", scope="some")
        self.assertEqual((r["title"], r["scope"], r["version"]), ("New title", "some", 1))
        d = store.get_decision_entry(self.conn, "D-001")
        self.assertIn("### D-001 New title", d["body"])
        self.assertNotIn("Old title", d["body"])                              # overwritten, not kept (D-158)
        self.assertIn("- **Scope:** some", d["body"])
        a = self.audit("decision.edit")[0]
        self.assertEqual((a["before"]["title"], a["after"]["title"], a["before"]["scope"], a["after"]["scope"]),
                         ("Old title", "New title", "all", "some"))
        self.assertNotIn("source", a["before"])                               # only what changed
        self.assertEqual(len(self.audit("decision.edit")), 1)

    def test_noop_writes_no_audit_row_and_clearing_works(self):
        self.do("decision.edit", decision="D-001", scope="all")
        self.assertEqual(self.audit("decision.edit"), [])
        self.do("decision.edit", decision="D-001", scope="")
        self.assertIsNone(store.get_decision_entry(self.conn, "D-001")["scope"])
        with self.assertRaisesRegex(ValueError, "nothing to change"):
            self.do("decision.edit", decision="D-001")
        with self.assertRaisesRegex(ValueError, "title must not be empty"):
            self.do("decision.edit", decision="D-001", title="")

    def test_nobody_edits_the_words_or_summary_of_an_owners_entry(self):
        for kw in ({"words": "other"}, {"words": ""}, {"summary": "new"}, {"summary": ""}):
            for who in (dict(), dict(actor="mcp:abc", via="mcp")):        # the owner on the CLI too (D-104, D-285)
                with self.assertRaisesRegex((ValueError, actions.Forbidden), "words and summary|autonomous"):
                    self.do("decision.edit", decision="D-001", **kw, **who)
        d = store.get_decision_entry(self.conn, "D-001")
        self.assertEqual((d["words"], d["summary"], d["version"]), (OWNER_WORDS, "old summary", 0))
        own = self.do("decision.add", title="Mine", topics=["t"], author="task-manager", words="a", summary="b")
        self.assertEqual(self.do("decision.edit", decision=own["id"], summary="c")["summary"], "c")   # its own: free
        self.do("decision.topics", decision="D-001", op="add", topics=["more"])                       # topics stay allowed
        self.do("decision.keywords", decision="D-001", op="add", keywords=["k"])

    def test_stale_version_conflicts(self):
        self.do("decision.edit", decision="D-001", title="B")
        with self.assertRaises(actions.Conflict):
            self.do("decision.edit", decision="D-001", title="C", version=0)
        self.do("decision.edit", decision="D-001", title="C", version=1)

    def test_only_native_entries_and_the_owners_stay_with_the_owner(self):
        self.drop_from_source("D-001")                                          # D-002.. come from the file
        self.imp()
        with self.assertRaisesRegex(ValueError, "mirrors DECISIONS.md"):
            self.do("decision.edit", decision="D-003", title="x")
        with self.assertRaises(actions.Forbidden):                              # D-104
            self.do("decision.edit", decision="D-001", title="x", actor="mcp:abc", via="mcp", autonomous=True)
        own = self.do("decision.add", title="Own", topics=["t"], author="task-manager")
        r = self.do("decision.edit", decision=own["id"], title="Reworded", actor="mcp:abc", via="mcp", autonomous=True)
        self.assertEqual(r["title"], "Reworded")


class Remove(Env):
    def setUp(self):
        super().setUp()
        self.add(title="One-off", topics=["night"], status="active until 2001-01-01 19:00 (one-off, D-278)")
        self.add(title="Other", topics=["night", "x"])
        self.do("decision.link", decision="D-001", to="D-002", kind="refines")

    def test_expire_deletes_and_keeps_the_old_entry_in_the_change_history(self):
        r = self.do("decision.expire", decision="D-001")
        self.assertEqual((r["deleted"], r["reason"]), (True, "expired"))
        self.assertIsNone(store.get_decision_entry(self.conn, "D-001"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decision_topics WHERE decision_id = 'D-001'")
                         .fetchone()[0], 0)
        self.assertEqual(store.get_decision_entry(self.conn, "D-002")["links"], [])         # the link went with it
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM decisions_fts WHERE id = 'D-001'").fetchone()[0], 0)
        a = self.audit("decision.expire")[0]
        self.assertEqual(a["after"]["reason"], "expired")
        self.assertIn("### D-001 One-off", a["before"]["body"])
        self.assertEqual((a["before"]["topics"], a["before"]["links"]), (["night"], ["refines D-002"]))
        self.do("decision.expire", decision="D-002", reason="done with it")
        self.assertEqual(self.audit("decision.expire")[0]["after"]["reason"], "expired: done with it")

    def test_delete_needs_a_reason_and_is_audited(self):
        with self.assertRaisesRegex(ValueError, "reason is required"):
            self.do("decision.delete", decision="D-002", reason=None)
        self.do("decision.delete", decision="D-002", reason="replaced by D-9")
        a = self.audit("decision.delete")[0]
        self.assertEqual((a["after"]["reason"], a["before"]["title"]), ("replaced by D-9", "Other"))
        with self.assertRaises(LookupError):
            self.do("decision.delete", decision="D-002", reason="again")

    def test_autonomous_session_may_remove_only_a_lapsed_one_off_of_the_owner(self):
        kw = dict(actor="mcp:abc", via="mcp", autonomous=True)
        with self.assertRaises(actions.Forbidden):
            self.do("decision.delete", decision="D-002", reason="x", **kw)
        with self.assertRaises(actions.Forbidden):
            self.do("decision.expire", decision="D-002", **kw)                      # not a one-off
        self.add(title="Future one-off", status="active until 2999-01-01 19:00")
        with self.assertRaises(actions.Forbidden):
            self.do("decision.expire", decision="D-003", **kw)                      # its date isn't past
        self.do("decision.expire", decision="D-001", **kw)                          # lapsed (2001)

    def test_file_entries_are_removed_in_the_file(self):
        self.drop_from_source("D-001", "D-002")
        self.imp()
        with self.assertRaisesRegex(ValueError, "mirrors DECISIONS.md"):
            self.do("decision.expire", decision="D-004")


class Confirm(Env):
    def test_the_owner_confirms_a_proposal(self):
        p = self.do("decision.add", title="Prop", topics=["x"], author="task-manager", words="reworded")
        r = self.do("decision.confirm", decision=p["id"])
        self.assertEqual((r["state"], r["status"], r["version"]), ("active", "active", p["version"] + 1))
        self.assertIn("- **Status:** active", store.get_decision_entry(self.conn, p["id"])["body"])
        a = self.audit("decision.confirm")[0]
        self.assertEqual((a["before"]["state"], a["after"]["state"]), ("proposed", "active"))
        self.do("decision.confirm", decision=p["id"])                              # already active: no-op
        self.assertEqual(len(self.audit("decision.confirm")), 1)

    def test_an_autonomous_session_cannot_confirm(self):
        p = self.do("decision.add", title="Prop", topics=["x"], author="task-manager")
        with self.assertRaises(actions.Forbidden):
            self.do("decision.confirm", decision=p["id"], actor="mcp:abc", via="mcp", autonomous=True)
        self.assertEqual(store.get_decision_entry(self.conn, p["id"])["state"], "proposed")

    def test_a_custom_status_survives_the_confirmation(self):
        p = self.do("decision.add", title="Prop", topics=["x"], author="task-manager", status="active until 2999-01-01")
        self.assertEqual(self.do("decision.confirm", decision=p["id"])["status"], "active until 2999-01-01")


class Topics(Env):
    def test_add_remove_and_keep_one(self):
        self.add(topics=["a"])
        r = self.do("decision.topics", decision="D-001", op="add", topics=["B", "a"])
        self.assertEqual(r["topics"], ["a", "b"])
        a = self.audit("decision.topics")[0]
        self.assertEqual((a["before"]["topics"], a["after"]["topics"]), (["a"], ["a", "b"]))
        self.do("decision.topics", decision="D-001", op="add", topics=["a"])         # no-op
        self.assertEqual(len(self.audit("decision.topics")), 1)
        self.do("decision.topics", decision="D-001", op="remove", topics=["a"])
        with self.assertRaisesRegex(ValueError, "at least one topic"):
            self.do("decision.topics", decision="D-001", op="remove", topics=["b"])
        with self.assertRaisesRegex(ValueError, "no topic zz"):
            self.do("decision.topics", decision="D-001", op="remove", topics=["zz"])

    def test_topics_work_on_a_file_entry_and_survive_a_reimport(self):
        self.imp()
        self.do("decision.topics", decision="D-001", op="add", topics=["budget"])
        with open(self.src, "a", encoding="utf-8") as fh:
            fh.write("\n### D-009 New\n- **Keywords:** zzz\n")
        self.imp()
        self.assertEqual(store.get_decision_entry(self.conn, "D-001")["topics"], ["budget"])

    def test_find_matches_topics(self):
        self.add(title="Alpha", topics=["lists"])
        self.add(title="Beta", topics=["all"])
        hits = store.find_decision_entries(self.conn, ["list"], None)               # whole-word / plural forms
        self.assertEqual([(h["id"], h["match"], h["matched"]) for h in hits], [("D-001", "keyword", ["list"])])
        self.assertEqual([h["id"] for h in store.find_decision_entries(self.conn, ["all"], None)], ["D-002"])


class Links(Env):
    def test_link_shows_on_both_ends_and_is_audited(self):
        self.add(title="Alpha")
        self.add(title="Beta")
        r = self.do("decision.link", decision="D-001", to="D-002", kind="conflicts")
        self.assertEqual(r["links"], ["conflicts D-002"])
        self.assertEqual(store.get_decision_entry(self.conn, "D-002")["links"][0]["direction"], "in")
        self.assertEqual(store.link_text(store.get_decision_entry(self.conn, "D-002")["links"][0]), "conflicts with D-001")
        a = self.audit("decision.link")[0]
        self.assertEqual((a["before"]["links"], a["after"]["links"]), ([], ["conflicts D-002"]))
        self.do("decision.link", decision="D-001", to="D-002", kind="conflicts")        # no-op
        self.assertEqual(len(self.audit("decision.link")), 1)
        with self.assertRaisesRegex(ValueError, "itself"):
            self.do("decision.link", decision="D-001", to="d-001", kind="refines")
        with self.assertRaisesRegex(ValueError, "kind"):
            self.do("decision.link", decision="D-001", to="D-002", kind="likes")
        with self.assertRaises(LookupError):
            self.do("decision.link", decision="D-001", to="D-9", kind="refines")
        self.do("decision.link", decision="D-001", to="D-002", kind="conflicts", op="remove")
        with self.assertRaisesRegex(ValueError, "no link"):
            self.do("decision.link", decision="D-001", to="D-002", kind="conflicts", op="remove")


class Import(Env):
    def test_the_file_import_leaves_native_entries_alone(self):
        self.imp()
        self.assertEqual(self.add(title="Native")["id"], "D-005")                  # above the file's D-004 (the import raised the floor)
        r = self.imp()
        self.assertEqual((r["entries"], r["removed"], r["detached"]), (5, [], []))   # not removed, not counted
        self.assertEqual(store.get_decision_entry(self.conn, "D-005")["origin"], "db")
        self.assertEqual(self.imp()["removed"], [])

    def test_a_file_entry_with_a_native_id_stops_the_import_loudly(self):
        self.imp()
        self.floor(49)
        self.add(title="Native")
        with open(self.src, "a", encoding="utf-8") as fh:
            fh.write("\n### D-050 From the file\n- **Keywords:** k\n")
            fh.write("\n### D-051 Another new one\n- **Keywords:** k\n")
        with self.assertRaisesRegex(dec.IdCollision, r"D-050 is in the file.*next free one is D-052"):
            self.imp()
        self.assertIsNone(store.get_decision_entry(self.conn, "D-051"))             # nothing was written
        self.assertEqual(store.get_decision_entry(self.conn, "D-050")["title"], "Native")
        # the 10-minute import: an error line, exit 1, and an alert once per episode
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        shutil.copy(self.src, os.path.join(repo, "DECISIONS.md"))
        alerts = []
        for _ in range(2):
            code, line = dec.cron_import(self.db, repo, alert=lambda subject, body: alerts.append((subject, body)))
            self.assertEqual(code, 1)
            self.assertIn("IdCollision", line)
        self.assertEqual(len(alerts), 1)
        self.assertIn("D-050", alerts[0][1])
        # fixed in the file: the import goes on and the episode ends
        self.drop_from_source("D-050")
        shutil.copy(self.src, os.path.join(repo, "DECISIONS.md"))
        self.assertEqual(dec.cron_import(self.db, repo, alert=lambda *a: alerts.append(a))[0], 0)
        self.assertEqual(store.get_meta(self.conn, dec.CRON_MARK), "")

    def test_the_hook_alerts_on_a_collision_and_still_builds_the_tldr(self):
        self.imp()
        self.floor(49)
        self.add(title="Native")
        with open(self.src, "a", encoding="utf-8") as fh:
            fh.write("\n### D-050 From the file\n- **Keywords:** k\n")
        alerts = []
        with mock.patch.object(dec, "_alert", lambda subject, body: alerts.append(subject)):
            out = dec.hook_output(1, 1, {"source": "compact", "cwd": dec.HERE}, self.src, self.db)
            dec.hook_output(1, 1, {"source": "compact", "cwd": dec.HERE}, self.src, self.db)
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NOT imported, the owner was alerted", text)
        self.assertIn("D-050 Native", text)
        self.assertIn("D-001 Night runs fill the week", text)
        self.assertEqual(alerts, ["decisions import failed"])                      # once for both hook runs

    def test_ids_come_from_the_db_alone_and_an_import_raises_the_floor(self):
        self.assertEqual(self.imp()["id_floor_raised"], True)                      # the file's highest is D-004
        self.assertEqual(store.get_meta(self.conn, "decision_id_floor"), "4")
        with patch_source(self.src):                                               # a readable file changes nothing
            self.assertEqual(self.add(title="First")["id"], "D-005")
        with open(self.src, "a", encoding="utf-8") as fh:                          # the owner's next statement
            fh.write("\n### D-009 Written into the file\n- **Keywords:** k\n")
        self.assertEqual(self.add(title="Second")["id"], "D-006")                  # the file is not looked at ...
        self.imp()                                                                 # ... but its import raises the floor
        self.assertEqual(self.add(title="Third")["id"], "D-010")
        os.remove(self.src)
        self.assertEqual(self.add(title="No file")["id"], "D-011")                 # the container: no file at all
        with open(self.src, "wb") as fh:
            fh.write(b"\xff\xfe")
        self.assertEqual(self.add(title="Bad file")["id"], "D-012")                # an unreadable file is not read

    def test_the_import_reports_a_raised_floor_once(self):
        r = self.imp()
        self.assertEqual((r["id_floor_raised"], store.get_meta(self.conn, "decision_id_floor")), (True, "4"))
        self.assertEqual(self.imp()["id_floor_raised"], False)                     # idempotent: no change, no audit
        self.assertEqual(len([a for a in self.audit("decision.import")]), 1)

    def test_native_entries_stay_after_the_file_ones_across_reimports(self):
        self.floor(4)
        self.add(title="Native")                                                   # D-005, ord 0 in an empty table
        self.imp()                                                                 # the file's rows get ord 0..4
        ids = [d["id"] for d in store.list_decision_entries(self.conn)]
        self.assertEqual(ids, ["D-001", "D-002", "D-003", "M-001", "D-004", "D-005"])
        with open(self.src, "a", encoding="utf-8") as fh:
            fh.write("\n### D-006 Later in the file\n- **Keywords:** k\n")
        self.imp()
        self.assertEqual([d["id"] for d in store.list_decision_entries(self.conn)][-2:], ["D-006", "D-005"])
        self.assertEqual(dec.tldr_lines(self.conn)[-1], "D-005 Native")

    def test_mass_removal_guard_counts_only_file_entries(self):
        self.imp()
        self.floor(49)
        for n in range(8):
            self.add(title=f"Native {n}")
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write("### D-001 Only one\n- **Status:** active\n")
        with self.assertRaisesRegex(dec.SourceError, "remove 4 of 5"):
            self.imp()

    def test_imported_rows_carry_an_author_from_their_type(self):
        self.imp()
        self.assertEqual({d["id"]: (d["author"], d["origin"]) for d in store.list_decision_entries(self.conn)},
                         {"D-001": ("owner", "file"), "D-002": ("owner", "file"), "D-003": ("owner", "file"),
                          "M-001": ("task-manager", "file"), "D-004": ("owner", "file")})


class Tldr(Env):
    def test_titles_only_proposals_left_out_and_under_the_hook_cap(self):
        for n in range(1, 241):             # about the size of the live store
            self.do("decision.add", title=f"Decision number {n} about some topic of the project", topics=["t"],
                    words="w", author="owner")
        self.do("decision.add", title="Waiting for the owner", topics=["t"], author="task-manager")
        lines = dec.tldr_lines(self.conn)
        self.assertEqual(len(lines), 240)
        self.assertEqual(lines[0], "D-001 Decision number 1 about some topic of the project")   # id + title, no topics
        self.assertNotIn("Waiting", "\n".join(lines))
        parts = dec.split_parts(lines, 3)
        self.assertEqual(sum(parts, []), lines)
        for i, p in enumerate(parts, 1):
            self.assertLess(len(dec.tldr_text(p, i, 3)), dec.HOOK_CONTEXT_CAP)
        self.assertIn("Waiting for the owner [proposed]",
                      "\n".join(dec.tldr_line(d) for d in store.list_decision_entries(self.conn, ["proposed"])))

    def test_hook_output_still_works_with_native_entries(self):
        self.imp()
        self.add(title="Native one")
        out = dec.hook_output(1, 3, {"source": "compact", "cwd": dec.HERE}, self.src, self.db)
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("D-005 Native one", text)
        self.assertIn("D-001 Night runs fill the week", text)               # the import ran too


class CliEnv(Env):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tasks.main(["--db", self.db, "decision", *argv])
        return code, out.getvalue(), err.getvalue()


class Cli(CliEnv):
    def test_commands(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):   # --author is required
            self.cli("add", "--title", "Keep titles", "--topic", "lists", "--words", "exact words")
        code, out, _ = self.cli("add", "--title", "Keep titles", "--topic", "lists", "--words", "exact words",
                                "--author", "owner", "--keyword", "tldr")
        self.assertEqual((code, out.splitlines()[0]), (0, "D-001 (state: active; topics: lists)"))
        self.assertIn("new topics: lists", out)
        code, _, err = self.cli("add", "--title", "No words", "--topic", "x", "--author", "owner")
        self.assertEqual(code, 1)
        self.assertIn("exact words", err)
        code, out, _ = self.cli("add", "--title", "Mine", "--topic", "lists", "--topic", "naming", "--author", "task-manager")
        self.assertIn("proposed", store.get_decision_entry(self.conn, "D-002")["state"])
        self.assertNotIn("new topics: lists", out)
        out = self.cli("show", "D-002")[1]
        self.assertIn("(proposed: not yet confirmed by the owner)", out)
        self.assertIn("(topics: lists, naming)", out)
        self.assertIn("D-176", out)                                              # the expand notice
        self.assertIn("   2  lists\n", self.cli("topics")[1])
        self.assertEqual(self.cli("topics", "D-002", "add", "extra")[1].splitlines()[0],
                         "D-002 (topics: lists, naming, extra)")
        self.assertEqual(self.cli("link", "D-002", "refines", "D-001")[0], 0)
        self.assertIn("no current decision matches naming", self.cli("find", "naming")[1])     # still proposed
        out = self.cli("find", "naming", "--all")[1]
        self.assertIn("=== D-002 (keywords: naming)", out)
        self.assertIn("D-176", out)                                                          # the expand notice
        self.assertIn("(links: refines D-001 Keep titles)", out)
        code, _, err = self.cli("edit", "D-002", "--title", "Mine, reworded", "--version", "0")
        self.assertEqual(code, 1)
        self.assertIn("changed since it was read", err)
        self.assertEqual(self.cli("edit", "D-002", "--title", "Mine, reworded")[0], 0)
        self.assertEqual(self.cli("confirm", "D-002")[0], 0)
        self.assertEqual(store.get_decision_entry(self.conn, "D-002")["state"], "active")
        code, out, _ = self.cli("expire", "D-002", "--reason", "carried out")
        self.assertIn("removed (expired: carried out)", out)
        self.assertEqual(self.cli("tldr")[1].count("\nD-"), 1)
        self.assertEqual(self.cli("delete", "D-001", "--reason", "gone")[0], 0)
        self.assertEqual(self.cli("delete", "D-001", "--reason", "again")[0], 1)


class CliAuthority(CliEnv):
    """The CLI as an autonomous session uses it (through Bash): refused what MCP refuses (D-104, D-285)."""
    def setUp(self):
        super().setUp()
        self.owner = self.add(title="Owner's", words="exact")["id"]                      # D-001, active
        self.tm = self.do("decision.add", title="Mine", topics=["t"], author="task-manager")["id"]
        self.oneoff = self.add(title="Lapsed", status="active until 2001-01-01 19:00 (one-off, D-278)")["id"]
        self.future = self.add(title="Future", status="active until 2999-01-01 19:00")["id"]
        env = mock.patch.dict(os.environ, {"CLAUDE_GUARD_DISABLE": "1"})
        env.start()
        self.addCleanup(env.stop)

    def refused(self, *argv):
        code, _, err = self.cli(*argv)
        self.assertEqual(code, 1, argv)
        self.assertIn("autonomous", err, argv)

    def test_an_unattended_session_may_not_record_confirm_or_change_the_owners(self):
        self.refused("add", "--title", "Fake", "--topic", "t", "--author", "owner", "--words", "w")
        self.refused("confirm", self.tm)                                              # its own proposal either
        self.refused("edit", self.owner, "--title", "Changed")
        self.refused("delete", self.owner, "--reason", "x")
        self.refused("expire", self.owner)                                            # not a one-off
        self.refused("expire", self.future)                                           # its date isn't past
        self.assertEqual(store.get_decision_entry(self.conn, self.tm)["state"], "proposed")
        self.assertEqual(store.get_decision_entry(self.conn, self.owner)["title"], "Owner's")
        self.assertEqual(store.get_decision_entry(self.conn, "D-005"), None)

    def test_what_it_may_do(self):
        code, out, _ = self.cli("add", "--title", "Its own", "--topic", "t", "--author", "task-manager")
        self.assertEqual((code, out.splitlines()[0]), (0, "D-005 (state: proposed; topics: t)"))
        self.assertEqual(self.cli("edit", self.tm, "--title", "Reworded")[0], 0)           # its own entry
        self.assertEqual(self.cli("topics", self.owner, "add", "more")[0], 0)              # topics and keywords
        self.assertEqual(self.cli("keywords", self.owner, "add", "kw")[0], 0)
        self.assertEqual(self.cli("expire", self.oneoff)[0], 0)                            # a lapsed one-off (D-286)
        a = self.audit("decision.add")[-1]
        self.assertEqual((a["actor"], a["via"]), ("cli", "cli"))

    def test_a_session_the_registry_knows_is_unattended_too(self):
        os.environ.pop("CLAUDE_GUARD_DISABLE")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-known"}), \
                mock.patch.object(actions, "autonomous_sessions", return_value={"sid-known"}):
            self.refused("confirm", self.tm)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-other"}), \
                mock.patch.object(actions, "autonomous_sessions", return_value={"sid-known"}):
            self.assertEqual(self.cli("confirm", self.tm)[0], 0)

    def test_the_owner_at_a_plain_shell_may(self):
        os.environ.pop("CLAUDE_GUARD_DISABLE")
        self.assertEqual(self.cli("confirm", self.tm)[0], 0)
        self.assertEqual(self.cli("edit", self.owner, "--title", "Fixed typo")[0], 0)
        self.assertEqual(self.cli("add", "--title", "Said", "--topic", "t", "--author", "owner", "--words", "w")[0], 0)
        self.assertEqual(self.cli("expire", self.future, "--reason", "done")[0], 0)
        code, _, err = self.cli("edit", self.owner, "--summary", "reworded")                # words / summary: nobody
        self.assertEqual(code, 1)
        self.assertIn("words and summary", err)


class Entrypoint(unittest.TestCase):
    """docker/entrypoint.sh's migrate step, with fake python3 and sleep: a failed migrate idles the container
    (no runner, so no restart loop writing a backup per start); no database yet or success goes on."""
    SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docker", "entrypoint.sh")

    def run_entry(self, migrate_rc, mode="runner"):
        tmp = tempfile.mkdtemp(prefix="afc-entry-")
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "data"))
        os.makedirs(os.path.join(tmp, "bin"))
        log = os.path.join(tmp, "calls.log")
        for name, body in (("python3", f'echo "python3 $*" >> {log}\ncase "$*" in *"store.py migrate"*) exit '
                                       f'{migrate_rc};; esac\nexit 0\n'),
                           ("sleep", f'echo "sleep $*" >> {log}\nexit 0\n')):
            path = os.path.join(tmp, "bin", name)
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\n" + body)
            os.chmod(path, 0o755)
        env = dict(os.environ, PATH=os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"], AFCLAUDE_DIR=tmp,
                   KEEPALIVE_STATE_DIR=os.path.join(tmp, "ka"), AFCLAUDE_BRIDGE_KNOWN_HOSTS=os.path.join(tmp, "kh"),
                   AFCLAUDE_SCHEDULER=mode)
        r = subprocess.run(["sh", self.SCRIPT], env=env, capture_output=True, text=True, timeout=60)
        with open(log) as fh:
            return r, fh.read().splitlines()

    def test_a_failed_migrate_idles_instead_of_starting_the_runner(self):
        r, calls = self.run_entry(1)
        self.assertEqual(len(calls), 2, calls)                   # the migrate, then the idle: nothing else ran
        self.assertIn("store.py migrate", calls[0])
        self.assertEqual(calls[1], "sleep infinity")
        self.assertIn("migrate FAILED", r.stderr)

    def test_no_database_yet_and_success_go_on_to_the_runner(self):
        for rc in (0, store.MIGRATE_NO_DB):
            r, calls = self.run_entry(rc)
            self.assertTrue(any("runner.py serve" in c for c in calls), (rc, calls))
            self.assertNotIn("sleep infinity", calls)

    def test_an_unknown_mode_alerts_and_idles_before_migrate(self):
        """Phase 3a S9 review: a stale `on` (or any value but runner/off) must not exit (a silent restart
        loop through migrate): the fallback alert, then the idle; neither migrate nor the runner runs."""
        for mode in ("on", "legacy"):
            r, calls = self.run_entry(0, mode)
            self.assertEqual(len(calls), 2, (mode, calls))
            self.assertIn("store.alert_once", calls[0])
            self.assertIn(f"AFClaude: unknown AFCLAUDE_SCHEDULER={mode}", calls[0])
            self.assertEqual(calls[1], "sleep infinity")
            self.assertIn("must be runner or off", r.stderr)
        r, calls = self.run_entry(0, "off")
        self.assertIn("store.py migrate", calls[0])
        self.assertEqual(calls[-1], "sleep infinity")


if __name__ == "__main__":
    unittest.main()
