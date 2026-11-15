#!/usr/bin/env python3
"""Offline tests for host.py (host path vs SSH bridge) and its fit with
docker/host_exec.py's whitelist. No real ssh: a stub `ssh` on PATH records the
remote command, or hands it to host_exec.py exactly as sshd's forced command
would ($SSH_ORIGINAL_COMMAND). host_exec.py logs to a temp file here. Nothing
here kills, launches or appends to a real file."""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import host  # noqa: E402
import keepalive as ka  # noqa: E402

_CFG_DIR = tempfile.TemporaryDirectory()


import pacing as budget  # noqa: E402

_OLD_BUDGET = (budget.SAMPLES_FILE, budget.FIRE_FILES)


def setUpModule():
    # the last-mile test below is about the bridge; pin the linear budget rule and point the
    # budget model's files (the "auto" last mile reads the ratio) away from this host's
    # sampler data (the budget model is tested elsewhere)
    testenv.setcfg(usage_model="linear")
    budget.SAMPLES_FILE = os.path.join(_CFG_DIR.name, "no_samples.jsonl")
    budget.FIRE_FILES = []


def tearDownModule():
    testenv.clear_settings()
    budget.SAMPLES_FILE, budget.FIRE_FILES = _OLD_BUDGET
    _CFG_DIR.cleanup()

HOST_EXEC = os.path.join(HERE, "docker", "host-kit", "host_exec.py")
LEGACY_HOST_EXEC = os.path.join(HERE, "docker", "host_exec.py")   # the pre-kit bridge (main's): BridgeHardening tests that one


class Env:
    """Temporarily set/unset environment variables."""
    def __init__(self, **kv):
        self.kv, self.old = kv, {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


TEST_HOME = os.path.join(testenv.DIR, "home")      # the kit makes its own working dir under HOME: never the real one
os.makedirs(TEST_HOME, exist_ok=True)


def run_host_exec(command, stdin=b"", log=None):
    """The kit with `command` as the client's command (`--run`: under sshd the env overrides are ignored)."""
    env = {k: v for k, v in os.environ.items() if k != "SSH_ORIGINAL_COMMAND"}
    env.update(AFCLAUDE_HOST_EXEC_LOG=log or os.devnull, HOME=TEST_HOME)
    return subprocess.run([sys.executable, HOST_EXEC, "--run", command], input=stdin, env=env, capture_output=True, timeout=60)


def run_legacy_exec(command, stdin=b"", log=None):
    """The pre-kit bridge with `command` as SSH_ORIGINAL_COMMAND."""
    env = dict(os.environ, SSH_ORIGINAL_COMMAND=command, AFCLAUDE_HOST_EXEC_LOG=log or os.devnull)
    return subprocess.run([sys.executable, LEGACY_HOST_EXEC], input=stdin, env=env, capture_output=True, timeout=60)


class InContainer(unittest.TestCase):
    def test_flag(self):
        for v, want in ((None, False), ("", False), ("0", False), ("1", True), ("yes", True)):
            with Env(AFCLAUDE_IN_CONTAINER=v):
                self.assertEqual(host.in_container(), want, v)


class RepoPaths(unittest.TestCase):
    """K1: the code runs from /app in the image, so what the host must understand comes from
    repo_dir() (AFCLAUDE_HOST_REPO in the container), never from the code's own directory."""
    HOST_REPO = "/srv/host-checkout/AFClaude"

    def test_on_the_host_it_is_the_code_directory(self):
        with Env(AFCLAUDE_IN_CONTAINER=None, AFCLAUDE_HOST_REPO="/ignored"):
            self.assertEqual(host.repo_dir(), host.HERE)
            self.assertEqual(host.local_path("/anywhere/x"), "/anywhere/x")
            self.assertEqual(host.to_local("cd /anywhere && x"), "cd /anywhere && x")

    def test_in_the_container_it_is_the_env_and_a_missing_one_raises(self):
        with Env(AFCLAUDE_IN_CONTAINER="1", AFCLAUDE_HOST_REPO=self.HOST_REPO + "/"):
            self.assertEqual(host.repo_dir(), self.HOST_REPO)
        for bad in (None, "", "relative/path"):
            with Env(AFCLAUDE_IN_CONTAINER="1", AFCLAUDE_HOST_REPO=bad):
                with self.assertRaises(host.HostRepoError):
                    host.repo_dir()

    def test_local_path_maps_only_the_repo_and_below(self):
        with Env(AFCLAUDE_IN_CONTAINER="1", AFCLAUDE_HOST_REPO=self.HOST_REPO):
            self.assertEqual(host.local_path(self.HOST_REPO), host.HERE)
            self.assertEqual(host.local_path(self.HOST_REPO + "/PROGRESS.md"), host.HERE + "/PROGRESS.md")
            for other in (self.HOST_REPO + "-wt-x", self.HOST_REPO + "-wt-x/PROGRESS.md", "/srv/host-checkout", ""):
                self.assertEqual(host.local_path(other), other)

    def test_to_local_rewrites_a_stored_command_by_whole_prefix(self):
        with Env(AFCLAUDE_IN_CONTAINER="1", AFCLAUDE_HOST_REPO=self.HOST_REPO):
            self.assertEqual(host.to_local(f"cd {self.HOST_REPO} && python3 usage_review.py --chain >> {self.HOST_REPO}/data/x.log"),
                             f"cd {host.HERE} && python3 usage_review.py --chain >> {host.HERE}/data/x.log")
            self.assertEqual(host.to_local(f"python3 {self.HOST_REPO}-wt-k1/x.py"), f"python3 {self.HOST_REPO}-wt-k1/x.py")

    def test_the_own_project_is_found_by_the_host_path(self):
        """A DB whose AFClaude project has the host path is found from a /app-style install."""
        import store
        testenv.fresh_db(self)
        conn = store.connect()
        try:
            store.add_project(conn, "k1-own", path=self.HOST_REPO)
            with Env(AFCLAUDE_IN_CONTAINER="1", AFCLAUDE_HOST_REPO=self.HOST_REPO):
                self.assertEqual(store.get_project(conn, host.repo_dir(), nearest=False)["name"], "k1-own")
                self.assertIsNone(store.get_project(conn, host.HERE, nearest=False))
        finally:
            conn.close()

    def test_the_install_wide_own_project_flag_follows_the_host_path(self):
        """permissions.afclaude_project_id compares with repo_dir(), not with the code directory (/app)."""
        import permissions
        import store
        testenv.fresh_db(self)
        conn = store.connect()
        try:
            pid = int(store.add_project(conn, "k1-own", path=self.HOST_REPO)["id"])
            with Env(AFCLAUDE_IN_CONTAINER="1", AFCLAUDE_HOST_REPO=self.HOST_REPO):
                self.assertEqual(permissions.afclaude_project_id(conn), pid)
            with Env(AFCLAUDE_IN_CONTAINER=None, AFCLAUDE_HOST_REPO=None):
                self.assertIsNone(permissions.afclaude_project_id(conn))
        finally:
            conn.close()


class BridgeArgv(unittest.TestCase):
    def test_claude_and_ka_resume(self):
        self.assertEqual(host.bridge_argv(["claude", "agents", "--json", "--all"]), ["claude", "agents", "--json", "--all"])
        self.assertEqual(host.bridge_argv(["/home/x/.local/bin/claude", "stop", "abcd1234"]), ["claude", "stop", "abcd1234"])
        self.assertEqual(host.bridge_argv([ka.KA_RESUME, "--session", "u", "--message", "hi there"]),
                         ["ka_resume.sh", "--session", "u", "--message", "hi there"])

    def test_tmux_shapes(self):
        self.assertEqual(host.bridge_argv(["tmux", "has-session", "-t", "=afc-0123abcd"]), ["tmux-has", "afc-0123abcd"])
        self.assertEqual(host.bridge_argv(["tmux", "kill-session", "-t", "=afc-0123abcd"]), ["tmux-kill", "afc-0123abcd"])
        self.assertEqual(host.bridge_argv(["tmux", "list-panes", "-s", "-t", "=afc-0123abcd", "-F", "#{pane_pid}"]),
                         ["tmux-panes", "afc-0123abcd"])
        self.assertEqual(host.bridge_argv(["tmux", "list-clients", "-t", "=afc-0123abcd", "-F", "#{client_activity}"]),
                         ["tmux-clients", "afc-0123abcd"])
        self.assertEqual(host.bridge_argv(["tmux", "capture-pane", "-e", "-p", "-t", "=afc-0123abcd:"]),
                         ["tmux-capture", "afc-0123abcd"])
        with self.assertRaises(ValueError):
            host.bridge_argv(["tmux", "capture-pane", "-e", "-p", "-t", "=Claude:"])

    def test_readable_names_and_rename(self):
        """3b S2: the readable afc-... names next to today's ka-<id8>, and the one-time rename."""
        n = "afc-afclaude-task-manager"
        self.assertEqual(host.bridge_argv(["tmux", "has-session", "-t", "=" + n]), ["tmux-has", n])
        self.assertEqual(host.bridge_argv(["tmux", "capture-pane", "-e", "-p", "-t", f"={n}:"]), ["tmux-capture", n])
        self.assertEqual(host.bridge_argv(["tmux", "list-panes", "-s", "-t", "=afc-review-2", "-F", "#{pane_pid}"]),
                         ["tmux-panes", "afc-review-2"])
        self.assertEqual(host.bridge_argv(["tmux", "rename-session", "-t", "=afc-0123abcd", n]),
                         ["tmux-rename", "afc-0123abcd", n])
        self.assertEqual(host.bridge_argv(["tmux", "rename-session", "-t", "=" + n, "afc-0123abcd"]),
                         ["tmux-rename", n, "afc-0123abcd"])           # the rollback (plan §3.4)
        for bad in ("=afc-a;b", "=afc-a b", "=afc-Upper", "=afc-" + "x" * 37, "=afc-", "=afc-x\n", "=afc-x:"):
            with self.assertRaises(ValueError, msg=bad):
                host.bridge_argv(["tmux", "has-session", "-t", bad])
        for argv in (["tmux", "rename-session", "-t", "afc-0123abcd", n],          # no exact-match "="
                     ["tmux", "rename-session", "-t", "=afc-0123abcd", "=" + n],
                     ["tmux", "rename-session", "-t", "=afc-0123abcd", "Claude"],
                     ["tmux", "rename-session", "-t", "=Claude", n],
                     ["tmux", "rename-session", "-t", "=afc-0123abcd", n, "x"]):
            with self.assertRaises(ValueError, msg=argv):
                host.bridge_argv(argv)

    def test_bridge_only_shapes_pass(self):
        for argv in (["kill-claude", "123"], ["claude-env", "123"], ["proc-snapshot"], ["usage-cache"],
                     ["append-note", "PROGRESS.md"], ["append-note", "ALERTS.md"]):
            self.assertEqual(host.bridge_argv(argv), argv)

    def test_refused(self):
        for argv in (["cat", "/etc/passwd"], ["tmux", "kill-server"], ["tmux", "has-session", "-t", "=Claude"],
                     ["tmux", "kill-session", "-t", "=afc-0123abcd", "-a"], ["tmux", "send-keys", "-t", "afc-0123abcd", "x"],
                     ["kill-claude", "1; rm"], ["append-note", "README.md"], ["proc-snapshot", "1"], [], ["sh", "-c", "id"]):
            with self.assertRaises(ValueError, msg=argv):
                host.bridge_argv(argv)


class SshArgv(unittest.TestCase):
    def test_construction(self):
        with Env(AFCLAUDE_BRIDGE_HOST="gw.example", AFCLAUDE_BRIDGE_USER="bob", AFCLAUDE_BRIDGE_KEY="/k",
                 AFCLAUDE_BRIDGE_KNOWN_HOSTS="/kh"):
            remote = ["ka_resume.sh", "--message", "it's a \"test\"; rm -rf / $(id)"]
            a = host.ssh_argv(remote)
        self.assertEqual(a[0], "ssh")
        self.assertEqual(a[a.index("-i") + 1], "/k")
        for opt in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "UserKnownHostsFile=/kh",
                    "HostKeyAlias=afclaude-host"):
            self.assertIn(opt, a)
        self.assertEqual(a[-3:-1], ["bob@gw.example", "--"])
        # host_exec.py splits $SSH_ORIGINAL_COMMAND with shlex: it must get the argv back unchanged
        self.assertEqual(shlex.split(a[-1]), remote)

    def test_the_multiplexing_socket_depends_on_the_key(self):
        """%C ignores the key: a master of the pre-kit key must never carry a call made with the kit's key."""
        paths = []
        for key in ("/run/secrets/host_bridge_ed25519", "/app/data/bridge/host_bridge_ed25519"):
            with Env(AFCLAUDE_BRIDGE_KEY=key):
                a = host.ssh_argv(["clock"])
            paths.append(a[a.index("ControlMaster=auto") + 2])
        self.assertTrue(all(p.startswith("ControlPath=/tmp/afclaude-ssh-") and p.endswith("-%C") for p in paths), paths)
        self.assertNotEqual(paths[0], paths[1])
        self.assertLess(max(len(p) for p in paths) - len("ControlPath=") - 2 + 40, 104)   # %C = 40 hex; sun_path limit

    def test_without_multiplexing_no_master_socket_is_used(self):
        """K6 review L2: the bridge check's retry must not go through a master that may hang."""
        normal, own = host.ssh_argv(["clock"]), host.ssh_argv(["clock"], multiplex=False)
        self.assertIn("ControlMaster=auto", normal)
        self.assertIn("ControlPersist=60", normal)
        self.assertIn("ControlMaster=no", own)
        self.assertIn("ControlPath=none", own)
        self.assertFalse([o for o in own if o.startswith("ControlPersist") or o.startswith("ControlPath=/")], own)
        def rest(a):
            return [o for o in a if o != "-o" and not o.startswith("Control")]
        self.assertEqual(rest(normal), rest(own))        # only the multiplexing options differ


class RunOnHost(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.rec = os.path.join(self.d, "rec.json")
        with open(os.path.join(self.d, "ssh"), "w") as fh:   # records its last arg + stdin
            fh.write("#!/bin/sh\nfor last; do :; done\n"
                     f"python3 -c 'import json,sys; json.dump({{\"cmd\": sys.argv[1], \"stdin\": sys.stdin.read()}}, "
                     f"open(\"{self.rec}\", \"w\"))' \"$last\"\necho remote-out\nexit 3\n")
        os.chmod(os.path.join(self.d, "ssh"), 0o755)

    def test_host_mode_is_plain_subprocess(self):
        with Env(AFCLAUDE_IN_CONTAINER=None):
            r = host.run_on_host(["sh", "-c", "cat; echo $X; pwd"], input="in\n", cwd=self.d,
                                 env={"X": "y", "PATH": "/usr/bin:/bin"}, timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.split(), ["in", "y", os.path.realpath(self.d)])

    def test_host_mode_claude_p_reads_user_settings_only(self):
        """Review L6: a local one-shot `claude -p` gets --setting-sources user, as the bridge's do."""
        calls = []
        with Env(AFCLAUDE_IN_CONTAINER=None), mock.patch.object(
                host.subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0)):
            host.run_on_host(["claude", "-p", "--model", "haiku"])
            host.run_on_host(["/home/x/.local/bin/claude", "-p", "--setting-sources", "user,project"])
            host.run_on_host(["claude", "agents", "--json"])
            host.run_on_host(["tmux", "has-session", "-p"])
        self.assertEqual(calls, [["claude", "--setting-sources", "user", "-p", "--model", "haiku"],
                                 ["/home/x/.local/bin/claude", "-p", "--setting-sources", "user,project"],
                                 ["claude", "agents", "--json"], ["tmux", "has-session", "-p"]])

    def test_container_mode_goes_over_ssh(self):
        with Env(AFCLAUDE_IN_CONTAINER="1", PATH=self.d + os.pathsep + os.environ["PATH"]):
            r = host.run_on_host(["tmux", "has-session", "-t", "=afc-0123abcd"], cwd="/nonexistent", timeout=10)
            self.assertEqual((r.returncode, r.stdout.strip()), (3, "remote-out"))
            self.assertEqual(json.load(open(self.rec)), {"cmd": "tmux-has afc-0123abcd", "stdin": ""})
            host.run_on_host(["claude", "-p", "--model", "haiku"], input="the prompt", timeout=10)
            self.assertEqual(json.load(open(self.rec)), {"cmd": "claude -p --model haiku", "stdin": "the prompt"})
            with self.assertRaises(ValueError):
                host.run_on_host(["rm", "-rf", "/"])

    def test_container_mode_multiplex_switch(self):
        calls = []
        with Env(AFCLAUDE_IN_CONTAINER="1"), mock.patch.object(
                host.subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 1)):
            host.run_on_host(["tmux", "has-session", "-t", "=afc-check"], timeout=5)
            host.run_on_host(["tmux", "has-session", "-t", "=afc-check"], timeout=5, multiplex=False)
        self.assertIn("ControlMaster=auto", calls[0])
        self.assertIn("ControlPath=none", calls[1])
        self.assertNotIn("ControlMaster=auto", calls[1])

    def test_helpers_container_mode(self):
        with Env(AFCLAUDE_IN_CONTAINER="1", PATH=self.d + os.pathsep + os.environ["PATH"]):
            self.assertFalse(host.kill_claude(42))            # stub exits 3 -> refused/vanished
            self.assertEqual(json.load(open(self.rec))["cmd"], "kill-claude 42")
            self.assertIsNone(host.claude_env(42))
            with self.assertRaises(OSError):
                host.append_note("/some/where/PROGRESS.md", "- a line\n")
            self.assertEqual(json.load(open(self.rec)), {"cmd": "append-note PROGRESS.md", "stdin": "- a line\n"})

    def test_append_note_host_mode(self):
        p = os.path.join(self.d, "PROGRESS.md")
        with Env(AFCLAUDE_IN_CONTAINER=None):
            host.append_note(p, "a\n")
            host.append_note(p, "b\n")
        self.assertEqual(open(p).read(), "a\nb\n")


class Whitelist(unittest.TestCase):
    """Every shape host.py produces is accepted by host_exec.py, and the rest is denied (126)."""

    def test_denied(self):
        for cmd in ("cat /etc/passwd", "tmux-kill ka-XYZ", "tmux-panes Claude", "append-note README.md",
                    "proc-snapshot 1", "usage-cache now", "kill-claude 1", "claude-env 1", "tmux kill-server", "",
                    "claude -p --model haiku --dangerously-skip-permissions"):
            self.assertEqual(run_host_exec(cmd).returncode, 126, cmd)

    def test_readable_names(self):
        """3b S2: every tmux verb takes ka-<8 hex> or afc-<[a-z0-9-], at most 36>, and nothing else;
        tmux-rename takes two such names. (Accepted names here are ones no tmux session has: tmux
        just exits 1.)"""
        ok = ("afc-00000000", "afc-s2-whitelist-test-none", "afc-" + "x" * 36)
        for verb in ("tmux-has", "tmux-panes", "tmux-clients", "tmux-capture", "tmux-kill"):
            for name in ok:
                r = run_host_exec(f"{verb} {name}")
                self.assertNotEqual(r.returncode, 126, (verb, name, r.stderr))
            for bad in ("afc-a\\;id", "'afc-a b'", "afc-Upper", "afc-" + "x" * 37, "afc-", "afc-x.y", "Claude",
                        "'afc-x\n'", "afc-x afc-y", ""):
                self.assertEqual(run_host_exec(f"{verb} {bad}").returncode, 126, (verb, bad))
        r = run_host_exec("tmux-rename afc-s2-whitelist-test-none afc-s2-whitelist-test-none2")
        self.assertNotEqual(r.returncode, 126, r.stderr)
        for bad in ("tmux-rename afc-s2-whitelist-test-none", "tmux-rename afc-a Claude",
                    "tmux-rename Claude afc-a", "tmux-rename afc-a afc-b afc-c", "tmux-rename afc-a 'afc-b;id'"):
            self.assertEqual(run_host_exec(bad).returncode, 126, bad)

    def test_ka_resume_needs_a_tmux_name(self):
        """3b S2: ka_resume.sh is allowed only with --tmux <a tmux name> (checked, not run)."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("host_exec_s2", HOST_EXEC)
        he = importlib.util.module_from_spec(spec)
        with Env(AFCLAUDE_HOST_EXEC_LOG=os.devnull):
            spec.loader.exec_module(he)
            sid = "abcdef01-0000-4000-8000-000000000001"
            for name in ("afc-abcdef01", "afc-afclaude-task-manager"):
                he.check_ka_resume(["--session", sid, "--tmux", name, "--message", "go", "--new"])   # no exit
            for bad in ([], ["--tmux", "../x"], ["--tmux", "afc-A"], ["--tmux", "afc-x\n"], ["--tmux"],
                        ["--tmux", "afc-" + "x" * 37]):
                with self.assertRaises(SystemExit, msg=bad):
                    he.check_ka_resume(["--session", sid, "--message", "go", *bad])

    @staticmethod
    def register_thread(sessions, name, pid, fname=None, **kw):
        with open(os.path.join(sessions, fname or f"{pid}.json"), "w") as fh:
            json.dump(dict({"pid": pid, "sessionId": "s", "name": name, "entrypoint": "sdk-cli", "status": "idle"},
                           **kw), fh)

    def test_rc_send_only_to_one_live_idle_thread(self):
        """D-288: the host checks the name the container sent against ~/.claude/sessions itself."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("host_exec_rc2", HOST_EXEC)
        he = importlib.util.module_from_spec(spec)
        with Env(AFCLAUDE_HOST_EXEC_LOG=os.devnull):
            spec.loader.exec_module(he)
        sessions = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, sessions)
        he.SESSIONS = sessions
        me, start = os.getpid(), he.proc_start(os.getpid())
        self.assertTrue(start)
        dead = 2 ** 22 + 12345                                    # above pid_max: no such process

        def denied(msg, name="rc-t"):
            with self.assertRaises(SystemExit, msg=msg) as cm:
                he.rc_send_cmd([name], b"go")
            self.assertEqual(cm.exception.code, 126, msg)
        denied("no registry entry")
        self.register_thread(sessions, "rc-t", dead)
        denied("dead pid")
        self.register_thread(sessions, "rc-t", me, procStart="1")
        denied("reused pid (procStart differs)")
        self.register_thread(sessions, "rc-t", me, procStart=start, status="busy")
        denied("busy")
        self.register_thread(sessions, "rc-t", me, procStart=start, status="waiting")
        denied("unknown status")
        self.register_thread(sessions, "rc-t", me, procStart=start, entrypoint="cli")
        denied("a terminal session")
        self.register_thread(sessions, "other", me, procStart=start)
        denied("another name")
        self.register_thread(sessions, "rc-t", me, procStart=start)
        cmd, _ = he.rc_send_cmd(["rc-t"], b"go")                  # the one live idle sdk-cli thread: allowed
        self.assertEqual(cmd[0], he.cfg().claude)
        self.register_thread(sessions, "rc-t", 1, fname="b.json")  # pid 1 is alive too: two live entries
        denied("two live entries")
        os.remove(os.path.join(sessions, "b.json"))
        self.register_thread(sessions, "rc-t", dead, fname="c.json")   # a stale entry of the name is ignored
        he.rc_send_cmd(["rc-t"], b"go")
        with open(os.path.join(sessions, "d.json"), "w") as fh:
            fh.write("{broken")                                    # unreadable: not ours to judge
        he.rc_send_cmd(["rc-t"], b"go")

    def test_rc_send_is_stopped_on_the_host_below_the_containers_timeout(self):
        """D-235: the sender can't outlive the dispatcher's own timeout and deliver after it recorded an error."""
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location("host_exec_rc3", HOST_EXEC)
        he = importlib.util.module_from_spec(spec)
        sessions = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, sessions)
        with Env(AFCLAUDE_HOST_EXEC_LOG=os.devnull):
            spec.loader.exec_module(he)
        with Env(SSH_ORIGINAL_COMMAND="rc-send rc-t"):
            he.SESSIONS = sessions
            he.CWD = os.path.join(sessions, "cwd")        # never the real ~/.local/state
            self.register_thread(sessions, "rc-t", os.getpid())
            run = mock.Mock(side_effect=subprocess.TimeoutExpired("claude", he.RC_HOST_TIMEOUT))
            stdin = mock.Mock(buffer=io.BytesIO(b"go"))
            with mock.patch.object(he.subprocess, "run", run), mock.patch.object(he.sys, "stdin", stdin), \
                    mock.patch.object(he.sys, "stderr", io.StringIO()):
                self.assertEqual(he.main(), 124)
        self.assertEqual(run.call_args.kwargs["timeout"], he.RC_HOST_TIMEOUT)

    def test_rc_send_through_the_forced_command_is_denied_without_a_thread(self):
        sessions = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, sessions)
        env = dict(os.environ, AFCLAUDE_HOST_EXEC_LOG=os.devnull, AFCLAUDE_HOST_EXEC_SESSIONS=sessions)
        env.pop("SSH_ORIGINAL_COMMAND", None)
        r = subprocess.run([sys.executable, HOST_EXEC, "--run", "rc-send rc-test-f1"], input=b"go", env=env,
                           capture_output=True, timeout=60)
        self.assertEqual((r.returncode, b"denied" in r.stderr), (126, True))   # no claude was run

    def test_rc_send_shape(self):
        """D-288: rc-send <name> (text on stdin) runs the fixed Haiku sender; host.py and host_exec.py
        agree on the flags, prompt and limits. Checked, never run (a real run would message a session)."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("host_exec_rc", HOST_EXEC)
        he = importlib.util.module_from_spec(spec)
        with Env(AFCLAUDE_HOST_EXEC_LOG=os.devnull):
            spec.loader.exec_module(he)
            for k in ("RC_SEND_FLAGS", "RC_SEND_PROMPT", "RC_TEXT_MAX", "RC_HOST_TIMEOUT"):
                self.assertEqual(getattr(he, k), getattr(host, k), k)
            self.assertEqual(he.RC_NAME.pattern, host.RC_NAME.pattern)
            # the sender has the SendMessage tool and nothing else, auto-approved under dontAsk
            self.assertIn("--tools=SendMessage", he.RC_SEND_FLAGS)
            self.assertIn("--allowedTools=SendMessage", he.RC_SEND_FLAGS)
            self.assertFalse(any("ListAgents" in f for f in he.RC_SEND_FLAGS))
            import rc_continue
            self.assertLess(he.RC_HOST_TIMEOUT, rc_continue.SEND_TIMEOUT)   # the container's ssh timeout
            sessions = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, sessions)
            he.SESSIONS = sessions
            self.register_thread(sessions, "rc-test-f1", os.getpid())
            cmd, stdin = he.rc_send_cmd(["rc-test-f1"], "go on \u00e4".encode())
            self.assertEqual(cmd, [he.cfg().claude, "-p", "--setting-sources", "user"] + host.RC_SEND_FLAGS[1:])
            self.assertEqual(stdin.decode(), host.rc_send_prompt("rc-test-f1", "go on \u00e4"))
            for args, data in (([], b"x"), (["a", "b"], b"x"), (["a;b"], b"x"), (["-p"], b"x"), (["a"], b""),
                               (["a"], b"x" * (host.RC_TEXT_MAX + 1)), (["a"], b"a\0b"), (["a"], b"\xff\xfe")):
                with self.assertRaises(SystemExit, msg=(args, data[:10])):
                    he.rc_send_cmd(args, data)
        self.assertEqual(host.bridge_argv(["rc-send", "rc-test-f1"]), ["rc-send", "rc-test-f1"])
        for bad in (["rc-send"], ["rc-send", "a b"], ["rc-send", "a", "b"]):
            with self.assertRaises(ValueError, msg=bad):
                host.bridge_argv(bad)
        for cmd, stdin in (("rc-send", b"x"), ("rc-send 'a;b'", b"x"), ("rc-send a b", b"x"),
                           ("rc-send rc-test-f1", b""), ("rc-send rc-test-f1", b"x" * (16 * 1024 + 1))):
            self.assertEqual(run_host_exec(cmd, stdin).returncode, 126, cmd)   # denied before any claude run

    def test_append_note_limits(self):
        self.assertEqual(run_host_exec("append-note PROGRESS.md", b"x" * (16 * 1024 + 1)).returncode, 126)
        self.assertEqual(run_host_exec("append-note ALERTS.md", b"a\0b").returncode, 126)
        self.assertEqual(run_host_exec("append-note ALERTS.md", b"\xff\xfe").returncode, 126)

    def test_read_only_shapes_accepted(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        log = os.path.join(d, "host_exec.log")
        r = run_host_exec("tmux-has afc-00000000", log=log)
        self.assertNotEqual(r.returncode, 126)
        r = run_host_exec("tmux-panes afc-00000000", log=log)
        self.assertNotEqual(r.returncode, 126)
        r = run_host_exec("proc-snapshot", log=log)
        self.assertEqual(r.returncode, 0, r.stderr)
        snap = json.loads(r.stdout)
        me = snap[str(os.getpid())]                      # this test runner: not claude -> no argv
        self.assertEqual(me[0], os.getppid())
        self.assertIsNone(me[2])
        with open(log) as fh:
            self.assertIn("OK proc-snapshot", fh.read())
        with open(os.path.join(TEST_HOME, ".claude.json"), "w") as fh:     # the kit's HOME is a temp dir
            json.dump({"cachedUsageUtilization": {"fetchedAtMs": 1, "utilization": {"five_hour": 3}, "other": "x"},
                       "oauthAccount": "secret"}, fh)
        r = run_host_exec("usage-cache", log=log)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"fetchedAtMs": 1, "utilization": {"five_hour": 3}})

    def test_every_host_py_shape_is_whitelisted(self):
        """The shapes host.py emits, run through host_exec.py's parser without executing:
        only the head/arity checks matter, so use targets that do nothing harmful."""
        for argv, ok_codes in (
                (["tmux", "has-session", "-t", "=afc-00000000"], None),
                (["tmux", "list-panes", "-s", "-t", "=afc-00000000", "-F", "#{pane_pid}"], None),
                (["tmux", "list-clients", "-t", "=afc-00000000", "-F", "#{client_activity}"], None),
                (["tmux", "capture-pane", "-e", "-p", "-t", "=afc-00000000:"], None),
                (["tmux", "kill-session", "-t", "=afc-00000000"], None),   # no such session: tmux exits 1
                (["tmux", "has-session", "-t", "=afc-s2-whitelist-test-none"], None),
                (["tmux", "capture-pane", "-e", "-p", "-t", "=afc-s2-whitelist-test-none:"], None),
                (["tmux", "rename-session", "-t", "=afc-s2-whitelist-test-none", "afc-s2-whitelist-test-x"], None)):
            r = run_host_exec(shlex.join(host.bridge_argv(argv)))
            self.assertNotEqual(r.returncode, 126, (argv, r.stderr))


def load_host_exec(state):
    """The pre-kit docker/host_exec.py as a module with its state dir (log, headless cwd) in `state`."""
    import importlib.util
    with Env(AFCLAUDE_HOST_EXEC_STATE=state, AFCLAUDE_HOST_EXEC_LOG=None):
        spec = importlib.util.spec_from_file_location("host_exec_hardening", LEGACY_HOST_EXEC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class BridgeHardening(unittest.TestCase):
    """Review k2 H2/H3: the bridge log and the cwd of the `claude -p` calls are out of the container's
    reach (<repo>/data is its read-write mount), caller text is one escaped log line, and a
    ka_resume --cwd in data is refused. Nothing here launches or signals anything."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.hx = load_host_exec(self.state)

    def test_the_log_lives_outside_data_and_is_private(self):
        self.assertEqual(self.hx.LOG, os.path.join(self.state, "host_exec.log"))
        self.assertFalse(self.hx.under_data(self.hx.LOG))
        self.hx.log("OK x")
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.hx.LOG).st_mode & 0o777, 0o600)
        default = load_host_exec(None)          # no override: the default is in the home, never in data
        self.assertFalse(default.under_data(default.LOG))
        self.assertTrue(default.LOG.endswith(os.path.join(".local", "state", "afclaude", "host_exec.log")))

    def test_the_log_is_not_followed_through_a_symlink(self):
        target = os.path.join(self.tmp, "authorized_keys")
        with open(target, "w") as fh:
            fh.write("ssh-ed25519 AAAA owner\n")
        os.makedirs(self.state)
        os.symlink(target, os.path.join(self.state, "host_exec.log"))
        r = run_legacy_exec("tmux-has afc-00000000", log=os.path.join(self.state, "host_exec.log"))
        self.assertNotEqual(r.returncode, 126)             # the call still runs: the log is a record, not a gate
        self.assertIn(b"log", r.stderr)                    # but the failure is visible
        self.assertEqual(open(target).read(), "ssh-ed25519 AAAA owner\n")

    def test_a_lone_surrogate_in_the_command_is_logged_escaped(self):
        self.hx.log("DENY bad \udcff byte")                # what non-UTF-8 SSH_ORIGINAL_COMMAND bytes become
        self.assertIn("\\udcff", open(self.hx.LOG).read())

    def test_caller_text_is_one_escaped_line(self):
        evil = "x\nssh-ed25519 AAAAkey k\n\r\x1b[2J\x00\u2028y"
        self.hx.log(f"OK {shlex.join(['ka_resume.sh', '--message', evil])}")
        self.hx.log("next")
        lines = open(self.hx.LOG).read().split("\n")
        self.assertEqual(len(lines), 3)                    # two entries and the final newline: no injected line
        self.assertIn("\\nssh-ed25519 AAAAkey k\\n", lines[0])
        self.assertIn("\\x1b", lines[0])
        for ln in lines[:2]:
            self.assertEqual(self.hx.one_line(ln), ln)     # nothing raw left in it
            self.assertFalse(ln.startswith("ssh-"))
        # the denial path (the command as received) is escaped the same way
        log = os.path.join(self.tmp, "deny.log")
        run_legacy_exec("bogus 'a\nssh-ed25519 AAAAkey k\nb'", log=log)
        self.assertEqual(len(open(log).read().splitlines()), 1)

    def test_ka_resume_refuses_the_data_dir_as_cwd(self):
        repo_data = os.path.join(self.hx.REPO, "data")
        link = os.path.join(self.tmp, "datalink")
        os.symlink(repo_data, link)
        sub = os.path.join(self.tmp, "sub")
        os.symlink(os.path.join(repo_data, "x", "y"), sub)
        args = ["--session", "11111111-2222-4333-8444-555555555555", "--tmux", "afc-x", "--message", "hi"]
        for cwd in (repo_data, os.path.join(repo_data, "a", "b"), repo_data + "/../data", link,
                    os.path.join(link, "deeper"), sub):
            with self.assertRaises(SystemExit, msg=cwd):
                with mock.patch.object(self.hx, "log"), mock.patch("sys.stderr"):
                    self.hx.check_ka_resume(args + ["--cwd", cwd])
            r = run_legacy_exec("ka_resume.sh " + shlex.join(args + ["--cwd", cwd]))
            self.assertEqual(r.returncode, 126, cwd)
            self.assertIn(b"container-writable", r.stderr)
        # the repo itself and a neighbour of data are not data
        for cwd in (self.hx.ROOT, self.hx.REPO, self.hx.REPO + "/data-other"):
            with mock.patch.object(self.hx, "log"):
                self.hx.check_ka_resume(args + ["--cwd", cwd])

    def fake_tree(self):
        """A stand-in workspace under tmp: root/work (a dir under ROOT), root/repo/data (the container's
        mount) with data/l -> root/work; the bridge module's ROOT/REPO/DATA point at it."""
        root = os.path.realpath(os.path.join(self.tmp, "root"))
        repo, work = os.path.join(root, "repo"), os.path.join(root, "work")
        os.makedirs(os.path.join(repo, "data"))
        os.makedirs(work)
        os.symlink(work, os.path.join(repo, "data", "l"))
        self.hx.ROOT, self.hx.REPO, self.hx.DATA = root, repo, os.path.join(repo, "data")
        return root, repo, work

    def deny_msg(self, args):
        """The denial text of check_ka_resume(args), or None when it passes."""
        err = io.StringIO()
        try:
            with mock.patch.object(self.hx, "log"), contextlib.redirect_stderr(err):
                self.hx.check_ka_resume(args)
        except SystemExit:
            return err.getvalue()
        return None

    def test_ka_resume_cwd_must_be_its_own_realpath(self):
        """Review H1: `data/l/..` resolves to a dir under ROOT but a plain `cd` reads it as data; a swap
        of the link after the check would do the same: only an already-resolved --cwd is accepted."""
        root, repo, work = self.fake_tree()
        args = ["--session", "11111111-2222-4333-8444-555555555555", "--tmux", "afc-x", "--message", "hi"]
        data = os.path.join(repo, "data")
        for cwd in (data + "/l/..", data + "/l", data + "/l/../", work + "/../work", work + "/", work + "/.",
                    "work", "./work", root + "/./work"):
            msg = self.deny_msg(args + ["--cwd", cwd])
            self.assertIsNotNone(msg, cwd)
            self.assertTrue("resolved" in msg or "container-writable" in msg, (cwd, msg))
        self.assertIn(work, self.deny_msg(args + ["--cwd", data + "/l"]))      # the message names the real path
        # an already-real path under ROOT (and the default) still works
        for cwd in (work, repo, root):
            self.assertIsNone(self.deny_msg(args + ["--cwd", cwd]), cwd)
        self.assertIsNone(self.deny_msg(args))

    def sh_tree(self):
        """A copy of ka_resume.sh in a fake repo (root/repo) with stub tmux/claude and a temp HOME: the
        script can neither start anything nor touch the real ~/.claude.json. -> (run, root, repo, work)."""
        root = os.path.realpath(os.path.join(self.tmp, "root"))
        repo, work = os.path.join(root, "repo"), os.path.join(root, "work")
        home, bindir = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "bin")
        for d in (os.path.join(repo, "data"), work, home, bindir):
            os.makedirs(d)
        os.symlink(work, os.path.join(repo, "data", "l"))
        script = shutil.copy(os.path.join(HERE, "ka_resume.sh"), repo)
        with open(os.path.join(home, ".claude.json"), "w") as fh:
            json.dump({"projects": {}}, fh)
        self.tmux_log = os.path.join(self.tmp, "tmux.log")
        for name, body in (("tmux", f'echo "$@" >> {shlex.quote(self.tmux_log)}\n'
                                    '[ "$1" = new-session ] && exit 0\n[ "$1" = has-session ] && exit 1\nexit 99\n'),
                           ("claude", "echo claude stub must not run >&2\nexit 99\n")):
            path = os.path.join(bindir, name)
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\n" + body)
            os.chmod(path, 0o755)
        env = dict(os.environ, HOME=home, KA_TRUST_ROOT=root, PATH=bindir + os.pathsep + os.environ["PATH"])

        def run(cwd):
            return subprocess.run([script, "--session", "11111111-2222-4333-8444-555555555555", "--tmux",
                                   "afc-hotfix-test", "--message", "hi", "--cwd", cwd],
                                  capture_output=True, text=True, timeout=60, env=env)
        return run, root, repo, work

    def test_ka_resume_sh_refuses_the_data_dir_itself(self):
        run, root, repo, work = self.sh_tree()
        data = os.path.join(repo, "data")
        link = os.path.join(self.tmp, "datalink")
        os.symlink(data, link)
        os.symlink(data, os.path.join(work, "back"))
        for cwd in (data, os.path.join(data, "z"), link, os.path.join(work, "back"), data + "/../data"):
            r = run(cwd)
            self.assertEqual(r.returncode, 2, (cwd, r.stderr))
            self.assertIn("container-writable", r.stderr)
            self.assertFalse(os.path.exists(self.tmux_log), cwd)          # nothing was started
        self.assertEqual(json.load(open(os.path.join(self.tmp, "home", ".claude.json"))), {"projects": {}})

    def test_ka_resume_sh_uses_the_resolved_cwd_with_cd_minus_P(self):
        """Review H1: data/l/.. and a link in data are resolved once; the run script cds (-P) to that
        resolved path, so a later swap of the link can't land in data."""
        run, root, repo, work = self.sh_tree()
        data = os.path.join(repo, "data")
        for cwd, real in ((data + "/l/..", root), (data + "/l", work), (work, work)):
            r = run(cwd)
            self.assertEqual(r.returncode, 0, (cwd, r.stderr))
            self.assertIn("started afc-hotfix-test", r.stdout)
            lines = open(os.path.join(repo, "run", "afc-hotfix-test.sh")).read().splitlines()
            self.assertEqual(lines[1], f"cd -P -- {shlex.quote(real)} || exit 1", cwd)
            # swap the link into data after the fact: the script written above still names `real`
            os.remove(os.path.join(data, "l"))
            os.symlink(data, os.path.join(data, "l"))
            self.assertEqual(open(os.path.join(repo, "run", "afc-hotfix-test.sh")).read().splitlines()[1],
                             f"cd -P -- {shlex.quote(real)} || exit 1")
            os.remove(os.path.join(data, "l"))
            os.symlink(work, os.path.join(data, "l"))
        trusted = json.load(open(os.path.join(self.tmp, "home", ".claude.json")))["projects"]
        self.assertNotIn(data, trusted)
        self.assertIn(work, trusted)                       # the resolved path was trusted, in the temp HOME

    def test_a_wider_state_dir_of_ours_is_narrowed_and_a_symlinked_one_refused(self):
        """Review L3: every level created by the bridge is 0700; a wider existing dir is fixed."""
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        state = os.path.join(home, ".local", "state", "afclaude")
        hx = load_host_exec(state)
        with Env(HOME=home):
            hx.log("OK x")
            for d in (state, os.path.dirname(state), os.path.join(home, ".local")):
                self.assertEqual(os.stat(d).st_mode & 0o777, 0o700, d)
            os.chmod(state, 0o775)
            hx.claude_cwd()
            self.assertEqual(os.stat(state).st_mode & 0o777, 0o700)
            other = os.path.join(self.tmp, "elsewhere")
            os.makedirs(other, mode=0o775)
            os.rename(state, state + ".old")
            os.symlink(other, state)
            with self.assertRaises(OSError):
                hx.private_dir(state)
            self.assertEqual(os.stat(other).st_mode & 0o777, 0o775)

    def test_headless_calls_run_in_a_private_dir_outside_data_with_user_settings_only(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0)

        for command, stdin in (("claude -p --no-session-persistence --permission-mode dontAsk /usage", b""),
                               ("claude -p --model haiku --output-format json", b"prompt")):
            with Env(SSH_ORIGINAL_COMMAND=command), mock.patch.object(self.hx.subprocess, "run", fake_run), \
                    mock.patch.object(self.hx.sys, "stdin", mock.Mock(buffer=mock.Mock(read=lambda n: stdin))):
                self.assertEqual(self.hx.main(), 0)
            cmd, kw = calls[-1]
            self.assertEqual(cmd[1:3], ["--setting-sources", "user"], command)
            self.assertEqual(kw["cwd"], os.path.join(self.state, "cwd"))
            self.assertFalse(self.hx.under_data(kw["cwd"]))
            self.assertEqual(os.stat(kw["cwd"]).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)
        with mock.patch.object(self.hx, "check_rc_thread"):
            cmd, _ = self.hx.rc_send_cmd(["rc-t"], b"text")
        self.assertEqual(cmd[1:3], ["--setting-sources", "user"])
        # everything else still runs in the workspace root
        with Env(SSH_ORIGINAL_COMMAND="claude agents --json"), mock.patch.object(self.hx.subprocess, "run", fake_run):
            self.hx.main()
        self.assertEqual(calls[-1][1]["cwd"], self.hx.ROOT)
        self.assertEqual(calls[-1][0], [self.hx.CLAUDE, "agents", "--json"])

    def test_an_unusable_headless_dir_denies_instead_of_falling_back(self):
        os.makedirs(self.state)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, self.hx.CWD)                 # a symlink is not the bridge's own dir
        with mock.patch.object(self.hx, "log"), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            self.hx.claude_cwd()
        os.unlink(self.hx.CWD)
        os.mkdir(self.hx.CWD, 0o777)
        os.chmod(self.hx.CWD, 0o777)                       # a wider dir of ours is narrowed (L3), then used
        self.assertEqual(self.hx.claude_cwd(), self.hx.CWD)
        self.assertEqual(os.stat(self.hx.CWD).st_mode & 0o777, 0o700)

    def test_host_side_headless_cwd_is_not_data_and_own_transcripts_stay_own(self):
        import usage_sampler as us
        with Env(AFCLAUDE_IN_CONTAINER=None):
            path = host.headless_cwd()
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)
        self.assertFalse(self.hx.under_data(path))
        default = os.path.join(os.path.expanduser("~"), ".local", "state", "afclaude", "cwd")
        with Env(AFCLAUDE_HEADLESS_CWD=None, AFCLAUDE_IN_CONTAINER="1"):
            self.assertEqual(host.headless_cwd(), default)
        self.assertTrue(any(m in default for m in us.OWN_CWD_MARKERS))   # usage split / stalled scans: still AFClaude's own


class KeepaliveInContainer(unittest.TestCase):
    """keepalive.py's /proc readers use the host snapshot inside the container."""

    def setUp(self):
        self.saved = dict(host._snapshot)
        host._snapshot.update(at=float("inf"), data={      # "fresh forever" for this test
            100: {"ppid": 1, "start": "555", "argv": ["claude", "rc"]},
            200: {"ppid": 100, "start": "777", "argv": ["/x/claude/versions/2", "--print", "--sdk-url", "u"]},
            300: {"ppid": 1, "start": "9", "argv": None}})

    def tearDown(self):
        host._snapshot.clear()
        host._snapshot.update(self.saved)

    def test_readers(self):
        with Env(AFCLAUDE_IN_CONTAINER="1"):
            self.assertTrue(ka.pid_alive(200))
            self.assertFalse(ka.pid_alive(201))
            self.assertEqual(ka.proc_ppid(200), 100)
            self.assertEqual(ka.proc_starttime(200), "777")
            self.assertEqual(ka.proc_argv(300), [])
            self.assertTrue(ka.rc_held(200))
            self.assertTrue(ka.is_ours(200, {100}))

    def test_preflight_fails_safe_without_host_info(self):
        def boom(*a, **k):
            raise OSError("bridge down")
        orig = ka.tmux_alive
        ka.tmux_alive = boom
        try:
            ok, problems, plan = ka.preflight("00000000-0000-0000-0000-000000000000")
        finally:
            ka.tmux_alive = orig
        self.assertFalse(ok)
        self.assertIsNone(plan)
        self.assertIn("host process info unavailable", problems[0])


class UsageViaBridge(unittest.TestCase):
    """The last-mile / budget-headroom / fresh_usage paths read the usage cache
    and refresh it through host.py inside the container (no ~/.claude.json)."""
    def test_last_mile_and_headroom_use_host(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc).replace(microsecond=0)
        reset = now + timedelta(hours=2)
        cache = {"fetchedAtMs": int(now.timestamp() * 1000),
                 "utilization": {"limits": [{"kind": "weekly_all", "percent": 50,
                                             "resets_at": reset.isoformat()}]}}
        calls, fired = [], []

        def uc():
            calls.append("usage-cache")
            return cache

        def roh(cmd, **kw):
            calls.append(" ".join(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def hf(sid, stall, reason, st, args):
            fired.append(stall["uuid"])
            ka.budget_headroom(ka.read_usage_cache(), now)
        olds = (host.usage_cache, host.run_on_host, ka.handle_fire, ka.CLAUDE_JSON)
        host.usage_cache, host.run_on_host, ka.handle_fire = uc, roh, hf
        ka.CLAUDE_JSON = "/nonexistent/.claude.json"
        try:
            with Env(AFCLAUDE_IN_CONTAINER="1"):
                ka.last_mile_pass("00000000-0000-0000-0000-000000000000", now, {"handled": {}, "fires": {}}, None)
        finally:
            host.usage_cache, host.run_on_host, ka.handle_fire, ka.CLAUDE_JSON = olds
        self.assertEqual(len(fired), 1)
        self.assertIn("claude -p --no-session-persistence --permission-mode dontAsk /usage", calls)
        self.assertGreaterEqual(calls.count("usage-cache"), 3)


if __name__ == "__main__":
    unittest.main()
