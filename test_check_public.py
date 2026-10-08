#!/usr/bin/env python3
"""Tests for tools/check_public.py and the .githooks it backs.

Each hook test runs in a throwaway git repo with HOME pointed at a temp dir, so the
real ~/.claude.json, global git identity and local denylist are never read. Fake
secrets are assembled at runtime so this file itself passes the guard.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
import check_public as cp  # noqa: E402

FAKE_KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n"
FAKE_GH = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_UUID = "1234abcd-0000-4000-8000-" + "feedfacecafe"
PUBLIC_IP = ".".join(["8", "8", "4", "4"])


class Repo:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="afc-public-")
        self.home = os.path.join(self.dir, "home")
        self.work = os.path.join(self.dir, "repo")
        os.makedirs(os.path.join(self.home, ".config", "afclaude"))
        shutil.copytree(os.path.join(HERE, ".githooks"), os.path.join(self.work, ".githooks"))
        os.makedirs(os.path.join(self.work, "tools"))
        shutil.copy(os.path.join(HERE, "tools", "check_public.py"), os.path.join(self.work, "tools"))
        with open(os.path.join(self.home, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"accountUuid": FAKE_UUID, "emailAddress": "owner@example.org",
                                        "displayName": "Zyxwv"}}, f)
        with open(os.path.join(self.home, ".config", "afclaude", "public_denylist.txt"), "w") as f:
            f.write("# local only\nsecret-host.example\n")
        self.env = dict(os.environ, HOME=self.home, GIT_CONFIG_NOSYSTEM="1")
        self.env.pop("AFCLAUDE_PUBLIC_DENYLIST", None)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.org")
        self.git("config", "core.hooksPath", ".githooks")
        self.write("README.md", "hello\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")

    def git(self, *args, check=True):
        return subprocess.run(["git"] + list(args), cwd=self.work, env=self.env,
                              capture_output=True, text=True, check=check)

    def write(self, name, text):
        p = os.path.join(self.work, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def commit(self, name, text, force=False):
        self.write(name, text)
        self.git("add", "-f" if force else "--", name)
        return self.git("commit", "-qm", "change " + name, check=False)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class HookTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()

    def tearDown(self):
        self.r.close()

    def assertRejected(self, res, what):
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn(what, res.stderr)

    def test_normal_change_accepted(self):
        res = self.r.commit("notes.md", "A normal change on 10.0.0.1 and 127.0.0.1, uuid "
                            "00000000-0000-4000-8000-000000000001.\n")
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_type_gate_refuses_a_commit_without_mypy(self):
        """With mypy.ini (the type-checking gate, D-209) the hook needs the .venv's mypy:
        this temp repo has no .venv, so no commit (the gate can't be skipped silently)."""
        self.r.write("mypy.ini", "[mypy]\nstrict = True\nfiles = a.py\n")
        self.r.git("add", "mypy.ini")
        self.assertRejected(self.r.commit("a.py", "x = 1\n"), "no mypy in .venv")

    def test_fake_private_key_rejected(self):
        self.assertRejected(self.r.commit("deploy.txt", FAKE_KEY), "private key")

    def test_backlog_rejected(self):
        self.assertRejected(self.r.commit("BACKLOG.md", "my projects\n", force=True), "forbidden path")

    def test_secret_dirs_rejected(self):
        self.assertRejected(self.r.commit("docker/secrets/k", "x\n", force=True), "forbidden path")
        self.r.git("reset", "-q")
        self.assertRejected(self.r.commit("data/afclaude.db", "x\n", force=True), "forbidden path")

    def test_token_rejected(self):
        self.assertRejected(self.r.commit("cfg.py", "TOKEN = '%s'\n" % FAKE_GH), "github token")

    def test_public_ip_rejected(self):
        self.assertRejected(self.r.commit("hosts.md", "server at %s\n" % PUBLIC_IP), "public IPv4")

    def test_account_uuid_and_email_rejected(self):
        self.assertRejected(self.r.commit("a.md", "org %s\n" % FAKE_UUID), "accountUuid")
        self.r.git("reset", "-q")
        self.assertRejected(self.r.commit("b.md", "mail owner@example.org\n"), "emailAddress")

    def test_denylist_rejected(self):
        self.assertRejected(self.r.commit("c.md", "see https://Secret-Host.example/x\n"), "denylist")

    def test_allow_marker_only_for_generic_checks(self):
        res = self.r.commit("d.md", "server %s  # public-check: allow\n" % PUBLIC_IP)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertRejected(self.r.commit("e.md", "secret-host.example  # public-check: allow\n"),
                            "denylist")

    def test_tree_mode(self):
        self.r.write("f.md", "k: %s\n" % FAKE_GH)
        self.r.git("add", "f.md")
        self.r.git("commit", "-qm", "bypass", "--no-verify")
        res = subprocess.run([sys.executable, "tools/check_public.py", "--tree", "HEAD"],
                             cwd=self.r.work, env=self.r.env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 1)
        self.assertIn("f.md:1: github token", res.stderr)


class UnitTests(unittest.TestCase):
    def test_owner_email_hash(self):
        fake = "someone.private@example.net"
        c = cp.Checker()
        c.ident, c.deny = [], []
        cp.BLOCKED_HASHES.add(hashlib.sha256(fake.encode()).hexdigest())
        try:
            c.check_line("x:1", "contact %s please" % fake.upper())
        finally:
            cp.BLOCKED_HASHES.discard(hashlib.sha256(fake.encode()).hexdigest())
        self.assertTrue(any("e-mail" in f for f in c.findings), c.findings)

    def test_no_false_positives(self):
        c = cp.Checker()
        c.ident, c.deny = [], []
        for line in [
            'ARG X_SHA256=02aa0cb229ba09050cba6638059dadb9eedc2276632ea43d6a57a2f8c1629dd5',
            '<script integrity="sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi">',
            "path /home/user/.claude/projects/-mnt-BlockVolume-Claude/memory/task_plan.md",
            "FROM=172.16.0.0/12 and version 2.1.284",
            "session a954f3c8-e15e-41dd-9b25-b2f8b1a88a06",
        ]:
            c.check_line("x:1", line)
        self.assertEqual(c.findings, [])


if __name__ == "__main__":
    unittest.main()
