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
import tempfile
import textwrap
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
        return self.do("decision.add", title=title, topics=list(topics), **kw)

    def imp(self):
        return dec.import_file(self.src, self.db, actor="cli", via="cli", conn=self.conn)


class Migration(unittest.TestCase):
    """v4 -> v5 on a copy: `store.py migrate` backs up, rebuilds decisions, keeps every row."""
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

    def test_a_fresh_database_is_v5_with_the_new_columns(self):
        conn = store.connect(os.path.join(self.tmp, "new.db"), create=True)
        try:
            self.assertEqual(store.get_meta(conn, "schema_version"), "5")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
            self.assertTrue({"slug", "project_id", "author", "origin"} <= cols)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM decision_topics").fetchone()[0], 0)
        finally:
            conn.close()


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
        self.assertEqual(self.add(id="D-300")["id"], "D-300")
        self.assertEqual(self.add(title="Second")["id"], "D-301")             # above the highest one
        self.do("decision.delete", decision="D-301", reason="test")
        self.assertEqual(self.add(title="Third")["id"], "D-302")              # a deleted number isn't reused
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.add(id="D-300")
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
        r = self.do("decision.edit", decision="d-001", title="New title", summary="new summary")
        self.assertEqual((r["title"], r["summary"], r["version"]), ("New title", "new summary", 1))
        d = store.get_decision_entry(self.conn, "D-001")
        self.assertIn("### D-001 New title", d["body"])
        self.assertNotIn("Old title", d["body"])                              # overwritten, not kept (D-158)
        self.assertIn("- **Scope:** all", d["body"])
        a = self.audit("decision.edit")[0]
        self.assertEqual((a["before"]["title"], a["after"]["title"], a["before"]["summary"], a["after"]["summary"]),
                         ("Old title", "New title", "old summary", "new summary"))
        self.assertNotIn("scope", a["before"])                                # only what changed
        self.assertEqual(len(self.audit("decision.edit")), 1)

    def test_noop_writes_no_audit_row_and_clearing_works(self):
        self.do("decision.edit", decision="D-001", summary="old summary")
        self.assertEqual(self.audit("decision.edit"), [])
        self.do("decision.edit", decision="D-001", summary="")
        self.assertIsNone(store.get_decision_entry(self.conn, "D-001")["summary"])
        with self.assertRaisesRegex(ValueError, "nothing to change"):
            self.do("decision.edit", decision="D-001")
        with self.assertRaisesRegex(ValueError, "title must not be empty"):
            self.do("decision.edit", decision="D-001", title="")
        with self.assertRaisesRegex(ValueError, "exact words"):
            self.do("decision.edit", decision="D-001", words="")

    def test_stale_version_conflicts(self):
        self.do("decision.edit", decision="D-001", title="B")
        with self.assertRaises(actions.Conflict):
            self.do("decision.edit", decision="D-001", title="C", version=0)
        self.do("decision.edit", decision="D-001", title="C", version=1)

    def test_only_native_entries_and_the_owners_stay_with_the_owner(self):
        self.imp()                                                              # D-002.. come from the file
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
        self.assertEqual(self.add(title="Native")["id"], "D-005")                  # above the file's D-004
        r = self.imp()
        self.assertEqual((r["entries"], r["removed"], r["detached"]), (5, [], []))   # not removed, not counted
        self.assertEqual(store.get_decision_entry(self.conn, "D-005")["origin"], "db")
        # the file later gets the same id: the native one stays, the file's is listed as ignored
        with open(self.src, "a", encoding="utf-8") as fh:
            fh.write("\n### D-005 From the file\n- **Keywords:** k\n")
        r = self.imp()
        self.assertEqual(r["detached"], ["D-005"])
        self.assertEqual(store.get_decision_entry(self.conn, "D-005")["title"], "Native")
        self.assertEqual(self.imp()["removed"], [])

    def test_mass_removal_guard_counts_only_file_entries(self):
        self.imp()
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
                    words="w", id=f"D-{n:03d}")
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


class Cli(Env):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tasks.main(["--db", self.db, "decision", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_commands(self):
        code, out, _ = self.cli("add", "--title", "Keep titles", "--topic", "lists", "--words", "exact words",
                                "--keyword", "tldr")
        self.assertEqual((code, out.splitlines()[0]), (0, "D-001 (state: active; topics: lists)"))
        self.assertIn("new topics: lists", out)
        code, _, err = self.cli("add", "--title", "No words", "--topic", "x")
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


if __name__ == "__main__":
    unittest.main()
