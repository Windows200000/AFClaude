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
                   dict(session_pct=99.5), dict(next_start=RESET - timedelta(hours=2)),
                   dict(weekly_pct=55.0), dict(now=RESET - timedelta(seconds=30))):
            self.assertEqual(decide(**kw)["status"], "skip", kw)
        self.assertEqual(decide(in_night=False, in_last_stretch=True)["status"], "fire")

    def test_next_start_skip_rule(self):
        # base: remaining 5%, rate 60 %/h -> fill-up duration (unscaled) 5 min.
        # a start strictly inside the window, well over 5 min before the reset: it would use
        # up the window's own remaining % itself, so the fill-up is skipped.
        self.assertEqual(decide(next_start=RESET - timedelta(hours=2))["status"], "skip")
        # D-219: a start too close to the reset to fill up the window itself (less than the
        # fill-up duration before it) does NOT excuse the fill-up.
        self.assertEqual(decide(next_start=RESET - timedelta(minutes=1))["status"], "fire")
        # a start that coincides with the reset opens a NEW session window, not a continuation
        # of this one -- the fill-up of THIS window still applies (the 23:00 run / 04:00 reset
        # case from D-219: "the 04:00 start" must not cancel the fill-up planned before 04:00).
        self.assertEqual(decide(next_start=RESET)["status"], "fire")
        # a start after the reset is even more clearly a new window.
        self.assertEqual(decide(next_start=RESET + timedelta(minutes=30))["status"], "fire")

    def test_fires_when_run_stopped_early_below_session_stop_pct(self):
        # the task-manager can go idle and stop well short of session_stop_pct (D-014's 95%
        # default); the fill-up must still cover whatever % is actually left, not just the
        # last few points of a run that stopped exactly at session_stop_pct.
        d = decide(now=RESET - timedelta(minutes=10), session_pct=66.0, weekly_pct=41.0)
        self.assertEqual(d["status"], "fire", d)
        self.assertAlmostEqual(d["remaining"], 34.0)

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


class Clock:
    """run()'s clock: _sleep advances it (no real waiting)."""
    def __init__(self, t):
        self.t = t
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += timedelta(seconds=s)


class ExactTiming(unittest.TestCase):
    """Follow-up 1: the watcher wakes at a planned fill-up start / a last-stretch slot start
    instead of up to a fill-up interval (60 s) or a poll (30 s) late (live test: fired 37 s late)."""
    SID = "f" * 8 + "-0000-0000-0000-" + "0" * 12

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        names = ("STATE_FILE", "FILLUP_FILE", "LOCK_FILE", "STOP_FILE", "PROGRESS_FILE", "read_usage_cache",
                 "fresh_usage", "handle_fire", "run_active", "_fillup_user", "_limit_hit_in", "stall_status",
                 "last_mile_pass", "deferred_window_start_pass", "watch_run_usage", "last_mile_next_start",
                 "log", "_now", "_sleep")
        self.old = {n: getattr(ka, n) for n in names}
        d = self.tmp.name
        ka.STATE_FILE, ka.FILLUP_FILE = os.path.join(d, "state.json"), os.path.join(d, "fillup.json")
        ka.LOCK_FILE, ka.STOP_FILE = os.path.join(d, "ka.lock"), os.path.join(d, "STOP")
        ka.PROGRESS_FILE = os.path.join(d, "PROGRESS.md")
        ka.save_state({"handled": handled(), "fires": {}})
        self.usage = {"session": {"percent": 95.0, "resets_at": RESET},
                      "weekly": {"percent": 53.0, "resets_at": RESET + timedelta(days=3)},
                      "fetched_at": RESET - timedelta(minutes=30)}
        self.refreshes = []

        def fresh(now, force=False):
            self.refreshes.append(now)
            self.usage = dict(self.usage, fetched_at=now)
            return self.usage
        ka.read_usage_cache = lambda: self.usage
        ka.fresh_usage = fresh
        ka.run_active = lambda sid, now, st=None: None
        ka._fillup_user = lambda now, ls, s: (False, None)
        ka._limit_hit_in = lambda sid, reset: False
        ka.stall_status = lambda sid, now: ("NO_STALL", "x", None)
        ka.last_mile_pass = lambda sid, now, st, args: None
        ka.deferred_window_start_pass = lambda sid, now, st, args: None
        ka.watch_run_usage = lambda sid, now: None
        ka.last_mile_next_start = lambda now, usage=None: None
        ka.log = lambda msg: None
        self.fired = []

        def fake_fire(sid, stall, reason, st, args):
            self.fired.append((self.clock.t, stall["uuid"]))
            st["handled"][stall["uuid"]] = {"at": self.clock.t.isoformat(), "result": "continued-in-place"}
            ka.save_state(st)
            open(ka.STOP_FILE, "w").close()          # done: let run() return
        ka.handle_fire = fake_fire
        import schedule
        self.old_nss = schedule.next_session_start
        schedule.next_session_start = lambda now, cfg=None: None

    def tearDown(self):
        import schedule
        schedule.next_session_start = self.old_nss
        for n, v in self.old.items():
            setattr(ka, n, v)
        self.tmp.cleanup()

    def run_loop(self, t0, until):
        self.clock = Clock(t0)
        ka._now = self.clock.now

        def sleep(s):
            self.clock.sleep(s)
            if self.clock.t > until:
                open(ka.STOP_FILE, "w").close()
        ka._sleep = sleep

        class A:
            session, arm, once, now = self.SID, True, False, False
        ka.run(A())

    def test_sleep_seconds(self):
        now = RESET
        self.assertEqual(ka.sleep_seconds(now), ka.POLL_SECONDS)
        self.assertEqual(ka.sleep_seconds(now, now + timedelta(seconds=7), None, now + timedelta(minutes=5)), 7)
        self.assertEqual(ka.sleep_seconds(now, now - timedelta(seconds=7)), ka.POLL_SECONDS)   # past: ignored
        self.assertEqual(ka.sleep_seconds(now, now + timedelta(milliseconds=10)), ka.MIN_SLEEP)

    def test_fillup_pass_returns_the_planned_start(self):
        start = RESET - timedelta(minutes=5.5)          # 5% at 60 %/h x 1.1
        due = ka.fillup_pass(self.SID, RESET - timedelta(minutes=30), None)
        self.assertEqual(due, start)
        self.assertEqual(self.refreshes, [])            # far from the start: the cache is enough

    def test_usage_refreshed_ahead_then_fires_at_start_without_waiting_for_usage(self):
        start = RESET - timedelta(minutes=5.5)
        ka.fillup_pass(self.SID, start - timedelta(seconds=90), None)     # inside FILLUP_PREFETCH
        self.assertEqual(self.refreshes, [start - timedelta(seconds=90)])
        ka.fresh_usage = lambda now, force=False: self.fail("no /usage call at the start: numbers are fresh")
        self.clock = Clock(start)
        ka.fillup_pass(self.SID, start, None)
        self.assertEqual([k for _, k in self.fired], [fillup.key(RESET)])

    def test_stale_usage_at_the_start_is_refreshed_first(self):
        start = RESET - timedelta(minutes=5.5)
        self.clock = Clock(start)
        ka.fillup_pass(self.SID, start, None)
        self.assertEqual(self.refreshes, [start])
        self.assertEqual(len(self.fired), 1)

    def test_watcher_fires_the_fillup_at_its_start(self):
        start = RESET - timedelta(minutes=5.5)
        self.run_loop(start - timedelta(minutes=7, seconds=13), until=RESET)
        self.assertEqual(len(self.fired), 1, self.fired)
        late = (self.fired[0][0] - start).total_seconds()
        self.assertTrue(0 <= late <= 2, late)           # was up to FILLUP_EVERY (60 s) late

    def test_watcher_fires_a_test_fillup_at_its_start(self):
        start = RESET - timedelta(minutes=3, seconds=1)
        ka.save_fillup({"test": {"session": self.SID, "key": fillup.test_key(RESET), "start": start.isoformat(),
                                 "reset": RESET.isoformat(), "remaining": 3.0}})
        self.run_loop(start - timedelta(minutes=35, seconds=49), until=RESET)
        self.assertEqual([k for _, k in self.fired], [fillup.test_key(RESET)])
        late = (self.fired[0][0] - start).total_seconds()
        self.assertTrue(0 <= late <= 2, late)           # the live test: 37 s late

    def test_watcher_wakes_at_a_last_stretch_slot_start(self):
        slot = RESET + timedelta(hours=1)
        calls = []
        ka.last_mile_pass = lambda sid, now, st, args: calls.append(now) or now + timedelta(minutes=15)  # a HOLD
        ka.last_mile_next_start = lambda now, usage=None: slot if now < slot else None
        old_fp, ka.fillup_pass = ka.fillup_pass, lambda sid, now, args: None
        try:
            self.run_loop(slot - timedelta(minutes=4, seconds=47), until=slot + timedelta(seconds=40))
        finally:
            ka.fillup_pass = old_fp
        after = [c for c in calls if c >= slot]
        self.assertTrue(after, calls)
        self.assertLessEqual((after[0] - slot).total_seconds(), 2)   # not at the HOLD's 15-min recheck

    def test_last_mile_next_start(self):
        old = ka.last_mile_hours
        ka.last_mile_hours = lambda pct, reset=None, now=None: 10.0
        try:
            u = {"weekly": {"percent": 80.0, "resets_at": RESET}}
            nxt = self.old["last_mile_next_start"]
            self.assertEqual(nxt(RESET - timedelta(hours=12), u), RESET - timedelta(hours=10))   # opens
            self.assertEqual(nxt(RESET - timedelta(hours=9), u), RESET - timedelta(hours=5))     # slot 1
            self.assertIsNone(nxt(RESET - timedelta(hours=1), u))
            self.assertIsNone(nxt(RESET, {"weekly": {}}))
            ka.last_mile_hours = lambda pct, reset=None, now=None: 7.0
            self.assertEqual(nxt(RESET - timedelta(hours=8), u), RESET - timedelta(hours=7))
            self.assertEqual(nxt(RESET - timedelta(hours=6), u), RESET - timedelta(hours=5))
        finally:
            ka.last_mile_hours = old


class StopSetting(unittest.TestCase):
    def test_manager_prompt_reads_setting(self):
        testenv.set_setting("session_stop_pct", 90)
        try:
            self.assertIn("≥90%", ka.session_message("continue", reason="r", progress="p"))
        finally:
            testenv.set_setting("session_stop_pct", None)


if __name__ == "__main__":
    unittest.main()
