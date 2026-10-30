#!/usr/bin/env python3
"""Offline tests for the run timing (D-274/D-275): a session-window start whose gate passed fires
late in its session window (window end - remaining / rate x run_start_factor, fillup.run_start),
or at once when the run before is still going (chained); every start's continue carries the
pacing lines (run_pace.md, run_chain.md); the fill-up and the inbox's held messages wait for the
planned start instead of the session-window start."""
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import fillup  # noqa: E402
import inbox  # noqa: E402
import keepalive as ka  # noqa: E402
import runner_state  # noqa: E402
import schedule  # noqa: E402

UTC = timezone.utc
SID = "00000000-0000-4000-8000-000000000001"
NIGHT = datetime(2026, 10, 4, 21, 0, tzinfo=UTC)         # Sun 23:00 Berlin, the night's first start
S2 = NIGHT + timedelta(hours=5)                           # Mon 04:00 Berlin, its second start (= the first's end)
END2 = S2 + timedelta(hours=5)                            # Mon 09:00 Berlin, the window's end
RATE = 60.0                                               # fillup.DEFAULT_RATE: 100 % take 100 min


def plan(**kw):
    args = dict(now=NIGHT, window_start=NIGHT, window_end=S2, session_pct=None, session_reset=None, rows=[],
                factor=1.5, ratio=0.15, headroom=None)
    args.update(kw)
    return fillup.run_start(**args)


class RunStart(unittest.TestCase):
    """fillup.run_start: start = window end - remaining / rate x factor."""

    def test_fresh_session_window(self):
        p = plan()
        self.assertEqual((p["remaining"], p["src"], p["rate"]), (100.0, "a fresh session window", RATE))
        self.assertEqual(p["start"], S2 - timedelta(minutes=150))       # 100 min x 1.5 before 04:00
        self.assertFalse(p["resets_inside"])

    def test_live_session_window_to_the_end(self):
        p = plan(session_pct=40.0, session_reset=S2 + timedelta(minutes=5))
        self.assertEqual(p["remaining"], 60.0)
        self.assertEqual(p["start"], S2 - timedelta(minutes=90))

    def test_live_session_window_resetting_inside(self):
        """The owner's evening session resets at 01:50: the run gets a fresh one after it."""
        p = plan(session_pct=32.0, session_reset=NIGHT + timedelta(hours=2, minutes=50))
        self.assertEqual(p["remaining"], 100.0)
        self.assertTrue(p["resets_inside"])

    def test_budget_caps_the_remaining(self):
        p = plan(headroom=3.0, ratio=0.15)                               # 3 weekly % = 20 session %
        self.assertAlmostEqual(p["remaining"], 20.0)
        self.assertEqual(p["src"], "the run's weekly budget")
        self.assertEqual(p["start"], S2 - timedelta(minutes=30))

    def test_measured_rate(self):
        rows = [{"start": f"2026-10-0{i}T21:00:00Z", "rate_pct_per_h": r} for i, r in enumerate((50, 75, 100), 1)]
        p = plan(rows=rows)
        self.assertEqual(p["rate"], 75.0)
        self.assertEqual(p["start"], S2 - timedelta(minutes=120))

    def test_never_before_the_window_start_or_now(self):
        self.assertEqual(plan(factor=5.0)["start"], NIGHT)
        self.assertEqual(plan(factor=0)["start"], NIGHT)                 # 0: at the session-window start
        later = S2 - timedelta(minutes=30)
        self.assertEqual(plan(now=later)["start"], later)


class Chained(unittest.TestCase):
    def test_chained(self):
        self.assertTrue(fillup.chained(run_end=S2, claude_reset=S2, next_start=S2))
        self.assertTrue(fillup.chained(run_end=S2, claude_reset=S2 + timedelta(minutes=10), next_start=S2))
        # the next start is the next night's: not right after
        self.assertFalse(fillup.chained(run_end=END2, claude_reset=END2, next_start=END2 + timedelta(hours=14)))
        # the session window the run opened late resets 3 h into the next one: no fresh window then
        self.assertFalse(fillup.chained(run_end=S2, claude_reset=S2 + timedelta(hours=3), next_start=S2))
        self.assertFalse(fillup.chained(run_end=S2, claude_reset=S2, next_start=None))


class Args:
    now = False
    arm = False


class WindowStart(unittest.TestCase):
    """keepalive.window_start_pass / deferred_window_start_pass with the late start on."""

    def setUp(self):
        testenv.clear_settings()                                         # run_start_factor 1.5 (default)
        testenv.notes_project(os.path.dirname(os.path.abspath(ka.PROGRESS_FILE)))
        self.fired = []
        self.usage = {"session": {"percent": 0.0, "resets_at": None}, "weekly": {"percent": 30.0}}
        self.active = None
        self.decision = {"go": True, "postpone": False, "recheck_at": None, "reason": "CONTINUE: test",
                         "text": "budget t", "headroom": 20.0}
        self.names = ("budget_eval", "fresh_usage", "handle_fire", "_fillup_rows", "_fillup_ratio", "run_active",
                      "DEFER_FILE", "STATE_FILE")
        self.old = {n: getattr(ka, n) for n in self.names}
        ka.DEFER_FILE = runner_state.live_path("keepalive.deferred")    # the DB, as the inbox reads it
        ka.STATE_FILE = runner_state.live_path("keepalive")
        ka.save_deferred({})
        ka.save_state({"handled": {}, "fires": {}})
        ka.budget_eval = lambda u, now: dict(self.decision)
        ka.fresh_usage = lambda now, force=False: self.usage
        ka._fillup_rows = lambda: []
        ka._fillup_ratio = lambda: 0.15
        ka.run_active = lambda sid, now, st=None: self.active

        def hf(sid, stall, reason, st, args):
            self.fired.append((stall, reason))
            st["handled"][stall["uuid"]] = {"result": "test", "at": "x"}
        ka.handle_fire = hf

    def tearDown(self):
        for n, v in self.old.items():
            setattr(ka, n, v)
        testenv.clear_settings()

    def test_gate_passed_the_run_starts_late(self):
        start = NIGHT + timedelta(seconds=5)
        d = ka.window_start_pass(SID, start, Args())
        planned = S2 - timedelta(minutes=150)                           # 01:30 Berlin
        self.assertTrue(d["go"])
        self.assertEqual(d["planned"], planned)
        self.assertEqual(self.fired, [])
        key = schedule.start_key(start)
        ent = ka.load_deferred()[key]
        self.assertTrue(ent["planned"])
        self.assertEqual((ka.parse_ts(ent["recheck_at"]), ka.parse_ts(ent["deadline"])), (planned, S2))
        self.assertEqual(ka.pending_deferral(SID, start), planned)     # the quickview's next run
        st = ka.load_state()
        self.assertEqual(ka.deferred_window_start_pass(SID, planned - timedelta(minutes=10), st, Args()), planned)
        self.assertIsNone(ka.deferred_window_start_pass(SID, planned, st, Args()))
        self.assertEqual(len(self.fired), 1)
        stall, reason = self.fired[0]
        self.assertEqual((stall["uuid"], stall["run_end"]), (key, S2))
        self.assertIn("window start (planned), planned late (D-274", reason)
        self.assertEqual(ka.load_deferred(), {})

    def test_planned_start_moves_with_the_owners_usage(self):
        """The owner opened a session window meanwhile (40% used, it lasts past 04:00): the run
        has 60% to use, so it starts later; the gate itself decides again at the planned time."""
        start = NIGHT + timedelta(seconds=5)
        ka.window_start_pass(SID, start, Args())
        planned = S2 - timedelta(minutes=150)
        self.usage["session"] = {"percent": 40.0, "resets_at": S2 + timedelta(minutes=30)}
        st = ka.load_state()
        later = S2 - timedelta(minutes=90)
        self.assertEqual(ka.deferred_window_start_pass(SID, planned, st, Args()), later)
        self.assertEqual(self.fired, [])
        self.decision = {"go": False, "postpone": False, "recheck_at": None, "reason": "HOLD: test", "text": "t"}
        self.assertIsNone(ka.deferred_window_start_pass(SID, later, st, Args()))
        self.assertEqual((self.fired, ka.load_deferred()), ([], {}))

    def test_last_session_window_of_the_night_starts_late_too(self):
        """At the night's last start a planned start is allowed until that window's end."""
        start = S2 + timedelta(seconds=5)
        ka.window_start_pass(SID, start, Args())
        ent = ka.load_deferred()[schedule.start_key(start)]
        self.assertEqual((ka.parse_ts(ent["recheck_at"]), ka.parse_ts(ent["deadline"])),
                         (END2 - timedelta(minutes=150), END2))

    def test_chained_onto_the_running_run(self):
        """The run before is still going at 04:00 and its session window resets about then: the
        04:00 start continues it at once (warm cache) instead of starting late."""
        self.active = S2 - timedelta(hours=2)
        ka.window_start_pass(SID, S2 + timedelta(seconds=5), Args())
        self.assertEqual(len(self.fired), 1)
        self.assertIn("chained onto the running run (D-274)", self.fired[0][1])
        self.assertEqual(self.fired[0][0]["run_end"], END2)
        self.assertEqual(ka.load_deferred(), {})

    def test_not_chained_when_the_session_window_resets_inside(self):
        """The run before opened its session window late: it resets at 07:00, inside the next
        window, so a chained start would only find its rest: planned late instead."""
        self.active = S2 - timedelta(hours=1)
        self.usage["session"] = {"percent": 90.0, "resets_at": S2 + timedelta(hours=3)}
        ka.window_start_pass(SID, S2 + timedelta(seconds=5), Args())
        self.assertEqual(self.fired, [])
        self.assertEqual(ka.parse_ts(ka.load_deferred()[schedule.start_key(S2)]["recheck_at"]),
                         END2 - timedelta(minutes=150))

    def test_last_stretch_and_run_now_fire_at_once(self):
        self.decision["last_mile"] = True
        ka.window_start_pass(SID, NIGHT + timedelta(seconds=5), Args())
        self.decision.pop("last_mile")
        ka.window_start_pass(SID, NIGHT + timedelta(seconds=5), types.SimpleNamespace(now=True, arm=False))
        self.assertEqual(len(self.fired), 2)
        self.assertEqual(ka.load_deferred(), {})

    def test_factor_zero_fires_at_the_start(self):
        testenv.set_setting("run_start_factor", 0)
        ka.window_start_pass(SID, NIGHT + timedelta(seconds=5), Args())
        self.assertEqual(len(self.fired), 1)

    def test_timing_failure_is_an_error(self):
        def boom():
            raise RuntimeError("no rows")
        ka._fillup_rows = boom
        errors = []
        old = ka.fail_stop
        ka.fail_stop = lambda subject, detail="", session=None: errors.append(subject)
        try:
            d = ka.window_start_pass(SID, NIGHT + timedelta(seconds=5), Args())
        finally:
            ka.fail_stop = old
        self.assertFalse(d["go"])
        self.assertEqual(errors, ["run timing failed at a session-window start"])
        self.assertEqual((self.fired, ka.load_deferred()), ([], {}))

    def test_inbox_hold_waits_for_the_planned_start(self):
        """D-244: a held "now" message goes out with the next start's message; that start is
        planned for 01:30, so the hold lasts until then, not 23:00 + grace."""
        since = NIGHT - timedelta(hours=1)
        self.assertEqual(inbox.hold_deadline(since), NIGHT)
        ka.window_start_pass(SID, NIGHT + timedelta(seconds=5), Args())
        self.assertEqual(inbox.hold_deadline(since), S2 - timedelta(minutes=150))

    def test_fillup_sees_the_planned_next_start(self):
        """The fill-up's "a start continues this window anyway" uses the start's planned time."""
        now = NIGHT + timedelta(hours=1)                                 # the next start: 04:00, planned 06:30
        self.assertEqual(ka.planned_next_start(SID, now, self.usage, [], 0.15), END2 - timedelta(minutes=150))
        testenv.set_setting("run_start_factor", 0)
        self.assertEqual(ka.planned_next_start(SID, now, self.usage, [], 0.15), S2)
        ka.defer_window_start(schedule.start_key(now), SID, now + timedelta(minutes=20), "r", S2, planned=True)
        self.assertEqual(ka.planned_next_start(SID, now, self.usage, [], 0.15), now + timedelta(minutes=20))


class PaceText(unittest.TestCase):
    """The pacing lines in a start's continue (D-275) and the chain line (D-274)."""

    def setUp(self):
        testenv.clear_settings()
        self.old = (ka.pending_deferral, ka.last_mile_next_start)
        ka.pending_deferral = lambda sid, now: None
        ka.last_mile_next_start = lambda now, usage=None: None

    def tearDown(self):
        ka.pending_deferral, ka.last_mile_next_start = self.old

    def test_late_run_no_chain(self):
        now = S2 - timedelta(minutes=150)
        t = ka.run_pace_text(SID, {"run_end": S2}, now, {"session": {"percent": 0, "resets_at": None}})
        self.assertIn("ends at 04:00 CEST", t)
        self.assertIn("from 03:00 CEST", t)
        self.assertNotIn("follows right after", t)                     # its session window resets 06:30

    def test_chained_run(self):
        t = ka.run_pace_text(SID, {"run_end": S2}, NIGHT, {"session": {"percent": 0, "resets_at": None}})
        self.assertIn("follows right after this one (D-274)", t)
        self.assertIn("next start at 04:00 CEST", t)
        self.assertIn("before about 05:00 CEST", t)

    def test_fillup_chained_onto_a_planned_start(self):
        """A fill-up ending at its reset (06:30) and the 04:00 start planned for 06:30: chained."""
        reset = S2 + timedelta(hours=2, minutes=30)
        ka.pending_deferral = lambda sid, now: reset
        t = ka.run_pace_text(SID, {"run_end": reset}, reset - timedelta(minutes=5),
                             {"session": {"percent": 95, "resets_at": reset}})
        self.assertIn("next start at 06:30 CEST", t)

    def test_continue_messages_carry_it(self):
        pace = ka.run_pace_text(SID, {"run_end": S2}, NIGHT, {"session": {}})
        for name, extra in (("continue", {}), ("last_mile", {}), ("fillup", {"remaining": "5", "reset": "04:00"})):
            m = ka.session_message(name, reason="r", progress="P.md", insert=pace, hooked=True, **extra)
            self.assertIn("finish only within its last hour", m)
            self.assertNotIn("\n", m)
        rules = ka.load_prompt("manager_project")
        self.assertIn("(D-275)", rules)


if __name__ == "__main__":
    unittest.main()
