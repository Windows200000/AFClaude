#!/usr/bin/env python3
"""Offline tests for stage_eta.py (D-213): a fixed schedule (every night 23:00 x 2, Berlin), a flat
forecast of the user's use, temp DB and files (testenv)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import export_quickview as qv  # noqa: E402
import schedule  # noqa: E402
import stage_eta as se  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)          # Fri 12:00 Berlin
RESET = datetime(2026, 10, 15, 17, 0, tzinfo=UTC)       # Thu 19:00 Berlin
SCHED = schedule.Config.every_day()
RATIO = 0.15                                            # one session window = 15 weekly %
THR = 85.0                                              # one session window left


def flat(user_week_pct):
    """The user's forecast: user_week_pct weekly % spread evenly over the week."""
    rate = user_week_pct / 168.0
    return lambda a, b: max((b - a).total_seconds() / 3600, 0.0) * rate


def gate(user_week=40.0, w=10.0, reset=RESET, forecast=True, **kw):
    return se.Gate(w, reset, RATIO, THR, flat(user_week) if forecast else None, SCHED, **kw)


def stages(*est):
    return [se.Stage(i + 1, f"stage {i + 1}", "P", "pending", e) for i, e in enumerate(est)]


class Formula(unittest.TestCase):
    def test_typical_week_matches_the_owners_formula(self):
        # (100 - 40) / 15 = 4 session windows a week: 3 nights pass the gate (week ends <= 85%),
        # the last stretch fills the last one
        r = se.predict(stages(1.0), gate(40.0), NOW)
        a = r["assumptions"]
        self.assertEqual(r["status"], "ok")
        self.assertAlmostEqual(a["formula_sessions_per_week"], 4.0, places=2)
        self.assertAlmostEqual(a["gate_sessions_per_week"], 4.0, places=2)
        self.assertAlmostEqual(a["sessions_per_day"], 4.0 / 7, places=3)
        self.assertAlmostEqual(a["user_forecast_week"], 40.0, places=1)

    def test_slots_are_session_window_starts_or_the_last_stretch(self):
        slots = se.capacity_slots(gate(40.0), NOW, RESET + timedelta(days=7))
        night_starts = {s for s, _ in schedule.session_slots(NOW, RESET + timedelta(days=7), SCHED)}
        for s in slots:
            self.assertLessEqual(s.sessions, 1.0 + 1e-9)
            if s.kind == "night":
                self.assertIn(s.at, night_starts)       # D-202: only at session-window starts
            else:
                self.assertEqual(s.kind, "last_stretch")
        week2 = [s for s in slots if s.at >= RESET]
        self.assertEqual([s.kind for s in week2], ["night"] * 3 + ["last_stretch"])
        # the stretch is right before the reset (D-020: at most 2 session windows)
        self.assertGreaterEqual(week2[-1].at, RESET + timedelta(days=7) - timedelta(hours=10))

    def test_night_gate_keeps_the_predicted_end_below_the_threshold(self):
        slots = [s for s in se.capacity_slots(gate(40.0), NOW, RESET + timedelta(days=7))
                 if s.at >= RESET and s.kind == "night"]
        fc, used = flat(40.0), 0.0
        for s in slots:
            used += s.sessions * RATIO * 100
            self.assertLessEqual(used + fc(RESET, RESET + timedelta(days=7)), THR + 1e-6)

    def test_straight_line_without_a_forecast(self):
        r = se.predict(stages(1.0, 1.0), gate(forecast=False), NOW)
        self.assertEqual(r["status"], "ok")
        self.assertIsNone(r["assumptions"]["user_forecast_week"])
        self.assertGreater(r["assumptions"]["gate_sessions_per_week"], 0)
        self.assertIsNotNone(r["stages"][1]["eta_days"])


class Cumulative(unittest.TestCase):
    def test_order_and_cumulative_sums(self):
        r = se.predict(stages(1.0, 2.0, 0.5, 3.0), gate(40.0), NOW)
        st = r["stages"]
        self.assertEqual([s["id"] for s in st], [1, 2, 3, 4])                 # the given (execution) order
        self.assertEqual([s["cum_sessions"] for s in st], [1.0, 3.0, 3.5, 6.5])
        days = [s["eta_days"] for s in st]
        self.assertEqual(days, sorted(days))
        self.assertLess(days[0], days[-1])
        self.assertTrue(all(s["eta"].startswith("~") or s["eta"] == "<1 d" for s in st))

    def test_more_capacity_means_earlier(self):
        slow = se.predict(stages(2.0, 2.0), gate(70.0), NOW)["stages"][-1]["eta_days"]
        fast = se.predict(stages(2.0, 2.0), gate(10.0), NOW)["stages"][-1]["eta_days"]
        self.assertLess(fast, slow)

    def test_a_zero_estimate_is_done_with_the_previous_stage(self):
        st = se.predict(stages(0.0, 1.0, 0.0), gate(40.0), NOW)["stages"]
        self.assertEqual(st[0]["eta_days"], 0.0)
        self.assertEqual(st[1]["done_at"], st[2]["done_at"])

    def test_not_this_week(self):
        # the week is nearly used up: the first stage only gets capacity after the reset
        r = se.predict(stages(1.0), gate(40.0, w=99.0), NOW)
        s = r["stages"][0]
        self.assertFalse(s["this_week"])
        self.assertGreater(se._ts(s["done_at"]), RESET)
        self.assertEqual(r["assumptions"]["this_week_sessions"], 0.0)
        early = se.predict(stages(0.5), gate(40.0, w=10.0), NOW)["stages"][0]
        self.assertTrue(early["this_week"])

    def test_beyond_the_horizon_is_unknown(self):
        r = se.predict(stages(50.0), gate(40.0), NOW, max_weeks=2)
        self.assertEqual(r["stages"][0]["eta"], "?")
        self.assertIsNone(r["stages"][0]["done_at"])

    def test_labels(self):
        self.assertEqual(se.eta_label(None), "?")
        self.assertEqual(se.eta_label(0.4), "<1 d")
        self.assertEqual(se.eta_label(2.6), "~3 d")


class Capacity(unittest.TestCase):
    def test_zero_capacity_is_unknown(self):
        r = se.predict(stages(1.0, 1.0), gate(100.0), NOW)     # the user's forecast fills the week
        self.assertEqual(r["status"], "unknown")
        self.assertIn("no AFClaude capacity", r["reason"])
        self.assertEqual([s["eta"] for s in r["stages"]], ["?", "?"])

    def test_negative_capacity_is_unknown(self):
        r = se.predict(stages(1.0), gate(150.0), NOW)          # more than the whole week
        self.assertEqual(r["status"], "unknown")
        self.assertEqual(r["assumptions"]["formula_sessions_per_week"], 0.0)
        self.assertEqual(r["stages"][0]["eta"], "?")

    def test_unknown_usage(self):
        for g in (gate(w=None), gate(reset=None), gate(reset=NOW - timedelta(hours=1))):
            r = se.predict(stages(1.0), g, NOW)
            self.assertEqual(r["status"], "unknown")
            self.assertEqual(r["stages"][0]["eta"], "?")
            self.assertIn("unknown", se.assumptions_text(r))

    def test_no_window_on_any_day(self):
        g = se.Gate(10.0, RESET, RATIO, THR, flat(40.0), schedule.Config({d: None for d in schedule.DAYS}))
        r = se.predict(stages(1.0), g, NOW)
        # no night windows: only the last stretch (D-020: at most 2 session windows a week)
        self.assertLessEqual(r["assumptions"]["gate_sessions_per_week"], 2.0 + 1e-9)


class Estimates(unittest.TestCase):
    def test_estimate_then_history_then_fallback(self):
        st = [se.Stage(1, "a", estimated=2.5), se.Stage(2, "b"), se.Stage(3, "c")]
        self.assertEqual([(x, s) for _, x, s in se.sessions_for(st, 1.7)],
                         [(2.5, "estimate"), (1.7, "history"), (1.7, "history")])
        self.assertEqual([(x, s) for _, x, s in se.sessions_for(st, None)],
                         [(2.5, "estimate"), (se.FALLBACK_SESSIONS, "fallback"), (se.FALLBACK_SESSIONS, "fallback")])
        r = se.predict(st, gate(), NOW, None)
        self.assertEqual(r["assumptions"]["estimate_sources"], {"estimate": 1, "history": 0, "fallback": 2})

    def test_history_default(self):
        h = lambda x: NOW + timedelta(hours=x)   # noqa: E731
        done = [(h(0), h(10)), (h(20), h(30)), (h(40), h(50)), (h(60), h(60))]   # the last: done instantly
        runs = [(h(1), h(3), 0.8), (h(21), h(23), 0.6), (h(25), h(27), 0.6), (h(41), h(43), 0.5),
                (h(59), h(61), 0.9)]            # the last overlaps only the instant one: no measurement
        self.assertEqual(se.history_default(done, runs), (0.8, 3))      # median of 0.8, 1.2, 0.5
        self.assertEqual(se.history_default(done[:2], runs), (None, 2))  # < MIN_HISTORY
        self.assertEqual(se.history_default([], runs), (None, 0))

    def test_history_splits_a_run_over_overlapping_stages(self):
        h = lambda x: NOW + timedelta(hours=x)   # noqa: E731
        done = [(h(0), h(10)), (h(0), h(10)), (h(0), h(10))]
        self.assertEqual(se.history_default(done, [(h(1), h(2), 0.9)]), (0.3, 3))


class Grouping(unittest.TestCase):
    def test_group_is_its_last_open_stage(self):
        r = se.predict(stages(1.0, 2.0, 1.0), gate(40.0), NOW)
        g = se.group_eta(r, [1, 2])
        self.assertEqual(g["eta_days"], r["stages"][1]["eta_days"])
        self.assertEqual(g["sessions"], 3.0)
        self.assertIsNone(se.group_eta(r, [99]))                         # nothing open in it
        u = se.predict(stages(1.0), gate(w=None), NOW)
        self.assertEqual(se.group_eta(u, [1])["eta"], "?")


class Log(unittest.TestCase):
    def test_record_once_a_day_and_score(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "log.jsonl")
            r = se.predict(stages(1.0, 1.0), gate(40.0), NOW)
            self.assertTrue(se.record(r, path, NOW))
            self.assertFalse(se.record(r, path, NOW + timedelta(hours=3)))   # same day
            self.assertTrue(se.record(r, path, NOW + timedelta(days=1)))
            with open(path) as fh:
                rows = [json.loads(ln) for ln in fh]
            self.assertEqual(len(rows), 2)
            self.assertEqual([s["id"] for s in rows[0]["stages"]], [1, 2])
            p1 = se._ts(rows[0]["stages"][0]["done_at"])
            sc = se.score(rows, {1: p1 + timedelta(days=2)}, now=p1 + timedelta(days=60))
            self.assertEqual(sc["n"], 2)
            self.assertAlmostEqual(sc["bias_days"], 2.0, places=2)
            self.assertEqual(sc["overdue"], 2)          # stage 2 is still open after its predicted time
            self.assertEqual(se.score([], {})["n"], 0)


class StoreAndDashboard(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(store.DB_PATH)
        p = f"ETA{id(self)}"
        act = lambda n, **kw: actions.perform(self.conn, n, kw, actor="cli", via="cli")   # noqa: E731
        self.act, self.p = act, p
        self.t1 = act("task.add", title="Dashboard 1: one", project=p)["id"]
        self.t2 = act("task.add", title="Dashboard 1: two", project=p)["id"]
        self.t3 = act("task.add", title="Dashboard 2a: three", project=p)["id"]
        self.t4 = act("task.add", title="Other stage", project=p)["id"]
        self.q = act("task.ask", question="a question?", project=p)["id"]

    def tearDown(self):
        self.conn.close()

    def test_estimated_sessions_through_the_write_path(self):
        t = self.act("task.edit", task_id=self.t1, estimated_sessions=2.5)
        self.assertEqual(t["estimated_sessions"], 2.5)
        t = self.act("task.update", task_id=self.t1, fields={"estimated_sessions": 1})
        self.assertEqual(t["estimated_sessions"], 1.0)
        for bad in (-1, 101, "2", True):
            with self.assertRaises(ValueError):
                self.act("task.edit", task_id=self.t1, estimated_sessions=bad)
        t = self.act("task.edit", task_id=self.t1, estimated_sessions=None)
        self.assertIsNone(t["estimated_sessions"])
        ev = store.task_events(self.conn, self.t1)
        self.assertTrue(any("estimated_sessions" in str(e["detail"]) for e in ev))

    def test_open_stages_in_execution_order_without_questions(self):
        self.act("task.edit", task_id=self.t2, estimated_sessions=3)
        ids = [s.id for s in se.open_stages(self.conn) if s.project == self.p]
        self.assertEqual(ids, [self.t1, self.t2, self.t3, self.t4])
        self.assertNotIn(self.q, ids)
        self.assertEqual(next(s for s in se.open_stages(self.conn) if s.id == self.t2).estimated, 3.0)
        self.act("task.finish", task_id=self.t1)
        self.assertNotIn(self.t1, [s.id for s in se.open_stages(self.conn)])

    def test_dashboard_block(self):
        self.act("task.finish", task_id=self.t1)
        tasks = store.list_tasks(self.conn, project=self.p)
        open_ = [se.Stage(t["id"], t["title"]) for t in tasks if t["status"] in se.OPEN and t["kind"] == "task"]
        eta = se.predict(open_, gate(40.0), NOW)
        out = qv.build_stages(tasks, eta)
        ph1 = out["phases"][0]
        self.assertEqual(ph1["eta"], next(s["eta"] for s in eta["stages"] if s["id"] == self.t2))
        self.assertEqual(ph1["eta_sessions"], 1.0)                # only the open step counts
        self.assertNotIn("eta", ph1["steps"][0])                  # the done step has none
        self.assertIn("eta", ph1["steps"][1])
        self.assertIn("eta", out["phases"][1])
        other = next(s for s in out["others"] if s["id"] == self.t4)
        self.assertEqual(other["eta_days"], eta["stages"][-1]["eta_days"])
        self.assertNotIn("eta", qv.build_stages(tasks)["phases"][0])   # no ETA: the old shape
        summ = qv.eta_summary(eta)
        self.assertEqual(summ["status"], "ok")
        self.assertIn("session windows/day", summ["text"])

    def test_export_stages_never_fails(self):
        if store.get_project(self.conn, qv.STAGES_PROJECT) is None:
            self.act("project.add", name=qv.STAGES_PROJECT)
        self.act("task.add", title="Dashboard 9: x", project=qv.STAGES_PROJECT)
        out = qv.stages(None, NOW)                               # no usage: ETAs unknown, page still renders
        self.assertIsNone(out["error"])
        self.assertEqual(out["eta"]["status"], "unknown")
        self.assertTrue(all(s.get("eta") in (None, "?") for p in out["phases"] for s in p["steps"]))

    def test_page_shows_the_eta(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            html = fh.read()
        for needle in ("etaTip", "eta-c", "p.eta", "t.eta", "sessions/day"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
