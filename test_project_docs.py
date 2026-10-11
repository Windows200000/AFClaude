#!/usr/bin/env python3
"""Tests for the project docs in the DB (dashboard phase 2d part 2): the docs/doc_entries tables,
the actions (doc.append, doc.set, doc.sync), the importer's idempotency, the byte-identical
export round trip of the public build log (PROGRESS.md, GOALS.md, EXCEPTIONS.md, the repo's own
committed files), the public-repo check on the exported files, and the CLI (tasks.py doc).

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

    def test_sync_db_ahead_and_conflict(self):
        self.sync("progress", "- 1\n")
        self.act("doc.append", project="P", name="progress", text="- 2 via cli\n")
        # the file did not change since the sync: the DB is ahead, nothing is imported
        self.assertEqual(self.sync("progress", "- 1\n")["outcome"], "db_ahead")
        self.assertEqual(self.render("progress"), "- 1\n- 2 via cli\n")
        # the file grew with the DB text in it: plain append, no conflict
        self.assertEqual(self.sync("progress", "- 1\n- 2 via cli\n- 3\n")["outcome"], "imported")
        self.act("doc.append", project="P", name="progress", text="- 4 via cli\n")
        # both changed and the file would drop DB text: refused, nothing written
        with self.assertRaises(actions.Conflict):
            self.sync("progress", "- 1\n- 2 via cli\n- 3 edited\n")
        self.assertEqual(self.render("progress"), "- 1\n- 2 via cli\n- 3\n- 4 via cli\n")
        r = self.sync("progress", "- 1\n- 2 via cli\n- 3 edited\n", force=True)    # the file wins
        self.assertTrue(r["replaced"])
        self.assertEqual(self.render("progress"), "- 1\n- 2 via cli\n- 3 edited\n")
        self.assertIn("- 4 via cli\n", [e["body"] for e in self.entries("progress")])   # kept as history


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
        # dual-write: a hand edit of the file since the last sync is imported first, not overwritten
        edited = orig.replace("progress log", "progress log (edited)", 1) + "- 23:59 via the CLI\n- by hand\n"
        self.write("PROGRESS.md", edited)
        x = pd.export(self.db, self.repo)
        self.assertEqual((x["files"]["PROGRESS.md"]["sync"], x["files"]["PROGRESS.md"]["result"]),
                         ("imported", "unchanged"))
        self.assertEqual(self.render("progress"), edited)
        # both changed: the export refuses that file and leaves it alone
        self.act("doc.append", project="P", name="progress", text="- db only\n")
        self.write("PROGRESS.md", orig)
        x = pd.export(self.db, self.repo)
        self.assertFalse(x["ok"])
        self.assertEqual(x["files"]["PROGRESS.md"]["result"], "conflict")
        self.assertEqual(self.read("PROGRESS.md").decode(), orig)

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
