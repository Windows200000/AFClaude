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
# the current kit version with its whitelist hash and the sha256 of each kit file: a command added, or a change of
# host_exec.py or ka_resume.sh, without a version bump fails test_whitelist_hash_matches_the_version and
# test_kit_files_are_pinned_with_the_version (docker/host-kit/host_exec.py: HOST_KIT_VERSION, COMMANDS; M5)
KIT_VERSIONS = {"3": {"whitelist": "2d289ec6ac9ab583", "host_exec.py": "74f4c84c4a915b9456591a7d422af3f706b361e43565fa845b80ea4415c932d7",  # public-check: allow (sha256 pin)
                        "ka_resume.sh": "ce76821ea7a99fd5d14f26c3a228267d262e0193f0d88bd5f143cb0fe8ec257a"}}  # public-check: allow (sha256 pin)
SID = "abcdef01-0000-4000-8000-000000000001"


def write_json(path, obj, mode=0o600):
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as fh:
        if isinstance(obj, str):
            fh.write(obj)
        else:
            json.dump(obj, fh)
    os.chmod(path, mode)


def good_config(root, data, **kw):
    cfg = {"format": 2, "roots": [root], "repo": HERE, "claude": "/usr/bin/true", "data": data}
    cfg.update(kw)
    return cfg


def load_he(host_json, home=None, log=os.devnull):
    """docker/host_exec.py as a module, reading `host_json` (its env override is read at import)."""
    env = {"AFCLAUDE_HOST_JSON": host_json, "AFCLAUDE_HOST_EXEC_LOG": log}
    if home:
        env["HOME"] = home
    with mock.patch.dict(os.environ, env), mock.patch.dict(os.environ):
        os.environ.pop("SSH_ORIGINAL_COMMAND", None)
        spec = importlib.util.spec_from_file_location("host_exec_kit", HOST_EXEC)
        he = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(he)
    return he


def run_exec(command, host_json, stdin=b"", home=None, log=os.devnull):
    """The kit as sshd runs it, except the command comes as `--run <command>` and the env overrides are honoured:
    under sshd (SSH_ORIGINAL_COMMAND set) they are not (L2; test_the_env_overrides_are_ignored_under_sshd)."""
    env = {k: v for k, v in os.environ.items() if k != "SSH_ORIGINAL_COMMAND"}
    env.update(AFCLAUDE_HOST_JSON=host_json, AFCLAUDE_HOST_EXEC_LOG=log)
    if home:
        env["HOME"] = home
    return subprocess.run([sys.executable, HOST_EXEC, "--run", command], input=stdin, env=env, capture_output=True, timeout=60)


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="kit-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "root")
        self.data = os.path.join(self.tmp, "data")          # the container-writable directory
        os.mkdir(self.root)
        os.mkdir(self.data)
        self.hj = os.path.join(self.tmp, "host.json")
        write_json(self.hj, self.config())

    def config(self, **kw):
        return good_config(self.root, self.data, **kw)


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
        good = self.config()
        bad = {
            "not json": "{nope",
            "a list": "[]",
            "empty": "",
            "missing key": {k: v for k, v in good.items() if k != "repo"},
            "no data key": {k: v for k, v in good.items() if k != "data"},
            "data relative": dict(good, data="data"),
            "unknown key": dict(good, extra=1),
            "format 1": dict(good, format=1),
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

    def share(self):
        """host.json's own directory, as <share>/afclaude: a kit file path below it, so KIT_DIR/../.. is it."""
        share = os.path.join(self.tmp, "share")
        kit = os.path.join(share, "host-kit", "3")
        os.makedirs(kit)
        for name in ("host_exec.py", "ka_resume.sh"):
            shutil.copy(os.path.join(os.path.dirname(HOST_EXEC) if name == "host_exec.py" else HERE, name), kit)
        write_json(os.path.join(share, "host.json"), self.config())
        return share, os.path.join(kit, "host_exec.py")

    def test_log_is_beside_host_json_not_in_the_data_dir(self):
        """H2: <share>/afclaude/host_exec.log, with or without a usable config; nothing is written under the
        repo's data dir or the container-writable data directory."""
        share, kit = self.share()
        env = {"PATH": os.environ["PATH"], "SSH_ORIGINAL_COMMAND": "kit-version", "HOME": self.tmp}
        r = subprocess.run([sys.executable, kit], env=env, capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        log = os.path.join(share, "host_exec.log")
        with open(log) as fh:
            self.assertIn("OK kit-version", fh.read())
        self.assertEqual(os.stat(log).st_mode & 0o777, 0o600)
        self.assertEqual(os.listdir(self.data), [])
        os.unlink(os.path.join(share, "host.json"))
        r = subprocess.run([sys.executable, kit], env=env, capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 126)
        with open(log) as fh:
            self.assertIn("DENY host config", fh.read())

    def test_log_symlink_is_refused_not_followed(self):
        """H2: a symlink at the log path (to authorized_keys, say) is not written through."""
        share, kit = self.share()
        target = os.path.join(self.tmp, "authorized_keys")
        write_json(target, "ssh-ed25519 AAAA owner\n")
        os.symlink(target, os.path.join(share, "host_exec.log"))
        env = {"PATH": os.environ["PATH"], "SSH_ORIGINAL_COMMAND": "kit-version", "HOME": self.tmp}
        r = subprocess.run([sys.executable, kit], env=env, capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)                    # the command itself still runs
        self.assertIn(b"log not written", r.stderr)                    # and the failure is said, not swallowed
        with open(target) as fh:
            self.assertEqual(fh.read(), "ssh-ed25519 AAAA owner\n")

    def test_caller_text_is_escaped_in_the_log(self):
        """H2: a newline (or any control or non-ASCII character) in an argument can't start a new log line."""
        share, kit = self.share()
        evil = "ka_resume.sh --message 'x\nssh-ed25519 AAAAevil k\n\t\r\x1b[31m \u00e4' --session nope"
        env = {"PATH": os.environ["PATH"], "SSH_ORIGINAL_COMMAND": evil, "HOME": self.tmp}
        r = subprocess.run([sys.executable, kit], env=env, capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 126)
        with open(os.path.join(share, "host_exec.log")) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("\\n", lines[0])
        self.assertIn("ssh-ed25519 AAAAevil", lines[0])               # present, but inside the escaped line
        self.assertTrue(all(32 <= ord(c) < 127 for c in lines[0]))
        he = load_he(self.hj)
        self.assertEqual(he.printable("a\nb\x00c\u00e4\u20ac"), "a\\x0ab\\x00c\\xe4\\u20ac")

    def test_the_env_overrides_are_ignored_under_sshd(self):
        """L2: with SSH_ORIGINAL_COMMAND set (sshd) AFCLAUDE_HOST_JSON / _HOST_EXEC_LOG / _SESSIONS and a changed
        HOME do not take effect; without it (the tests, the install) they do."""
        share, kit = self.share()
        other = os.path.join(self.tmp, "other.json")
        write_json(other, self.config(roots=[self.tmp]))
        env = {"PATH": os.environ["PATH"], "SSH_ORIGINAL_COMMAND": "host-config get", "HOME": self.tmp,
               "AFCLAUDE_HOST_JSON": other, "AFCLAUDE_HOST_EXEC_LOG": os.path.join(self.tmp, "mine.log")}
        r = subprocess.run([sys.executable, kit], env=env, capture_output=True, timeout=60)
        self.assertEqual(json.loads(r.stdout)["roots"], [self.root])      # the file next to the kit, not `other`
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "mine.log")))
        self.assertTrue(os.path.exists(os.path.join(share, "host_exec.log")))
        r = run_exec("host-config get", other)                            # the test entry honours it
        self.assertEqual(json.loads(r.stdout)["roots"], [self.tmp])
        for argv in (["--run", "clock"], ["--print-version"]):             # install-only modes: not under sshd
            r = subprocess.run([sys.executable, kit, *argv], env=env, capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 126, argv)


class KitVersion(TempDir):
    def test_the_image_and_the_kit_agree(self):
        he = load_he(self.hj)
        self.assertEqual(host.HOST_KIT_VERSION, he.HOST_KIT_VERSION)
        self.assertEqual([n for n, _ in host.KIT_FILES], list(he.KIT_FILES))

    def test_whitelist_hash_matches_the_version(self):
        he = load_he(self.hj)
        self.assertEqual(KIT_VERSIONS.get(he.HOST_KIT_VERSION, {}).get("whitelist"), he.whitelist_hash(),
                         "the command list changed: bump HOST_KIT_VERSION and add the pair to KIT_VERSIONS")

    def test_kit_files_are_pinned_with_the_version(self):
        """M5: a change of host_exec.py or ka_resume.sh (which changes on main without the command list
        changing) fails here until HOST_KIT_VERSION is bumped and the new hashes are pinned."""
        he = load_he(self.hj)
        pinned = KIT_VERSIONS.get(he.HOST_KIT_VERSION, {})
        for name, rel in host.KIT_FILES:
            with open(os.path.join(HERE, rel), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), pinned.get(name),
                                 f"{rel} changed under kit version {he.HOST_KIT_VERSION}: bump HOST_KIT_VERSION in "
                                 "docker/host-kit/host_exec.py and host.py, pin the new hashes in KIT_VERSIONS")
        self.assertEqual(host.kit_source_hashes(), {n: pinned[n] for n, _ in host.KIT_FILES})

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
                                "commands": sorted(he.COMMANDS), "config_format": he.CONFIG_FORMAT,
                                "files": host.kit_source_hashes()})     # run from the checkout: the files are the image's

    def test_every_bridge_shape_the_image_builds_for_the_kit_is_accepted(self):
        for argv in (["kit-version"], ["host-config", "get"]):
            r = run_exec(" ".join(host.bridge_argv(argv)), self.hj)
            self.assertEqual(r.returncode, 0, (argv, r.stderr))

    def test_near_misses_are_denied(self):
        for cmd in ("kit-version now", "kit-version --json", "Kit-Version", "host-config", "host-config set",
                    "host-config set roots", "host-config set bogus /x", "host-config del roots", "host-config get x",
                    "host-config get roots", "host-config set format 2"):
            self.assertEqual(run_exec(cmd, self.hj).returncode, 126, cmd)
        for argv in (["kit-version", "x"], ["host-config"], ["host-config", "set", "bogus", "x"], ["host-config", "set", "roots"],
                     ["host-config", "set", "repo", "/x"]):
            with self.assertRaises(ValueError, msg=argv):
                host.bridge_argv(argv)


class ImageSide(unittest.TestCase):
    """host.check_kit: a kit that differs from HOST_KIT_VERSION, or does not answer, is an error."""

    def answer(self, rc=0, out="", err=""):
        return mock.patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], rc, out, err))

    def info(self, version, files=None):
        return json.dumps({"kit_version": version, "whitelist_hash": "x", "commands": [], "config_format": 2,
                           "files": files if files is not None else host.kit_source_hashes()})

    def serve(self, info, conf):
        """kit-version answers `info`, host-config get answers `conf` (a dict), over a fake ssh."""
        def fake(argv, **kw):
            out = info if argv[-1] == "kit-version" else json.dumps(conf)
            return subprocess.CompletedProcess(argv, 0, out, "")
        return mock.patch.object(host.subprocess, "run", fake)

    def test_check_host_compares_files_and_repo(self):
        """M5, M3: the host check (never import or a request) compares the kit's file hashes with the image's own
        copies and host.json's repo with AFCLAUDE_HOST_REPO; any difference is a HostKitError."""
        env = {"AFCLAUDE_IN_CONTAINER": "1", "AFCLAUDE_HOST_REPO": "/srv/repo"}
        with mock.patch.dict(os.environ, env):
            with self.serve(self.info(host.HOST_KIT_VERSION), {"repo": "/srv/repo/"}):
                self.assertEqual(host.check_host()["kit_version"], host.HOST_KIT_VERSION)
            bad = dict(host.kit_source_hashes(), **{"ka_resume.sh": "0" * 64})
            with self.serve(self.info(host.HOST_KIT_VERSION, bad), {"repo": "/srv/repo"}), \
                    self.assertRaises(host.HostKitError) as cm:
                host.check_host()
            self.assertIn("other files", str(cm.exception))
            for files in ({}, None):
                with self.serve(json.dumps({"kit_version": host.HOST_KIT_VERSION, "commands": [], "files": files}),
                                {"repo": "/srv/repo"}), self.assertRaises(host.HostKitError):
                    host.check_host()
            with self.serve(self.info(host.HOST_KIT_VERSION), {"repo": "/srv/other"}), \
                    self.assertRaises(host.HostKitError) as cm:
                host.check_host()
            self.assertIn("/srv/other", str(cm.exception))
            for conf in ({}, {"repo": 1}, []):
                with self.serve(self.info(host.HOST_KIT_VERSION), conf), self.assertRaises(host.HostKitError, msg=conf):
                    host.check_host()
            with self.serve(self.info("0"), {"repo": "/srv/repo"}), self.assertRaises(host.HostKitError):
                host.check_host()
            with self.answer(rc=126, err="host_exec: denied (not whitelisted)"):
                self.assertIsNone(host.check_host())           # the pre-kit bridge: reported by the first host check

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
        write_json(self.hj, self.config(roots=[self.root, self.other]))
        he = load_he(self.hj)
        self.assertEqual(he.under_roots(self.other), self.other)
        self.assertIsNone(he.under_roots(self.outside))

    def test_a_root_that_is_itself_a_symlink(self):
        link = os.path.join(self.tmp, "rootlink")
        os.symlink(self.root, link)
        write_json(self.hj, good_config(link, self.data))
        he = load_he(self.hj)
        self.assertEqual(he.under_roots(self.sub), self.sub)
        self.assertEqual(he.under_roots(os.path.join(link, "proj")), self.sub)
        self.assertIsNone(he.under_roots(self.outside))

    def test_the_data_dir_is_no_cwd_and_no_doc_dir(self):
        """H3: the container writes the data dir (and .claude/settings.json in it would load as project settings
        of a launch there). It is refused even when a root contains it, also through symlinks and `..`."""
        write_json(self.hj, self.config(roots=[self.tmp]))              # a root that contains root/, outside/ and data/
        he = load_he(self.hj)
        sub = os.path.join(self.data, "sub")
        os.mkdir(sub)
        via = os.path.join(self.root, "to-data")
        os.symlink(self.data, via)
        for cwd in (self.data, sub, self.data + "/", self.data + "/sub/..", via, os.path.join(via, "sub"),
                    os.path.join(self.root, "..", "data")):
            self.assertIsNone(he.under_roots(cwd), cwd)
            with self.assertRaises(SystemExit, msg=cwd):
                he.check_ka_resume(self.args(cwd))
            r = run_exec(f"doc-files read {shlex.quote(cwd)} PROGRESS.md", self.hj)
            self.assertEqual(r.returncode, 126, cwd)
        self.assertEqual(he.under_roots(self.root), self.root)          # its neighbours stay fine
        self.assertEqual(he.under_roots(self.root + "/proj"), self.root + "/proj")
        os.symlink(self.sub, os.path.join(self.data, "elsewhere"))      # a symlink in data to a root dir: resolved target
        self.assertEqual(he.under_roots(os.path.join(self.data, "elsewhere")), self.sub)

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


class HostConfigIsReadOnly(TempDir):
    """D-338 (review:k2 H1): host.json is written only by the owner's install on the host. The bridge has no
    `host-config set`: a container that could change it could point the kit at files it writes itself."""

    def test_every_set_shape_is_denied_and_the_file_stays(self):
        with open(self.hj) as fh:
            before = fh.read()
        for cmd in ("host-config set roots /tmp", "host-config set repo /tmp", f"host-config set claude {sys.executable}",
                    f"host-config set repo {self.data}", f"host-config set claude {self.data}/x.sh",
                    f"host-config set roots {self.data}", "host-config set data /tmp", "host-config set"):
            r = run_exec(cmd, self.hj)
            self.assertEqual(r.returncode, 126, (cmd, r.stderr))
            self.assertIn(b"not whitelisted", r.stderr)
            with open(self.hj) as fh:
                self.assertEqual(fh.read(), before, cmd)
        self.assertEqual(run_exec("host-config get", self.hj).returncode, 0)

    def test_the_image_has_no_such_shape_and_the_kit_has_no_writer(self):
        with self.assertRaises(ValueError):
            host.bridge_argv(["host-config", "set", "repo", "/x"])
        self.assertFalse(hasattr(host, "HOST_CONFIG_KEYS"))
        he = load_he(self.hj)
        self.assertFalse(hasattr(he, "config_set"))
        self.assertFalse(hasattr(he, "SET_KEYS"))

    def test_the_container_writable_data_dir_is_refused_for_every_path_host_json_names(self):
        """At read (host.json written by someone else, or a path turned into a symlink into the data dir)."""
        he = load_he(self.hj)
        link = os.path.join(self.tmp, "to-data")
        os.symlink(self.data, link)
        script = os.path.join(self.data, "x.sh")
        write_json(script, "#!/bin/sh\n", 0o755)
        for why, cfg in (("claude in data", self.config(claude=script)),
                         ("claude via symlink", self.config(claude=os.path.join(link, "x.sh"))),
                         ("repo is data", self.config(repo=self.data)),
                         ("repo below data", self.config(repo=os.path.join(self.data, "r"))),
                         ("root is data", self.config(roots=[self.data])),
                         ("root via symlink", self.config(roots=[link])),
                         ("root below data", self.config(roots=[os.path.join(self.data, "w")])),
                         ("config dir in data", self.config(config_dirs={"w": os.path.join(self.data, "c")}))):
            write_json(self.hj, cfg)
            with self.assertRaises(he.ConfigError, msg=why):
                he.load_config(self.hj)
            self.assert_denied_everywhere()
        # a root that CONTAINS the data dir is fine; the data dir itself is refused as a cwd (Roots)
        write_json(self.hj, self.config(roots=[self.tmp]))
        he.load_config(self.hj)

    def assert_denied_everywhere(self):
        for cmd in ("kit-version", "host-config get"):
            r = run_exec(cmd, self.hj)
            self.assertEqual(r.returncode, 126)
            self.assertIn(b"inside the data directory", r.stderr)


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

    def run_install(self, *extra, args=None, src=None, env_extra=None, path=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AFCLAUDE_", "XDG_", "SSH_"))}
        env["HOME"] = self.home
        env.update(env_extra or {})
        if path:
            env["PATH"] = path + ":" + env["PATH"]
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

    def derived(self, key):
        return subprocess.run(["ssh-keygen", "-y", "-f", key], capture_output=True, text=True, check=True).stdout.strip()

    def bridge_key(self):
        return os.path.join(self.data, "bridge", "host_bridge_ed25519")

    def make_key(self, comment, kind="ed25519"):
        os.makedirs(os.path.dirname(self.bridge_key()), exist_ok=True)
        if os.path.exists(self.bridge_key()):
            os.unlink(self.bridge_key())
        subprocess.run(["ssh-keygen", "-q", "-t", kind, "-N", "", "-C", comment, "-f", self.bridge_key()], check=True,
                       capture_output=True)

    def backups(self):
        return sorted(f for f in os.listdir(os.path.dirname(self.ak)) if f.startswith("authorized_keys.bak-afclaude-"))

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
            self.assertEqual(json.load(fh), {"format": 2, "roots": [self.root], "repo": self.src, "claude": self.claude,
                                             "data": self.data})
        # the key lives in the data dir
        key = os.path.join(self.data, "bridge", "host_bridge_ed25519")
        self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(key)).st_mode & 0o777, 0o700)
        self.assertFalse(os.path.exists(os.path.join(self.src, "docker", "secrets")))
        # authorized_keys: the other keys and the OLD line stay, one new line points at current/
        lines = self.lines()
        self.assertEqual(lines[:2], [self.OTHER, self.OLD])
        self.assertEqual(len(lines), 3)
        blob = self.derived(key).split()[1]
        new = lines[2]
        self.assertEqual(new, f'command="{kits}/current/host_exec.py",from="172.28.0.0/24",restrict ssh-ed25519 {blob} '
                              'afclaude-host-bridge-kit')

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
        self.assertIn('from="10.20.0.0/16",restrict ', self.lines()[-1])
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
        self.assertEqual(len(self.backups()), 2)                      # one timestamped backup per run
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
        self.patch_kit('HOST_KIT_VERSION = "3"', 'HOST_KIT_VERSION = "99"')
        # the stub ssh-keygen cannot read the key from authorized_keys: the line check fails
        stub = os.path.join(self.tmp, "bin")
        os.mkdir(stub)
        real = shutil.which("ssh-keygen")
        write_json(os.path.join(stub, "ssh-keygen"),
                   f'#!/bin/sh\ncase "$2" in *authorized_keys) exit 1;; esac\nexec {real} "$@"\n', 0o755)
        r = self.run_install(args=["--data", self.data], path=stub)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("back as they were", r.stderr)
        self.assertEqual(self.lines(), before_lines)
        self.assertEqual(os.readlink(os.path.join(self.share, "host-kit", "current")), before_link)

    def test_a_failure_after_the_authorized_keys_write_and_the_link_switch_is_undone(self):
        """M2: a plain `set -e` failure (not one of the script's own checks) at the link switch: the file and
        the link are back, nothing is left over."""
        self.assertEqual(self.run_install().returncode, 0)
        before_lines, before_link = self.lines(), os.readlink(os.path.join(self.share, "host-kit", "current"))
        self.patch_kit('HOST_KIT_VERSION = "3"', 'HOST_KIT_VERSION = "99"')
        stub = os.path.join(self.tmp, "bin")
        os.mkdir(stub)
        real, once = shutil.which("mv"), os.path.join(self.tmp, "mv-failed")
        # fail the link switch (mv -T .current.NNN .../current) the first time only: the undo uses mv too
        write_json(os.path.join(stub, "mv"),
                   f'#!/bin/sh\ncase "$3" in */host-kit/current) [ -e {once} ] || {{ : > {once}; exit 1; }};; esac\n'
                   f'exec {real} "$@"\n', 0o755)
        r = self.run_install(args=["--data", self.data], path=stub)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(once), "the stub never ran: the test does not exercise the failure")
        self.assertIn("back as they were", r.stderr)
        self.assertEqual(self.lines(), before_lines)
        kits = os.path.join(self.share, "host-kit")
        self.assertEqual(os.readlink(os.path.join(kits, "current")), before_link)
        self.assertEqual([f for f in os.listdir(kits) if f.startswith(".")], [])

    def test_a_failure_on_a_first_install_removes_the_link_and_restores_the_file(self):
        before = self.lines()
        stub = os.path.join(self.tmp, "bin")
        os.mkdir(stub)
        real = shutil.which("ssh-keygen")
        write_json(os.path.join(stub, "ssh-keygen"),
                   f'#!/bin/sh\ncase "$2" in *authorized_keys) exit 1;; esac\nexec {real} "$@"\n', 0o755)
        r = self.run_install(path=stub)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.lines(), before)
        kits = os.path.join(self.share, "host-kit")
        self.assertFalse(os.path.lexists(os.path.join(kits, "current")))
        self.assertEqual([f for f in os.listdir(kits) if f.startswith(".")], [])

    def test_the_install_works_from_an_ssh_login(self):
        """M1: an SSH login (SSH_CONNECTION, SSH_CLIENT, SSH_TTY set) is where the owner runs this; only a forced
        command call (SSH_ORIGINAL_COMMAND) is refused the install modes."""
        r = self.run_install(env_extra={"SSH_CONNECTION": "10.0.0.1 5555 10.0.0.2 22", "SSH_CLIENT": "10.0.0.1 5555 22",
                                        "SSH_TTY": "/dev/pts/9"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.lines()), 3)
        plain = {k: v for k, v in os.environ.items() if k != "SSH_ORIGINAL_COMMAND"}
        for argv in (["--print-version"], ["--check-config", os.path.join(self.share, "host.json")]):
            r = subprocess.run([sys.executable, HOST_EXEC, *argv], env=dict(plain, SSH_CONNECTION="a b c d", SSH_ORIGINAL_COMMAND=""),
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 126, argv)                        # a forced-command call: refused
            r = subprocess.run([sys.executable, HOST_EXEC, *argv], env=dict(plain, SSH_CONNECTION="a b c d"),
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, (argv, r.stderr))              # an interactive login: allowed

    def test_a_forced_command_environment_refuses_the_install_modes(self):
        r = self.run_install(env_extra={"SSH_ORIGINAL_COMMAND": "kit-version"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.lines(), [self.OTHER, self.OLD])
        self.assertFalse(os.path.lexists(os.path.join(self.share, "host-kit", "current")))

    def test_the_public_key_is_derived_never_copied_from_the_data_dir(self):
        """H4: a .pub in the (container-writable) data dir is not read; a second line in it never reaches
        authorized_keys, and an install that finds a hostile .pub still produces exactly one new line."""
        self.assertEqual(self.run_install().returncode, 0)
        base = [self.OTHER, self.OLD]
        evil = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEVILEVILEVILEVILEVILEVILEVILEVILEVILEVILEV x"
        key = self.bridge_key()
        for pub in (self.derived(key) + "\n" + evil + "\n", evil + "\n", "ssh-ed25519\tAAAA\tx\n", "garbage", ""):
            with open(key + ".pub", "w") as fh:
                fh.write(pub)
            r = self.run_install(args=["--data", self.data])
            self.assertEqual(r.returncode, 0, (pub, r.stdout + r.stderr))
            lines = self.lines()
            self.assertEqual(len(lines), 3, pub)
            self.assertEqual(lines[:2], base)
            self.assertNotIn("EVIL", "\n".join(lines))
            self.assertTrue(lines[2].endswith(self.derived(key).split()[1] + " afclaude-host-bridge-kit"))
            with open(key + ".pub") as fh:
                self.assertEqual(fh.read(), pub, "the install must not touch it")

    def test_a_private_key_whose_comment_is_hostile_is_refused_or_neutralised(self):
        """H4: the comment lives inside the private key file (container-controlled). A plain one is replaced by the
        script's own; one with a tab or newline is refused, authorized_keys untouched."""
        before = self.lines()
        self.make_key("owner@laptop restrict,command=\"x\"")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.lines()[-1].endswith("afclaude-host-bridge-kit"))
        self.assertNotIn("owner@laptop", self.lines()[-1])
        self.assertNotIn("command=\"x\"", self.lines()[-1])
        os.chmod(self.ak, 0o600)
        with open(self.ak, "w") as fh:
            fh.write("\n".join(before) + "\n")
        for comment in ("a\tb", "a\nssh-ed25519 AAAAevil c"):
            try:
                self.make_key(comment)
            except subprocess.CalledProcessError:
                continue                                   # this ssh-keygen refuses such a comment itself
            r = self.run_install()
            joined = "\n".join(self.lines())
            self.assertNotIn("evil", joined)               # whichever way ssh-keygen -y prints it (this one: no comment at all)
            self.assertNotIn("a\tb", joined)
            if r.returncode == 0:
                self.assertEqual(len(self.lines()), 3)
                with open(self.ak, "w") as fh:
                    fh.write("\n".join(before) + "\n")
            else:
                self.assertIn("not one plain line", r.stderr)
                self.assertEqual(self.lines(), before)

    def test_a_derived_public_key_that_is_not_one_plain_line_is_refused(self):
        """H4: whatever `ssh-keygen -y` prints (older/newer versions print the container-chosen comment), a result
        with a second line, a tab, a CR or a non-base64 body stops the install before anything is changed."""
        self.assertEqual(self.run_install().returncode, 0)
        before = self.lines()
        blob = self.derived(self.bridge_key()).split()[1]
        stub = os.path.join(self.tmp, "bin")
        os.mkdir(stub)
        real = shutil.which("ssh-keygen")
        for printed in (f"ssh-ed25519 {blob} c\\nssh-ed25519 AAAAevil", f"ssh-ed25519\\t{blob}", f"ssh-ed25519 {blob}\\tx",
                        f"ssh-ed25519 {blob} c\\r", f"ssh-ed25519 {blob}x!", f"ssh-rsa {blob}", "", f'command=\\"x\\" ssh-ed25519 {blob}'):
            write_json(os.path.join(stub, "ssh-keygen"),
                       f'#!/bin/sh\ncase "$1" in -y) printf \'{printed}\\n\';; *) exec {real} "$@";; esac\n', 0o755)
            r = self.run_install(args=["--data", self.data], path=stub)
            self.assertEqual(r.returncode, 1, (printed, r.stdout))
            self.assertIn("public key", r.stderr)
            self.assertEqual(self.lines(), before, printed)

    def test_a_key_that_is_not_ed25519_or_is_a_symlink_is_refused(self):
        before = self.lines()
        self.make_key("x", kind="ecdsa")
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("ssh-ed25519", r.stderr)
        self.assertEqual(self.lines(), before)
        os.unlink(self.bridge_key())
        victim = os.path.join(self.tmp, "victim")
        os.symlink(victim, self.bridge_key())              # dangling: ssh-keygen would write through it
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(victim))
        self.assertIn("symlink", r.stderr)

    def test_paths_inside_the_data_dir_are_refused_at_install(self):
        """H1/H3: the Claude binary, a root or the repo inside the container-writable data directory, and a
        host.json that names another data directory than the one this install was given."""
        script = os.path.join(self.data, "x.sh")
        write_json(script, "#!/bin/sh\n", 0o755)
        for args in (["--root", self.root, "--data", self.data, "--claude", script],
                     ["--root", self.data, "--data", self.data, "--claude", self.claude],
                     ["--root", self.root, "--data", self.src, "--claude", self.claude]):      # data = the repo
            r = self.run_install(args=args)
            self.assertEqual(r.returncode, 1, (args, r.stdout))
            self.assertIn("data", r.stderr)
            self.assertFalse(os.path.exists(os.path.join(self.share, "host.json")), args)
            self.assertEqual(self.lines(), [self.OTHER, self.OLD])
        self.assertEqual(self.run_install().returncode, 0)
        other = os.path.join(self.tmp, "other-data")
        os.mkdir(other)
        r = self.run_install(args=["--data", other])
        self.assertEqual(r.returncode, 1)
        self.assertIn("names the data directory", r.stderr)

    def test_old_backups_are_pruned_to_ten(self):
        for i in range(14):
            path = f"{self.ak}.bak-afclaude-2020010{i % 10}-0000{i:02d}-1"
            write_json(path, "old\n")
            os.utime(path, (1_600_000_000 + i, 1_600_000_000 + i))
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(len(self.backups()), 10)


LEGACY_EXEC = os.path.join(HERE, "docker", "host_exec.py")
# sha256 of docker/host_exec.py as main has it: the owner's live authorized_keys line runs THAT file from the
# main checkout, so merging a change of it would replace the live bridge at once, before any install. It is
# removed only after the owner removed the old line (docs/phase5b_plan.md K2).
LEGACY_SHA256 = "7c2e2e1e834a6bb1d3cfbde63b4d9cd9ac4eac1fb5752407a3ce93f5375ecc39"
KIT_ONLY = [["kit-version"], ["host-config", "get"], ["tmux-check"], ["df"],
            ["clock"], ["sessions", "default"], ["transcripts", "default", "--changed-since", "0"],
            ["doc-files", "read", "/x", "PROGRESS.md"], ["claude", "auth", "status"], ["claude", "doctor"]]


def run_legacy(command):
    env = dict(os.environ, SSH_ORIGINAL_COMMAND=command, AFCLAUDE_HOST_EXEC_LOG=os.devnull)
    return subprocess.run([sys.executable, LEGACY_EXEC], input=b"", env=env, capture_output=True, timeout=60)


class LegacyBridge(unittest.TestCase):
    """The live bridge is docker/host_exec.py of the main checkout (the owner's authorized_keys line): it stays
    as it is until the kit is installed, and the image works with it."""

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

    def test_a_new_kit_command_is_known_to_the_legacy_list(self):
        he = load_he(os.devnull)
        self.assertEqual(set(he.COMMANDS) - host.LEGACY_HEADS,
                         {"kit-version", "host-config", "transcripts", "sessions", "doc-files", "tmux-check", "df",
                          "clock"}, "a new kit command: the pre-kit bridge denies it (rc 126 not whitelisted -> HostKitError)")

    def test_the_runner_shapes_are_accepted_by_the_legacy_bridge(self):
        shapes = ([["tmux", verb, *rest] for verb, rest in (
            ("has-session", ["-t", "=afc-00000000"]), ("list-panes", ["-s", "-t", "=afc-00000000", "-F", "#{pane_pid}"]),
            ("list-clients", ["-t", "=afc-00000000", "-F", "#{client_activity}"]),
            ("capture-pane", ["-e", "-p", "-t", "=afc-00000000:"]), ("kill-session", ["-t", "=afc-00000000"]),
            ("rename-session", ["-t", "=afc-00000000", "afc-00000001"]))] + [["proc-snapshot"]])
        for argv in shapes:
            remote = host.bridge_argv(argv)
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
            host.bridge_argv(argv)      # the image builds the shape; the legacy file's heads are LEGACY_HEADS (above)

    def test_kit_only_shapes_are_refused_by_the_legacy_bridge_and_become_a_kit_error(self):
        """M4: no pre-check round trip. The command is sent once; the old bridge's rc 126 "not whitelisted"
        becomes a HostKitError naming the install."""
        for argv in KIT_ONLY:
            remote = host.bridge_argv(argv)
            r = run_legacy(shlex.join(remote))
            self.assertEqual(r.returncode, 126, remote)
            self.assertIn(b"not whitelisted", r.stderr)
            answer = subprocess.CompletedProcess([], r.returncode, r.stdout.decode(), r.stderr.decode())
            with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": "1"}), \
                    mock.patch.object(host.subprocess, "run", return_value=answer) as run, \
                    self.assertRaises(host.HostKitError, msg=remote) as cm:
                host.run_on_host(argv)
            self.assertEqual(run.call_count, 1)
            self.assertIn("host kit", str(cm.exception))
            self.assertIn("install_host_bridge.sh", str(cm.exception))


class RunOnHost(unittest.TestCase):
    """host.run_on_host in the container: one ssh call per command, the caller's timeout, no kit pre-check;
    host.repo_dir() never calls the bridge (review:k2 M3, M4)."""
    ENV = {"AFCLAUDE_IN_CONTAINER": "1", "AFCLAUDE_HOST_REPO": "/srv/repo/"}

    def test_one_call_with_the_callers_timeout(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv[-1], kw.get("timeout")))
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(host.subprocess, "run", fake_run):
            r = host.run_on_host(["tmux", "has-session", "-t", "=afc-a"], timeout=7)
        self.assertEqual(calls, [("tmux-has afc-a", 7)])
        self.assertEqual((r.returncode, r.stdout), (0, "ok"))

    def test_a_timeout_is_the_callers_own_and_comes_up_unchanged(self):
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch.object(host.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 7)) as run, \
                self.assertRaises(subprocess.TimeoutExpired):
            host.run_on_host(["proc-snapshot"], timeout=7)
        self.assertEqual(run.call_count, 1)                    # no kit-version round trip before it

    def test_only_the_not_whitelisted_denial_is_a_kit_error(self):
        for rc, err, kit in ((126, "host_exec: denied (not whitelisted)", True), (126, "host_exec: denied (not a claude pid)", False),
                             (1, "not whitelisted", False), (255, "ssh: refused", False)):
            answer = subprocess.CompletedProcess([], rc, "", err)
            with mock.patch.dict(os.environ, self.ENV), mock.patch.object(host.subprocess, "run", return_value=answer):
                if kit:
                    with self.assertRaises(host.HostKitError):
                        host.run_on_host(["kill-claude", "5"])
                else:
                    self.assertEqual(host.run_on_host(["kill-claude", "5"]).returncode, rc)

    def test_repo_dir_is_the_env_and_never_calls_the_bridge(self):
        with mock.patch.dict(os.environ, self.ENV), mock.patch.object(host.subprocess, "run") as run:
            self.assertEqual(host.repo_dir(), "/srv/repo")
            self.assertEqual(host.repo_dir(), "/srv/repo")
        run.assert_not_called()
        for bad in ("", "relative"):
            with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": "1", "AFCLAUDE_HOST_REPO": bad}), \
                    mock.patch.object(host.subprocess, "run") as run, self.assertRaises(host.HostRepoError):
                host.repo_dir()
            run.assert_not_called()

    def test_importing_the_modules_that_read_repo_dir_makes_no_bridge_call(self):
        """M3: keepalive.py and the rest call repo_dir() at import; with a bridge that is down they must still
        import (D-257: the runner records the alert and pauses, an import error would hide it)."""
        code = ("import subprocess, sys\n"
                "def boom(*a, **k): raise SystemExit('bridge called at import')\n"
                "subprocess.run = boom; subprocess.Popen = boom\n"
                "import testenv, host, keepalive, prompts, decisions, questions, actions, project_docs, nightly_tasks\n"
                "print(host.repo_dir())\n")
        env = dict(os.environ, **self.ENV)
        r = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertEqual(r.stdout.strip().splitlines()[-1], os.path.normpath(HERE))     # testenv's AFCLAUDE_HOST_REPO


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

    def test_unreadable_things_are_reported_not_skipped(self):
        """L4 (D-235): a missing projects dir, a directory or a file that can't be read come back in `errors`."""
        self.tx("a.jsonl", [self.user("one")])
        out = self.ok_json("transcripts default --changed-since 0")
        self.assertEqual((out["errors"], len(out["files"])), ([], 1))
        with mock.patch.object(self.he, "read_head", side_effect=PermissionError(13, "Permission denied")):
            out = json.loads(self.he.transcripts_json(["default", "--changed-since", "0"]))
        self.assertEqual(out["files"], [])
        self.assertEqual(out["errors"][0]["error"], "Permission denied")
        self.assertTrue(out["errors"][0]["path"].endswith("a.jsonl"))
        shutil.rmtree(os.path.join(self.cdir, "projects"))
        out = self.ok_json("transcripts default --changed-since 0")
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("No such file", out["errors"][0]["error"])

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
        write_json(self.hj, self.config(config_dirs={"work-2": other}))
        self.assertEqual(self.ok_json("transcripts work-2 --changed-since 0")["files"], [])
        self.assertEqual(self.ok_json("sessions work-2")["files"], [])
        he = load_he(self.hj, home=self.home)
        self.assertEqual(he.cfg().config_dirs, (("work-2", other),))
        self.assertEqual(json.loads(he.config_text(he.cfg()))["config_dirs"], {"work-2": other})
        for cmd in ("transcripts nope --changed-since 0", "sessions nope", "sessions ../x", "sessions Default",
                    "transcripts /etc --changed-since 0", "sessions default extra"):
            self.assertEqual(self.call(cmd).returncode, 126, cmd)
        for bad in ({"default": other}, {"X": other}, {"a": "rel"}, {"a": other + "/"}, ["a"]):
            write_json(self.hj, self.config(config_dirs=bad))
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
        write_json(self.hj, self.config(repo=repo))
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
        write_json(self.hj, self.config(claude=stub))
        for cmd, want in (("claude auth status", b"args: auth status\n"), ("claude doctor", b"args: doctor\n")):
            r = self.call(cmd)
            self.assertEqual((r.returncode, r.stdout), (0, want), r.stderr)
        for cmd in ("claude auth", "claude auth login", "claude auth status --json", "claude auth logout",
                    "claude doctor --fix", "claude doctor x", "claude config", "claude mcp list"):
            self.assertEqual(self.call(cmd).returncode, 126, cmd)

    def test_headless_claude_runs_in_the_kits_own_directory_with_user_settings_only(self):
        """H3: -p, rc-send and the checks never run in a directory the container writes (the data dir's
        .claude/settings.json would load as project settings there)."""
        stub = os.path.join(self.tmp, "claude-stub")
        write_json(stub, '#!/bin/sh\necho "cwd: $(pwd -P)"; echo "args: $*"; cat >/dev/null\n', 0o755)
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "data", ".claude"))
        write_json(os.path.join(repo, "data", ".claude", "settings.json"), {"hooks": {"SessionStart": []}})
        write_json(self.hj, self.config(claude=stub, repo=repo))
        safe = os.path.join(self.home, ".local", "state", "afclaude", "cwd")
        usage = "claude -p --no-session-persistence --permission-mode dontAsk /usage"
        haiku = "claude -p --model haiku --no-session-persistence --output-format json"
        for cmd, args in ((usage, "args: -p --setting-sources user --no-session-persistence --permission-mode dontAsk /usage"),
                          (haiku, "args: -p --setting-sources user --model haiku --no-session-persistence --output-format json"),
                          ("claude doctor", "args: doctor"), ("claude agents --json", "args: agents --json")):
            r = self.call(cmd, stdin=b"hi")
            self.assertEqual(r.returncode, 0, (cmd, r.stderr))
            self.assertEqual(r.stdout.decode().splitlines(), [f"cwd: {safe}", args], cmd)
        self.assertEqual(os.stat(safe).st_mode & 0o777, 0o700)
        for bad in (0o755, 0o770):                          # somebody else could write it: refused
            os.chmod(safe, bad)
            r = self.call("claude doctor")
            self.assertEqual(r.returncode, 126, bad)
            self.assertIn(b"mode 0700", r.stderr)
        os.chmod(safe, 0o700)
        os.rmdir(safe)
        os.symlink(os.path.join(repo, "data"), safe)        # a symlink into the data dir: refused, not followed
        self.assertEqual(self.call("claude doctor").returncode, 126)

    def test_rc_send_gets_the_user_settings_only_flag(self):
        sessions = os.path.join(self.tmp, "sessions")
        os.mkdir(sessions)
        with open(os.path.join(sessions, "1.json"), "w") as fh:
            json.dump({"pid": os.getpid(), "name": "rc-a", "entrypoint": "sdk-cli", "status": "idle"}, fh)
        self.he.SESSIONS = sessions
        cmd, _ = self.he.rc_send_cmd(["rc-a"], b"hello")
        self.assertEqual(cmd[1:4], ["-p", "--setting-sources", "user"])
        self.assertEqual(cmd[4:], host.RC_SEND_FLAGS[1:])

    def test_append_note_refuses_a_symlink_and_appends_otherwise(self):
        """H1/H2: PROGRESS.md as a symlink (to authorized_keys, say) is not written through."""
        repo = os.path.join(self.tmp, "repo")
        os.mkdir(repo)
        write_json(self.hj, self.config(repo=repo))
        r = run_exec("append-note PROGRESS.md", self.hj, stdin=b"- a\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(repo, "PROGRESS.md")) as fh:
            self.assertEqual(fh.read(), "- a\n")
        target = os.path.join(self.tmp, "authorized_keys")
        write_json(target, "owner\n")
        os.symlink(target, os.path.join(repo, "ALERTS.md"))
        r = run_exec("append-note ALERTS.md", self.hj, stdin=b"ssh-ed25519 AAAA evil\n")
        self.assertEqual(r.returncode, 126)
        with open(target) as fh:
            self.assertEqual(fh.read(), "owner\n")

    def test_check_commands_have_a_timeout(self):
        stub = os.path.join(self.tmp, "claude-stub")
        write_json(stub, "#!/bin/sh\nsleep 30\n", 0o755)
        write_json(self.hj, self.config(claude=stub))
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
