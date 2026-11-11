#!/usr/bin/env python3
"""Offline tests for the host kit (phase 5b K2): docker/host-kit/host_exec.py's host.json, `kit-version`,
`host-config`, the allowed roots and the output cap, the reads (transcripts, sessions, doc-files) and check
commands, host.py's kit check and gate, the legacy bridge (docker/host_exec.py: pinned, still served and
accepted by the image until the owner's install), and docker/install_host_bridge.sh against a temporary HOME (the real authorized_keys and kit are never
touched). Nothing here signals a process or runs claude/tmux."""
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (hermetic env; before the AFClaude imports)
import host  # noqa: E402

HOST_EXEC = os.path.join(HERE, "docker", "host-kit", "host_exec.py")
INSTALL = os.path.join(HERE, "docker", "install_host_bridge.sh")
# every kit version and its whitelist hash: a command added without a version bump fails
# test_whitelist_hash_matches_the_version (docker/host-kit/host_exec.py: HOST_KIT_VERSION, COMMANDS)
KIT_VERSIONS = {"1": "19f410841fbc971d", "2": "2d289ec6ac9ab583"}
SID = "abcdef01-0000-4000-8000-000000000001"


def write_json(path, obj, mode=0o600):
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as fh:
        if isinstance(obj, str):
            fh.write(obj)
        else:
            json.dump(obj, fh)
    os.chmod(path, mode)


def good_config(root, **kw):
    cfg = {"format": 1, "roots": [root], "repo": HERE, "claude": "/usr/bin/true"}
    cfg.update(kw)
    return cfg


def load_he(host_json, home=None, log=os.devnull):
    """docker/host_exec.py as a module, reading `host_json` (its env override is read at import)."""
    env = {"AFCLAUDE_HOST_JSON": host_json, "AFCLAUDE_HOST_EXEC_LOG": log}
    if home:
        env["HOME"] = home
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("host_exec_kit", HOST_EXEC)
        he = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(he)
    return he


def run_exec(command, host_json, stdin=b"", home=None, log=os.devnull):
    env = dict(os.environ, SSH_ORIGINAL_COMMAND=command, AFCLAUDE_HOST_JSON=host_json, AFCLAUDE_HOST_EXEC_LOG=log)
    if home:
        env["HOME"] = home
    return subprocess.run([sys.executable, HOST_EXEC], input=stdin, env=env, capture_output=True, timeout=60)


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="kit-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "root")
        os.mkdir(self.root)
        self.hj = os.path.join(self.tmp, "host.json")
        write_json(self.hj, good_config(self.root))


class HostJson(TempDir):
    """A missing or malformed host.json refuses EVERY command (126), including the harmless ones."""
    COMMANDS = ("kit-version", "host-config get", "proc-snapshot", "usage-cache", "tmux-has afc-abc",
                f"ka_resume.sh --session {SID} --tmux afc-abc --message hi")

    def assert_refused(self, why):
        for cmd in self.COMMANDS:
            r = run_exec(cmd, self.hj)
            self.assertEqual(r.returncode, 126, (why, cmd, r.stderr))
            self.assertIn(b"host config", r.stderr, (why, cmd))
            self.assertIn(b"re-run the host kit install", r.stderr)

    def test_good_config_is_accepted(self):
        r = run_exec("kit-version", self.hj)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing(self):
        os.unlink(self.hj)
        self.assert_refused("missing")

    def test_malformed(self):
        good = good_config(self.root)
        bad = {
            "not json": "{nope",
            "a list": "[]",
            "empty": "",
            "missing key": {k: v for k, v in good.items() if k != "repo"},
            "unknown key": dict(good, extra=1),
            "format 2": dict(good, format=2),
            "format true": dict(good, format=True),
            "roots empty": dict(good, roots=[]),
            "roots not a list": dict(good, roots=self.root),
            "root relative": dict(good, roots=["work/x"]),
            "root with ..": dict(good, roots=[self.root + "/../x"]),
            "root is /": dict(good, roots=["/"]),
            "root not normalized": dict(good, roots=[self.root + "/"]),
            "root twice": dict(good, roots=[self.root, self.root]),
            "nine roots": dict(good, roots=[f"/r{i}" for i in range(9)]),
            "root not a string": dict(good, roots=[1]),
            "repo relative": dict(good, repo="repo"),
            "claude relative": dict(good, claude="claude"),
        }
        for why, content in bad.items():
            write_json(self.hj, content)
            self.assert_refused(why)

    def test_mode_owner_and_type(self):
        os.chmod(self.hj, 0o644)
        self.assert_refused("group/other readable")
        os.chmod(self.hj, 0o600)
        real = os.path.join(self.tmp, "real.json")
        os.rename(self.hj, real)
        os.symlink(real, self.hj)
        self.assert_refused("symlink")

    def test_log_beside_the_missing_config(self):
        """With no usable config the refusal is logged next to where host.json should be."""
        os.unlink(self.hj)
        with mock.patch.dict(os.environ, {"AFCLAUDE_HOST_EXEC_LOG": ""}):
            env = dict(os.environ, SSH_ORIGINAL_COMMAND="kit-version", AFCLAUDE_HOST_JSON=self.hj)
            env.pop("AFCLAUDE_HOST_EXEC_LOG")
            r = subprocess.run([sys.executable, HOST_EXEC], env=env, capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 126)
        with open(os.path.join(self.tmp, "host_exec.log")) as fh:
            self.assertIn("DENY host config", fh.read())

    def test_log_goes_under_the_repo_once_the_config_is_read(self):
        repo = os.path.join(self.tmp, "repo")
        os.mkdir(repo)
        write_json(self.hj, good_config(self.root, repo=repo))
        env = dict(os.environ, SSH_ORIGINAL_COMMAND="kit-version", AFCLAUDE_HOST_JSON=self.hj)
        env.pop("AFCLAUDE_HOST_EXEC_LOG", None)
        self.assertEqual(subprocess.run([sys.executable, HOST_EXEC], env=env, capture_output=True, timeout=60).returncode, 0)
        with open(os.path.join(repo, "data", "host_exec.log")) as fh:
            self.assertIn("OK kit-version", fh.read())


class KitVersion(TempDir):
    def test_the_image_and_the_kit_agree(self):
        he = load_he(self.hj)
        self.assertEqual(host.HOST_KIT_VERSION, he.HOST_KIT_VERSION)
        self.assertEqual(host.HOST_CONFIG_KEYS, he.SET_KEYS)

    def test_whitelist_hash_matches_the_version(self):
        he = load_he(self.hj)
        self.assertEqual(KIT_VERSIONS.get(he.HOST_KIT_VERSION), he.whitelist_hash(),
                         "the command list changed: bump HOST_KIT_VERSION and add the pair to KIT_VERSIONS")

    def test_commands_are_the_heads_main_dispatches(self):
        """COMMANDS (what kit-version lists and hashes) is exactly the set of heads main()'s chain tests."""
        with open(HOST_EXEC) as fh:
            body = fh.read().split("def main()", 1)[1].split("def install_args", 1)[0]
        heads = set(re.findall(r'head == "([^"]+)"', body))
        self.assertEqual(heads, set(load_he(self.hj).COMMANDS))

    def test_kit_version_answer(self):
        r = run_exec("kit-version", self.hj)
        info = json.loads(r.stdout)
        he = load_he(self.hj)
        self.assertEqual(info, {"kit_version": he.HOST_KIT_VERSION, "whitelist_hash": he.whitelist_hash(),
                                "commands": sorted(he.COMMANDS), "config_format": 1})

    def test_every_bridge_shape_the_image_builds_for_the_kit_is_accepted(self):
        for argv in (["kit-version"], ["host-config", "get"]):
            r = run_exec(" ".join(host.bridge_argv(argv)), self.hj)
            self.assertEqual(r.returncode, 0, (argv, r.stderr))

    def test_near_misses_are_denied(self):
        for cmd in ("kit-version now", "kit-version --json", "Kit-Version", "host-config", "host-config set",
                    "host-config set roots", "host-config set bogus /x", "host-config del roots", "host-config get x",
                    "host-config get roots", "host-config set format 2"):
            self.assertEqual(run_exec(cmd, self.hj).returncode, 126, cmd)
        for argv in (["kit-version", "x"], ["host-config"], ["host-config", "set", "bogus", "x"], ["host-config", "set", "roots"]):
            with self.assertRaises(ValueError, msg=argv):
                host.bridge_argv(argv)


class ImageSide(unittest.TestCase):
    """host.check_kit: a kit that differs from HOST_KIT_VERSION, or does not answer, is an error."""

    def answer(self, rc=0, out="", err=""):
        return mock.patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], rc, out, err))

    def info(self, version):
        return json.dumps({"kit_version": version, "whitelist_hash": "x", "commands": [], "config_format": 1})

    def test_matching_version(self):
        with self.answer(out=self.info(host.HOST_KIT_VERSION)):
            self.assertEqual(host.check_kit()["kit_version"], host.HOST_KIT_VERSION)

    def test_mismatch_names_both_versions(self):
        with self.answer(out=self.info("0")):
            with self.assertRaises(host.HostKitError) as cm:
                host.check_kit()
        self.assertIn("'0'", str(cm.exception))
        self.assertIn(repr(host.HOST_KIT_VERSION), str(cm.exception))
        self.assertIn("re-run the host kit install", str(cm.exception))

    def test_the_pre_kit_bridge_is_no_kit_yet_not_an_error(self):
        """The old bridge denies `kit-version` as "not whitelisted" (126): kit_info/check_kit -> None, which
        the host check reports; any other failure is still an error."""
        with self.answer(rc=126, err="host_exec: denied (not whitelisted)"):
            self.assertIsNone(host.kit_info())
            self.assertIsNone(host.check_kit())
        for rc, err in ((126, "host_exec: denied (host config: /x unreadable: No such file)"), (255, "ssh: refused"), (1, "")):
            with self.answer(rc=rc, err=err), self.assertRaises(host.HostKitError, msg=(rc, err)) as cm:
                host.kit_info()
            self.assertIn("re-run the host kit install", str(cm.exception))

    def test_garbage_answer(self):
        for out in ("", "not json", "[]", '{"kit_version": 1}', '{"kit_version": "2"}'):
            with self.answer(out=out):
                with self.assertRaises(host.HostKitError, msg=out):
                    host.kit_info()


class Roots(TempDir):
    """Every path the caller names must resolve inside an allowed root: `..`, symlinks and relative
    paths do not get out."""

    def setUp(self):
        super().setUp()
        self.sub = os.path.join(self.root, "proj")
        os.mkdir(self.sub)
        self.outside = os.path.join(self.tmp, "outside")
        os.mkdir(self.outside)
        os.symlink(self.outside, os.path.join(self.root, "escape"))          # a symlink out of the root
        os.symlink(self.sub, os.path.join(self.root, "inside"))              # one that stays inside
        self.other = os.path.join(self.tmp, "second-root")
        os.mkdir(self.other)
        self.he = load_he(self.hj)

    def args(self, cwd=None):
        a = ["--session", SID, "--tmux", "afc-abc", "--message", "go"]
        return a + (["--cwd", cwd] if cwd is not None else [])

    def test_inside_is_accepted_and_resolved(self):
        for cwd, want in ((self.root, self.root), (self.sub, self.sub), (self.sub + "/", self.sub),
                          (self.root + "/proj/../proj", self.sub), (os.path.join(self.root, "inside"), self.sub)):
            out = self.he.check_ka_resume(self.args(cwd))
            self.assertEqual(out[out.index("--cwd") + 1], want, cwd)
            self.assertEqual(out.count("--cwd"), 1)

    def test_no_cwd_is_the_first_root(self):
        out = self.he.check_ka_resume(self.args() + ["--new"])
        self.assertEqual(out[out.index("--cwd") + 1], self.root)
        self.assertIn("--new", out)

    def test_outside_dotdot_symlink_relative_are_denied(self):
        for cwd in (self.outside, self.tmp, "/", "/etc", self.root + "/..", self.root + "/../outside",
                    self.root + "/proj/../../outside", os.path.join(self.root, "escape"),
                    os.path.join(self.root, "escape", "x"), self.root + "-sibling", "proj", "./proj", "", "~"):
            with self.assertRaises(SystemExit, msg=cwd):
                self.he.check_ka_resume(self.args(cwd))

    def test_a_second_root(self):
        write_json(self.hj, good_config(self.root, roots=[self.root, self.other]))
        he = load_he(self.hj)
        self.assertEqual(he.under_roots(self.other), self.other)
        self.assertIsNone(he.under_roots(self.outside))

    def test_a_root_that_is_itself_a_symlink(self):
        link = os.path.join(self.tmp, "rootlink")
        os.symlink(self.root, link)
        write_json(self.hj, good_config(link))
        he = load_he(self.hj)
        self.assertEqual(he.under_roots(self.sub), self.sub)
        self.assertEqual(he.under_roots(os.path.join(link, "proj")), self.sub)
        self.assertIsNone(he.under_roots(self.outside))

    def test_over_the_wire(self):
        r = run_exec(f"ka_resume.sh {' '.join(self.args(self.outside))}", self.hj)
        self.assertEqual(r.returncode, 126)
        self.assertIn(b"outside the allowed roots", r.stderr)


class OutputCap(TempDir):
    def test_over_the_cap_is_an_error_not_a_cut_off(self):
        he = load_he(self.hj)
        out = io.StringIO()
        with mock.patch.object(he, "OUT_MAX", 10), mock.patch.object(he.sys, "stdout", out), \
                mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": "proc-snapshot"}), \
                mock.patch.object(he.sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                he.main()
        self.assertEqual(cm.exception.code, 126)
        self.assertEqual(out.getvalue(), "")

    def test_within_the_cap_is_written_whole(self):
        he = load_he(self.hj)
        out = io.StringIO()
        with mock.patch.object(he.sys, "stdout", out):
            he.emit("x" * he.OUT_MAX)
        self.assertEqual(len(out.getvalue()), he.OUT_MAX)
        with mock.patch.object(he.sys, "stdout", out), mock.patch.object(he.sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                he.emit("x" * (he.OUT_MAX + 1))
            with self.assertRaises(SystemExit):
                he.emit("ä" * (he.OUT_MAX // 2 + 1))     # the cap counts bytes


class HostConfigSet(TempDir):
    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        os.mkdir(os.path.join(self.home, ".claude"))
        os.mkdir(os.path.join(self.home, "work"))
        self.work = os.path.join(self.tmp, "work")
        os.mkdir(self.work)
        self.exe = os.path.join(self.tmp, "claude-stub")
        write_json(self.exe, "#!/bin/sh\n", 0o755)

    def cmd(self, *words):
        return run_exec("host-config set " + " ".join(words), self.hj, home=self.home)

    def config(self):
        with open(self.hj) as fh:
            return json.load(fh)

    def test_set_roots(self):
        r = self.cmd("roots", self.work, os.path.join(self.home, "work"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.config()["roots"], [self.work, os.path.join(self.home, "work")])
        self.assertEqual(os.stat(self.hj).st_mode & 0o777, 0o600)
        self.assertEqual(run_exec("host-config get", self.hj).returncode, 0)     # the next call reads it fine

    def test_set_resolves_symlinks(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.work, link)
        self.assertEqual(self.cmd("roots", link).returncode, 0)
        self.assertEqual(self.config()["roots"], [self.work])

    def test_set_repo_and_claude(self):
        self.assertEqual(self.cmd("repo", self.work).returncode, 0)
        self.assertEqual(self.cmd("claude", self.exe).returncode, 0)
        self.assertEqual((self.config()["repo"], self.config()["claude"]), (self.work, self.exe))

    def test_set_refused_leaves_the_file_alone(self):
        before = self.config()
        plain = os.path.join(self.tmp, "plain")
        write_json(plain, "x", 0o644)
        for words in (("roots", "relative/dir"), ("roots", os.path.join(self.tmp, "missing")), ("roots", self.work + "/.."),
                      ("roots", "/"), ("roots", "/etc"), ("roots", self.home), ("roots", self.tmp),      # the home, its ancestor
                      ("roots", os.path.join(self.home, ".claude")), ("roots", plain),
                      ("roots", *[self.work] * 9), ("roots",), ("repo", self.work, self.work), ("repo", plain),
                      ("claude", plain), ("claude", "claude"), ("claude", self.work), ("claude", os.path.join(self.tmp, "missing"))):
            r = self.cmd(*words)
            self.assertEqual(r.returncode, 126, (words, r.stderr))
            self.assertEqual(self.config(), before, words)

    def test_a_directory_of_another_user_is_refused(self):
        he = load_he(self.hj, home=self.home)
        real_stat = os.stat
        def other_owner(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1, *st[5:]))
        with mock.patch.object(he.os, "stat", other_owner):
            with self.assertRaises(he.ConfigError):
                he._owned_dir(self.work)


class InstallScript(unittest.TestCase):
    """docker/install_host_bridge.sh run for real, with a temporary HOME: the kit, host.json, the key,
    the authorized_keys line and the order of things."""
    def setUp(self):
        if not shutil.which("ssh-keygen"):
            self.skipTest("no ssh-keygen")
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="install-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.tmp, "root")
        self.data = os.path.join(self.tmp, "data")
        for d in (self.home, self.root, self.data, os.path.join(self.home, ".ssh")):
            os.mkdir(d)
        self.claude = os.path.join(self.home, "claude-stub")
        write_json(self.claude, "#!/bin/sh\n", 0o755)
        self.ak = os.path.join(self.home, ".ssh", "authorized_keys")
        # an unrelated key of the owner and the pre-kit bridge line (a key of its own, command in a checkout)
        self.OTHER = self.pubkey("owner@laptop")
        self.OLD = ('command="/old/checkout/docker/host_exec.py",from="172.16.0.0/12",no-pty '
                    + self.pubkey("afclaude-host-bridge"))
        with open(self.ak, "w") as fh:
            fh.write(self.OTHER + "\n" + self.OLD + "\n")
        os.chmod(self.ak, 0o600)
        # a copy of the checkout's kit sources: the script finds them next to itself
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(self.src, "docker", "host-kit"))
        for rel in ("docker/install_host_bridge.sh", "docker/host-kit/host_exec.py", "ka_resume.sh"):
            shutil.copy(os.path.join(HERE, rel), os.path.join(self.src, rel))
        self.share = os.path.join(self.home, ".local", "share", "afclaude")

    def run_install(self, *extra, args=None, src=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AFCLAUDE_", "XDG_", "SSH_"))}
        env["HOME"] = self.home
        a = args if args is not None else ["--root", self.root, "--data", self.data, "--claude", self.claude]
        return subprocess.run(["bash", os.path.join(src or self.src, "docker", "install_host_bridge.sh"), *a, *extra],
                              env=env, capture_output=True, text=True, timeout=120)

    def pubkey(self, comment):
        path = os.path.join(self.tmp, "k-" + re.sub(r"\W", "", comment))
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", path], check=True)
        with open(path + ".pub") as fh:
            return fh.read().strip()

    def patch_kit(self, old, new):
        path = os.path.join(self.src, "docker", "host-kit", "host_exec.py")
        with open(path) as fh:
            text = fh.read()
        self.assertIn(old, text)
        with open(path, "w") as fh:
            fh.write(text.replace(old, new))

    def lines(self):
        with open(self.ak) as fh:
            return fh.read().splitlines()

    def test_fresh_install(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        kits = os.path.join(self.share, "host-kit")
        he = load_he(os.path.join(self.share, "host.json"))
        self.assertEqual(os.readlink(os.path.join(kits, "current")), he.HOST_KIT_VERSION)
        for name in ("host_exec.py", "ka_resume.sh"):
            self.assertTrue(os.access(os.path.join(kits, he.HOST_KIT_VERSION, name), os.X_OK), name)
        # host.json: mode 0600, what was asked for
        self.assertEqual(os.stat(os.path.join(self.share, "host.json")).st_mode & 0o777, 0o600)
        with open(os.path.join(self.share, "host.json")) as fh:
            self.assertEqual(json.load(fh), {"format": 1, "roots": [self.root], "repo": self.src, "claude": self.claude})
        # the key lives in the data dir
        key = os.path.join(self.data, "bridge", "host_bridge_ed25519")
        self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(key)).st_mode & 0o777, 0o700)
        self.assertFalse(os.path.exists(os.path.join(self.src, "docker", "secrets")))
        # authorized_keys: the other keys and the OLD line stay, one new line points at current/
        lines = self.lines()
        self.assertEqual(lines[:2], [self.OTHER, self.OLD])
        self.assertEqual(len(lines), 3)
        with open(key + ".pub") as fh:
            blob = fh.read().split()[1]
        new = lines[2]
        self.assertIn(f'command="{kits}/current/host_exec.py",from="172.16.0.0/12",no-pty,no-port-forwarding,'
                      'no-agent-forwarding,no-X11-forwarding,no-user-rc ssh-ed25519 ' + blob, new)
        self.assertTrue(new.endswith("afclaude-host-bridge-kit"))
        # the closing message names the old line and how to remove it; running that sed removes only it
        self.assertIn("1 old bridge line(s)", r.stdout)
        sed = [l for l in r.stdout.splitlines() if l.strip().startswith("sed -i")][0].strip()
        subprocess.run(sed, shell=True, check=True)
        self.assertEqual(self.lines(), [self.OTHER, new])

    def test_the_kit_runs_through_the_current_link(self):
        self.assertEqual(self.run_install().returncode, 0)
        cur = os.path.join(self.share, "host-kit", "current", "host_exec.py")
        env = {"SSH_ORIGINAL_COMMAND": "kit-version", "PATH": os.environ["PATH"], "HOME": self.home}
        r = subprocess.run([cur], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["kit_version"], load_he(os.path.join(self.share, "host.json")).HOST_KIT_VERSION)
        env["SSH_ORIGINAL_COMMAND"] = "cat /etc/passwd"
        self.assertEqual(subprocess.run([cur], env=env, capture_output=True, timeout=60).returncode, 126)

    def test_from_is_the_runners_network(self):
        r = self.run_install("--from", "10.20.0.0/16")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('from="10.20.0.0/16",no-pty', self.lines()[-1])
        for bad in ('10.0.0.1" ,command="x', "a b", ""):
            r = self.run_install("--from", bad)
            self.assertNotEqual(r.returncode, 0, bad)

    def test_rerun_is_idempotent_and_keeps_host_json(self):
        self.assertEqual(self.run_install().returncode, 0)
        first = self.lines()
        with open(os.path.join(self.share, "host.json")) as fh:
            cfg = fh.read()
        r = self.run_install("--from", "10.0.0.0/8", args=["--data", self.data])      # no --root needed once host.json exists
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        again = self.lines()
        self.assertEqual(len(again), len(first))
        self.assertEqual(again[:2], first[:2])
        self.assertIn('from="10.0.0.0/8"', again[2])                  # the same key's line is replaced
        with open(os.path.join(self.share, "host.json")) as fh:
            self.assertEqual(fh.read(), cfg)
        self.assertIn("already installed", r.stdout)

    def test_no_root_and_no_host_json_stops_before_touching_anything(self):
        r = self.run_install(args=[])
        self.assertEqual(r.returncode, 1)
        self.assertIn("--root", r.stderr)
        self.assertEqual(self.lines(), [self.OTHER, self.OLD])
        self.assertFalse(os.path.exists(os.path.join(self.share, "host-kit", "current")))

    def test_an_invalid_host_json_stops_the_install(self):
        os.makedirs(self.share)
        write_json(os.path.join(self.share, "host.json"), "{broken")
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("not valid", r.stderr)
        self.assertEqual(self.lines(), [self.OTHER, self.OLD])

    def test_same_version_with_other_files_is_refused(self):
        self.assertEqual(self.run_install().returncode, 0)
        with open(os.path.join(self.src, "docker", "host-kit", "host_exec.py"), "a") as fh:
            fh.write("# changed without a version bump\n")
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("bump HOST_KIT_VERSION", r.stderr)

    def test_a_failing_self_test_leaves_the_old_line_and_no_new_one(self):
        # a kit that does not refuse (the denial exits 0): the rejected-command self-test fails
        self.patch_kit("sys.exit(126)", "sys.exit(0)")
        before = self.lines()
        r = self.run_install()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAILED", r.stderr)
        self.assertEqual(self.lines(), before)                        # old line kept, no new line
        self.assertFalse(os.path.lexists(os.path.join(self.share, "host-kit", "current")))
        self.assertEqual([d for d in os.listdir(os.path.join(self.share, "host-kit")) if d.startswith(".stage")], [])

    def test_a_broken_kit_never_gets_the_current_link_or_a_line(self):
        self.patch_kit("emit(kit_version_json())", "sys.exit(3)")      # kit-version itself fails
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.lines(), [self.OTHER, self.OLD])

    def test_a_failing_line_check_puts_everything_back(self):
        # a newer kit is installed first; the next install's final check fails -> link and file as before
        self.assertEqual(self.run_install().returncode, 0)
        before_lines, before_link = self.lines(), os.readlink(os.path.join(self.share, "host-kit", "current"))
        self.patch_kit('HOST_KIT_VERSION = "2"', 'HOST_KIT_VERSION = "99"')
        # the stub ssh-keygen cannot read the key from authorized_keys: the line check fails
        stub = os.path.join(self.tmp, "bin")
        os.mkdir(stub)
        real = shutil.which("ssh-keygen")
        write_json(os.path.join(stub, "ssh-keygen"),
                   f'#!/bin/sh\ncase "$2" in *authorized_keys) exit 1;; esac\nexec {real} "$@"\n', 0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AFCLAUDE_", "XDG_", "SSH_"))}
        env.update(HOME=self.home, PATH=stub + ":" + os.environ["PATH"])
        r = subprocess.run(["bash", os.path.join(self.src, "docker", "install_host_bridge.sh")], env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("back as they were", r.stderr)
        self.assertEqual(self.lines(), before_lines)
        self.assertEqual(os.readlink(os.path.join(self.share, "host-kit", "current")), before_link)


LEGACY_EXEC = os.path.join(HERE, "docker", "host_exec.py")
# sha256 of docker/host_exec.py as main has it: the owner's live authorized_keys line runs THAT file from the
# main checkout, so merging a change of it would replace the live bridge at once, before any install. It is
# removed only after the owner removed the old line (docs/phase5b_plan.md K2).
LEGACY_SHA256 = "7c2e2e1e834a6bb1d3cfbde63b4d9cd9ac4eac1fb5752407a3ce93f5375ecc39"
KIT_ONLY = [["kit-version"], ["host-config", "get"], ["host-config", "set", "repo", "/x"], ["tmux-check"], ["df"],
            ["clock"], ["sessions", "default"], ["transcripts", "default", "--changed-since", "0"],
            ["doc-files", "read", "/x", "PROGRESS.md"], ["claude", "auth", "status"], ["claude", "doctor"]]


def run_legacy(command):
    env = dict(os.environ, SSH_ORIGINAL_COMMAND=command, AFCLAUDE_HOST_EXEC_LOG=os.devnull)
    return subprocess.run([sys.executable, LEGACY_EXEC], input=b"", env=env, capture_output=True, timeout=60)


class LegacyBridge(unittest.TestCase):
    """The live bridge is docker/host_exec.py of the main checkout (the owner's authorized_keys line): it stays
    as it is until the kit is installed, and the image works with it."""

    def legacy_state(self):
        return mock.patch.dict(host._kit_cache, {"at": float("inf"), "info": None})

    def test_the_legacy_file_is_the_one_main_has(self):
        with open(LEGACY_EXEC, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), LEGACY_SHA256,
                             "docker/host_exec.py is the live bridge until the owner's install: do not edit it "
                             "(the kit's source is docker/host-kit/host_exec.py)")

    def test_the_kit_source_is_another_file(self):
        with open(HOST_EXEC, "rb") as a, open(LEGACY_EXEC, "rb") as b:
            self.assertNotEqual(a.read(), b.read())

    def test_legacy_heads_are_what_the_file_dispatches(self):
        with open(LEGACY_EXEC) as fh:
            heads = set(re.findall(r'head == "([^"]+)"', fh.read())) | {"ka_resume.sh"}
        self.assertEqual(heads, set(host.LEGACY_HEADS))

    def test_a_new_kit_command_is_known_to_the_gate(self):
        he = load_he(os.devnull)
        self.assertEqual(set(he.COMMANDS) - host.LEGACY_HEADS,
                         {"kit-version", "host-config", "transcripts", "sessions", "doc-files", "tmux-check", "df",
                          "clock"}, "a new kit command: the image must not send it to the pre-kit bridge (host.needs_kit)")

    def test_the_runner_shapes_are_accepted_by_the_legacy_bridge_and_the_gate(self):
        shapes = ([["tmux", verb, *rest] for verb, rest in (
            ("has-session", ["-t", "=afc-00000000"]), ("list-panes", ["-s", "-t", "=afc-00000000", "-F", "#{pane_pid}"]),
            ("list-clients", ["-t", "=afc-00000000", "-F", "#{client_activity}"]),
            ("capture-pane", ["-e", "-p", "-t", "=afc-00000000:"]), ("kill-session", ["-t", "=afc-00000000"]),
            ("rename-session", ["-t", "=afc-00000000", "afc-00000001"]))] + [["proc-snapshot"]])
        with self.legacy_state():
            for argv in shapes:
                remote = host.bridge_argv(argv)
                host.gate_command(remote)                                   # no error: the image sends it
                self.assertNotEqual(run_legacy(shlex.join(remote)).returncode, 126, remote)
            # the shapes that are only checked (a real claude / launch would start): the legacy file's own checks
            spec = importlib.util.spec_from_file_location("host_exec_legacy", LEGACY_EXEC)
            legacy = importlib.util.module_from_spec(spec)
            with mock.patch.dict(os.environ, {"AFCLAUDE_HOST_EXEC_LOG": os.devnull}):
                spec.loader.exec_module(legacy)
            ka = ["--session", SID, "--tmux", "afc-abcdef01", "--message", "go", "--cwd", legacy.ROOT, "--name", "t",
                  "--model", "claude-opus-5-5", "--effort", "high", "--new", "--no-remote-control"]
            legacy.check_ka_resume(ka)
            for argv in (["ka_resume.sh", *ka], ["claude", "agents", "--json"], ["claude", "agents", "--json", "--all"],
                         ["claude", "-p", "--no-session-persistence", "--permission-mode", "dontAsk", "/usage"],
                         ["claude", "-p", "--model", "haiku", "--no-session-persistence", "--output-format", "json"],
                         ["claude", "stop", "0123abcd"], ["kill-claude", "1"], ["claude-env", "1"], ["usage-cache"],
                         ["append-note", "ALERTS.md"], ["rc-send", "work-a1"]):
                host.gate_command(host.bridge_argv(argv))

    def test_kit_only_shapes_are_refused_by_the_legacy_bridge_and_never_sent(self):
        with self.legacy_state():
            for argv in KIT_ONLY:
                remote = host.bridge_argv(argv)
                r = run_legacy(shlex.join(remote))
                self.assertEqual(r.returncode, 126, remote)
                self.assertIn(b"not whitelisted", r.stderr)
                if remote != ["kit-version"]:                      # the one command that asks which bridge this is
                    with self.assertRaises(host.HostKitError, msg=remote) as cm:
                        host.gate_command(remote)
                    self.assertIn("not installed yet", str(cm.exception))
            with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": "1"}), \
                    mock.patch.object(host.subprocess, "run") as run, self.assertRaises(host.HostKitError):
                host.run_on_host(["claude", "doctor"])
            run.assert_not_called()                               # refused before ssh

    def test_kit_version_itself_is_never_gated(self):
        with self.legacy_state():
            host.gate_command(["kit-version"])


class KitGate(unittest.TestCase):
    """The image refuses a command the installed kit does not list; a kit that cannot be asked does not hide
    the command's own failure."""

    def setUp(self):
        self.addCleanup(host._kit_cache.update, dict(host._kit_cache))
        self.addCleanup(host._host_repo.update, dict(host._host_repo))
        host._host_repo["path"] = None

    def installed(self, commands, version=None):
        host._kit_cache.update(at=time.monotonic(), info={"kit_version": version or host.HOST_KIT_VERSION,
                                                            "whitelist_hash": "x", "commands": list(commands)})

    def test_a_command_the_kit_does_not_list_is_refused(self):
        self.installed(["tmux-has", "claude"])
        host.gate_command(["tmux-has", "afc-a"])
        host.gate_command(["claude", "agents", "--json"])
        with self.assertRaises(host.HostKitError) as cm:
            host.gate_command(["df"])
        self.assertIn("does not list 'df'", str(cm.exception))
        with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": "1"}), \
                mock.patch.object(host.subprocess, "run") as run, self.assertRaises(host.HostKitError):
            host.run_on_host(["proc-snapshot"])
        run.assert_not_called()

    def test_every_command_of_this_version_passes_the_gate(self):
        he = load_he(os.devnull)
        self.installed(he.COMMANDS)
        for argv in KIT_ONLY:
            host.gate_command(host.bridge_argv(argv))

    def test_kit_state_is_cached_and_the_legacy_answer_is_asked_again(self):
        host._kit_cache.update(at=None, info=None)
        with mock.patch.object(host, "kit_info", return_value=None) as ki, mock.patch("sys.stderr") as err:
            host.kit_state()
            host.kit_state()
            self.assertEqual(ki.call_count, 1)
            self.assertIn("no host kit installed yet", "".join(c.args[0] for c in err.write.call_args_list))
            host._kit_cache["at"] = time.monotonic() - host.KIT_RECHECK - 1
            host.kit_state()
            self.assertEqual(ki.call_count, 2)
        info = {"kit_version": "2", "commands": ["df"]}
        host._kit_cache["at"] = time.monotonic() - host.KIT_RECHECK - 1
        with mock.patch.object(host, "kit_info", return_value=info) as ki:
            self.assertEqual(host.kit_state(), info)
            host._kit_cache["at"] = time.monotonic() - 10 * host.KIT_RECHECK
            self.assertEqual(host.kit_state(), info)           # an installed kit is not asked again
            self.assertEqual(ki.call_count, 1)

    def test_a_kit_that_cannot_be_asked_does_not_stop_the_command(self):
        host._kit_cache.update(at=None, info=None)
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv[-1])         # ssh's last argument: the remote command line
            return subprocess.CompletedProcess(argv, 255 if argv[-1] == "kit-version" else 0, "", "")

        with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": "1"}), mock.patch.object(host.subprocess, "run", fake_run):
            r = host.run_on_host(["tmux", "has-session", "-t", "=afc-a"])
        self.assertEqual(calls, ["kit-version", "tmux-has afc-a"])
        self.assertEqual(r.returncode, 0)

    def test_repo_dir_is_the_kits_host_json_and_the_env_is_ignored(self):
        self.installed(["host-config"])
        out = json.dumps({"format": 1, "roots": ["/w"], "repo": "/srv/kit-repo/", "claude": "/c"})
        with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": "1", "AFCLAUDE_HOST_REPO": "/srv/other"}), \
                mock.patch.object(host, "run_on_host", return_value=subprocess.CompletedProcess([], 0, out, "")) as run, \
                mock.patch("sys.stderr") as err:
            self.assertEqual(host.repo_dir(), "/srv/kit-repo")
            self.assertEqual(host.repo_dir(), "/srv/kit-repo")
        run.assert_called_once_with(["host-config", "get"], timeout=30)       # once per process
        self.assertIn("differs from the host kit's repo", "".join(c.args[0] for c in err.write.call_args_list))

    def test_repo_dir_falls_back_to_the_env_only_while_no_kit_is_installed(self):
        host._kit_cache.update(at=float("inf"), info=None)
        with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": "1", "AFCLAUDE_HOST_REPO": "/srv/other/"}):
            self.assertEqual(host.repo_dir(), "/srv/other")
        for bad in ("", "relative"):
            with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": "1", "AFCLAUDE_HOST_REPO": bad}), \
                    self.assertRaises(host.HostRepoError):
                host.repo_dir()

    def test_repo_dir_errors_when_the_kit_cannot_be_asked_or_answers_wrongly(self):
        with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": "1", "AFCLAUDE_HOST_REPO": "/srv/other"}):
            host._kit_cache.update(at=None, info=None)
            with mock.patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "", "no")), \
                    self.assertRaises(host.HostKitError):           # bridge down: no guess from the env
                host.repo_dir()
            self.installed(["host-config"])
            for rc, out in ((1, ""), (0, "nope"), (0, "{}"), (0, '{"repo": "rel"}')):
                with mock.patch.object(host, "run_on_host", return_value=subprocess.CompletedProcess([], rc, out, "")), \
                        self.assertRaises(host.HostKitError, msg=(rc, out)):
                    host.repo_dir()
        self.assertIsNone(host._host_repo["path"])


class KitReads(TempDir):
    """transcripts, sessions, doc-files read, the check commands (kit version 2)."""

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        self.cdir = os.path.join(self.home, ".claude")
        os.makedirs(os.path.join(self.cdir, "projects", "-w-p", "subagents"))
        os.makedirs(os.path.join(self.cdir, "sessions"))
        self.he = load_he(self.hj, home=self.home)

    def tx(self, name, entries, mtime=None, raw=None):
        path = os.path.join(self.cdir, "projects", "-w-p", name)
        with open(path, "w") as fh:
            fh.write(raw if raw is not None else "".join(json.dumps(e) + "\n" for e in entries))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def call(self, cmd, **kw):
        r = run_exec(cmd, self.hj, home=self.home, **kw)
        return r

    def ok_json(self, cmd, **kw):
        r = self.call(cmd, **kw)
        self.assertEqual(r.returncode, 0, (cmd, r.stderr))
        return json.loads(r.stdout)

    def user(self, text, ts="2026-11-01T10:00:00Z", **kw):
        return dict({"type": "user", "timestamp": ts, "uuid": "u-" + text,
                     "message": {"role": "user", "content": text}}, **kw)

    # ---- transcripts

    def test_transcripts_filter_projection_and_recursion(self):
        big = "x" * 5000
        self.tx("old.jsonl", [self.user("old")], mtime=1000)
        self.tx("new.jsonl", [self.user("first"), {"type": "assistant", "timestamp": "t2", "isApiErrorMessage": True,
                                                   "message": {"model": "<synthetic>", "content": [
                                                       {"type": "text", "text": "limit " + big},
                                                       {"type": "tool_use", "id": "t", "input": {"big": big}}]}},
                                self.user("last", isCompactSummary=True, extra="dropped")], mtime=5000)
        sub = os.path.join(self.cdir, "projects", "-w-p", "subagents", "agent-1.jsonl")
        with open(sub, "w") as fh:
            fh.write(json.dumps(self.user("sub")) + "\n")
        os.utime(sub, (6000, 6000))
        self.tx("notes.txt", [], raw="no")
        os.symlink(os.path.join(self.cdir, "projects", "-w-p", "new.jsonl"), os.path.join(self.cdir, "projects", "-w-p", "link.jsonl"))
        out = self.ok_json("transcripts default --changed-since 2000 --head 1 --tail 2")
        self.assertFalse(out["truncated"])
        self.assertEqual([f["path"] for f in out["files"]], ["-w-p/new.jsonl", "-w-p/subagents/agent-1.jsonl"])
        new = out["files"][0]
        self.assertEqual((new["size"], new["mtime"], new["tail_complete"], new["bad_lines"]),
                         (os.path.getsize(os.path.join(self.cdir, "projects", "-w-p", "new.jsonl")), 5000.0, True, 0))
        self.assertEqual([e["uuid"] for e in new["head"]], ["u-first"])
        self.assertEqual([e.get("uuid") for e in new["tail"]], [None, "u-last"])
        self.assertEqual(new["tail"][0], {"type": "assistant", "timestamp": "t2", "isApiErrorMessage": True,
                                          "model": "<synthetic>", "text": "limit " + big[:294], "tool_result": False})
        self.assertEqual(new["tail"][1], {"type": "user", "timestamp": "2026-11-01T10:00:00Z", "uuid": "u-last",
                                          "isCompactSummary": True, "text": "last"})
        self.assertEqual(len(self.call("transcripts default --changed-since 0").stdout), len(self.call(
            "transcripts default --changed-since 0 --head 1 --tail 5").stdout))        # the defaults: 1 and 5
        self.assertEqual(len(self.ok_json("transcripts default --changed-since 0")["files"]), 3)

    def test_a_half_written_last_line_is_counted_not_dropped_silently(self):
        self.tx("a.jsonl", [], raw=json.dumps(self.user("one")) + '\n{"type": "assis')
        f = self.ok_json("transcripts default --changed-since 0")["files"][0]
        self.assertEqual((len(f["tail"]), f["bad_lines"]), (1, 1))

    def test_a_cap_is_a_truncated_flag_never_a_silent_cut(self):
        for i in range(3):
            self.tx(f"f{i}.jsonl", [self.user(str(i))])
        self.he.TX_MAX_FILES = 2
        out = json.loads(self.he.transcripts_json(["default", "--changed-since", "0"]))
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["files"]), 2)
        self.he.TX_MAX_FILES = 2000
        self.tx("long.jsonl", [self.user("y" * 3000 + str(i)) for i in range(20)])
        self.he.TX_TAIL_READ = 256 * 1024          # one block: the file is smaller, so complete
        self.assertFalse(json.loads(self.he.transcripts_json(["default", "--changed-since", "0"]))["truncated"])
        self.tx("huge.jsonl", [], raw=("z" * 400_000 + "\n") * 3)
        self.he.TX_TAIL_READ = 256 * 1024
        out = json.loads(self.he.transcripts_json(["default", "--changed-since", "0", "--tail", "3"]))
        self.assertTrue(out["truncated"])
        self.assertFalse([f for f in out["files"] if f["path"].endswith("huge.jsonl")][0]["tail_complete"])

    def test_the_tail_of_a_big_file_is_read_from_the_end(self):
        entries = [self.user(f"m{i}" + "p" * 2000) for i in range(2000)]      # ~4.6 MB
        self.tx("big.jsonl", entries)
        f = self.ok_json("transcripts default --changed-since 0 --head 2 --tail 3")["files"][0]
        self.assertEqual([e["uuid"][:7] for e in f["tail"]], ["u-m1997", "u-m1998", "u-m1999"])
        self.assertEqual([e["uuid"][:5] for e in f["head"]], ["u-m0p", "u-m1p"])
        self.assertTrue(f["tail_complete"])

    def test_config_dir_ids(self):
        other = os.path.join(self.tmp, "cfg2")
        os.makedirs(os.path.join(other, "projects"))
        write_json(self.hj, good_config(self.root, config_dirs={"work-2": other}))
        self.assertEqual(self.ok_json("transcripts work-2 --changed-since 0")["files"], [])
        self.assertEqual(self.ok_json("sessions work-2")["files"], [])
        he = load_he(self.hj, home=self.home)
        self.assertEqual(he.cfg().config_dirs, (("work-2", other),))
        self.assertEqual(json.loads(he.config_text(he.cfg()))["config_dirs"], {"work-2": other})
        for cmd in ("transcripts nope --changed-since 0", "sessions nope", "sessions ../x", "sessions Default",
                    "transcripts /etc --changed-since 0", "sessions default extra"):
            self.assertEqual(self.call(cmd).returncode, 126, cmd)
        for bad in ({"default": other}, {"X": other}, {"a": "rel"}, {"a": other + "/"}, ["a"]):
            write_json(self.hj, good_config(self.root, config_dirs=bad))
            self.assertEqual(self.call("kit-version").returncode, 126, bad)

    def test_transcripts_near_misses(self):
        for cmd in ("transcripts", "transcripts default", "transcripts default --changed-since",
                    "transcripts default --changed-since abc", "transcripts default --changed-since -1",
                    "transcripts default --changed-since 0 --head", "transcripts default --changed-since 0 --head x",
                    "transcripts default --changed-since 0 --head 21", "transcripts default --changed-since 0 --since 1",
                    "transcripts default --changed-since 0 --tail 1 extra", "transcripts default --changed-since 1e99",
                    "transcripts default --changed-since nan"):
            self.assertEqual(self.call(cmd).returncode, 126, cmd)

    # ---- sessions

    def test_sessions(self):
        d = os.path.join(self.cdir, "sessions")
        write_json(os.path.join(d, "123.json"), {"pid": 123, "name": "work-a1", "status": "idle"}, 0o644)
        write_json(os.path.join(d, "456.json"), "{half", 0o644)
        write_json(os.path.join(d, "x.txt"), "ignored", 0o644)
        os.symlink("/etc/passwd", os.path.join(d, "link.json"))
        out = self.ok_json("sessions default")
        self.assertFalse(out["truncated"])
        by = {f["file"]: f for f in out["files"]}
        self.assertEqual(set(by), {"123.json", "456.json", "link.json"})
        self.assertEqual(by["123.json"]["data"], {"pid": 123, "name": "work-a1", "status": "idle"})
        self.assertIn("unreadable", by["456.json"]["error"])            # reported, not skipped
        self.assertIn("not a small regular file", by["link.json"]["error"])
        self.he.SESSIONS_MAX = 1
        self.assertTrue(json.loads(self.he.sessions_json(["default"]))["truncated"])

    # ---- doc-files

    def test_doc_files(self):
        proj = os.path.join(self.root, "proj")
        os.mkdir(proj)
        for name, text in (("PROGRESS.md", "p ä\n"), ("GOALS.md", "g\n")):
            with open(os.path.join(proj, name), "w") as fh:
                fh.write(text)
        out = self.ok_json(f"doc-files read {proj} PROGRESS.md GOALS.md EXCEPTIONS.md")
        self.assertEqual(out, {"PROGRESS.md": "p ä\n", "GOALS.md": "g\n", "EXCEPTIONS.md": None})
        self.assertEqual(self.ok_json(f"doc-files read {self.root}/proj/../proj GOALS.md"), {"GOALS.md": "g\n"})

    def test_doc_files_refusals(self):
        proj = os.path.join(self.root, "proj")
        os.mkdir(proj)
        secret = os.path.join(self.tmp, "secret.md")
        with open(secret, "w") as fh:
            fh.write("s")
        os.symlink(secret, os.path.join(proj, "GOALS.md"))                    # a symlinked file
        os.symlink(self.tmp, os.path.join(self.root, "escape"))              # a symlinked directory
        with open(os.path.join(proj, "DECISIONS.md"), "wb") as fh:
            fh.write(b"\xff\xfe")
        for cmd in (f"doc-files read {proj} GOALS.md", f"doc-files read {proj} DECISIONS.md",
                    f"doc-files read {self.root}/escape PROGRESS.md", f"doc-files read {self.tmp} PROGRESS.md",
                    "doc-files read /etc PROGRESS.md", "doc-files read relative PROGRESS.md",
                    f"doc-files read {proj} README.md", f"doc-files read {proj} ../x", f"doc-files read {proj}",
                    f"doc-files read {proj} PROGRESS.md PROGRESS.md", f"doc-files write {proj} PROGRESS.md",
                    "doc-files read", f"doc-files read {proj}/PROGRESS.md PROGRESS.md"):
            self.assertEqual(self.call(cmd).returncode, 126, cmd)
        os.unlink(os.path.join(proj, "DECISIONS.md"))
        os.mkdir(os.path.join(proj, "DECISIONS.md"))                          # a directory by that name
        self.assertEqual(self.call(f"doc-files read {proj} DECISIONS.md").returncode, 126)
        self.he.DOC_MAX = 3
        with open(os.path.join(proj, "PROGRESS.md"), "w") as fh:
            fh.write("abcd")
        with self.assertRaises(SystemExit):
            self.he.doc_files_json(["read", proj, "PROGRESS.md"])

    def test_the_repo_may_be_read_even_outside_the_roots(self):
        repo = os.path.join(self.tmp, "repo")
        os.mkdir(repo)
        with open(os.path.join(repo, "PROGRESS.md"), "w") as fh:
            fh.write("x")
        write_json(self.hj, good_config(self.root, repo=repo))
        self.assertEqual(self.ok_json(f"doc-files read {repo} PROGRESS.md"), {"PROGRESS.md": "x"})
        self.assertEqual(self.call(f"doc-files read {repo}/data PROGRESS.md").returncode, 126)

    # ---- the check commands

    def test_df_and_clock(self):
        out = self.ok_json("df")
        self.assertEqual(set(out), {"claude_dir", "repo"})
        self.assertEqual(out["repo"]["path"], HERE)
        for v in out.values():
            self.assertGreater(v["total_bytes"], v["free_bytes"] - 1)
        before = int(time.time() * 1000)
        self.assertLess(abs(self.ok_json("clock")["epoch_ms"] - before), 60_000)
        for cmd in ("df -h", "df /", "clock now", "clock --utc"):
            self.assertEqual(self.call(cmd).returncode, 126, cmd)

    def test_claude_auth_status_and_doctor_run_the_configured_binary(self):
        stub = os.path.join(self.tmp, "claude-stub")
        write_json(stub, '#!/bin/sh\necho "args: $*"\n', 0o755)
        write_json(self.hj, good_config(self.root, claude=stub))
        for cmd, want in (("claude auth status", b"args: auth status\n"), ("claude doctor", b"args: doctor\n")):
            r = self.call(cmd)
            self.assertEqual((r.returncode, r.stdout), (0, want), r.stderr)
        for cmd in ("claude auth", "claude auth login", "claude auth status --json", "claude auth logout",
                    "claude doctor --fix", "claude doctor x", "claude config", "claude mcp list"):
            self.assertEqual(self.call(cmd).returncode, 126, cmd)

    def test_check_commands_have_a_timeout(self):
        stub = os.path.join(self.tmp, "claude-stub")
        write_json(stub, "#!/bin/sh\nsleep 30\n", 0o755)
        write_json(self.hj, good_config(self.root, claude=stub))
        he = load_he(self.hj, home=self.home)
        he.CHECK_TIMEOUT = 1
        with mock.patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": "claude doctor"}), mock.patch("sys.stderr"):
            self.assertEqual(he.main(), 124)

    def test_tmux_check_creates_sees_and_kills_the_session(self):
        he = self.he
        calls = []

        def fake(rc_new=0, rc_has=(1, 0), rc_kill=0):
            has = iter(rc_has)

            def run(argv, **kw):
                calls.append(argv[1:])
                rc = {"has-session": lambda: next(has), "new-session": lambda: rc_new,
                      "kill-session": lambda: rc_kill}[argv[1]]()
                return subprocess.CompletedProcess(argv, rc, "", "boom")
            return run

        out = io.StringIO()
        with mock.patch.object(he.subprocess, "run", fake()), mock.patch("sys.stdout", out):
            self.assertEqual(he.tmux_check(), 0)
        self.assertEqual(out.getvalue(), "ok\n")
        self.assertEqual([c[0] for c in calls], ["has-session", "new-session", "has-session", "kill-session"])
        self.assertEqual(calls[0], ["has-session", "-t", "=afc-check"])
        for kw in ({"rc_new": 1, "rc_has": (1,)}, {"rc_has": (1, 1)}, {"rc_kill": 1}):
            calls.clear()
            with mock.patch.object(he.subprocess, "run", fake(**kw)), mock.patch("sys.stderr") as err:
                self.assertEqual(he.tmux_check(), 1, kw)
            self.assertIn("tmux-check failed", "".join(c.args[0] for c in err.write.call_args_list))
        calls.clear()                                                         # a leftover session is killed first
        with mock.patch.object(he.subprocess, "run", fake(rc_has=(0, 0))), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(he.tmux_check(), 0)
        self.assertEqual([c[0] for c in calls], ["has-session", "kill-session", "new-session", "has-session", "kill-session"])
        for cmd in ("tmux-check afc-x", "tmux-check now"):
            self.assertEqual(self.call(cmd).returncode, 126, cmd)

    def test_every_new_bridge_shape_is_built_by_host_py_and_accepted_by_the_kit(self):
        proj = os.path.join(self.root, "proj")
        os.mkdir(proj)
        for argv in (["sessions", "default"], ["transcripts", "default", "--changed-since", "1700000000.5"],
                     ["transcripts", "default", "--changed-since", "0", "--head", "2", "--tail", "9"],
                     ["transcripts", "default", "--changed-since", "0", "--tail", "9", "--head", "0"],
                     ["doc-files", "read", proj, "PROGRESS.md", "GOALS.md"], ["df"], ["clock"]):
            r = self.call(shlex.join(host.bridge_argv(argv)))
            self.assertEqual(r.returncode, 0, (argv, r.stderr))
        for argv in (["claude", "auth", "status"], ["claude", "doctor"]):
            self.assertEqual(host.bridge_argv(argv), argv)
        for argv in (["sessions"], ["sessions", "A"], ["sessions", "a", "b"], ["transcripts", "default"],
                     ["transcripts", "default", "--changed-since", "x"],
                     ["transcripts", "default", "--changed-since", "0", "--head"],
                     ["transcripts", "default", "--changed-since", "0", "--head", "1", "--head", "2"],
                     ["transcripts", "default", "--changed-since", "0", "--bogus", "1"],
                     ["doc-files", "read", proj], ["doc-files", "read", "rel", "GOALS.md"],
                     ["doc-files", "read", proj, "README.md"], ["doc-files", "write", proj, "GOALS.md"],
                     ["doc-files", "read", proj, "GOALS.md", "GOALS.md"], ["df", "-h"], ["clock", "x"],
                     ["tmux-check", "x"]):
            with self.assertRaises(ValueError, msg=argv):
                host.bridge_argv(argv)


if __name__ == "__main__":
    unittest.main()
