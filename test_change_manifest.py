#!/usr/bin/env python3
"""Offline tests for change_manifest.py (D-243) on a temp git repo: AST-level symbol changes
(added, removed, changed body or signature, a method, a rename, constants), the interfaces (schema,
settings keys, CLI, MCP tools, audited actions, crontab, .claude settings), test files left out,
docs as a word diff, the --live-hunks switch, decision-keyword matching on a fake decisions file,
the mapping flags, the audit-log section on a temp DB, the snapshot comparison and the CLI's exit
codes."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import change_manifest as cm  # noqa: E402
import store  # noqa: E402

BASE_FILES = {
    "mod.py": '''
        import os

        LIMIT = 3


        def keep(a):
            return a


        def gone(x):
            return x * 2


        def body_changes(a):
            return a + 1


        def sig_changes(a):
            return a


        def old_name(v):
            total = v + 41
            return total


        class Box:
            size = 1

            def open(self):
                return "open"

            def close(self):
                return "close"
        ''',
    "store.py": '''
        SCHEMA_VERSION = 4
        SCHEMA = """
        CREATE TABLE IF NOT EXISTS tasks (
            id    INTEGER PRIMARY KEY,
            title TEXT NOT NULL   -- the title
        );
        CREATE INDEX IF NOT EXISTS tasks_title ON tasks(title);
        """
        COLUMNS = {"tasks": [("version", "INTEGER")]}
        ''',
    "actions.py": '''
        SETTINGS = {"window_tz": Setting("Europe/Berlin"), "old_key": Setting(1)}


        @action("task.add", "task")
        def task_add(conn, ctx):
            return 1


        @action("task.drop", "task")
        def task_drop(conn, ctx):
            return 2
        ''',
    "mcp_server.py": '''
        @tool("List tasks.")
        async def afclaude_list(limit: int = 20) -> str:
            return ""
        ''',
    "tasks.py": '''
        import argparse


        def main():
            ap = argparse.ArgumentParser()
            js = argparse.ArgumentParser(add_help=False)
            js.add_argument("--json")
            sub = ap.add_subparsers(dest="cmd")
            p = sub.add_parser("add", parents=[js])
            p.add_argument("title")
            for name in ("finish", "cancel"):
                p = sub.add_parser(name)
                p.add_argument("id")
        ''',
    "docker/crontab": '''
        # AFClaude crontab
        */15 * * * * python3 usage_sampler.py # AFClaude usage sampler
        ''',
    ".claude/settings.json": json.dumps({"permissions": {"allow": ["Bash(python3 tasks.py:*)"]}}),
    "docs/guide.md": "Intro line.\n\nThe watcher compacts the session at a start, after a save prompt and a wait.\n",
    "test_mod.py": '''
        import unittest


        class T(unittest.TestCase):
            def test_old_case(self):
                pass
        ''',
    "keepalive.py": "def tick():\n    return 1\n",
}

HEAD_FILES = {
    "mod.py": '''
        import os
        import json

        LIMIT = 4
        NEW_CONST = "x"


        def keep(a):
            return a


        def body_changes(a):
            return a + 2


        def sig_changes(a, b=None):
            return a


        def new_name(v):
            total = v + 41
            return total


        def added(y):
            """D-900 says so."""
            return y


        class Box:
            size = 1

            def open(self):
                return "opened"

            def close(self):
                return "close"

            def lock(self):
                return "locked"
        ''',
    "store.py": '''
        SCHEMA_VERSION = 5
        SCHEMA = """
        CREATE TABLE IF NOT EXISTS tasks (
            id    INTEGER PRIMARY KEY,
            title TEXT NOT NULL,  -- the title
            owner TEXT
        );
        CREATE TABLE IF NOT EXISTS inbox (
            id   INTEGER PRIMARY KEY,
            body TEXT NOT NULL
        );
        """
        COLUMNS = {"tasks": [("version", "INTEGER"), ("estimate", "REAL")]}


        def _migrate_v5(conn):
            conn.execute("UPDATE tasks SET owner = 'x'")
            conn.execute("INSERT INTO inbox(body) SELECT title FROM tasks")
        ''',
    "actions.py": '''
        SETTINGS = {"window_tz": Setting("UTC"), "new_key": Setting(2)}


        @action("task.add", "task")
        def task_add(conn, ctx):
            return 10


        @action("inbox.add", "inbox")
        def inbox_add(conn, ctx):
            return 3
        ''',
    "mcp_server.py": '''
        @tool("List tasks.")
        async def afclaude_list(limit: int = 20, project: str = "") -> str:
            return ""


        @tool("The owner's inbox.")
        async def afclaude_inbox() -> str:
            return ""
        ''',
    "tasks.py": '''
        import argparse


        def main():
            ap = argparse.ArgumentParser()
            js = argparse.ArgumentParser(add_help=False)
            js.add_argument("--json")
            sub = ap.add_subparsers(dest="cmd")
            p = sub.add_parser("add", parents=[js])
            p.add_argument("title")
            p.add_argument("--priority")
            for name in ("finish", "cancel"):
                p = sub.add_parser(name)
                p.add_argument("id")
            p = sub.add_parser("inbox")
            ib = p.add_subparsers(dest="inbox_cmd")
            q = ib.add_parser("list")
            q.add_argument("--all")
        ''',
    "docker/crontab": '''
        # AFClaude crontab
        */15 * * * * python3 usage_sampler.py # AFClaude usage sampler
        */5 * * * * python3 inbox.py # AFClaude inbox
        ''',
    ".claude/settings.json": json.dumps({"permissions": {"allow": ["Bash(python3 tasks.py:*)",
                                                                    "Bash(python3 change_manifest.py:*)"]}}),
    "docs/guide.md": "Intro line.\n\nThe watcher compacts the session at a run end, after a save prompt and a wait.\n",
    "test_mod.py": '''
        import unittest


        class T(unittest.TestCase):
            def test_new_case(self):
                pass
        ''',
    "keepalive.py": "def tick():\n    return 2\n",
}

DECISIONS = textwrap.dedent('''
    # decisions

    ### D-900 Inbox for owner messages
    - **Type:** owner
    - **Keywords:** inbox, owner messages, triage
    - **Status:** active

    ### D-901 A topic decision
    - **Keywords:** tasks, inbox
    - **Status:** active

    ### D-902 Another topic decision
    - **Keywords:** tasks
    - **Status:** active

    ### D-903 Third
    - **Keywords:** tasks
    - **Status:** active

    ### D-904 Fourth
    - **Keywords:** tasks
    - **Status:** active

    ### D-905 Old box rule
    - **Keywords:** box, opened
    - **Status:** superseded by D-906

    ### D-906 Box rule
    - **Keywords:** locked, opened
    - **Status:** active

    ### D-907 No keywords yet
    - **Status:** active
    ''')


def write_tree(root, files):
    for path, text in files.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(textwrap.dedent(text).lstrip("\n"))


def run_git(root, *args):
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, text=True)


class Repo(unittest.TestCase):
    """One temp repo: a base commit and a head commit."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="manifest-test-")
        cls.root = os.path.join(cls.tmp, "repo")
        os.makedirs(cls.root)
        run_git(cls.root, "init", "-q")
        run_git(cls.root, "config", "user.email", "t@example.invalid")
        run_git(cls.root, "config", "user.name", "t")
        run_git(cls.root, "config", "core.hooksPath", "/dev/null")
        write_tree(cls.root, BASE_FILES)
        run_git(cls.root, "add", "-A")
        run_git(cls.root, "commit", "-qm", "base")
        write_tree(cls.root, HEAD_FILES)
        run_git(cls.root, "add", "-A")
        run_git(cls.root, "commit", "-qm", "head")
        cls.base, cls.head = cm.resolve_range("HEAD~1..HEAD", cwd=cls.root)
        cls.decisions_path = os.path.join(cls.tmp, "DECISIONS.md")
        with open(cls.decisions_path, "w") as fh:
            fh.write(DECISIONS)
        cls.decisions = cm.load_decisions(cls.decisions_path)
        cls.man = cm.build(cls.base, cls.head, cwd=cls.root, decisions=cls.decisions)
        cls.s1 = cls.man.sections["1"][1]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def changes(self, path):
        bp = cm.parse_py(cm.show(self.base, path, self.root))
        hp = cm.parse_py(cm.show(self.head, path, self.root))
        return {c.qual: c for c in cm.compare_py(path, bp, hp)}


class Symbols(Repo):
    def test_added_removed_changed(self):
        ch = self.changes("mod.py")
        self.assertEqual(ch["added"].op, "+")
        self.assertEqual(ch["gone"].op, "-")
        self.assertEqual(ch["body_changes"].op, "~")
        self.assertEqual(ch["body_changes"].tags, ["body"])
        self.assertEqual((ch["body_changes"].plus, ch["body_changes"].minus), (1, 1))
        self.assertNotIn("keep", ch)                     # unchanged: not listed

    def test_signature_change(self):
        c = self.changes("mod.py")["sig_changes"]
        self.assertEqual(c.tags, ["sig"])
        self.assertIn("was sig_changes(a)", cm._fmt_sym(c))
        self.assertIn("sig_changes(a, b=None)", cm._fmt_sym(c))

    def test_methods_in_a_class(self):
        ch = self.changes("mod.py")
        self.assertEqual(ch["Box.open"].op, "~")
        self.assertEqual(ch["Box.lock"].op, "+")
        self.assertNotIn("Box.close", ch)
        self.assertNotIn("Box", ch)                      # its own body (size = 1) didn't change

    def test_rename_and_constants(self):
        ch = self.changes("mod.py")
        self.assertEqual(ch["new_name"].op, ">")
        self.assertEqual(ch["new_name"].tags, ["renamed from old_name"])
        self.assertNotIn("old_name", ch)
        self.assertEqual(ch["LIMIT"].op, "~")
        self.assertEqual(ch["NEW_CONST"].op, "+")

    def test_line_numbers_and_imports_in_manifest(self):
        self.assertRegex(self.s1, r"\+ added\(y\)  L\d+ \(3 lines\)")
        self.assertRegex(self.s1, r"- gone\(x\)  base L\d+")
        self.assertIn("imports: +json", self.s1)

    def test_parse_error_is_reported(self):
        pf = cm.parse_py("def broken(:\n")
        self.assertIsNotNone(pf.error)


class Interfaces(Repo):
    def section(self, name):
        lines = self.s1.split("## Interfaces")[1].splitlines()
        i = next(n for n, ln in enumerate(lines) if ln.startswith(name + ":"))
        out = []
        for ln in lines[i + 1:]:
            if not ln.startswith("  "):
                break
            out.append(ln.strip())
        return lines[i], out

    def test_schema(self):
        _, s = self.section("DB schema / migrations")
        self.assertIn("store.py: + table inbox(id, body)", s)
        self.assertIn("store.py: ~ table tasks: +owner TEXT", s)
        self.assertIn("store.py: - index tasks_title", s)
        self.assertIn("store.py: ~ SCHEMA_VERSION 4 -> 5", s)
        self.assertTrue(any(x.startswith("store.py: ~ COLUMNS[tasks]") for x in s))
        self.assertIn("store.py: + migration _migrate_v5: touches inbox, tasks", s)

    def test_settings_keys(self):
        _, s = self.section("settings keys")
        self.assertIn("actions.py SETTINGS: +new_key, -old_key, ~window_tz", s)

    def test_cli(self):
        _, s = self.section("CLI commands/options")
        self.assertIn("tasks.py: ~ add: +--priority", s)
        self.assertIn("tasks.py: + inbox", s)
        self.assertIn("tasks.py: + inbox list [--all]", s)
        self.assertFalse(any("finish" in x or "cancel" in x for x in s))   # loop-made, unchanged

    def test_cli_loop_expansion(self):
        tree = cm.parse_py(cm.show(self.head, "tasks.py", self.root)).tree
        cmds = cm.cli_commands(tree)
        self.assertEqual(cmds["finish"], {"id"})
        self.assertNotIn("--json", cmds[""])              # the parent parser is skipped

    def test_mcp_tools_and_actions(self):
        _, mcp = self.section("MCP tools")
        self.assertTrue(any(x.startswith("mcp_server.py: + afclaude_inbox") for x in mcp))
        self.assertIn("mcp_server.py: ~ afclaude_list [sig]", mcp)
        _, act = self.section("audited actions")
        self.assertTrue(any(x.startswith("actions.py: + inbox.add (inbox_add)") for x in act))
        self.assertIn("actions.py: - task.drop", act)
        self.assertIn("actions.py: ~ task.add (task_add) [body]", act)

    def test_cron_and_claude(self):
        _, cron = self.section("cron / compose / docker")
        self.assertIn("docker/crontab: + */5 * * * * python3 inbox.py # AFClaude inbox", cron)
        _, cl = self.section(".claude settings, hooks, agents")
        self.assertIn('.claude/settings.json: ~permissions.allow: +"Bash(python3 change_manifest.py:*)"', cl)

    def test_empty_category_says_none(self):
        man = cm.build(self.base, self.base, cwd=self.root)
        self.assertIn("MCP tools: none", man.sections["1"][1])


class TestsLeftOut(Repo):
    def test_only_a_count_line(self):
        out = cm.render(self.man)
        self.assertIn("tests: 1 files changed (omitted)", out)
        self.assertNotIn("test_mod.py", out)
        self.assertNotIn("test_new_case", out)
        self.assertNotIn("test_old_case", out)


class Diffs(Repo):
    def test_docs_are_a_word_diff(self):
        s2 = self.man.sections["2"][1]
        self.assertIn("=== docs/guide.md", s2)
        self.assertIn("[-start,-]{+run end,+}", s2)
        self.assertNotIn("Intro line.", s2)                # no context lines
        self.assertIn("=== docker/crontab", s2)            # infra: a line diff
        self.assertIn("+*/5 * * * * python3 inbox.py", s2)

    def test_trim_words(self):
        line = " ".join(f"w{i}" for i in range(40)) + " [-old-]{+new+} " + " ".join(f"v{i}" for i in range(40))
        t = cm.trim_words(line, keep=3)
        self.assertEqual(t, "… w37 w38 w39 [-old-]{+new+} v0 v1 v2 …")

    def test_live_hunks_off_by_default(self):
        self.assertIn("off (--live-hunks)", self.man.sections["3a"][1])
        self.assertIn("keepalive.py", self.man.sections["3a"][1])
        on = cm.build(self.base, self.head, cwd=self.root, decisions=self.decisions, live_hunks=True)
        self.assertIn("=== keepalive.py", on.sections["3a"][1])
        self.assertIn("+    return 2", on.sections["3a"][1])


class Decisions(Repo):
    def test_parse(self):
        ids = [d.id for d in self.decisions]
        self.assertEqual(ids, [f"D-{n}" for n in range(900, 908)])
        d900 = self.decisions[0]
        self.assertEqual(d900.keywords, ("inbox", "owner messages", "triage"))
        self.assertFalse(cm.active(self.decisions[5]))
        self.assertEqual(self.decisions[7].keywords, ())

    def test_missing_file_raises(self):
        with self.assertRaises(cm.ManifestError):
            cm.load_decisions(os.path.join(self.tmp, "nope.md"))

    def test_keyword_forms(self):
        rx = cm.keyword_re("keep-alive")
        assert rx is not None
        for s in ("keep-alive", "keep_alive", "keepalive", "Keep alive"):
            self.assertTrue(rx.search(s), s)
        self.assertFalse(rx.search("keepalived"))
        tasks = cm.keyword_re("tasks")
        assert tasks is not None
        self.assertTrue(tasks.search("task_events"))
        self.assertIsNone(cm.keyword_re("db"))

    def test_matching(self):
        changes = [c for p in ("mod.py", "actions.py", "store.py", "tasks.py", "mcp_server.py")
                   for c in self.changes(p).values()]
        mr = cm.match_decisions(changes, self.decisions)
        hits = {m.change.key: m.hits for m in mr.matches}
        # "tasks" is on 4 decisions: a topic word, skipped
        self.assertEqual(mr.topic_keywords, 1)
        self.assertFalse(any("tasks" in kws for h in hits.values() for kws in h.values()))
        # a citation always matches
        self.assertEqual(hits["mod.py:added"], {"D-900": ["cites"]})
        # two specific keywords of D-906 (locked, opened) in Box.lock? no: one each -> no match
        self.assertNotIn("mod.py:Box.lock", hits)
        self.assertNotIn("mod.py:Box.open", hits)
        # superseded D-905 (box, opened) never matches
        self.assertFalse(any("D-905" in h for h in hits.values()))

    def test_two_keywords_match(self):
        sym = cm.Sym("triage_inbox", "def", 1, 2, "triage_inbox()", "", "", None, ["def triage_inbox():"])
        c = cm.SymChange("x.py", "def", "+", "triage_inbox", sym, None, changed_lines=sym.src)
        mr = cm.match_decisions([c], self.decisions)
        self.assertEqual(mr.matches[0].hits, {"D-900": ["inbox", "triage"]})

    def test_broad_keyword_is_skipped(self):
        syms = []
        for i in range(10):
            s = cm.Sym(f"f{i}", "def", 1, 2, f"f{i}()", "", "", None, [f"def f{i}(): triage inbox"])
            syms.append(cm.SymChange("x.py", "def", "+", f"f{i}", s, None, changed_lines=s.src))
        mr = cm.match_decisions(syms, self.decisions)
        self.assertEqual(set(mr.broad), {"inbox", "triage"})
        self.assertEqual(mr.matches, [])

    def test_section_3b_lists_matches_and_hunks(self):
        s3 = self.man.sections["3b"][1]
        self.assertIn("+ mod.py:added: D-900(cites)", s3)
        self.assertIn('+    """D-900 says so."""', s3)


class Mapping(Repo):
    def write(self, text):
        path = os.path.join(self.tmp, "mapping.txt")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_flags(self):
        mapping = cm.load_mapping(self.write(textwrap.dedent("""
            # worker mapping
            - mod.py:added: none
            body_changes: D-900
            mod.py:Box.open: D-905
            mod.py:nothing_here: none
            sig_changes: D-999
            """)))
        man = cm.build(self.base, self.head, cwd=self.root, decisions=self.decisions, mapping=mapping)
        s5 = man.sections["5"][1]
        self.assertIn("missing from the mapping", s5)
        self.assertIn("  + mod.py:Box.lock", s5)
        self.assertNotIn("  ~ mod.py:body_changes", s5)          # bare name resolved
        self.assertIn("mod.py:nothing_here", s5.split("name no changed")[1])
        self.assertIn("mod.py:Box.open: D-905 (superseded by D-906)", s5)
        self.assertIn("sig_changes: D-999 (unknown)", s5)
        self.assertIn("mod.py:added: mapping none; cites D-900", s5)
        self.assertNotIn("LIMIT", s5)                             # constants need no mapping

    def test_bad_line_raises(self):
        with self.assertRaises(cm.ManifestError):
            cm.load_mapping(self.write("mod.py:added maybe D-1\n"))


class Audit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="manifest-db-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db = os.path.join(self.tmp, "afclaude.db")
        conn = store.connect(self.db, create=True)
        with store.transaction(conn):
            store.add_audit(conn, "cli", "cli", "task.add", "task", 1, ts="2026-10-14T09:00:00.000Z")
            store.add_audit(conn, "cli", "cli", "task.add", "task", 2, ts="2026-10-14T10:05:00.000Z")
            store.add_audit(conn, "mcp:abc", "mcp", "task.add", "task", 3, ts="2026-10-14T10:06:00.000Z")
            store.add_audit(conn, "owner", "dashboard", "setting.set", "setting", "window_tz",
                            ts="2026-10-14T11:00:00.000Z")
            store.add_audit(conn, "cli", "cli", "task.finish", "task", 2, ts="2026-10-14T13:00:00.000Z")
        conn.close()

    def test_grouped_by_action_in_range(self):
        out = cm.audit_section(self.db, "2026-10-14T12:00:00+02:00", "2026-10-14T12:30:00Z")
        lines = out.splitlines()
        self.assertIn("3 rows", lines[0])
        self.assertTrue(lines[1].startswith("  task.add x2  by cli, mcp:abc"))
        self.assertIn("[task:2, task:3]", lines[1])
        self.assertTrue(lines[2].startswith("  setting.set x1  by owner"))
        self.assertNotIn("task.finish", out)

    def test_missing_db_raises(self):
        with self.assertRaises(cm.ManifestError):
            cm.audit_section(os.path.join(self.tmp, "none.db"), "2026-10-14", None)

    def test_bad_time_raises(self):
        with self.assertRaises(cm.ManifestError):
            cm.audit_section(self.db, "yesterday", None)


class Snapshot(unittest.TestCase):
    def snap(self, **kw):
        s = {"taken_at": "2026-10-14T10:00:00.000Z",
             "containers": {"afclaude": {"id": "aaa", "image": "i1", "status": "running",
                                         "started_at": "t1", "restarts": 0}},
             "images": {"afclaude-manager:latest": "i1"}, "host_crontab": "", "at_jobs": [],
             "scheduled_jobs": {"j1": "pending 2026-10-20 x"}}
        s.update(kw)
        return s

    def test_no_change(self):
        out = cm.compare_snapshots(self.snap(), self.snap(taken_at="2026-10-14T11:00:00.000Z"))
        self.assertEqual(out.count("no change"), 5)

    def test_changes(self):
        new = self.snap(containers={"afclaude": {"id": "bbb", "image": "i2", "status": "running",
                                                 "started_at": "t2", "restarts": 0}},
                        images={"afclaude-manager:latest": "i2"}, host_crontab="0 * * * * x # AFClaude",
                        at_jobs=["7 Wed Oct 15 a opc"], scheduled_jobs={"j1": "started 2026-10-20 x"})
        out = cm.compare_snapshots(self.snap(), new)
        self.assertIn("~ afclaude: id aaa -> bbb; image i1 -> i2; started_at t1 -> t2", out)
        self.assertIn("~ afclaude-manager:latest: i1 -> i2", out)
        self.assertIn("+ 0 * * * * x # AFClaude", out)
        self.assertIn("+ 7 Wed Oct 15 a opc", out)
        self.assertIn("~ j1: pending 2026-10-20 x -> started 2026-10-20 x", out)

    def test_take_snapshot_read_only_and_failing_source(self):
        tmp = tempfile.mkdtemp(prefix="manifest-snap-")
        self.addCleanup(shutil.rmtree, tmp)
        db = os.path.join(tmp, "afclaude.db")
        store.connect(db, create=True).close()
        calls = []

        def fake(argv, ok_codes=(0,)):
            calls.append(argv)
            if argv[0] == "crontab":
                return subprocess.CompletedProcess(argv, 1, "", "no crontab for user")
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(cm, "run_cmd", fake):
            snap = cm.take_snapshot(db)
        self.assertEqual(snap["host_crontab"], "")
        self.assertEqual(snap["containers"], {})
        self.assertFalse(any(a[:2] == ["docker", "restart"] for a in calls))
        with mock.patch.object(cm, "run_cmd", side_effect=cm.ManifestError("docker: not found")):
            with self.assertRaises(cm.ManifestError):
                cm.take_snapshot(db)


class Cli(Repo):
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cm.main(list(args))
        finally:
            os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()

    def test_budget(self):
        code, out, _ = self.run_main("HEAD~1..HEAD", "--budget", "--decisions", self.decisions_path)
        self.assertEqual(code, 0)
        self.assertRegex(out, r"  1 manifest: ≈\d+ tok")
        self.assertRegex(out, r"total ≈\d+ tok")

    def test_sections(self):
        code, out, _ = self.run_main("HEAD~1..HEAD", "--sections", "1,5", "--decisions", self.decisions_path)
        self.assertEqual(code, 0)
        self.assertIn("# 1 manifest", out)
        self.assertIn("# 5 decision mapping", out)
        self.assertNotIn("# 2 ", out)

    def test_errors_exit_2(self):
        code, _, err = self.run_main("nope..HEAD", "--decisions", self.decisions_path)
        self.assertEqual(code, 2)
        self.assertIn("ERROR", err)
        code, _, err = self.run_main("HEAD~1..HEAD", "--decisions", os.path.join(self.tmp, "x.md"))
        self.assertEqual(code, 2)
        code, _, err = self.run_main("HEAD~1..HEAD", "--decisions", self.decisions_path, "--since",
                                     "2026-10-14", "--db", os.path.join(self.tmp, "none.db"))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
