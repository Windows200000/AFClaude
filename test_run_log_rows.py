#!/usr/bin/env python3
"""Offline tests for the run_log decision rows of phase 3a S9(b) (docs/phase3a_plan.md): the budget
gate (GO / HOLD / POSTPONE) and the D-274 timing (DEFER / FIRE) at a session-window start and at a
postponed one's re-decision, the last-stretch slot's rows (one per slot and decision), the fill-up's
FILLUP row, the usage reviews' START / POSTPONE rows, once-keyed rows (a retry adds none), no rows in
a dry run, a failed write fail-stopping (D-234/D-235) and the row being no audit row (D-256)."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import actions  # noqa: E402
import keepalive as ka  # noqa: E402
import store  # noqa: E402
import usage_review as ur  # noqa: E402
from test_pick import Args, Base, DryArgs, NOW, SID_A  # noqa: E402

UTC = timezone.utc
KEY = "window-start-2026-10-29-s2"
REAL_HANDLE_FIRE = ka.handle_fire


class Rows(Base):
    def setUp(self):
        super().setUp()
        self.d = {"go": True, "reason": "GO: plenty of quota", "text": "b", "headroom": 3.0}
        self.timing = None
        for n, v in (("fresh_usage", lambda now, force=False: {"session": {}}),
                     ("budget_eval", lambda u, now: dict(self.d)),
                     ("run_timing", lambda sid, now, u, d: self.timing),
                     ("automation_paused", lambda what, sid=None: False),
                     ("defer_window_start", mock.Mock()), ("window_ping_after", mock.Mock()),
                     ("handle_fire", mock.Mock(return_value=None)),
                     ("load_state", lambda: {"handled": {}, "fires": {}})):
            p = mock.patch.object(ka, n, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(ka.schedule, "start_key", lambda now: KEY)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("pacing.record_forecast")
        p.start()
        self.addCleanup(p.stop)

    def rows(self, comp="keepalive"):
        return [(r["decision"], r["reason"]) for r in reversed(store.run_log(self.conn, comp))]

    def window(self, args=Args()):
        return ka.window_start_pass(SID_A, NOW, args)

    def test_a_go_with_a_timing_now_writes_go_and_fire(self):
        self.window()
        self.assertEqual([d for d, _ in self.rows()], ["GO", "FIRE"])
        self.assertEqual(self.rows()[0][1], f"window start {KEY}: GO: plenty of quota")
        self.assertIn("starts now", self.rows()[1][1])
        self.assertEqual(self.rows()[0][0], "GO")

    def test_a_late_timing_writes_defer_not_fire(self):
        self.timing = {"start": NOW + timedelta(hours=2), "end": NOW + timedelta(hours=4), "reason": "late"}
        self.window()
        self.assertEqual([d for d, _ in self.rows()], ["GO", "DEFER"])
        self.assertIn("(D-274: late)", self.rows()[1][1])

    def test_a_hold_and_a_postpone(self):
        self.d = {"go": False, "reason": "HOLD: no", "postpone": False}
        self.window()
        self.d = {"go": False, "reason": "POSTPONE: user active", "postpone": True, "recheck_at": NOW + timedelta(minutes=30)}
        self.window()
        self.assertEqual([d for d, _ in self.rows()], ["HOLD", "POSTPONE"])
        self.assertIn("postponed to", self.rows()[1][1])
        ka.defer_window_start.assert_called_once()

    def test_a_dry_run_writes_no_row(self):
        self.window(DryArgs())
        self.assertEqual(self.rows(), [])

    def test_paused_and_a_failed_reading_are_holds(self):
        with mock.patch.object(ka, "automation_paused", lambda what, sid=None: True), \
                mock.patch.object(ka, "paused_reason", lambda sid=None: "test"), \
                mock.patch.object(ka, "resume_hint", lambda sid=None: "x"):
            self.window()
        with mock.patch.object(ka, "fresh_usage", lambda now, force=False: None):
            self.window()
        self.assertEqual([d for d, _ in self.rows()], ["HOLD", "HOLD"])
        self.assertIn("PAUSED", self.rows()[0][1])

    def test_a_retry_adds_no_go_or_fire_row(self):
        """The owner-busy / db-pause retry re-decides the same gate: GO and FIRE are once per start."""
        ka.handle_fire.return_value = ka.OWNER_BUSY
        self.window()
        ent = {"session": SID_A, "recheck_at": NOW.isoformat()}
        for _ in range(2):
            ka.deferred_act(SID_A, NOW, {"handled": {}, "fires": {}}, Args(), KEY, ent, {KEY: ent})
        self.assertEqual([d for d, _ in self.rows()], ["GO", "FIRE"])

    def test_a_deferred_start_decides_again_with_new_rows(self):
        self.d = {"go": False, "reason": "POSTPONE: still active", "postpone": True, "recheck_at": NOW + timedelta(minutes=20)}
        ent = {"session": SID_A, "recheck_at": NOW.isoformat()}
        with mock.patch.object(ka, "save_deferred"):
            ka.deferred_act(SID_A, NOW, {"handled": {}, "fires": {}}, Args(), KEY, ent, {KEY: ent})
            self.d = {"go": True, "reason": "GO: ok", "text": "b"}
            ka.deferred_act(SID_A, NOW + timedelta(minutes=20), {"handled": {}, "fires": {}}, Args(), KEY, ent, {KEY: ent})
        self.assertEqual([d for d, _ in self.rows()], ["POSTPONE", "GO", "FIRE"])

    def test_a_failed_write_fail_stops_and_aborts_the_start(self):
        with mock.patch.object(ka, "_run_log", side_effect=RuntimeError("db gone")):
            d = self.window()
        self.assertFalse(d["go"])
        ka.fail_stop.assert_called_once()
        self.assertIn("run_log", ka.fail_stop.call_args[0][0])
        ka.handle_fire.assert_not_called()

    def test_the_rows_are_no_audit_rows(self):
        before = self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        self.window()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], before)

    # ------------------------------------------------------------ last stretch and fill-up
    def last_stretch(self, args=Args()):
        p = {"key": "last-mile-k", "slot": 1, "resets_at": NOW + timedelta(hours=4)}
        return ka.last_stretch_act(SID_A, NOW, {"handled": {}, "fires": {}}, args, p)

    def test_the_last_stretch_slot_has_one_row_per_decision(self):
        self.d = {"go": False, "reason": "HOLD: not yet", "postpone": False}
        for _ in range(3):
            self.last_stretch()
        self.d = {"go": True, "reason": "GO: now", "text": "b"}
        for _ in range(2):
            self.last_stretch()
        self.assertEqual([d for d, _ in self.rows()], ["HOLD", "GO"])
        self.assertTrue(self.rows()[1][1].startswith("last-stretch slot last-mile-k: "))

    def test_the_last_stretch_waiting_for_a_limit_reset_is_a_hold(self):
        reset = NOW + timedelta(minutes=30)
        with mock.patch.object(ka, "stall_status", lambda sid, now: ("WAIT_RESET", "limit", {"reset": reset})):
            self.last_stretch()
            self.last_stretch()
        self.assertEqual([d for d, _ in self.rows()], ["HOLD"])
        self.assertIn("waits for the limit reset", self.rows()[0][1])

    def test_a_fill_up_start_writes_a_row_with_its_outcome(self):
        st = {"handled": {}, "fires": {}}
        done = lambda *a, **k: st["handled"].update({"fillup-k": {"result": "continued-in-place"}})
        with mock.patch.object(ka, "handle_fire", REAL_HANDLE_FIRE), \
                mock.patch.object(ka, "_start_session", done):
            ka.handle_fire(SID_A, {"uuid": "fillup-k", "timestamp": NOW, "prompt": "fillup"}, "fill-up", st, Args())
        (dec, why), = self.rows()
        self.assertEqual(dec, "FILLUP")
        self.assertEqual(why, "start fillup-k: fill-up: continued-in-place")


class Reviews(Base):
    def setUp(self):
        super().setUp()
        self.saved = {n: getattr(ka, n) for n in ("claim_tmux", "fire")}
        ka.claim_tmux = lambda sid, kind, **k: "afc-review"
        ka.fire = lambda *a, **k: (0, "started", "")
        self.addCleanup(lambda: [setattr(ka, n, v) for n, v in self.saved.items()])
        for n, v in (("record_review", lambda *a, **k: {"one_off": True, "reviews": ["usage"]}),
                     ("report_doc", lambda now: "review:x"), ("fail_stop", mock.Mock())):
            p = mock.patch.object(ur, n, v)
            p.start()
            self.addCleanup(p.stop)

    def rows(self):
        return [(r["decision"], r["session_id"], r["reason"]) for r in reversed(store.run_log(self.conn, "usage_review"))]

    def test_a_started_review_writes_a_start_row_with_its_session(self):
        st = {"runs": [], "one_off_runs": []}
        self.assertTrue(ur.run_once(st, NOW, "usage"))
        (dec, sid, why), = self.rows()
        self.assertEqual(dec, "START")
        self.assertEqual(sid, st["one_off_runs"][-1]["session"])
        self.assertIn("one-off usage review started", why)

    def test_a_failed_launch_writes_the_row_and_fail_stops(self):
        ka.fire = lambda *a, **k: (1, "", "boom")
        st = {"runs": [], "one_off_runs": []}
        self.assertFalse(ur.run_once(st, NOW, "usage"))
        self.assertIn("launch failed", self.rows()[0][2])
        ur.fail_stop.assert_called_once()

    def test_a_postponement_is_a_row(self):
        ur.note_postponed({}, NOW, "one-off usage", NOW + timedelta(hours=1), "session 99% used", 1)
        (dec, sid, why), = self.rows()
        self.assertEqual((dec, sid), ("POSTPONE", None))
        self.assertIn("one-off usage review postponed to", why)

    def test_a_failed_row_write_fail_stops(self):
        with mock.patch.object(ka, "_run_log", side_effect=RuntimeError("db gone")):
            ur.note_postponed({}, NOW, "one-off usage", NOW + timedelta(hours=1), "x", 1)
        ur.fail_stop.assert_called_once()
        self.assertIn("run_log", ur.fail_stop.call_args[0][0])


class OnceKey(Base):
    def test_once_writes_one_row_and_checks_its_prefix(self):
        add = lambda r, once: actions.run_log_add(self.conn, "keepalive", "GO", r, actor="keepalive", via="runner", once=once)
        self.assertGreater(add("slot a: x", "slot a: "), 0)
        self.assertEqual(add("slot a: y", "slot a: "), 0)
        self.assertGreater(add("slot b: x", "slot b: "), 0)
        with self.assertRaisesRegex(ValueError, "prefix"):
            add("slot c: x", "slot z: ")
        self.assertEqual(len(store.run_log(self.conn, "keepalive")), 2)


if __name__ == "__main__":
    unittest.main()
