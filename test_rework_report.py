#!/usr/bin/env python3
"""Tests for tools/rework_report.py (D-307 #7) on a temp git repo: Worker-Model/Reworks trailer
parsing, per-model merged/reworked/rate counts, unresolved Reworks, revision-range filtering, and
the CLI's printed report."""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
import rework_report as rr  # noqa: E402


def run_git(root, *args):
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, text=True)


def commit(root, subject, trailers=(), empty=True):
    msg = subject + ("\n\n" + "\n".join(trailers) if trailers else "")
    args = ["commit", "-q", "-m", msg]
    if empty:
        args.insert(1, "--allow-empty")
    run_git(root, *args)


class Repo(unittest.TestCase):
    """A temp repo with: a plain base commit, a 'step1' merge by sonnet, a 'step2' merge by
    opus, a 'fix1' merge by opus that reworks step1, and a plain commit with a Reworks trailer
    naming a branch nobody claimed (unresolved)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rework-test-")
        cls.root = os.path.join(cls.tmp, "repo")
        os.makedirs(cls.root)
        run_git(cls.root, "init", "-q")
        run_git(cls.root, "config", "user.email", "t@example.invalid")
        run_git(cls.root, "config", "user.name", "t")
        run_git(cls.root, "config", "core.hooksPath", "/dev/null")
        with open(os.path.join(cls.root, "f"), "w") as fh:
            fh.write("base\n")
        run_git(cls.root, "add", "-A")
        commit(cls.root, "base", empty=False)
        cls.base = subprocess.run(["git", "-C", cls.root, "rev-parse", "HEAD"],
                                   capture_output=True, text=True, check=True).stdout.strip()
        commit(cls.root, "Merge branch 'step1'", ["Worker-Model: sonnet"])
        commit(cls.root, "Merge branch 'step2'", ["Worker-Model: opus"])
        commit(cls.root, "Merge branch 'fix1'", ["Worker-Model: opus", "Reworks: step1"])
        commit(cls.root, "hotfix on main", ["Reworks: never-merged"])
        cls.head = subprocess.run(["git", "-C", cls.root, "rev-parse", "HEAD"],
                                   capture_output=True, text=True, check=True).stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)


class LogAndBuild(Repo):
    def test_log_parses_trailers(self):
        commits = rr.log(self.root, [])
        by_subject = {c.subject: c for c in commits}
        self.assertEqual(by_subject["Merge branch 'step1'"].models, ["sonnet"])
        self.assertEqual(by_subject["Merge branch 'fix1'"].models, ["opus"])
        self.assertEqual(by_subject["Merge branch 'fix1'"].reworks, ["step1"])
        self.assertEqual(by_subject["base"].models, [])

    def test_branch_of(self):
        self.assertEqual(rr.branch_of("Merge branch 'step1'"), "step1")
        self.assertIsNone(rr.branch_of("hotfix on main"))

    def test_merged_and_reworked_counts(self):
        merged, reworked, unresolved = rr.build(rr.log(self.root, []))
        self.assertEqual(merged["sonnet"], 1)
        self.assertEqual(merged["opus"], 2)          # step2 and fix1 both ran on opus
        self.assertEqual(reworked["sonnet"], 1)       # fix1 reworks step1 (sonnet)
        self.assertEqual(reworked.get("opus", 0), 0)  # step2 was never reworked
        self.assertEqual(unresolved, ["never-merged"])

    def test_report_text_and_rates(self):
        merged, reworked, unresolved = rr.build(rr.log(self.root, []))
        text = rr.report(merged, reworked, unresolved)
        self.assertIn("sonnet   merged 1    reworked 1    rework rate 100%", text)
        self.assertIn("opus     merged 2    reworked 0    rework rate 0%", text)
        self.assertIn("unresolved Reworks", text)
        self.assertIn("never-merged", text)

    def test_revision_range_excludes_earlier_commits(self):
        merged, reworked, unresolved = rr.build(rr.log(self.root, [self.base + ".." + self.head]))
        self.assertEqual(merged["sonnet"], 1)
        self.assertEqual(merged["opus"], 2)
        # the base commit itself (no trailers) is excluded either way; nothing else should change
        self.assertEqual(sum(merged.values()), 3)

    def test_no_trailers_at_all(self):
        merged, reworked, unresolved = rr.build(rr.log(self.root, [self.base]))
        self.assertEqual(rr.report(merged, reworked, unresolved), "no Worker-Model trailers found")


class Cli(Repo):
    def test_main_prints_report(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = rr.main(["--repo", self.root])
        self.assertEqual(rc, 0)
        self.assertIn("rework rate", buf.getvalue())

    def test_main_reports_git_failure(self):
        rc = rr.main(["--repo", self.tmp])  # not a git repo
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
