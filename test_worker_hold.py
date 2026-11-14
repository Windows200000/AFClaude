"""D-340: holding background workers' reports across the task-manager's hand-off + /clear
(worker_hold.py, `tasks.py worker`), the hand-off's held_workers field and the prompts that use them."""
import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import testenv  # noqa: F401  (before any AFClaude module: no live state)
import worker_hold as wh

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 8, 15, tzinfo=UTC)
SID = "39d4e2ae-0000-4000-8000-000000000001"


class Clock:
    """A fake clock that a fake sleep moves on (no real waiting)."""
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class HoldGoWait(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = wh.hold_dir(os.path.join(self.tmp.name, "afclaude.db"))
        self.clock = Clock(T0)

    def wait(self, agent="a1", max_seconds=wh.WAIT_MAX, on_sleep=None):
        def sleep(s):
            self.clock.sleep(s)
            if on_sleep:
                on_sleep(self.clock.t)
        return wh.wait(self.d, agent, max_seconds, wh.POLL, self.clock.now, sleep)

    def test_a_worker_never_held_reports_at_once(self):
        self.assertEqual(self.wait(), ("free", None))

    def test_the_hold_lives_next_to_the_database_and_is_private(self):
        wh.hold(self.d, ["a1", "a2"], T0)
        self.assertEqual(self.d, os.path.join(self.tmp.name, "worker_holds"))
        self.assertEqual(os.stat(self.d).st_mode & 0o777, 0o700)
        self.assertEqual([h["agent"] for h in wh.holds(self.d)], ["a1", "a2"])

    def test_a_held_worker_waits_until_the_go(self):
        wh.hold(self.d, ["a1"], T0)
        state, at = self.wait(on_sleep=lambda t: t >= T0 + timedelta(minutes=2) and wh.go(self.d, ["a1"]))
        self.assertEqual((state, at), ("go", T0))
        self.assertEqual(self.clock.t, T0 + timedelta(minutes=2))
        self.assertEqual(wh.holds(self.d), [])

    def test_still_held_after_one_call_says_run_again(self):
        wh.hold(self.d, ["a1"], T0)
        self.assertEqual(self.wait(max_seconds=60), ("held", T0))
        self.assertEqual(self.clock.t, T0 + timedelta(seconds=60))

    def test_the_hold_runs_out_after_hold_max(self):
        """Bounded (D-307 #4's exception): a forgotten go costs HOLD_MAX, never a lost report."""
        wh.hold(self.d, ["a1"], T0)
        self.clock.t = T0 + wh.HOLD_MAX - timedelta(seconds=20)
        self.assertEqual(self.wait(), ("expired", T0))
        self.assertLessEqual(self.clock.t, T0 + wh.HOLD_MAX + timedelta(seconds=wh.POLL))
        self.assertEqual(wh.holds(self.d), [])                    # the flag is gone: a later wait is free
        self.assertEqual(self.wait(), ("free", None))

    def test_go_all_and_unknown_ids(self):
        wh.hold(self.d, ["a1", "a2"], T0)
        self.assertEqual(wh.go(self.d, ["a2", "zz"]), (["a2"], ["zz"]))
        self.assertEqual(wh.go(self.d, [], every=True), (["a1"], []))

    def test_a_bad_agent_id_is_refused(self):
        for bad in ("", "../x", "a b", "x" * 65):
            with self.assertRaises(ValueError):
                wh.hold(self.d, [bad])
        self.assertFalse(os.path.exists(self.d))

    def test_an_unreadable_flag_is_an_error(self):
        wh.hold(self.d, ["a1"], T0)
        with open(os.path.join(self.d, "a1"), "w") as fh:
            fh.write("{")
        with self.assertRaises(ValueError):
            self.wait()


class Cli(unittest.TestCase):
    """`tasks.py worker ...`: no database connection is opened (a waiting worker holds none)."""
    def setUp(self):
        import tasks
        self.tasks = tasks
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "afclaude.db")

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, env or {}), \
                mock.patch.object(self.tasks.store, "connect", side_effect=AssertionError("connected")):
            rc = self.tasks.main(["--db", self.db, "worker", *argv])
        return rc, out.getvalue() + err.getvalue()

    def test_hold_wait_go(self):
        rc, out = self.run_cli("hold", "a1", "a2")
        self.assertEqual(rc, 0)
        self.assertIn("tasks.py worker go a1 a2", out)
        rc, out = self.run_cli("list")
        self.assertIn("a1  held since", out)
        rc, out = self.run_cli("wait", "a1", "--max", "0")
        self.assertEqual(rc, wh.EXIT_HELD)
        self.assertIn("run this command again; do not end your turn", out)
        rc, out = self.run_cli("go", "a1", "a2")
        self.assertEqual((rc, out.strip()), (0, "released a1, a2"))
        rc, out = self.run_cli("wait", "a1")
        self.assertEqual((rc, out.strip()), (0, "not held: send your report now"))

    def test_wait_takes_the_agent_id_from_the_environment(self):
        self.run_cli("hold", "a7")
        rc, _ = self.run_cli("wait", "--max", "0", env={wh.AGENT_VAR: "a7"})
        self.assertEqual(rc, wh.EXIT_HELD)

    def test_errors_exit_2(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(wh.AGENT_VAR, None)
            rc, out = self.run_cli("wait")
        self.assertEqual(rc, wh.EXIT_ERROR)
        self.assertIn("no agent id", out)
        rc, out = self.run_cli("go")
        self.assertEqual(rc, wh.EXIT_ERROR)
        self.assertIn("--all", out)


class HandoffField(unittest.TestCase):
    def test_held_workers_is_stored_and_rendered(self):
        import store
        body = store.handoff_body({"summary": "s", "held_workers": ["a1b2: K4b fix round, branch k4b-fix"]})
        self.assertEqual(body["held_workers"], ["a1b2: K4b fix round, branch k4b-fix"])
        text = store.render_handoff({"id": 5, "created_at": "x", "body": body})
        self.assertIn("Held workers (waiting for your go): (1) a1b2: K4b fix round, branch k4b-fix", text)

    def test_an_item_must_start_with_the_agent_id(self):
        import store
        with self.assertRaisesRegex(ValueError, "must start with the worker's agent id"):
            store.handoff_body({"summary": "s", "held_workers": ["the K4b worker"]})


class Prompts(unittest.TestCase):
    def test_the_save_prompt_holds_the_workers_first(self):
        import keepalive as ka
        text = ka.save_text(SID, "mid-run")
        hold = text.index(f"python3 {ka.TASKS_PY} worker hold <agent id>")
        self.assertLess(hold, text.index("handoff write"))
        for part in ("SendMessage", "HOLD (D-340)", "TaskStop", "held_workers"):
            self.assertIn(part, text)
        for refresh in (ka.save_refresh_text(SID, 9, "x"), ka.save_retry_text(SID, 8)):
            self.assertIn("held_workers", refresh)

    def test_the_resume_text_releases_them(self):
        import keepalive as ka
        text = ka.load_prompt("compact_resume").format(handoff="H", progress="P.md", tasks="/r/tasks.py",
                                                       when="w", compaction="c")
        self.assertIn("`python3 /r/tasks.py worker go <agent id> [<agent id> ...]`", text)
        self.assertIn('"GO: send your report now."', text)

    def test_the_worker_and_manager_rules_carry_the_procedure(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, ".claude", "agents", "afclaude-worker.md")) as fh:
            worker = fh.read()
        for part in ("Hold (D-340)", "tasks.py worker wait <your agent id>", "exits 3", "D-307 #4"):
            self.assertIn(part, worker)
        with open(os.path.join(here, "prompts", "manager.md")) as fh:
            manager = fh.read()
        for part in ("tasks.py worker hold|go <agent id>", "held_workers", "SendMessage HOLD / GO", "timers"):
            self.assertIn(part, manager)


if __name__ == "__main__":
    unittest.main()
