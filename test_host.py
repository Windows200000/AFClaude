#!/usr/bin/env python3
"""Offline tests for host.py (host path vs SSH bridge) and its fit with
docker/host_exec.py's whitelist. No real ssh: a stub `ssh` on PATH records the
remote command, or hands it to host_exec.py exactly as sshd's forced command
would ($SSH_ORIGINAL_COMMAND). host_exec.py logs to a temp file here. Nothing
here kills, launches or appends to a real file."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import host  # noqa: E402
import keepalive as ka  # noqa: E402
import afclaude_config  # noqa: E402

_CFG_DIR = tempfile.TemporaryDirectory()
_OLD_CFG = afclaude_config.CONFIG_FILE


import pacing as budget  # noqa: E402

_OLD_BUDGET = (budget.SAMPLES_FILE, budget.USER_MODEL_FILE, budget.FIRE_FILES)


def setUpModule():
    # the last-mile test below is about the bridge; pin the linear budget rule and point the
    # budget model's files (the "auto" last mile reads the ratio) away from this host's
    # sampler data (the budget model is tested elsewhere)
    afclaude_config.CONFIG_FILE = os.path.join(_CFG_DIR.name, "afclaude.json")
    with open(afclaude_config.CONFIG_FILE, "w") as fh:
        json.dump({"usage_model": "linear"}, fh)
    budget.SAMPLES_FILE = os.path.join(_CFG_DIR.name, "no_samples.jsonl")
    budget.USER_MODEL_FILE = os.path.join(_CFG_DIR.name, "no_user_model.json")
    budget.FIRE_FILES = []


def tearDownModule():
    afclaude_config.CONFIG_FILE = _OLD_CFG
    budget.SAMPLES_FILE, budget.USER_MODEL_FILE, budget.FIRE_FILES = _OLD_BUDGET
    _CFG_DIR.cleanup()

HOST_EXEC = os.path.join(HERE, "docker", "host_exec.py")


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


def run_host_exec(command, stdin=b"", log=None):
    env = dict(os.environ, SSH_ORIGINAL_COMMAND=command, AFCLAUDE_HOST_EXEC_LOG=log or os.devnull)
    return subprocess.run([sys.executable, HOST_EXEC], input=stdin, env=env, capture_output=True, timeout=60)


class InContainer(unittest.TestCase):
    def test_flag(self):
        for v, want in ((None, False), ("", False), ("0", False), ("1", True), ("yes", True)):
            with Env(AFCLAUDE_IN_CONTAINER=v):
                self.assertEqual(host.in_container(), want, v)


class BridgeArgv(unittest.TestCase):
    def test_claude_and_ka_resume(self):
        self.assertEqual(host.bridge_argv(["claude", "agents", "--json", "--all"]), ["claude", "agents", "--json", "--all"])
        self.assertEqual(host.bridge_argv(["/home/x/.local/bin/claude", "stop", "abcd1234"]), ["claude", "stop", "abcd1234"])
        self.assertEqual(host.bridge_argv([ka.KA_RESUME, "--session", "u", "--message", "hi there"]),
                         ["ka_resume.sh", "--session", "u", "--message", "hi there"])

    def test_tmux_shapes(self):
        self.assertEqual(host.bridge_argv(["tmux", "has-session", "-t", "=ka-0123abcd"]), ["tmux-has", "ka-0123abcd"])
        self.assertEqual(host.bridge_argv(["tmux", "kill-session", "-t", "=ka-0123abcd"]), ["tmux-kill", "ka-0123abcd"])
        self.assertEqual(host.bridge_argv(["tmux", "list-panes", "-s", "-t", "=ka-0123abcd", "-F", "#{pane_pid}"]),
                         ["tmux-panes", "ka-0123abcd"])

    def test_bridge_only_shapes_pass(self):
        for argv in (["kill-claude", "123"], ["claude-env", "123"], ["proc-snapshot"], ["usage-cache"],
                     ["append-note", "PROGRESS.md"], ["append-note", "ALERTS.md"]):
            self.assertEqual(host.bridge_argv(argv), argv)

    def test_refused(self):
        for argv in (["cat", "/etc/passwd"], ["tmux", "kill-server"], ["tmux", "has-session", "-t", "=Claude"],
                     ["tmux", "kill-session", "-t", "=ka-0123abcd", "-a"], ["tmux", "send-keys", "-t", "ka-0123abcd", "x"],
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


class RunOnHost(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
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

    def test_container_mode_goes_over_ssh(self):
        with Env(AFCLAUDE_IN_CONTAINER="1", PATH=self.d + os.pathsep + os.environ["PATH"]):
            r = host.run_on_host(["tmux", "has-session", "-t", "=ka-0123abcd"], cwd="/nonexistent", timeout=10)
            self.assertEqual((r.returncode, r.stdout.strip()), (3, "remote-out"))
            self.assertEqual(json.load(open(self.rec)), {"cmd": "tmux-has ka-0123abcd", "stdin": ""})
            host.run_on_host(["claude", "-p", "--model", "haiku"], input="the prompt", timeout=10)
            self.assertEqual(json.load(open(self.rec)), {"cmd": "claude -p --model haiku", "stdin": "the prompt"})
            with self.assertRaises(ValueError):
                host.run_on_host(["rm", "-rf", "/"])

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

    def test_append_note_limits(self):
        self.assertEqual(run_host_exec("append-note PROGRESS.md", b"x" * (16 * 1024 + 1)).returncode, 126)
        self.assertEqual(run_host_exec("append-note ALERTS.md", b"a\0b").returncode, 126)
        self.assertEqual(run_host_exec("append-note ALERTS.md", b"\xff\xfe").returncode, 126)

    def test_read_only_shapes_accepted(self):
        log = os.path.join(tempfile.mkdtemp(), "host_exec.log")
        r = run_host_exec("tmux-has ka-00000000", log=log)
        self.assertNotEqual(r.returncode, 126)
        r = run_host_exec("tmux-panes ka-00000000", log=log)
        self.assertNotEqual(r.returncode, 126)
        r = run_host_exec("proc-snapshot", log=log)
        self.assertEqual(r.returncode, 0)
        snap = json.loads(r.stdout)
        me = snap[str(os.getpid())]                      # this test runner: not claude -> no argv
        self.assertEqual(me[0], os.getppid())
        self.assertIsNone(me[2])
        self.assertIn("OK proc-snapshot", open(log).read())
        if os.path.exists(os.path.expanduser("~/.claude.json")):
            r = run_host_exec("usage-cache", log=log)
            self.assertEqual(r.returncode, 0)
            self.assertIsInstance(json.loads(r.stdout), dict)

    def test_every_host_py_shape_is_whitelisted(self):
        """The shapes host.py emits, run through host_exec.py's parser without executing:
        only the head/arity checks matter, so use targets that do nothing harmful."""
        for argv, ok_codes in (
                (["tmux", "has-session", "-t", "=ka-00000000"], None),
                (["tmux", "list-panes", "-s", "-t", "=ka-00000000", "-F", "#{pane_pid}"], None),
                (["tmux", "kill-session", "-t", "=ka-00000000"], None)):   # no such session: tmux exits 1
            r = run_host_exec(shlex.join(host.bridge_argv(argv)))
            self.assertNotEqual(r.returncode, 126, (argv, r.stderr))


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
