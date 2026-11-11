#!/usr/bin/env python3
"""Offline tests for the host kit (phase 5b K2, part 1): docker/host_exec.py's host.json, `kit-version`,
`host-config`, the allowed roots and the output cap, host.py's kit check, and
docker/install_host_bridge.sh against a temporary HOME (the real authorized_keys and kit are never
touched). Nothing here signals a process or runs claude/tmux."""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (hermetic env; before the AFClaude imports)
import host  # noqa: E402

HOST_EXEC = os.path.join(HERE, "docker", "host_exec.py")
INSTALL = os.path.join(HERE, "docker", "install_host_bridge.sh")
# every kit version and its whitelist hash: a command added without a version bump fails
# test_whitelist_hash_matches_the_version (docker/host_exec.py: HOST_KIT_VERSION, COMMANDS)
KIT_VERSIONS = {"1": "19f410841fbc971d"}
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

    def test_an_old_kit_denies_the_command(self):
        with self.answer(rc=126, err="host_exec: denied (not whitelisted)"):
            with self.assertRaises(host.HostKitError) as cm:
                host.kit_info()
        self.assertIn("re-run the host kit install", str(cm.exception))

    def test_garbage_answer(self):
        for out in ("", "not json", "[]", '{"kit_version": 1}'):
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
        os.makedirs(os.path.join(self.src, "docker"))
        for rel in ("docker/install_host_bridge.sh", "docker/host_exec.py", "ka_resume.sh"):
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
        path = os.path.join(self.src, "docker", "host_exec.py")
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
        with open(os.path.join(self.src, "docker", "host_exec.py"), "a") as fh:
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
        self.patch_kit('HOST_KIT_VERSION = "1"', 'HOST_KIT_VERSION = "99"')
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


if __name__ == "__main__":
    unittest.main()
