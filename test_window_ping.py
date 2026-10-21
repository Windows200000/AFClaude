#!/usr/bin/env python3
"""Offline tests for the window-start ping (D-277): a session-window start whose run starts later
(planned late, D-274; postponed) or is held sends one tiny context-free request while no Claude
session window is open, so the session window opens on the schedule; recorded per start in the
keepalive state's "window_pings" with the session reset read after it. No real claude call: the
host call, the sleep and the usage refresh are stubbed."""
import os
import subprocess
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import failstop  # noqa: E402
import keepalive as ka  # noqa: E402
import runner_state  # noqa: E402
import schedule  # noqa: E402

UTC = timezone.utc
SID = "00000000-0000-4000-8000-000000000077"
NIGHT = datetime(2026, 10, 4, 21, 0, tzinfo=UTC)         # Sun 23:00 Berlin, the night's first start
S2 = NIGHT + timedelta(hours=5)                           # Mon 04:00 Berlin, its second start
FIVE = timedelta(hours=5)
OK_JSON = '{"type": "result", "is_error": false, "result": "OK", "total_cost_usd": 0.0004}'


def armed(**kw):
    return types.SimpleNamespace(**{"arm": True, "now": False, **kw})


class Floor(unittest.TestCase):
    def test_floor_to_ten_minutes(self):
        self.assertEqual(ka.floor_ping(NIGHT + timedelta(seconds=30)), NIGHT)
        self.assertEqual(ka.floor_ping(NIGHT + timedelta(minutes=19, seconds=59, microseconds=5)),
                         NIGHT + timedelta(minutes=10))
        self.assertEqual(ka.floor_ping(NIGHT), NIGHT)


class WindowPing(unittest.TestCase):
    """keepalive.window_start_pass -> window_ping_after -> window_ping."""

    def setUp(self):
        testenv.clear_settings()                                         # window_ping on, run_start_factor 1.5
        testenv.unpause()
        testenv.notes_project(os.path.dirname(os.path.abspath(ka.PROGRESS_FILE)))
        self.fired, self.calls, self.alerts, self.sleeps = [], [], [], []
        self.usage = {"session": {"percent": 0.0, "resets_at": None}, "weekly": {"percent": 30.0}}
        self.decision = {"go": True, "postpone": False, "recheck_at": None, "reason": "CONTINUE: test",
                         "text": "budget t", "headroom": 20.0}
        self.ping_at = NIGHT + timedelta(seconds=30)
        self.after = None                                                # the session reset read after the ping
        self.reply = subprocess.CompletedProcess([], 0, OK_JSON, "")
        names = ("budget_eval", "fresh_usage", "handle_fire", "_fillup_rows", "_fillup_ratio", "run_active",
                 "DEFER_FILE", "STATE_FILE", "_now", "_sleep", "refresh_usage_checked", "alert")
        old = {n: getattr(ka, n) for n in names}
        self.addCleanup(lambda: [setattr(ka, n, v) for n, v in old.items()])
        self.addCleanup(testenv.clear_settings)
        ka.DEFER_FILE = runner_state.live_path("keepalive.deferred")
        ka.STATE_FILE = runner_state.live_path("keepalive")
        ka.save_deferred({})
        ka.save_state({"handled": {}, "fires": {}})
        st = ka.load_state()                                             # earlier tests' pings out
        st["window_pings"] = {}
        ka.save_state(st)
        ka.budget_eval = lambda u, now: dict(self.decision)
        ka.fresh_usage = lambda now, force=False: self.usage
        ka._fillup_rows = lambda: []
        ka._fillup_ratio = lambda: 0.15
        ka.run_active = lambda sid, now, st=None: None
        ka._now = lambda: self.ping_at
        ka._sleep = self.sleeps.append
        ka.alert = lambda s, b="": self.alerts.append((s, b))
        ka.refresh_usage_checked = lambda cwd=ka.HERE, now=None: {
            "ok": True, "rc": 0, "out": "", "usage": {"fetched_at": now,
                                                     "session": {"percent": 1.0, "resets_at": self.after}}}

        def hf(sid, stall, reason, st, args):
            self.fired.append((stall, reason))
            st["handled"][stall["uuid"]] = {"result": "test", "at": "x"}
        ka.handle_fire = hf

        def host_call(argv, input=None, cwd=None, env=None, timeout=None):
            self.calls.append((argv, input, env, timeout))
            if isinstance(self.reply, BaseException):
                raise self.reply
            return self.reply
        p = mock.patch.object(ka.host, "run_on_host", side_effect=host_call)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("pacing.record_forecast")                       # armed starts log the forecast
        p.start()
        self.addCleanup(p.stop)

    def pings(self):
        return ka.load_state().get("window_pings") or {}

    def start(self, at=NIGHT + timedelta(seconds=5), args=None):
        return ka.window_start_pass(SID, at, args or armed())

    def test_planned_late_start_pings_and_records_the_match(self):
        self.after = NIGHT + FIVE                                        # 23:00:30 floored to 23:00 + 5 h
        d = self.start()
        self.assertTrue(d["go"])
        self.assertIn("planned", d)
        self.assertEqual(self.fired, [])
        key = schedule.start_key(NIGHT)
        self.assertIn(key, ka.load_deferred())                          # the planned start stays stored
        self.assertEqual(len(self.calls), 1)
        argv, prompt, env, timeout = self.calls[0]
        self.assertEqual(argv, ka.POKE_ARGV)                             # the bridge-whitelisted Haiku shape
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(prompt, ka.POKE_PROMPT)
        self.assertIs(env, ka.SCRUBBED_ENV)
        self.assertEqual(timeout, ka.WINDOW_PING_TIMEOUT)
        self.assertEqual(self.sleeps, [ka.WINDOW_PING_VERIFY_AFTER.total_seconds()])
        rec = self.pings()[key]
        self.assertEqual(rec["status"], "ok")
        self.assertTrue(rec["matched"])
        self.assertTrue(rec["on_schedule"])
        self.assertEqual(ka.parse_ts(rec["at"]), self.ping_at)
        self.assertEqual(ka.parse_ts(rec["resets_at"]), NIGHT + FIVE)
        self.assertEqual(rec["cost_usd"], 0.0004)
        self.assertEqual(self.alerts, [])
        self.assertIn("window-ping", testenv.notes(os.path.dirname(os.path.abspath(ka.PROGRESS_FILE))))
        self.assertFalse(failstop.paused()["paused"])

    def test_held_start_pings(self):
        self.decision = {"go": False, "postpone": False, "recheck_at": None, "reason": "HOLD: test", "text": "t"}
        self.after = NIGHT + FIVE
        self.start()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.pings()[schedule.start_key(NIGHT)]["why"], "the gate held")

    def test_postponed_start_pings(self):
        self.decision = {"go": False, "postpone": True, "recheck_at": NIGHT + timedelta(hours=1),
                         "reason": "POSTPONE: test", "text": "t"}
        self.after = NIGHT + FIVE
        self.start()
        self.assertEqual(len(self.calls), 1)
        self.assertIn(schedule.start_key(NIGHT), ka.load_deferred())

    def test_setting_off(self):
        testenv.set_setting("window_ping", False)
        self.start()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.pings(), {})

    def test_open_session_window_no_ping(self):
        self.usage["session"] = {"percent": 12.0, "resets_at": NIGHT + timedelta(hours=2)}
        self.start()
        self.assertEqual(self.calls, [])

    def test_a_reset_in_the_past_is_no_open_window(self):
        self.usage["session"] = {"percent": 40.0, "resets_at": NIGHT - timedelta(minutes=20)}
        self.after = NIGHT + FIVE
        self.start()
        self.assertEqual(len(self.calls), 1)

    def test_run_firing_at_once_no_ping(self):
        testenv.set_setting("run_start_factor", 0)                       # fires at the session-window start
        self.start()
        self.assertEqual(len(self.fired), 1)
        self.assertEqual(self.calls, [])

    def test_manual_now_and_dry_run_no_ping(self):
        self.start(args=armed(now=True, arm=False))
        self.start(args=armed(arm=False))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.pings(), {})

    def test_paused_no_ping(self):
        import actions
        import store
        c = store.connect()
        try:
            actions.perform(c, "automation.set", {"paused": True, "reason": "owner looks"}, actor="cli", via="cli")
        finally:
            c.close()
        self.addCleanup(testenv.unpause)
        self.start()
        self.assertEqual(self.calls, [])

    def test_once_per_session_window_start(self):
        self.after = NIGHT + FIVE
        self.start()
        ka.save_deferred({})
        self.start(at=NIGHT + timedelta(minutes=1))                       # a second pass of the same start
        self.assertEqual(len(self.calls), 1)
        self.ping_at = S2 + timedelta(seconds=30)                         # the night's next start pings again
        self.after = S2 + FIVE
        self.start(at=S2 + timedelta(seconds=5))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(set(self.pings()), {schedule.start_key(NIGHT), schedule.start_key(S2)})

    def test_failed_request_alerts_without_pausing(self):
        self.reply = subprocess.CompletedProcess([], 1, "", "boom")
        d = self.start()
        self.assertTrue(d["go"])
        self.assertIn(schedule.start_key(NIGHT), ka.load_deferred())     # the planned run is kept
        rec = self.pings()[schedule.start_key(NIGHT)]
        self.assertEqual(rec["status"], "request-failed")
        self.assertIn("rc=1", rec["error"])
        self.assertEqual([s for s, _ in self.alerts], ["window-start ping failed"])
        self.assertEqual(self.sleeps, [])
        self.assertFalse(failstop.paused()["paused"])

    def test_timeout_alerts(self):
        self.reply = subprocess.TimeoutExpired(["claude"], ka.WINDOW_PING_TIMEOUT)
        self.start()
        self.assertEqual(self.pings()[schedule.start_key(NIGHT)]["status"], "request-failed")
        self.assertEqual(len(self.alerts), 1)

    def test_unexpected_error_never_blocks_the_start(self):
        self.reply = RuntimeError("bridge exploded")
        d = self.start()
        self.assertTrue(d["go"])
        self.assertIn(schedule.start_key(NIGHT), ka.load_deferred())
        self.assertIn("RuntimeError", self.alerts[0][1])
        self.assertFalse(failstop.paused()["paused"])

    def test_window_not_opened_alerts(self):
        self.after = None                                                # still no session window after it
        self.start()
        rec = self.pings()[schedule.start_key(NIGHT)]
        self.assertEqual(rec["status"], "unmatched")
        self.assertFalse(rec["matched"])
        self.assertEqual(len(self.alerts), 1)
        self.assertFalse(failstop.paused()["paused"])

    def test_verify_reading_failed_alerts(self):
        ka.refresh_usage_checked = lambda cwd=ka.HERE, now=None: {"ok": False, "rc": 1, "out": "x",
                                                                     "usage": {"fetched_at": NIGHT}}
        self.start()
        self.assertEqual(self.pings()[schedule.start_key(NIGHT)]["status"], "verify-failed")
        self.assertEqual(len(self.alerts), 1)

    def test_late_ping_matches_its_floor_but_not_the_schedule(self):
        self.ping_at = NIGHT + timedelta(minutes=12)
        self.after = NIGHT + timedelta(minutes=10) + FIVE
        self.start(at=NIGHT + timedelta(minutes=11))
        rec = self.pings()[schedule.start_key(NIGHT)]
        self.assertTrue(rec["matched"])
        self.assertFalse(rec["on_schedule"])
        self.assertEqual(self.alerts, [])


if __name__ == "__main__":
    unittest.main()
