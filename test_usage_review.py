#!/usr/bin/env python3
"""Offline tests for usage_review.py's one-off review (--only / --at, D-220/D-221): run_once()
fires immediately and logs under st["one_off_runs"] without touching the monthly schedule;
schedule_once() spools an at-shim job for the right UTC time without running or scheduling
anything itself; main() wires --only/--at together."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import keepalive as ka  # noqa: E402
import usage_review as ur  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {"fire": ka.fire, "alert": ka.alert, "log": ka.log, "progress_note": ka.progress_note,
                     "state": ur.STATE, "subprocess_run": ur.subprocess.run}
        self.fires = []
        self.alerts = []
        ka.fire = lambda sid, cwd, msg, plan, new=False, **k: self.fires.append(
            (sid, msg, plan, new, k)) or (0, "started", "")
        ka.alert = lambda subject, body="": self.alerts.append((subject, body))
        ka.log = lambda msg: None
        ka.progress_note = lambda line: None
        ur.STATE = os.path.join(self.tmp.name, "usage_review_state.json")

    def tearDown(self):
        ka.fire, ka.alert, ka.log, ka.progress_note = (self.saved["fire"], self.saved["alert"],
                                                        self.saved["log"], self.saved["progress_note"])
        ur.STATE = self.saved["state"]
        ur.subprocess.run = self.saved["subprocess_run"]
        self.tmp.cleanup()


class RunOnce(Base):
    def test_fires_once_and_logs_under_one_off_runs(self):
        st = {"next_run_at": (NOW + timedelta(days=5)).isoformat(), "runs": []}
        ok = ur.run_once(st, NOW, "stage-eta")
        self.assertTrue(ok)
        self.assertEqual(len(self.fires), 1)
        sid, msg, plan, new, k = self.fires[0]
        self.assertEqual(plan, "resume")
        self.assertTrue(new)
        self.assertIn("one-off", k.get("name", ""))
        # only the one-off prompt + guard_respect + manager: never the regular usage_review.md
        # brief, and the owner's verbatim D-220/D-221 concerns are in it
        self.assertNotIn("periodic usage-model review", msg)
        self.assertIn("Schedule the eta review at end of this week", msg)
        self.assertIn("massive underestimate", msg)
        self.assertIn("Maybe count design chats towards afclaude usage", msg)
        self.assertIn("three categories", msg)
        # the monthly schedule is untouched
        self.assertEqual(st["runs"], [])
        self.assertEqual(st["next_run_at"], (NOW + timedelta(days=5)).isoformat())
        self.assertEqual(len(st["one_off_runs"]), 1)
        self.assertEqual(st["one_off_runs"][0]["only"], "stage-eta")
        self.assertEqual(st["one_off_runs"][0]["session"], sid)
        self.assertEqual(self.alerts, [])

    def test_failed_launch_alerts_and_still_does_not_touch_the_schedule(self):
        ka.fire = lambda *a, **k: (1, "", "boom")
        st = {"next_run_at": "2026-11-01T00:00:00+00:00", "runs": []}
        ok = ur.run_once(st, NOW, "stage-eta")
        self.assertFalse(ok)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("one-off stage-eta review launch failed", self.alerts[0][0])
        self.assertEqual(st["next_run_at"], "2026-11-01T00:00:00+00:00")

    def test_two_one_off_runs_each_fire_once(self):
        st = {"next_run_at": "2026-11-01T00:00:00+00:00", "runs": []}
        ur.run_once(st, NOW, "stage-eta")
        ur.run_once(st, NOW + timedelta(days=1), "stage-eta")
        self.assertEqual(len(self.fires), 2)
        self.assertEqual(len(st["one_off_runs"]), 2)
        self.assertNotEqual(st["one_off_runs"][0]["session"], st["one_off_runs"][1]["session"])


class ScheduleOnce(Base):
    def setUp(self):
        super().setUp()
        self.calls = []

        def fake_run(argv, input=None, text=None, capture_output=None, env=None):   # noqa: A002
            self.calls.append({"argv": argv, "input": input, "env": env})
            return type("R", (), {"returncode": 0, "stdout": "job abc123 at 2026-10-15 14:00 UTC\n",
                                  "stderr": ""})()
        ur.subprocess.run = fake_run

    def test_schedules_the_right_utc_time_and_command(self):
        # Thu 15.10.2026 16:00 Europe/Berlin = 14:00 UTC (still CEST, DST ends 25.10.2026)
        res = ur.schedule_once("stage-eta", "2026-10-15 16:00", now=NOW)
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.calls), 1)
        argv, cmd = self.calls[0]["argv"], self.calls[0]["input"]
        self.assertEqual(argv[:2], ["at", "-t"])
        self.assertEqual(argv[2], "202610151400")
        self.assertIn("usage_review.py --only stage-eta --now", cmd)
        self.assertEqual(self.calls[0]["env"]["TZ"], "UTC")
        self.assertEqual(res["at_utc"], datetime(2026, 10, 15, 14, 0, tzinfo=UTC).isoformat())
        self.assertEqual(self.alerts, [])

    def test_winter_time_is_cet(self):
        # 15.12.2026 16:00 Europe/Berlin (CET, UTC+1) = 15:00 UTC
        ur.schedule_once("stage-eta", "2026-12-15 16:00", now=NOW)
        self.assertEqual(self.calls[0]["argv"][2], "202612151500")

    def test_bad_time_is_rejected_without_calling_at(self):
        res = ur.schedule_once("stage-eta", "not a time", now=NOW)
        self.assertFalse(res["ok"])
        self.assertIn("error", res)
        self.assertEqual(self.calls, [])

    def test_at_failure_alerts(self):
        ur.subprocess.run = lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": "no at"})()
        res = ur.schedule_once("stage-eta", "2026-10-15 16:00", now=NOW)
        self.assertFalse(res["ok"])
        self.assertEqual(len(self.alerts), 1)

    def test_scheduling_never_writes_the_review_state(self):
        self.assertFalse(os.path.exists(ur.STATE))
        ur.schedule_once("stage-eta", "2026-10-15 16:00", now=NOW)
        self.assertFalse(os.path.exists(ur.STATE))   # no state file touched: not st["runs"], not next_run_at


class Main(Base):
    def setUp(self):
        super().setUp()
        self.calls = []
        ur.subprocess.run = lambda argv, **k: self.calls.append((argv, k)) or type(
            "R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()

    def test_at_without_only_errors(self):
        old_argv = sys.argv
        sys.argv = ["usage_review.py", "--at", "2026-10-15 16:00"]
        try:
            with self.assertRaises(SystemExit):
                ur.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(self.calls, [])   # argparse error: never got to scheduling

    def test_only_at_schedules_without_firing(self):
        old_argv = sys.argv
        sys.argv = ["usage_review.py", "--only", "stage-eta", "--at", "2026-10-15 16:00"]
        try:
            ur.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.fires), 0)        # scheduled, not fired
        self.assertFalse(os.path.exists(ur.STATE))  # the monthly state untouched

    def test_only_now_fires_without_touching_next_run_at(self):
        old_argv = sys.argv
        sys.argv = ["usage_review.py", "--only", "stage-eta", "--now"]
        try:
            ur.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(len(self.fires), 1)
        with open(ur.STATE) as fh:
            st = json.load(fh)
        self.assertEqual(st["runs"], [])
        self.assertEqual(len(st["one_off_runs"]), 1)
        self.assertEqual(st["next_run_at"], ur.FIRST_RUN.isoformat())   # default: never advanced


if __name__ == "__main__":
    unittest.main()
