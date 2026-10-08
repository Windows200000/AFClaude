#!/usr/bin/env python3
"""Offline tests for the fill-up run (D-212): fillup.py (pure) and keepalive's fill-up pass."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic DB / data dir)
import fillup  # noqa: E402
import keepalive as ka  # noqa: E402
import run_metrics  # noqa: E402

UTC = timezone.utc
RESET = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)          # 04:00 Berlin: the 23:00 run's session reset
FIRE_AT = RESET - timedelta(hours=5) + timedelta(seconds=40)


def rows(*rates):
    return [{"start": (RESET - timedelta(days=30 - i)).isoformat(), "rate_pct_per_h": r} for i, r in enumerate(rates)]


def handled(**extra):
    h = {"window-start-2026-10-08": {"at": FIRE_AT.isoformat(), "result": "continued-in-place",
                                     "reason": "window start, CONTINUE: ...; budget for this run +14.0% (...)",
                                     "usage": {"weekly": {"percent": 40.0}}}}
    h.update(extra)
    return h


def decide(**kw):
    base = dict(now=RESET - timedelta(minutes=5), session_pct=95.0, session_reset=RESET, weekly_pct=53.0,
                handled=handled(), rows=rows(60, 70, 50), ratio=0.14, enabled=True, factor=1.1, paused=False,
                run_active=False, limit_hit=False, in_night=True, in_last_stretch=False, next_start=None,
                user_active=False, user_recheck=None, budget_fallback=lambda: None)
    base.update(kw)
    return fillup.decide(**base)


class Computation(unittest.TestCase):
    def test_rate_median_of_recent_runs(self):
        self.assertEqual(fillup.fill_rate(rows(60, 70, 50))[0], 60.0)
        r, src = fillup.fill_rate(rows(*([10] * 5 + [100] * 10)))     # only the last 10 count
        self.assertEqual(r, 100.0)
        self.assertIn("10 runs", src)

    def test_rate_fallback(self):
        self.assertEqual(fillup.fill_rate([])[0], fillup.DEFAULT_RATE)
        self.assertEqual(fillup.fill_rate([{"rate_pct_per_h": None}, "junk"])[0], fillup.DEFAULT_RATE)

    def test_formula_with_factor(self):
        # 5% at 60 %/h = 5 min; x 1.1 = 5.5 min before the reset
        self.assertEqual(fillup.start_time(RESET, 5, 60, 1.1), RESET - timedelta(minutes=5.5))
        self.assertEqual(fillup.start_time(RESET, 5, 60, 2.0), RESET - timedelta(minutes=10))
        p = fillup.plan(RESET, 95, rows(60), 1.1, 0.14)
        self.assertAlmostEqual(p["cost"], 0.7)
        self.assertAlmostEqual(p["time_min"], 5.0)

    def test_run_budget(self):
        self.assertEqual(fillup.run_budget({"budget_pct": 3.5}), 3.5)
        self.assertEqual(fillup.run_budget({"reason": "x; budget for this run +14.0% (y)"}), 14.0)
        self.assertIsNone(fillup.run_budget({"reason": "nothing"}))


class Decide(unittest.TestCase):
    def test_wait_then_fire(self):
        d = decide(now=RESET - timedelta(minutes=30))
        self.assertEqual(d["status"], "wait")
        self.assertEqual(d["recheck_at"], RESET - timedelta(minutes=5.5))
        self.assertEqual(decide()["status"], "fire")
        self.assertEqual(decide()["key"], fillup.key(RESET))

    def test_dedup_per_session_window(self):
        h = handled(**{fillup.key(RESET + timedelta(milliseconds=400)): {"result": "continued-in-place"}})
        self.assertEqual(decide(handled=h)["status"], "done")

    def test_skips(self):
        cases = {
            "off": dict(enabled=False),
            "none": dict(handled={}),
        }
        for want, kw in cases.items():
            self.assertEqual(decide(**kw)["status"], want, kw)
        for kw in (dict(limit_hit=True), dict(session_pct=100.0), dict(in_night=False),
                   dict(session_pct=99.5), dict(next_start=RESET - timedelta(minutes=1)),
                   dict(weekly_pct=55.0), dict(now=RESET - timedelta(seconds=30))):
            self.assertEqual(decide(**kw)["status"], "skip", kw)
        self.assertEqual(decide(in_night=False, in_last_stretch=True)["status"], "fire")

    def test_budget_fallback_when_unknown(self):
        h = {"window-start-x": {"at": FIRE_AT.isoformat(), "result": "continued-in-place", "reason": "?"}}
        self.assertEqual(decide(handled=h, budget_fallback=lambda: 0.7)["status"], "fire")
        self.assertEqual(decide(handled=h, budget_fallback=lambda: None)["status"], "skip")

    def test_run_still_going_waits(self):
        self.assertEqual(decide(run_active=True)["status"], "wait")

    def test_paused_and_owner_active(self):
        self.assertEqual(decide(paused=True)["status"], "hold")
        self.assertEqual(decide(user_active=True, user_recheck=RESET + timedelta(minutes=20))["status"], "skip")
        d = decide(now=RESET - timedelta(minutes=40), session_pct=50, weekly_pct=41.0,
                   user_active=True, user_recheck=RESET - timedelta(minutes=20))
        self.assertEqual(d["status"], "hold")
        self.assertEqual(d["recheck_at"], RESET - timedelta(minutes=20))

    def test_fill_up_fire_is_not_the_window_run(self):
        h = {fillup.key(RESET - timedelta(hours=5)): {"at": FIRE_AT.isoformat(), "result": "continued-in-place"}}
        self.assertIsNone(fillup.window_run(h, RESET))

    def test_run_metrics_kind(self):
        self.assertEqual(run_metrics.fire_kind(fillup.key(RESET)), "fill-up")
        self.assertEqual(run_metrics.fire_kind(fillup.test_key(RESET)), "fill-up")


class Test(unittest.TestCase):
    def test_decide_test(self):
        t = {"key": fillup.test_key(RESET), "start": (RESET - timedelta(minutes=3)).isoformat(),
             "reset": RESET.isoformat(), "remaining": 3}
        dt = fillup.decide_test
        self.assertEqual(dt(now=RESET - timedelta(minutes=4), test=t, handled={}, paused=False)["status"], "wait")
        self.assertEqual(dt(now=RESET - timedelta(minutes=2), test=t, handled={}, paused=False)["status"], "fire")
        self.assertEqual(dt(now=RESET - timedelta(minutes=2), test=t, handled={}, paused=True)["status"], "hold")
        self.assertEqual(dt(now=RESET - timedelta(minutes=2), test=t, handled={t["key"]: {}},
                            paused=False)["status"], "done")
        self.assertEqual(dt(now=RESET, test=t, handled={}, paused=False)["status"], "expired")


class FirePath(unittest.TestCase):
    """keepalive.fillup_pass -> handle_fire with prompt fillup, dedup through the state file."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (ka.STATE_FILE, ka.FILLUP_FILE, ka.read_usage_cache, ka.fresh_usage, ka.handle_fire,
                    ka.run_active, ka._fillup_user, ka._limit_hit_in, ka.PROGRESS_FILE)
        ka.STATE_FILE = os.path.join(self.tmp.name, "state.json")
        ka.FILLUP_FILE = os.path.join(self.tmp.name, "fillup.json")
        ka.PROGRESS_FILE = os.path.join(self.tmp.name, "PROGRESS.md")
        ka.save_state({"handled": handled(), "fires": {}})
        u = {"session": {"percent": 95.0, "resets_at": RESET}, "weekly": {"percent": 53.0, "resets_at": RESET
                                                                           + timedelta(days=3)},
             "fetched_at": RESET - timedelta(minutes=7)}
        ka.read_usage_cache = lambda: u
        ka.fresh_usage = lambda now, force=False: u
        ka.run_active = lambda sid, now, st=None: None
        ka._fillup_user = lambda now, ls, s: (False, None)
        ka._limit_hit_in = lambda sid, reset: False
        self.fired = []

        def fake_fire(sid, stall, reason, st, args):
            self.fired.append((stall, reason))
            st["handled"][stall["uuid"]] = {"at": datetime.now(UTC).isoformat(), "result": "continued-in-place"}
            ka.save_state(st)
        ka.handle_fire = fake_fire

    def tearDown(self):
        (ka.STATE_FILE, ka.FILLUP_FILE, ka.read_usage_cache, ka.fresh_usage, ka.handle_fire,
         ka.run_active, ka._fillup_user, ka._limit_hit_in, ka.PROGRESS_FILE) = self.old
        self.tmp.cleanup()

    def test_fires_once_with_fillup_prompt(self):
        import schedule
        old = schedule.next_session_start
        schedule.next_session_start = lambda now, cfg=None: None
        try:
            ka.fillup_pass("s" * 36, RESET - timedelta(minutes=30), None)
            self.assertEqual(self.fired, [])
            self.assertEqual(json.load(open(ka.FILLUP_FILE))["plan"]["status"], "wait")
            ka.fillup_pass("s" * 36, RESET - timedelta(minutes=5), None)
            ka.fillup_pass("s" * 36, RESET - timedelta(minutes=4), None)
        finally:
            schedule.next_session_start = old
        self.assertEqual(len(self.fired), 1)
        stall, reason = self.fired[0]
        self.assertEqual(stall["prompt"], "fillup")
        self.assertEqual(stall["uuid"], fillup.key(RESET))
        self.assertEqual(stall["fields"]["remaining"], "5")
        msg = ka.session_message("fillup", reason=reason, progress="P.md", **stall["fields"])
        self.assertIn("remaining ~5%", msg)
        self.assertIn("≥95%", msg)       # the manager prompt's stop comes from session_stop_pct

    def test_test_entry(self):
        t = {"session": "s" * 36, "key": fillup.test_key(RESET), "start": (RESET - timedelta(minutes=3)).isoformat(),
             "reset": RESET.isoformat(), "remaining": 3.0}
        ka.save_fillup({"test": t})
        ka.fillup_pass("s" * 36, RESET - timedelta(minutes=2), None)
        self.assertEqual(len(self.fired), 1)
        self.assertEqual(self.fired[0][0]["uuid"], t["key"])
        self.assertIn("TEST", self.fired[0][1])
        self.assertNotIn("test", ka.load_fillup())      # done: the entry is removed


class StopSetting(unittest.TestCase):
    def test_manager_prompt_reads_setting(self):
        testenv.set_setting("session_stop_pct", 90)
        try:
            self.assertIn("≥90%", ka.session_message("continue", reason="r", progress="p"))
        finally:
            testenv.set_setting("session_stop_pct", None)


if __name__ == "__main__":
    unittest.main()
