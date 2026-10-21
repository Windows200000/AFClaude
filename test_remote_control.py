#!/usr/bin/env python3
"""D-269: Remote Control as a setting per session category (remote_control.py): the registry
entries, the strict read at launch, keepalive.fire's ka_resume.sh argument, ka_resume.sh itself
(a copy with a stub tmux; no session starts), docker/host_exec.py's whitelist and the export block.
Offline: a temp DB (testenv)."""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import export_quickview as qv  # noqa: E402
import keepalive as ka  # noqa: E402
import remote_control as rcs  # noqa: E402
import store  # noqa: E402

SID = "abcdef01-0000-4000-8000-000000000001"
KEYS = [rcs.PREFIX + c for c in rcs.CATEGORIES]


class Base(unittest.TestCase):
    def setUp(self):
        for k in KEYS:
            self.addCleanup(testenv.set_setting, k, None)


class Registry(Base):
    def test_one_bool_setting_per_category_default_on(self):
        self.assertEqual(set(rcs.CATEGORIES), {"task_manager", "review", "continue", "test"})
        self.assertEqual({k for k in actions.SETTINGS if k.startswith(rcs.PREFIX)}, set(KEYS))
        for k in KEYS:
            spec = actions.SETTINGS[k]
            self.assertIs(spec.default(), True, k)                        # today's launch: --remote-control
            self.assertEqual((spec.kind, spec.section), ("bool", "automation"), k)
            self.assertIn("D-269", spec.doc)

    def test_set_validates(self):
        conn = store.connect()
        try:
            with self.assertRaisesRegex(ValueError, "must be true or false"):
                actions.perform(conn, "setting.set", {"key": "remote_control_review", "value": "off"},
                                actor="cli", via="cli")
            with self.assertRaisesRegex(actions.Forbidden, "not allowed for an autonomous"):   # owner-only
                actions.perform(conn, "setting.set", {"key": "remote_control_review", "value": False},
                                actor="mcp", via="mcp", autonomous=True)
        finally:
            conn.close()

    def test_unknown_category(self):
        with self.assertRaisesRegex(rcs.RemoteControlError, "unknown session category 'worker'"):
            rcs.launch_flags("worker")


class Read(Base):
    def test_default_and_set(self):
        self.assertEqual(rcs.launch_flags(rcs.TASK_MANAGER), [])
        testenv.set_setting("remote_control_task_manager", False)
        self.assertEqual(rcs.launch_flags(rcs.TASK_MANAGER), [rcs.NO_RC_FLAG])
        self.assertEqual(rcs.launch_flags(rcs.REVIEW), [])                # the others keep theirs

    def test_invalid_saved_value_raises(self):
        testenv.put_raw("remote_control_continue", "no")                 # around actions.py
        with self.assertRaisesRegex(rcs.RemoteControlError, "remote_control_continue: the saved value is invalid"):
            rcs.launch_flags(rcs.CONTINUE)

    def test_missing_db_raises(self):
        old = store.DB_PATH
        store.DB_PATH = os.path.join(testenv.DIR, "nope", "missing.db")
        try:
            with self.assertRaises(store.DBError):
                rcs.launch_flags(rcs.TASK_MANAGER)
        finally:
            store.DB_PATH = old

    def test_cli(self):
        env = dict(os.environ)
        r = subprocess.run(["python3", os.path.join(HERE, "remote_control.py"), "flags", "test"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))
        testenv.set_setting("remote_control_test", False)
        r = subprocess.run(["python3", os.path.join(HERE, "remote_control.py"), "flags", "test"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, rcs.NO_RC_FLAG))
        testenv.put_raw("remote_control_test", 3)
        r = subprocess.run(["python3", os.path.join(HERE, "remote_control.py"), "flags", "test"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("invalid", r.stderr)


class Fire(Base):
    """keepalive.fire: no extra argument by default (the command line of before D-269), the flag when
    the category is off, and an invalid setting aborts before anything runs on the host."""
    def setUp(self):
        super().setUp()
        testenv.tmux_row(SID)                                          # its stored tmux name (3b S2)
        self.old = ka.host.run_on_host
        self.runs = []
        ka.host.run_on_host = lambda argv, **k: self.runs.append(argv) or types.SimpleNamespace(
            returncode=0, stdout="started", stderr="")

    def tearDown(self):
        ka.host.run_on_host = self.old
        super().tearDown()

    def test_default_is_unchanged(self):
        ka.fire(SID, "/tmp", "go", "resume", new=True)
        self.assertEqual(self.runs[-1][-1], "--new")
        self.assertNotIn(rcs.NO_RC_FLAG, self.runs[-1])

    def test_category_off(self):
        testenv.set_setting("remote_control_review", False)
        ka.fire(SID, "/tmp", "go", "resume", category=rcs.REVIEW)
        self.assertEqual(self.runs[-1][-1], rcs.NO_RC_FLAG)
        ka.fire(SID, "/tmp", "go", "resume")                               # task_manager: still on
        self.assertNotIn(rcs.NO_RC_FLAG, self.runs[-1])

    def test_invalid_aborts_before_any_host_call(self):
        testenv.put_raw("remote_control_task_manager", "maybe")
        with self.assertRaisesRegex(rcs.RemoteControlError, "remote_control_task_manager"):
            ka.fire(SID, "/tmp", "go", "stop-bg-then-resume")
        self.assertEqual(self.runs, [])                                  # no `claude stop`, no resume


class KaResume(unittest.TestCase):
    """A copy of ka_resume.sh (its run/ dir lands in the temp dir) with a stub tmux: the generated
    launch script has --remote-control unless --no-remote-control is given. Nothing real runs."""
    def launch_line(self, *extra):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(os.path.join(HERE, "ka_resume.sh"), d)
            bindir = os.path.join(d, "bin")
            os.makedirs(bindir)
            tmux = os.path.join(bindir, "tmux")
            with open(tmux, "w") as fh:
                fh.write('#!/bin/sh\ncase "$1" in has-session) exit 1;; esac\nexit 0\n')
            os.chmod(tmux, 0o755)
            env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"], HOME=d, KA_TRUST_ROOT=d)
            r = subprocess.run(["bash", os.path.join(d, "ka_resume.sh"), "--session", SID, "--tmux", "afc-rc-test",
                                "--message", "go", "--cwd", d, *extra], env=env, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("started afc-rc-test", r.stdout)                 # run/<tmux name>.sh (3b S2)
            with open(os.path.join(d, "run", "afc-rc-test.sh")) as fh:
                return [ln for ln in fh.read().splitlines() if " claude " in ln][0]

    def test_default_has_remote_control(self):
        line = self.launch_line()
        self.assertIn(" --resume abcdef01-0000-4000-8000-000000000001 --remote-control --name afc-rc-test ", line)

    def test_no_remote_control(self):
        line = self.launch_line("--new", "--no-remote-control")
        self.assertNotIn("--remote-control", line)
        self.assertIn(" --session-id abcdef01-0000-4000-8000-000000000001 --name afc-rc-test ", line)


class HostBridge(unittest.TestCase):
    """docker/host_exec.py's ka_resume.sh check accepts --no-remote-control as a flag without a value."""
    def setUp(self):
        os.environ["AFCLAUDE_HOST_EXEC_LOG"] = os.path.join(testenv.DIR, "host_exec.log")
        spec = importlib.util.spec_from_file_location("host_exec_rc", os.path.join(HERE, "docker", "host_exec.py"))
        self.he = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.he)

    def tearDown(self):
        os.environ.pop("AFCLAUDE_HOST_EXEC_LOG", None)

    def test_flag_accepted(self):
        self.he.check_ka_resume(["--session", SID, "--tmux", "ka-abcdef01", "--message", "go", rcs.NO_RC_FLAG,
                                 "--new"])   # no exit
        with self.assertRaises(SystemExit):
            self.he.check_ka_resume(["--session", SID, "--tmux", "ka-abcdef01", "--message", "go",
                                     "--remote-control", "off"])


class Export(Base):
    def test_block(self):
        testenv.set_setting("remote_control_review", False)
        testenv.put_raw("remote_control_test", "x")
        b = qv.remote_control_block()
        self.assertEqual({c: (v["enabled"], v["source"][:7]) for c, v in b.items()},
                         {"task_manager": (True, "default"), "review": (False, "db"),
                          "continue": (True, "default"), "test": (None, "invalid")})
        self.assertEqual(b["review"]["setting"], "remote_control_review")


if __name__ == "__main__":
    unittest.main()
