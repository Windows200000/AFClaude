#!/usr/bin/env python3
"""Offline tests for export_quickview.py's phase strip (build_stages / stages)."""
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["AFCLAUDE_CONFIG"] = os.devnull   # hermetic: the default window
import export_quickview as qv  # noqa: E402
import store  # noqa: E402


def T(seq, title, status="pending"):
    return {"id": seq, "stage_seq": seq, "title": title, "status": status, "priority": "high"}


class BuildStages(unittest.TestCase):
    def test_grouping_status_and_current(self):
        out = qv.build_stages([
            T(1, "Dispatcher", "done"),
            T(2, "Dashboard 1: config", "done"),
            T(3, "Dashboard 2: runners", "done"),
            T(4, "Dashboard 3: prompts", "in_progress"),
            T(5, "Dashboard 8b: deploy"),
            T(6, "Dashboard 8a: review", "done"),
            T(7, "Dashboard 8: deploy", "cancelled"),     # superseded: dropped
            T(8, "Dashboard 9: retire", "cancelled"),     # only cancelled: phase disappears
            T(9, "Dashboard 10: later", "blocked"),
            T(10, "Dashboard", "cancelled"),              # no number: an "other" stage
        ])
        ph = {p["n"]: p for p in out["phases"]}
        self.assertEqual(list(ph), [1, 2, 3, 8, 10])       # numeric order, 9 gone
        self.assertEqual(ph[1]["status"], "done")
        self.assertEqual(ph[3]["status"], "in_progress")
        self.assertEqual(ph[8]["status"], "in_progress")   # partly done
        self.assertEqual((ph[8]["done"], ph[8]["total"]), (1, 2))
        self.assertEqual([s["key"] for s in ph[8]["steps"]], ["8a", "8b"])
        self.assertEqual(ph[8]["steps"][0]["title"], "review")
        self.assertEqual(ph[10]["status"], "blocked")
        self.assertEqual([p["n"] for p in out["phases"] if p["current"]], [3])
        self.assertEqual([o["title"] for o in out["others"]], ["Dispatcher", "Dashboard"])

    def test_all_done_has_no_current(self):
        out = qv.build_stages([T(1, "Dashboard 1: a", "done")])
        self.assertFalse(any(p["current"] for p in out["phases"]))

    def test_stages_from_db_and_unknown_project(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            old_path, old_proj = store.DB_PATH, qv.STAGES_PROJECT
            try:
                store.DB_PATH = db
                conn = store.connect(db)
                store.add_project(conn, "P")
                store.add_task(conn, "Dashboard 1: a", project="P")
                store.add_task(conn, "Other", project="P")
                conn.close()
                qv.STAGES_PROJECT = "P"
                out = qv.stages()
                self.assertIsNone(out["error"])
                self.assertEqual([p["n"] for p in out["phases"]], [1])
                self.assertEqual(out["project"], "P")
                qv.STAGES_PROJECT = "nope"
                self.assertTrue(qv.stages()["error"])      # reported, not raised
            finally:
                store.DB_PATH, qv.STAGES_PROJECT = old_path, old_proj


class WindowInfo(unittest.TestCase):
    """The 23:00-09:00 window spans midnight: inside it, the next window is the following
    evening's; outside, this evening's."""

    def Z(self, s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    def test_window_info(self):
        out = qv.window_info(self.Z("2026-09-29T22:30:00Z"))        # Wed 00:30 CEST: inside
        self.assertEqual(out["window_berlin"], "23:00–09:00")
        self.assertTrue(out["in_window"])
        self.assertEqual(out["window_end_berlin"], "Wed 30.09. 09:00 CEST")
        self.assertEqual(out["next_window_berlin"], "Wed 30.09. 23:00 CEST")
        out = qv.window_info(self.Z("2026-09-29T21:30:00Z"))        # Tue 23:30 CEST: inside
        self.assertEqual(out["window_end_berlin"], "Wed 30.09. 09:00 CEST")
        self.assertEqual(out["next_window_berlin"], "Wed 30.09. 23:00 CEST")
        out = qv.window_info(self.Z("2026-09-30T10:00:00Z"))        # Wed 12:00 CEST: outside
        self.assertFalse(out["in_window"])
        self.assertIsNone(out["window_end_berlin"])
        self.assertEqual(out["next_window_berlin"], "Wed 30.09. 23:00 CEST")
        out = qv.window_info(self.Z("2026-10-25T12:00:00Z"))        # first CET evening
        self.assertEqual(out["next_window_berlin"], "Sun 25.10. 23:00 CET")



class LimitsBlock(unittest.TestCase):
    OLD = {"generated_at": "x", "ratio": {"status": "ok", "median": 0.13333, "trimmed_mean": 0.1, "n": 55},
           "windows_per_week": 7.5, "windows_left_this_week": 6.2,
           "attribution": {"week_share": {"status": "ok", "own_share": 1.0, "other_share": 0.0}}}

    def test_old_snapshot_still_renders(self):
        b = qv.limits_from_snapshot(self.OLD)
        self.assertEqual((b["ratio_status"], b["ratio_median"], b["ratio_n"]), ("ok", 0.1333, 55))
        self.assertIsNone(b["ratio_pref"])
        self.assertIsNone(qv.limits_from_snapshot(None))

    def test_per_window_fields(self):
        snap = dict(self.OLD,
                    ratio_windows={"status": "ok", "n": 11, "n_total": 17, "weighted": 0.15368, "weighted_stdev": 0.01834,
                                   "p25": 0.1125, "p75": 0.16273, "se": 0.0045, "vs_15min": {"median_15min": 0.1381}},
                    preferred_ratio={"value": 0.15368, "source": "windows", "flagged": False,
                                     "windows_per_week": 6.507, "windows_left_this_week": 5.21})
        b = qv.limits_from_snapshot(snap)
        self.assertEqual((b["ratio_pref"], b["ratio_spread"], b["ratio_windows_n"]), (0.1537, 0.0183, 11))
        self.assertEqual(b["ratio_iqr"], [0.1125, 0.1627])
        self.assertEqual((b["ratio_pref_source"], b["ratio_15min_median"]), ("windows", 0.1381))
        self.assertEqual((b["windows_per_week_pref"], b["windows_left_pref"]), (6.5, 5.2))
        self.assertEqual(b["ratio_median"], 0.1333)   # old field unchanged


class ThresholdBlock(unittest.TestCase):
    """The keep-alive + usage section carries pacing.threshold_info() (the reserve threshold,
    the ratio's variance and the model's inaccuracy); a failure is shown, not raised."""

    def test_threshold_block(self):
        import pacing
        old = pacing.threshold_info
        seen = {}
        try:
            pacing.threshold_info = lambda now=None, decision=None: seen.update(d=decision) or {"threshold": {"active": 84.6}}
            self.assertEqual(qv.threshold_block(datetime(2026, 10, 4), {"mode": "night"})["threshold"]["active"], 84.6)
            self.assertEqual(seen["d"], {"mode": "night"})
            qv.threshold_block(datetime(2026, 10, 4), {"mode": "linear"})       # not a pacing decision
            self.assertIsNone(seen["d"])

            def boom(**kw):
                raise RuntimeError("x")
            pacing.threshold_info = boom
            self.assertIn("RuntimeError", qv.threshold_block(datetime(2026, 10, 4))["error"])
        finally:
            pacing.threshold_info = old

    def test_page_renders_the_tile(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        for needle in ("k.threshold", "Reserve threshold", "model error", "dynamic default"):
            self.assertIn(needle, page)



class NextRunBlock(unittest.TestCase):
    """D-166: the keep-alive + usage section shows when the next run takes place
    (pacing.next_run), not the budget rule; a failure is shown, not raised."""

    def test_block(self):
        import pacing
        from datetime import timezone
        old = pacing.next_run
        at = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        try:
            pacing.next_run = lambda u, now, decision=None: {
                "at": at, "kind": "last_stretch", "label": "last stretch", "reason": "predicted 98.6% > 84.6%",
                "predicted_end": 98.6123, "threshold": 84.58, "last_stretch_at": at,
                "expected": {"at": at, "kind": "last_stretch", "label": "last stretch", "reason": "r",
                             "predicted_end": 101.04, "last_stretch_at": at}}
            b = qv.next_run_block({}, datetime(2026, 10, 4, tzinfo=timezone.utc))
            self.assertEqual((b["kind"], b["label"], b["predicted_end"], b["threshold"]),
                             ("last_stretch", "last stretch", 98.6, 84.6))
            self.assertTrue(b["at_berlin"].startswith("Thu 08.10. 14:00"))
            self.assertEqual((b["expected"]["kind"], b["expected"]["predicted_end"]), ("last_stretch", 101.0))
            self.assertTrue(b["expected"]["at_berlin"].startswith("Thu 08.10. 14:00"))

            def boom(*a, **kw):
                raise RuntimeError("x")
            pacing.next_run = boom
            self.assertIn("RuntimeError", qv.next_run_block({}, datetime(2026, 10, 4))["error"])
        finally:
            pacing.next_run = old

    def test_page_shows_the_next_run_not_the_rule(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        self.assertIn("k.next_run", page)
        self.assertIn("Next run (if you use nothing more)", page)   # D-200
        self.assertIn("With your forecast usage", page)
        self.assertIn("nr.expected", page)
        self.assertNotIn("budget_rule_now", page)
        self.assertNotIn("Budget rule", page)


if __name__ == "__main__":
    unittest.main()
