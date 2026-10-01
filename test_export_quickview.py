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


if __name__ == "__main__":
    unittest.main()
