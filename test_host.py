#!/usr/bin/env python3
"""Offline tests for host.py (host path vs SSH bridge) and its fit with
docker/host_exec.py's whitelist. No real ssh: a stub `ssh` on PATH records the
remote command, or hands it to host_exec.py exactly as sshd's forced command
would ($SSH_ORIGINAL_COMMAND). host_exec.py logs to a temp file here. Nothing
here kills, launches or appends to a real file."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

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
        self.assertEqual(host.bridge_argv(["tmux", "list-clients", "-t", "=ka-0123abcd", "-F", "#{client_activity}"]),
                         ["tmux-clients", "ka-0123abcd"])
        self.assertEqual(host.bridge_argv(["tmux", "capture-pane", "-e", "-p", "-t", "=ka-0123abcd:"]),
                         ["tmux-capture", "ka-0123abcd"])
        with self.assertRaises(ValueError):
            host.bridge_argv(["tmux", "capture-pane", "-e", "-p", "-t", "=Claude:"])

    def test_readable_names_and_rename(self):
        """3b S2: the readable afc-... names next to today's ka-<id8>, and the one-time rename."""
        n = "afc-afclaude-task-manager"
        self.assertEqual(host.bridge_argv(["tmux", "has-session", "-t", "=" + n]), ["tmux-has", n])
        self.assertEqual(host.bridge_argv(["tmux", "capture-pane", "-e", "-p", "-t", f"={n}:"]), ["tmux-capture", n])
        self.assertEqual(host.bridge_argv(["tmux", "list-panes", "-s", "-t", "=afc-review-2", "-F", "#{pane_pid}"]),
                         ["tmux-panes", "afc-review-2"])
        self.assertEqual(host.bridge_argv(["tmux", "rename-session", "-t", "=ka-0123abcd", n]),
                         ["tmux-rename", "ka-0123abcd", n])
        self.assertEqual(host.bridge_argv(["tmux", "rename-session", "-t", "=" + n, "ka-0123abcd"]),
                         ["tmux-rename", n, "ka-0123abcd"])           # the rollback (plan §3.4)
        for bad in ("=afc-a;b", "=afc-a b", "=afc-Upper", "=afc-" + "x" * 37, "=afc-", "=afc-x\n", "=afc-x:"):
            with self.assertRaises(ValueError, msg=bad):
                host.bridge_argv(["tmux", "has-session", "-t", bad])
        for argv in (["tmux", "rename-session", "-t", "ka-0123abcd", n],          # no exact-match "="
                     ["tmux", "rename-session", "-t", "=ka-0123abcd", "=" + n],
                     ["tmux", "rename-session", "-t", "=ka-0123abcd", "Claude"],
                     ["tmux", "rename-session", "-t", "=Claude", n],
                     ["tmux", "rename-session", "-t", "=ka-0123abcd", n, "x"]):
            with self.assertRaises(ValueError, msg=argv):
                host.bridge_argv(argv)

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

    def test_readable_names(self):
        """3b S2: every tmux verb takes ka-<8 hex> or afc-<[a-z0-9-], at most 36>, and nothing else;
        tmux-rename takes two such names. (Accepted names here are ones no tmux session has: tmux
        just exits 1.)"""
        ok = ("ka-00000000", "afc-s2-whitelist-test-none", "afc-" + "x" * 36)
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
            for name in ("ka-abcdef01", "afc-afclaude-task-manager"):
                he.check_ka_resume(["--session", sid, "--tmux", name, "--message", "go", "--new"])   # no exit
            for bad in ([], ["--tmux", "../x"], ["--tmux", "afc-A"], ["--tmux", "afc-x\n"], ["--tmux"],
                        ["--tmux", "afc-" + "x" * 37]):
                with self.assertRaises(SystemExit, msg=bad):
                    he.check_ka_resume(["--session", sid, "--message", "go", *bad])

    def test_rc_send_shape(self):
        """D-288: rc-send <name> (text on stdin) runs the fixed Haiku sender; host.py and host_exec.py
        agree on the flags, prompt and limits. Checked, never run (a real run would message a session)."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("host_exec_rc", HOST_EXEC)
        he = importlib.util.module_from_spec(spec)
        with Env(AFCLAUDE_HOST_EXEC_LOG=os.devnull):
            spec.loader.exec_module(he)
            for k in ("RC_SEND_FLAGS", "RC_SEND_PROMPT", "RC_TEXT_MAX"):
                self.assertEqual(getattr(he, k), getattr(host, k), k)
            self.assertEqual(he.RC_NAME.pattern, host.RC_NAME.pattern)
            cmd, stdin = he.rc_send_cmd(["rc-test-f1"], "go on \u00e4".encode())
            self.assertEqual(cmd, [he.CLAUDE] + host.RC_SEND_FLAGS)
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
                (["tmux", "list-clients", "-t", "=ka-00000000", "-F", "#{client_activity}"], None),
                (["tmux", "capture-pane", "-e", "-p", "-t", "=ka-00000000:"], None),
                (["tmux", "kill-session", "-t", "=ka-00000000"], None),   # no such session: tmux exits 1
                (["tmux", "has-session", "-t", "=afc-s2-whitelist-test-none"], None),
                (["tmux", "capture-pane", "-e", "-p", "-t", "=afc-s2-whitelist-test-none:"], None),
                (["tmux", "rename-session", "-t", "=afc-s2-whitelist-test-none", "afc-s2-whitelist-test-x"], None)):
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
