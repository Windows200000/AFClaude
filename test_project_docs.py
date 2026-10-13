#!/usr/bin/env python3
"""Tests for the project docs in the DB (dashboard phase 2d part 2): the docs/doc_entries tables,
the actions (doc.append, doc.set, doc.sync with its 3-way merge of hand edits and runner notes),
the importer's idempotency, the byte-identical export round trip of the public build log
(PROGRESS.md, GOALS.md, EXCEPTIONS.md, the repo's own committed files), the public-repo check on
the exported files, the CLI (tasks.py doc), the runners' notes (append_note, notify.py, keepalive's
progress_note: into the DB, no file written) and the scheduled import (import --cron).

    python3 -m unittest test_project_docs
"""
import contextlib
import io
import os
import shutil
import sqlite3
import tempfile
import unittest

import testenv  # noqa: F401  (hermetic DB/config; before any AFClaude module)
import actions
import project_docs as pd
import store
import tasks

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_LOG_FILES = ("PROGRESS.md", "GOALS.md", "EXCEPTIONS.md")


class Env(unittest.TestCase):
    """A fresh DB, a project 'P' whose directory is a temp repo, and a reviews dir."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-docs-")
        self.db = os.path.join(self.tmp, "data", "afclaude.db")
        self.repo = os.path.join(self.tmp, "repo")
        self.reviews = os.path.join(self.tmp, "data", "reviews")
        os.makedirs(self.repo)
        os.makedirs(self.reviews)
        store.connect(self.db, create=True).close()
        with self.conn() as c:
            store.add_project(c, "P", None, self.repo)
        self._env = dict(os.environ)
        # the public check reads the identity terms from $HOME and the local denylist: hermetic ones
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        self.denylist = os.path.join(self.tmp, "denylist.txt")
        with open(self.denylist, "w") as fh:
            fh.write("# test\nsecret-host.example\n")
        os.environ.update(HOME=home, AFCLAUDE_PUBLIC_DENYLIST=self.denylist)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, True)

    @contextlib.contextmanager
    def conn(self):
        c = store.connect(self.db)
        try:
            yield c
        finally:
            c.close()

    def act(self, action, actor="cli", via="cli", **params):
        with self.conn() as c:
            return actions.perform(c, action, params, actor=actor, via=via)

    def render(self, name, project="P"):
        with self.conn() as c:
            d = store.get_doc(c, store.get_project(c, project)["id"], name)
            return None if d is None else store.doc_render(c, d["id"])

    def entries(self, name):
        with self.conn() as c:
            d = store.get_doc(c, store.get_project(c, "P")["id"], name)
            return [dict(r) for r in c.execute("SELECT * FROM doc_entries WHERE doc_id=? ORDER BY id", (d["id"],))]

    def write(self, fname, text, where=None):
        with open(os.path.join(where or self.repo, fname), "wb") as fh:
            fh.write(text.encode("utf-8"))

    def read(self, fname):
        with open(os.path.join(self.repo, fname), "rb") as fh:
            return fh.read()

    def sync(self, name, text, **kw):
        return self.act("doc.sync", actor="runner:import", via="runner", project="P", name=name, text=text, **kw)

    def audits(self):
        with self.conn() as c:
            return store.audit_log(c, limit=1000, target_type="doc")


class Tables(Env):
    def test_entries_are_append_only_and_docs_never_deleted(self):
        self.act("doc.append", project="P", name="progress", text="- a\n")
        with self.conn() as c:
            for sql in ("UPDATE doc_entries SET body='x'", "DELETE FROM doc_entries", "DELETE FROM docs"):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                    c.execute(sql)
            c.rollback()

    def test_version_bumps_on_every_write(self):
        self.act("doc.set", project="P", name="goals", text="v1\n")
        with self.conn() as c:
            v1 = store.get_doc(c, store.get_project(c, "P")["id"], "goals")["version"]
        self.act("doc.set", project="P", name="goals", text="v2\n")
        with self.conn() as c:
            self.assertEqual(store.get_doc(c, store.get_project(c, "P")["id"], "goals")["version"], v1 + 1)

    def test_names_and_kinds(self):
        self.assertEqual([store.doc_kind(n) for n in ("progress", "exceptions", "alerts", "goals", "review:2026-10-01")],
                         ["log", "log", "log", "body", "body"])
        for bad in ("backlog", "decisions", "review:", "review:../x", "", None, 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                store.doc_kind(bad)

    def test_split_chunks_is_lossless(self):
        for text in ("", "x", "# T\n\n- a\n  more\n- b", "| a |\n|---|\n| 1 |\n", "a\r\nb\r\n- c\r\n",
                     "## H\n* i\n\n## J\n"):
            self.assertEqual("".join(store.split_chunks(text)), text)
        self.assertEqual(store.split_chunks("# T\nintro\n- a\n  cont\n- b\n## H\n"),
                         ["# T\nintro\n", "- a\n  cont\n", "- b\n", "## H\n"])


class Actions(Env):
    def test_append_is_exact_and_audited(self):
        r = self.act("doc.append", project="P", name="progress", text="- 10:00 one\n")
        self.act("doc.append", project="P", name="progress", text="- 10:05 two, no newline")
        self.assertEqual(r["kind"], "log")
        self.assertEqual(self.render("progress"), "- 10:00 one\n- 10:05 two, no newline")
        a = self.audits()
        self.assertEqual([x["action"] for x in a], ["doc.append", "doc.append"])
        self.assertNotIn("10:00 one", str(a[-1]["after"]))      # the audit names the entry, not its text
        e = self.entries("progress")
        self.assertEqual([(x["op"], x["author"], x["via"]) for x in e], [("append", "cli", "cli")] * 2)
        self.assertEqual(e[0]["body_sha256"], store.sha256_text("- 10:00 one\n"))

    def test_append_refuses_body_docs_and_bad_text(self):
        with self.assertRaisesRegex(ValueError, "not a log"):
            self.act("doc.append", project="P", name="goals", text="x\n")
        for bad in ("", "  \n", "a\0b", 7):
            with self.assertRaises(ValueError):
                self.act("doc.append", project="P", name="progress", text=bad)
        with self.assertRaises(store.NotFound):
            self.act("doc.append", project="nope", name="progress", text="x\n")

    def test_set_versions_history_and_noop(self):
        self.act("doc.set", project="P", name="goals", text="# Goals\n1. [ ] a\n")
        self.act("doc.set", project="P", name="goals", text="# Goals\n1. [x] a\n")
        n = len(self.audits())
        r = self.act("doc.set", project="P", name="goals", text="# Goals\n1. [x] a\n")
        self.assertTrue(r["unchanged"])
        self.assertEqual(len(self.audits()), n)                  # a no-op writes no audit row
        self.assertEqual(self.render("goals"), "# Goals\n1. [x] a\n")
        self.assertEqual([x["body"] for x in self.entries("goals")], ["# Goals\n1. [ ] a\n", "# Goals\n1. [x] a\n"])
        with self.assertRaises(actions.Conflict):
            self.act("doc.set", project="P", name="goals", text="z\n", version=0)

    def test_sync_cases(self):
        text = "# Log\n\n- 1\n- 2\n"
        r = self.sync("progress", text)
        self.assertEqual((r["outcome"], r["appended"]), ("imported", 3))
        self.assertEqual(self.render("progress"), text)
        n = len(self.audits())
        self.assertEqual(self.sync("progress", text)["outcome"], "in_sync")
        self.assertEqual(len(self.audits()), n)                  # idempotent: no entry, no audit row
        # appended in the file -> only the new entries
        text += "- 3\n  detail\n- 4\n"
        r = self.sync("progress", text)
        self.assertEqual((r["outcome"], r["appended"], r.get("replaced")), ("imported", 2, False))
        # an in-place edit -> one replace keeping the unchanged entries, plus the changed tail
        edited = text.replace("- 2\n", "- 2 (edited)\n")
        r = self.sync("progress", edited)
        self.assertEqual((r["outcome"], r["kept"], r["replaced"]), ("imported", 2, True))
        self.assertEqual(self.render("progress"), edited)
        last = self.entries("progress")[-4:]
        self.assertEqual([(e["op"], e["keep"]) for e in last], [("append", 0), ("replace", 2), ("append", 0),
                                                                  ("append", 0)])
        # truncated at an entry boundary
        cut = "# Log\n\n- 1\n"
        self.sync("progress", cut)
        self.assertEqual(self.render("progress"), cut)
        # a body doc: one replace per change
        self.sync("goals", "a\n")
        self.sync("goals", "b\n")
        self.assertEqual([(e["op"], e["keep"], e["body"]) for e in self.entries("goals")],
                         [("replace", 0, "a\n"), ("replace", 0, "b\n")])

    def test_sync_db_ahead_merge_and_conflict(self):
        self.sync("progress", "- 1\n")
        self.act("doc.append", project="P", name="progress", text="- 2 via cli\n")
        # the file did not change since the sync: the DB is ahead, nothing is imported
        self.assertEqual(self.sync("progress", "- 1\n")["outcome"], "db_ahead")
        self.assertEqual(self.render("progress"), "- 1\n- 2 via cli\n")
        # the file grew with the DB text in it: plain append, no conflict
        self.assertEqual(self.sync("progress", "- 1\n- 2 via cli\n- 3\n")["outcome"], "imported")
        self.act("doc.append", project="P", name="progress", text="- 4 via cli\n")
        # both changed, different lines (an in-place edit of the file, an append in the DB): merged
        r = self.sync("progress", "- 1\n- 2 via cli\n- 3 edited\n")
        self.assertEqual((r["outcome"], r["replaced"]), ("merged", True))
        self.assertEqual(self.render("progress"), "- 1\n- 2 via cli\n- 3 edited\n- 4 via cli\n")
        self.assertEqual(self.sync("progress", "- 1\n- 2 via cli\n- 3 edited\n")["outcome"], "db_ahead")
        # both changed the same line (a rewrite through doc.set vs a hand edit): refused, nothing written
        self.act("doc.set", project="P", name="progress", text="- 1 db\n- 2 via cli\n- 3 edited\n- 4 via cli\n")
        with self.assertRaises(actions.Conflict):
            self.sync("progress", "- 1 file\n- 2 via cli\n- 3 edited\n")
        self.assertEqual(self.render("progress"), "- 1 db\n- 2 via cli\n- 3 edited\n- 4 via cli\n")
        r = self.sync("progress", "- 1 file\n- 2 via cli\n- 3 edited\n", force=True)    # the file wins
        self.assertTrue(r["replaced"])
        self.assertEqual(self.render("progress"), "- 1 file\n- 2 via cli\n- 3 edited\n")
        self.assertIn("- 4 via cli\n", [e["body"] for e in self.entries("progress")])   # kept as history

    def test_hand_appends_and_runner_notes_merge(self):
        """The switch's everyday case: the task-manager appends to PROGRESS.md by hand while the
        runners append notes to the DB; each import merges, nothing is lost, nothing conflicts."""
        base = "# Log\n\n- 1\n"
        self.sync("progress", base)
        self.act("doc.append", actor="runner:notify", via="runner", project="P", name="progress", text="- [ALERT] a\n")
        f1 = base + "- 2 by hand\n"
        self.assertEqual(self.sync("progress", f1)["outcome"], "merged")
        self.assertEqual(self.render("progress"), base + "- [ALERT] a\n- 2 by hand\n")
        self.act("doc.append", actor="runner:keepalive", via="runner", project="P", name="progress", text="- note\n")
        f2 = f1 + "- 3 by hand\n"                   # the file still has no runner lines (no export yet)
        self.assertEqual(self.sync("progress", f2)["outcome"], "merged")
        self.assertEqual(self.render("progress"), base + "- [ALERT] a\n- 2 by hand\n- note\n- 3 by hand\n")
        f3 = f2.replace("- 1\n", "- 1 (done)\n")      # and an in-place edit of an old line
        self.assertEqual(self.sync("progress", f3)["outcome"], "merged")
        self.assertEqual(self.render("progress"), "# Log\n\n- 1 (done)\n- [ALERT] a\n- 2 by hand\n- note\n- 3 by hand\n")
        # a mark from before file_text existed: the base is the DB's text as of the mark's entry
        self.sync("goals", "a\nb\n")
        with self.conn() as c:
            c.execute("UPDATE docs SET file_text=NULL WHERE name='goals'")
            c.commit()
        self.act("doc.set", project="P", name="goals", text="a\nb\nc (db)\n")
        self.assertEqual(self.sync("goals", "a (file)\nb\n")["outcome"], "merged")
        self.assertEqual(self.render("goals"), "a (file)\nb\nc (db)\n")

    def test_merge3(self):
        m = actions.merge3
        self.assertEqual(m("a\n", "a\nx\n", "a\ny\n"), "a\nx\ny\n")              # both appended: ours first
        self.assertEqual(m("a\nb\n", "a\nb\nx\n", "A\nb\n"), "A\nb\nx\n")         # edit + append
        self.assertEqual(m("a\nb\n", "a\nB\n", "a\nB\n"), "a\nB\n")               # the same change once
        self.assertIsNone(m("a\nb\n", "a\nB1\n", "a\nB2\n"))                   # the same line, differently
        self.assertEqual(m("", "x\n", "y\n"), "x\ny\n")
        self.assertEqual(m("a\nb\nc\n", "a\nc\n", "a\nb\nc\nd\n"), "a\nc\nd\n")  # a deletion + an append


class Importer(Env):
    def setUp(self):
        super().setUp()
        for f in BUILD_LOG_FILES:
            shutil.copy(os.path.join(HERE, f), self.repo)
        self.write("ALERTS.md", "- **2026-10-01 17:32** x\n  y\n  _(push: z)_\n")
        self.write("2026-10-01.md", "# Review\n\nbody\n", self.reviews)
        self.write("bad name!.md", "x\n", self.reviews)

    def run_import(self, **kw):
        return pd.import_all(self.db, self.repo, self.reviews, **kw)

    def test_import_is_idempotent_and_verified(self):
        r = self.run_import()
        self.assertEqual(r["project"], "P")
        t = r["totals"]
        self.assertEqual((t["files"], t["imported"], t["conflict"], t["unverified"]), (5, 5, 0, 0))
        self.assertEqual(r["docs"]["review:bad name!"]["outcome"], "skipped")
        self.assertTrue(all(d.get("verified") for n, d in r["docs"].items() if n != "review:bad name!"))
        self.assertEqual(set(r["counts"]), {"progress", "goals", "exceptions", "alerts", "review:2026-10-01"})
        self.assertGreater(r["counts"]["progress"], 10)          # split into entries
        with self.conn() as c:
            n_entries = c.execute("SELECT COUNT(*) FROM doc_entries").fetchone()[0]
        n_audit = len(self.audits())
        r2 = self.run_import()
        self.assertEqual((r2["totals"]["in_sync"], r2["totals"]["entries_added"]), (5, 0))
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM doc_entries").fetchone()[0], n_entries)
        self.assertEqual(len(self.audits()), n_audit)
        # the files were never touched
        for f in BUILD_LOG_FILES:
            with open(os.path.join(HERE, f), "rb") as fh:
                self.assertEqual(self.read(f), fh.read())

    def test_missing_files_and_unknown_project(self):
        os.remove(os.path.join(self.repo, "ALERTS.md"))
        r = self.run_import()
        self.assertEqual(r["docs"]["alerts"]["outcome"], "missing")
        with self.assertRaises(store.NotFound):
            pd.import_all(self.db, os.path.join(self.tmp, "elsewhere"), self.reviews)

    def test_export_round_trip_is_byte_identical(self):
        """The three build-log files: import, delete, export -> the same bytes; then nothing to do."""
        originals = {f: self.read(f) for f in BUILD_LOG_FILES}
        self.run_import()
        for f in BUILD_LOG_FILES:
            os.remove(os.path.join(self.repo, f))
        x = pd.export(self.db, self.repo)
        self.assertTrue(x["ok"], x)
        self.assertEqual({f: d["result"] for f, d in x["files"].items()}, dict.fromkeys(BUILD_LOG_FILES, "written"))
        for f in BUILD_LOG_FILES:
            self.assertEqual(self.read(f), originals[f], f)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "ALERTS.md.tmp")))
        self.assertNotIn("ALERTS.md", x["files"])                # never exported (D-181)
        x = pd.export(self.db, self.repo)
        self.assertEqual({d["result"] for d in x["files"].values()}, {"unchanged"})
        self.assertEqual(self.run_import()["totals"]["imported"], 0)

    def test_export_writes_db_entries_and_keeps_hand_edits(self):
        self.run_import()
        orig = self.read("PROGRESS.md").decode()
        # after the switch: the task-manager appends through the write path, the export writes the file
        self.act("doc.append", project="P", name="progress", text="- 23:59 via the CLI\n")
        chk = pd.export(self.db, self.repo, check=True)
        self.assertEqual(chk["files"]["PROGRESS.md"]["result"], "would write")
        self.assertEqual(self.read("PROGRESS.md").decode(), orig)          # --check writes nothing
        pd.export(self.db, self.repo)
        self.assertEqual(self.read("PROGRESS.md").decode(), orig + "- 23:59 via the CLI\n")
        # a hand edit of the file since the last sync is imported first, not overwritten
        edited = orig.replace("progress log", "progress log (edited)", 1) + "- 23:59 via the CLI\n- by hand\n"
        self.write("PROGRESS.md", edited)
        x = pd.export(self.db, self.repo)
        self.assertEqual((x["files"]["PROGRESS.md"]["sync"], x["files"]["PROGRESS.md"]["result"]),
                         ("imported", "unchanged"))
        self.assertEqual(self.render("progress"), edited)
        # both changed (a runner note in the DB, a hand edit in the file): merged, then written out
        self.act("doc.append", actor="runner:notify", via="runner", project="P", name="progress", text="- db only\n")
        self.write("PROGRESS.md", edited + "- by hand 2\n")
        x = pd.export(self.db, self.repo)
        self.assertTrue(x["ok"], x)
        self.assertEqual((x["files"]["PROGRESS.md"]["sync"], x["files"]["PROGRESS.md"]["result"]),
                         ("merged", "written"))
        self.assertEqual(self.read("PROGRESS.md").decode(), edited + "- db only\n- by hand 2\n")
        self.assertEqual(self.run_import()["docs"]["progress"]["outcome"], "in_sync")
        # both changed the same line: the export refuses that file and leaves it alone
        merged = self.read("PROGRESS.md").decode()
        self.act("doc.set", project="P", name="progress", text=merged.replace("- by hand 2\n", "- db rewrite\n"))
        self.write("PROGRESS.md", merged.replace("- by hand 2\n", "- file rewrite\n"))
        x = pd.export(self.db, self.repo)
        self.assertFalse(x["ok"])
        self.assertEqual(x["files"]["PROGRESS.md"]["result"], "conflict")
        self.assertEqual(self.read("PROGRESS.md").decode(), merged.replace("- by hand 2\n", "- file rewrite\n"))

    def test_public_check_on_exported_files(self):
        self.run_import()
        clean = pd.public_findings("PROGRESS.md", self.render("progress"))
        self.assertEqual(clean, [])
        for f in ("GOALS.md", "EXCEPTIONS.md"):
            self.assertEqual(pd.public_findings(f, self.render(f[:-3].lower())), [], f)
        before = self.read("GOALS.md")
        self.act("doc.set", project="P", name="goals", text="# Goals\nsee secret-host.example\n")
        x = pd.export(self.db, self.repo)
        self.assertFalse(x["ok"])
        g = x["files"]["GOALS.md"]
        self.assertEqual(g["result"], "refused: public-repo check")
        self.assertTrue(any("denylist" in f for f in g["findings"]))
        self.assertEqual(self.read("GOALS.md"), before)          # not written


class Notes(Env):
    """The runners' notes (§7.7 switch): append_note() writes the DB only, never a file; a project
    the DB doesn't know or a broken DB goes to the fallback file (§7.8); notify.py and
    keepalive.progress_note() use it."""
    def fallback(self):
        try:
            with open(os.path.join(self.tmp, "data", store.FALLBACK_NAME), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def test_append_note_into_the_db(self):
        self.assertTrue(pd.append_note("alerts", "- **x** y\n", "runner:notify", repo=self.repo, db_path=self.db))
        self.assertEqual(self.render("alerts"), "- **x** y\n")
        self.assertEqual([(e["author"], e["via"]) for e in self.entries("alerts")], [("runner:notify", "runner")])
        self.assertEqual(os.listdir(self.repo), [])                                 # no file written
        self.assertEqual(pd.doc_text("alerts", self.repo, self.db), "- **x** y\n")
        self.assertIsNone(pd.doc_text("goals", self.repo, self.db))

    def test_append_note_unknown_project_and_broken_db(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(pd.append_note("progress", "- lost?\n", "runner:keepalive",
                                            repo=os.path.join(self.tmp, "elsewhere"), db_path=self.db))
        self.assertIn("- lost?", self.fallback())
        os.remove(self.db)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(pd.append_note("progress", "- db gone\n", "runner:keepalive", repo=self.repo,
                                            db_path=self.db))
        self.assertIn("doc.append", self.fallback())

    def test_notify_and_keepalive_notes(self):
        import notify
        import keepalive as ka
        saved = (notify.push, pd.HERE, store.DB_PATH, ka.PROGRESS_FILE)
        notify.push = lambda text: "test push"
        pd.HERE, store.DB_PATH = self.repo, self.db
        ka.PROGRESS_FILE = os.path.join(self.repo, "PROGRESS.md")
        try:
            self.assertEqual(notify.notify("disk full", "details"), "test push")
            ka.progress_note("window-start: not resumed")
        finally:
            notify.push, pd.HERE, store.DB_PATH, ka.PROGRESS_FILE = saved
        self.assertRegex(self.render("alerts"), r"^- \*\*\d{4}-\d\d-\d\d \d\d:\d\d\*\* disk full\n  details\n  _\(push: test push\)_\n$")
        prog = self.render("progress")
        self.assertIn("[ALERT] disk full (details in the alerts doc)\n", prog)
        self.assertIn("[keepalive.py] window-start: not resumed\n", prog)
        self.assertEqual({e["author"] for e in self.entries("progress")}, {"runner:notify", "runner:keepalive"})
        self.assertEqual(os.listdir(self.repo), [])                                 # ALERTS.md / PROGRESS.md untouched


class CronImport(Env):
    """import --cron (docker/crontab): quiet when nothing happened, one line otherwise; a conflict
    alerts once per episode and doc."""
    def setUp(self):
        super().setUp()
        self.alerts = []
        self.saved_alert = pd._alert
        pd._alert = lambda subject, body: self.alerts.append(subject)

    def tearDown(self):
        pd._alert = self.saved_alert
        super().tearDown()

    def cron(self):
        return pd.cron_import(self.db, self.repo, self.reviews)

    def test_quiet_import_merge_and_conflict_alert(self):
        self.write("PROGRESS.md", "- 1\n")
        code, line = self.cron()
        self.assertEqual(code, 0)
        self.assertIn("progress: imported +1", line)
        self.assertEqual(self.cron(), (0, ""))                                       # nothing new: silent
        self.act("doc.append", actor="runner:notify", via="runner", project="P", name="progress", text="- n\n")
        self.assertEqual(self.cron(), (0, ""))                                       # DB ahead: silent
        self.write("PROGRESS.md", "- 1\n- 2\n")
        self.assertIn("progress: merged +1", self.cron()[1])
        self.act("doc.set", project="P", name="progress", text="- 1 db\n- n\n- 2\n")
        self.write("PROGRESS.md", "- 1 file\n- 2\n")
        code, line = self.cron()
        self.assertEqual(code, 1)
        self.assertIn("progress: conflict", line)
        self.cron()
        self.assertEqual(self.alerts, ["docs import: conflict in P/progress"])      # once per episode
        self.write("PROGRESS.md", "- 1 db\n- n\n- 2\n")                             # resolved by hand
        self.assertEqual(self.cron()[0], 0)
        self.write("PROGRESS.md", "- 1 db\n- n\n- 2 x\n")
        self.act("doc.set", project="P", name="progress", text="- 1 db\n- n\n- 2 y\n")
        self.cron()
        self.assertEqual(len(self.alerts), 2)                                        # a new episode alerts again

    def test_cli_flag(self):
        self.write("GOALS.md", "# G\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pd.main(["import", "--cron", "--db", self.db, "--repo", self.repo,
                                      "--reviews-dir", self.reviews]), 0)
        self.assertIn("goals: imported", out.getvalue())


class Cli(Env):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tasks.main(["--db", self.db, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_append_set_show_list(self):
        self.assertEqual(self.cli("doc", "append", "progress", "- 10:00 one", "--project", "P")[0], 0)
        self.assertEqual(self.render("progress"), "- 10:00 one\n")          # newline added
        code, out, _ = self.cli("doc", "set", "goals", "# G\n", "--project", self.repo)
        self.assertEqual(code, 0)
        self.assertIn("entry #", out)
        code, out, _ = self.cli("doc", "show", "goals", "--project", "P")
        self.assertEqual((code, out), (0, "# G\n"))
        code, out, _ = self.cli("doc", "list", "--project", "P")
        self.assertIn("progress", out)
        code, _, err = self.cli("doc", "append", "goals", "x", "--project", "P")
        self.assertEqual(code, 1)
        self.assertIn("not a log", err)

    def test_import_and_export(self):
        for f in BUILD_LOG_FILES:
            shutil.copy(os.path.join(HERE, f), self.repo)
        code, out, _ = self.cli("doc", "import", "--repo", self.repo)
        self.assertEqual(code, 0, out)
        self.assertIn("imported", out)
        code, out, _ = self.cli("doc", "export", "--repo", self.repo)
        self.assertEqual(code, 0, out)
        self.assertIn("unchanged", out)
        with self.conn() as c:
            authors = {r[0] for r in c.execute("SELECT DISTINCT author FROM doc_entries")}
        self.assertEqual(authors, {"cli"})

    def test_store_import_docs_command(self):
        shutil.copy(os.path.join(HERE, "GOALS.md"), self.repo)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(store.main(["import-docs", "--db", self.db, "--repo", self.repo,
                                         "--reviews-dir", self.reviews]), 0)
        with self.conn() as c:
            authors = {r[0] for r in c.execute("SELECT DISTINCT author FROM doc_entries")}
        self.assertEqual(authors, {"runner:import"})
        with open(os.path.join(HERE, "GOALS.md"), encoding="utf-8", newline="") as fh:
            self.assertEqual(self.render("goals"), fh.read())


if __name__ == "__main__":
    unittest.main()
