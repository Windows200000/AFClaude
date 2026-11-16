#!/usr/bin/env python3
"""K5 part 2b Brief A (D-344, D-181): `doc export --emit` (the container side: nothing written, the texts as JSON)
and tools/write_docs.py (the host side: whitelisted names, no symlinks, atomic, all-or-nothing, --check).

    python3 -m unittest test_write_docs
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
import testenv  # noqa: E402,F401
import project_docs as pd  # noqa: E402
import tasks  # noqa: E402
import write_docs as wd  # noqa: E402
from test_project_docs import Env  # noqa: E402

NAMES = ("PROGRESS.md", "GOALS.md", "EXCEPTIONS.md")


def emission(path, **files):
    """The JSON `doc export --emit` prints for the project whose directory is `path`, for the given {name: text}."""
    return json.dumps({"project": "P", "project_path": path, "emit": True, "ok": True,
                       "files": {n: {"result": "emitted", "text": t} for n, t in files.items()}})


class Emit(Env):
    def setUp(self):
        super().setUp()
        for f, t in (("PROGRESS.md", "# P progress\n\n- a\n"), ("GOALS.md", "# goals\n\ngo\n"),
                     ("EXCEPTIONS.md", "# exceptions\n")):
            self.write(f, t)
        self.run_import_all()

    def run_import_all(self):
        pd.import_all(self.db, self.repo, self.reviews)

    def test_emit_writes_nothing_and_carries_the_texts(self):
        self.act("doc.append", project="P", name="progress", text="- via the container\n")
        before = {f: self.read(f) for f in NAMES}
        x = pd.export(self.db, self.repo, emit=True)
        self.assertTrue(x["ok"] and x["emit"], x)
        self.assertEqual(x["files"]["PROGRESS.md"]["result"], "emitted")
        self.assertEqual(x["files"]["PROGRESS.md"]["text"], "# P progress\n\n- a\n- via the container\n")
        self.assertEqual({x["files"][f]["result"] for f in ("GOALS.md", "EXCEPTIONS.md")}, {"unchanged"})
        self.assertEqual({f: self.read(f) for f in NAMES}, before)       # the container wrote no file
        self.assertEqual(set(x["files"]), set(NAMES))                    # never ALERTS.md (D-181)
        self.assertEqual(os.path.realpath(x["project_path"]), os.path.realpath(self.repo))   # for write_docs

    def test_the_mark_is_not_moved_so_a_stale_file_never_overwrites_the_db(self):
        self.act("doc.append", project="P", name="progress", text="- new\n")
        pd.export(self.db, self.repo, emit=True)
        self.run_import_all()                       # the file was never written: the DB is ahead, nothing is lost
        self.assertTrue(self.render("progress").endswith("- new\n"))
        # then the host writes what was emitted, and the next import sees it as in sync
        x = pd.export(self.db, self.repo, emit=True)
        wd.run(json.dumps(x), self.repo)
        r = pd.import_all(self.db, self.repo, self.reviews)
        self.assertEqual(r["docs"]["progress"]["outcome"], "in_sync")
        self.assertTrue(self.render("progress").endswith("- new\n"))

    def test_the_public_check_still_refuses(self):
        self.act("doc.append", project="P", name="progress", text="- see secret-host.example\n")
        x = pd.export(self.db, self.repo, emit=True)
        self.assertFalse(x["ok"])
        self.assertTrue(x["files"]["PROGRESS.md"]["result"].startswith("refused"))
        self.assertNotIn("text", x["files"]["PROGRESS.md"])
        with self.assertRaises(wd.Refused):
            wd.run(json.dumps(x), self.repo)

    def test_the_cli_prints_json_and_refuses_emit_with_check(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = tasks.main(["--db", self.db, "doc", "export", "--emit", "--repo", self.repo, "--project", "P"])
        self.assertEqual(rc, 0)
        self.assertEqual(set(json.loads(out.getvalue())["files"]), set(NAMES))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = tasks.main(["--db", self.db, "doc", "export", "--emit", "--check", "--repo", self.repo])
        self.assertEqual(rc, 1)
        self.assertIn("exclusive", err.getvalue())


class Helper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name

    def path(self, name):
        return os.path.join(self.repo, name)

    def put(self, name, text="old\n"):
        with open(self.path(name), "w", encoding="utf-8") as f:
            f.write(text)

    def get(self, name):
        with open(self.path(name), encoding="utf-8") as f:
            return f.read()

    def test_writes_the_three_files_atomically_and_keeps_the_mode(self):
        self.put("GOALS.md")
        os.chmod(self.path("GOALS.md"), 0o640)
        res = wd.run(emission(self.repo, **{"PROGRESS.md": "p\n", "GOALS.md": "g\n", "EXCEPTIONS.md": "e\n"}), self.repo)
        self.assertEqual(res, dict.fromkeys(NAMES, "written"))
        self.assertEqual([self.get(n) for n in NAMES], ["p\n", "g\n", "e\n"])
        self.assertEqual(os.stat(self.path("GOALS.md")).st_mode & 0o777, 0o640)
        self.assertEqual(sorted(os.listdir(self.repo)), sorted(NAMES))       # no temp file left behind
        self.assertEqual(wd.run(emission(self.repo, **{"GOALS.md": "g\n"}), self.repo), {"GOALS.md": "unchanged"})

    def test_text_is_written_byte_for_byte(self):
        text = "a\r\nbä\n\n"
        wd.run(emission(self.repo, **{"PROGRESS.md": text}), self.repo)
        with open(self.path("PROGRESS.md"), "rb") as f:
            self.assertEqual(f.read(), text.encode("utf-8"))

    def test_check_writes_nothing_and_says_what_would_change(self):
        self.put("PROGRESS.md", "same\n")
        res = wd.run(emission(self.repo, **{"PROGRESS.md": "same\n", "GOALS.md": "new\n"}), self.repo, check=True)
        self.assertEqual(res, {"PROGRESS.md": "unchanged", "GOALS.md": "would write"})
        self.assertFalse(os.path.exists(self.path("GOALS.md")))

    def test_a_symlink_is_refused_and_nothing_else_is_written(self):
        outside = os.path.join(self.repo, "outside.txt")
        with open(outside, "w") as f:
            f.write("keep\n")
        os.symlink(outside, self.path("GOALS.md"))
        with self.assertRaisesRegex(wd.Refused, "symlink"):
            wd.run(emission(self.repo, **{"PROGRESS.md": "p\n", "GOALS.md": "g\n"}), self.repo)
        self.assertFalse(os.path.exists(self.path("PROGRESS.md")))          # all or nothing
        with open(outside) as f:
            self.assertEqual(f.read(), "keep\n")

    def test_a_dangling_symlink_is_refused_too(self):
        os.symlink(os.path.join(self.repo, "nowhere"), self.path("PROGRESS.md"))
        with self.assertRaisesRegex(wd.Refused, "symlink"):
            wd.run(emission(self.repo, **{"PROGRESS.md": "p\n"}), self.repo)

    def test_a_name_outside_the_whitelist_is_refused(self):
        for bad in ("ALERTS.md", "../PROGRESS.md", "sub/GOALS.md", "progress.md", "README.md", ".git/config", "/etc/passwd"):
            with self.assertRaisesRegex(wd.Refused, "only PROGRESS.md"):
                wd.run(emission(self.repo, **{"PROGRESS.md": "p\n", bad: "x"}), self.repo)
        self.assertEqual(os.listdir(self.repo), [])

    def test_a_directory_in_the_place_of_a_file_or_a_missing_repo_is_refused(self):
        os.mkdir(self.path("GOALS.md"))
        with self.assertRaisesRegex(wd.Refused, "regular file"):
            wd.run(emission(self.repo, **{"GOALS.md": "g\n"}), self.repo)
        with self.assertRaisesRegex(wd.Refused, "not a directory"):
            none = os.path.join(self.repo, "none")
            wd.run(emission(none, **{"PROGRESS.md": "p\n"}), none)

    def test_bad_input_is_refused(self):
        good = {"result": "emitted", "text": "t"}
        for raw in ("not json", "[]", "{}", json.dumps({"files": {}, "emit": True}),
                    json.dumps({"files": {"GOALS.md": good}}),                                  # not an emission
                    json.dumps({"emit": True, "files": {"GOALS.md": {"result": "conflict", "error": "x"}}}),
                    json.dumps({"emit": True, "files": {"GOALS.md": {"result": "emitted"}}}),   # no text
                    json.dumps({"emit": True, "files": {"GOALS.md": {"result": "emitted", "text": 5}}}),
                    json.dumps({"emit": True, "ok": False, "files": {"GOALS.md": good}})):
            with self.assertRaises(wd.Refused, msg=raw):
                wd.run(raw, self.repo)
        self.assertEqual(os.listdir(self.repo), [])

    def test_another_projects_log_is_refused(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaisesRegex(wd.Refused, "not of the target"):
                wd.run(emission(other, **{"PROGRESS.md": "p\n"}), self.repo)
            with self.assertRaisesRegex(wd.Refused, "not of the target"):       # --check too
                wd.run(emission(other, **{"PROGRESS.md": "p\n"}), self.repo, check=True)
        self.assertEqual(os.listdir(self.repo), [])

    def test_a_link_to_the_projects_directory_is_the_same_directory(self):
        link = self.repo + "-link"
        os.symlink(self.repo, link)
        self.addCleanup(os.unlink, link)
        self.assertEqual(wd.run(emission(link, **{"GOALS.md": "g\n"}), self.repo), {"GOALS.md": "written"})

    def test_an_emission_without_the_project_directory_is_refused(self):
        x = json.loads(emission(self.repo, **{"GOALS.md": "g\n"}))
        del x["project_path"]
        with self.assertRaisesRegex(wd.Refused, "project_path"):
            wd.run(json.dumps(x), self.repo)
        x["project_path"] = None
        with self.assertRaisesRegex(wd.Refused, "project_path"):
            wd.run(json.dumps(x), self.repo)

    def test_a_failure_while_writing_the_second_file_changes_no_file(self):
        self.put("PROGRESS.md")
        self.put("GOALS.md")
        real = wd.stage
        calls = []

        def flaky(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(path, text)
        with unittest.mock.patch.object(wd, "stage", side_effect=flaky), self.assertRaises(OSError):
            wd.run(emission(self.repo, **{"PROGRESS.md": "p\n", "GOALS.md": "g\n"}), self.repo)
        self.assertEqual(sorted(os.listdir(self.repo)), ["GOALS.md", "PROGRESS.md"])      # no temp file
        self.assertEqual((self.get("PROGRESS.md"), self.get("GOALS.md")), ("old\n", "old\n"))

    def test_nothing_is_renamed_before_every_temp_file_is_written(self):
        self.put("PROGRESS.md")
        order = []
        real_stage, real_replace = wd.stage, wd.os.replace
        with unittest.mock.patch.object(wd, "stage", side_effect=lambda *a: order.append("stage") or real_stage(*a)), \
                unittest.mock.patch.object(wd.os, "replace", side_effect=lambda *a: order.append("rename") or real_replace(*a)):
            wd.run(emission(self.repo, **{"PROGRESS.md": "p\n", "GOALS.md": "g\n", "EXCEPTIONS.md": "e\n"}), self.repo)
        self.assertEqual(order, ["stage"] * 3 + ["rename"] * 3)

    def test_a_failed_write_leaves_no_temp_file(self):
        self.put("PROGRESS.md")
        with unittest.mock.patch.object(wd.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                wd.run(emission(self.repo, **{"PROGRESS.md": "p\n"}), self.repo)
        self.assertEqual(os.listdir(self.repo), ["PROGRESS.md"])
        self.assertEqual(self.get("PROGRESS.md"), "old\n")


class Script(unittest.TestCase):
    """The helper as the host runs it: a plain python3 start on stdin, exit codes."""

    def run_helper(self, stdin, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "tools", "write_docs.py"), *args], input=stdin,
                              capture_output=True, text=True, timeout=60)

    def test_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_helper(emission(d, **{"GOALS.md": "g\n"}), "--repo", d, "--check")
            self.assertEqual((r.returncode, r.stdout.split()), (1, ["GOALS.md", "would", "write"]))
            r = self.run_helper(emission(d, **{"GOALS.md": "g\n"}), "--repo", d)
            self.assertEqual((r.returncode, r.stdout.split()), (0, ["GOALS.md", "written"]))
            r = self.run_helper(emission(d, **{"GOALS.md": "g\n"}), "--repo", d, "--check")
            self.assertEqual(r.returncode, 0)
            r = self.run_helper("nope", "--repo", d)
            self.assertEqual(r.returncode, 1)
            self.assertIn("refused", r.stderr)
            r = self.run_helper("", "--repo", d, "--input", os.path.join(d, "absent.json"))
            self.assertEqual(r.returncode, 2)

    def test_it_is_stdlib_only_and_python_39_syntax(self):
        import ast
        with open(os.path.join(HERE, "tools", "write_docs.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read(), feature_version=(3, 9))
        stdlib = set(sys.stdlib_module_names)
        mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(mods, stdlib | {"__future__"})


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
