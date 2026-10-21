#!/usr/bin/env python3
"""D-260: a project's task-manager model / effort (manager_launch.py) and its use in
keepalive.fire (ka_resume.sh --model/--effort). Offline: a temp DB, ka_resume.sh never runs."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import export_quickview as qv  # noqa: E402
import keepalive as ka  # noqa: E402
import manager_launch as ml  # noqa: E402
import store  # noqa: E402

SID = "abcdef01-0000-4000-8000-000000000001"
OTHER = "abcdef02-0000-4000-8000-000000000002"


class Base(unittest.TestCase):
    def setUp(self):
        testenv.fresh_db(self)                  # no other module's projects (or their foreign keys)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)

    def do(self, _action, /, **params):
        return actions.perform(self.conn, _action, params, actor="cli", via="cli")

    def project(self, name, manager=SID, **launch):
        self.do("project.add", name=name)
        if manager:
            self.do("project.edit", project=name, manager_session=manager)
        if launch:
            self.do("project.manager_launch", project=name, **launch)

    def poke(self, name, **cols):
        """Write around actions.py (an older/newer writer, a hand edit): the read must still check it."""
        self.conn.execute(f"UPDATE projects SET {', '.join(f'{k}=?' for k in cols)} WHERE name=?",
                          [*cols.values(), name])
        self.conn.commit()


class Checks(unittest.TestCase):
    def test_choices(self):
        self.assertEqual((ml.DEFAULT_MODEL, ml.DEFAULT_EFFORT), (ka.LAUNCH["model"], ka.LAUNCH["effort"]))
        self.assertIn(ml.DEFAULT_MODEL, ml.MODELS)
        self.assertIn(ml.DEFAULT_EFFORT, ml.EFFORTS)
        self.assertFalse([m for m in ml.MODELS if "haiku" in m])
        self.assertEqual(ml.check_model(" claude-sonnet-5-5 "), "claude-sonnet-5-5")
        self.assertEqual(ml.check_effort("MAX"), "max")
        for bad in ("opus", "", None, "claude-haiku-5-5", 3):
            with self.assertRaisesRegex(ValueError, "model must be one of", msg=repr(bad)):
                ml.check_model(bad)
        for bad in ("", "ultra", None, 1):
            with self.assertRaisesRegex(ValueError, "effort must be one of", msg=repr(bad)):
                ml.check_effort(bad)

    def test_host_bridge_allows_every_choice(self):
        """docker/host_exec.py checks ka_resume.sh's --effort / --model: every choice must pass it."""
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "docker", "host_exec.py")) as fh:
            src = fh.read()
        for e in ml.EFFORTS:
            self.assertIn(f'"{e}"', src)


class ForSession(Base):
    def test_unmanaged_or_unset_is_empty(self):
        self.assertEqual(ml.for_session(self.conn, SID), {})
        self.project("P")
        self.assertEqual(ml.for_session(self.conn, SID), {})
        self.project("Q", manager=OTHER, model="claude-sonnet-5-5")
        self.assertEqual(ml.for_session(self.conn, SID), {})

    def test_set_values(self):
        self.project("P", effort="max")
        self.assertEqual(ml.for_session(self.conn, SID), {"effort": "max"})
        self.do("project.manager_launch", project="P", model="claude-sonnet-5-5")
        self.assertEqual(ml.for_session(self.conn, SID), {"model": "claude-sonnet-5-5", "effort": "max"})
        self.project("Q", model="claude-sonnet-5-5", effort="max")     # same session, same launch: fine
        self.assertEqual(ml.for_session(self.conn, SID), {"model": "claude-sonnet-5-5", "effort": "max"})

    def test_invalid_stored_value_raises(self):
        self.project("P")
        self.poke("P", manager_model="gpt-9")
        with self.assertRaisesRegex(ml.LaunchError, "project 'P': stored task-manager launch is invalid: model"):
            ml.for_session(self.conn, SID)

    def test_conflicting_projects_raise(self):
        self.project("P", model="claude-sonnet-5-5")
        self.project("Q")                                               # unset = the default: differs
        with self.assertRaisesRegex(ml.LaunchError, "different task-manager launches"):
            ml.for_session(self.conn, SID)


class Fire(Base):
    """keepalive.fire passes the project's launch to ka_resume.sh; a bad one aborts before anything runs."""
    def setUp(self):
        super().setUp()
        self.old = ka.host.run_on_host, dict(ka.LAUNCH)
        self.runs = []
        ka.host.run_on_host = lambda argv, **k: self.runs.append(argv) or types.SimpleNamespace(
            returncode=0, stdout="started", stderr="")

    def tearDown(self):
        ka.host.run_on_host, launch = self.old
        ka.LAUNCH.clear()
        ka.LAUNCH.update(launch)
        super().tearDown()

    def flags(self):
        argv = self.runs[-1]
        return {a[2:]: argv[i + 1] for i, a in enumerate(argv) if a in ("--model", "--effort", "--name")}

    def test_default_launch(self):
        ka.fire(SID, "/tmp", "go", "resume")
        self.assertEqual(self.flags(), {"model": "claude-opus-5-5", "effort": "high", "name": ka.LAUNCH["name"]})

    def test_project_launch_wins(self):
        self.project("P", model="claude-sonnet-5-5", effort="xhigh")
        ka.fire(SID, "/tmp", "go", "resume", name="mgr", model="claude-opus-5")   # e.g. the dispatcher's
        self.assertEqual(self.flags(), {"model": "claude-sonnet-5-5", "effort": "xhigh", "name": "mgr"})
        ka.fire(OTHER, "/tmp", "go", "resume", model="claude-opus-5")             # not a task-manager
        self.assertEqual(self.flags()["model"], "claude-opus-5")
        self.do("project.manager_launch", project="P", model="default")           # back to LAUNCH's model
        ka.fire(SID, "/tmp", "go", "send-keys")
        self.assertEqual((self.flags()["model"], self.flags()["effort"]), ("claude-opus-5-5", "xhigh"))

    def test_invalid_launch_aborts_before_any_host_call(self):
        self.project("P")
        self.poke("P", manager_effort="turbo")
        with self.assertRaisesRegex(ml.LaunchError, "effort must be one of"):
            ka.fire(SID, "/tmp", "go", "stop-bg-then-resume")
        self.assertEqual(self.runs, [])                                 # no `claude stop`, no resume


class Export(Base):
    def test_block(self):
        self.project("P", model="claude-sonnet-5-5")
        self.project("Q", manager=None)
        b = qv.manager_launch_block()
        self.assertEqual(b["default"], {"model": "claude-opus-5-5", "effort": "high"})
        self.assertEqual({p["name"]: (p["model"], p["effort"], p["source"]) for p in b["projects"]},
                         {"P": ("claude-sonnet-5-5", "high", {"model": "project", "effort": "default"}),
                          "Q": ("claude-opus-5-5", "high", {"model": "default", "effort": "default"})})


if __name__ == "__main__":
    unittest.main()
