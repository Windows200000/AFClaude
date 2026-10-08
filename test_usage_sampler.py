import unittest
from datetime import datetime, timezone

import testenv  # noqa: F401  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import usage_sampler as us

UTC = timezone.utc
TEXT = """Current session: 52% used
What's contributing to your limits usage?

Last 24h · 251 requests · 1 session
  100% of your usage came from subagent-heavy sessions
  66% of your usage was at >150k context
  Top subagents: general-purpose 53%

Last 7d · 2933 requests · 41 sessions
  88% of your usage came from subagent-heavy sessions
  68% of your usage was at >150k context
  58% of your usage came from sessions active for 8+ hours
  Top subagents: general-purpose 19%, workflow-subagent 10%
"""
R = datetime(2026, 10, 1, 17, 0, 0, 189650, tzinfo=UTC)


def usage(w=94.0, s=52.0, wr=R, sr=R):
    return {"weekly": {"percent": w, "resets_at": wr}, "session": {"percent": s, "resets_at": sr},
            "limits_raw": [{"kind": "weekly_all"}, {"kind": "session"}], "text": TEXT}


class T(unittest.TestCase):
    def test_limits_kinds(self):
        self.assertEqual(us.limits_kinds(usage()), ["session", "weekly_all"])
        self.assertEqual(us.limits_kinds({}), [])

    def test_token_totals(self):
        act = {"own": {"tokens": {"claude-opus-4": {"out": 10, "think": 10, "cache_r": 5, "cache_w": 7}}},
               "other": {"tokens": {"claude-haiku": {"out": 100, "think": 0, "cache_r": 1, "cache_w": 2},
                                    "claude-sonnet": {"out": 10, "think": 0, "cache_r": 1, "cache_w": 2}}}}
        t = us.token_totals(act)
        self.assertEqual(t["own_w_tokens"], 60.0)
        self.assertAlmostEqual(t["other_w_tokens"], 40.0)
        self.assertEqual((t["own_cache_read"], t["own_cache_write"]), (5, 7))
        self.assertEqual((t["other_cache_read"], t["other_cache_write"]), (2, 4))
        idle = us.token_totals(None)
        self.assertEqual(set(idle.values()), {0})
        self.assertEqual(len(idle), 6)

    def test_deltas(self):
        prev = us.pct_state(usage(w=93.0, s=50.0))
        d = us.pct_deltas(usage(w=94.0, s=52.0), prev)
        self.assertEqual((d["weekly_pct_delta"], d["session_pct_delta"], d["pct_step"]), (1.0, 2.0, True))
        d = us.pct_deltas(usage(w=93.5, s=52.0), us.pct_state(usage(w=93.0, s=50.0)))
        self.assertFalse(d["pct_step"])
        self.assertEqual(d["weekly_pct_delta"], 0.5)

    def test_deltas_none(self):
        d = us.pct_deltas(usage(), None)
        self.assertEqual((d["weekly_pct_delta"], d["session_pct_delta"], d["pct_step"]), (None, None, False))
        # session reset in between: resets_at moved 5h, pct dropped
        prev = us.pct_state(usage(w=90.0, s=99.0, sr=R.replace(hour=12)))
        d = us.pct_deltas(usage(w=91.0, s=2.0), prev)
        self.assertIsNone(d["session_pct_delta"])
        self.assertEqual(d["weekly_pct_delta"], 1.0)
        # weekly reset
        prev = us.pct_state(usage(w=99.0, wr=R.replace(day=24)))
        self.assertIsNone(us.pct_deltas(usage(w=1.0), prev)["weekly_pct_delta"])

    def test_session_start(self):
        self.assertEqual(us.session_start(usage()), "2026-10-01T12:00:00.189650+00:00")
        self.assertIsNone(us.session_start({}))
        self.assertEqual(us.session_start({"session": {"resets_at": "2026-10-01 17:00:00+00:00"}}),
                         "2026-10-01T12:00:00+00:00")

    def test_breakdown(self):
        b = us.parse_breakdown(TEXT)
        self.assertEqual((b["requests_7d"], b["sessions_7d"], b["requests_24h"], b["sessions_24h"]), (2933, 41, 251, 1))
        self.assertEqual((b["over_ctx_pct_7d"], b["over_ctx_pct_24h"], b["over_ctx_threshold_k"]), (68, 66, 150))
        self.assertEqual((b["subagent_heavy_pct_7d"], b["subagent_heavy_pct_24h"]), (88, 100))
        self.assertEqual(b["long_session_pct_7d"], 58)
        self.assertNotIn("long_session_pct_24h", b)
        self.assertEqual(b["top_subagents_7d"], {"general-purpose": 19, "workflow-subagent": 10})

    def test_breakdown_robust(self):
        self.assertEqual(us.parse_breakdown(""), {})
        self.assertEqual(us.parse_breakdown(None), {})
        self.assertEqual(us.parse_breakdown("garbage · text"), {})

    def test_derived_never_fails(self):
        out = us.derived_fields({"limits_raw": 5, "text": None}, None, None)
        self.assertIn("breakdown", out)
        self.assertEqual(out["own_w_tokens"], 0)

    def test_series_line(self):
        row = {"at": "x", "usage": usage(), "own_w_tokens": 1, "other_w_tokens": 2,
               "activity": {"own": {"human_prompts": 1}, "other": {"human_prompts": 3}}}
        l = us.series_line(row)
        self.assertEqual((l["weekly_pct"], l["session_pct"], l["human_prompts"]), (94.0, 52.0, 4))
        self.assertNotIn("human_prompts", us.series_line({"at": "x", "usage": {}}))


class RatioSnapshot(unittest.TestCase):
    def rows(self):
        from datetime import timedelta
        end = datetime(2026, 9, 26, 17, 0, tzinfo=UTC)
        rows = [{"at": (end - timedelta(hours=5, minutes=10)).isoformat(),
                 "usage": {"session": {"percent": 0.0, "resets_at": None},
                           "weekly": {"percent": 30.0, "resets_at": R}}}]
        for i in range(1, 20):
            rows.append({"at": (end - timedelta(hours=5) + timedelta(minutes=15 * i)).isoformat(),
                         "usage": {"session": {"percent": float(2 * i), "resets_at": end},   # datetimes, as in memory
                                   "weekly": {"percent": 30.0 + (6 * i) // 19, "resets_at": R}}})
        return rows, end

    def test_records_completed_window_once_and_keeps_old_fields(self):
        import os
        import tempfile
        from datetime import timedelta
        rows, end = self.rows()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "session_windows.jsonl")
            t = end - timedelta(minutes=20)
            snap = us.ratio_snapshot(rows, t, windows_path=path)     # window still open
            self.assertFalse(os.path.exists(path))
            self.assertIn("ratio", snap)
            t = end + timedelta(minutes=5)
            snap = us.ratio_snapshot(rows, t, windows_path=path)
            snap = us.ratio_snapshot(rows, t, windows_path=path)     # second run: no duplicate
            with open(path) as fh:
                self.assertEqual(len(fh.readlines()), 1)
            self.assertEqual(snap["ratio_windows"]["n"], 1)
            self.assertAlmostEqual(snap["ratio_windows"]["weighted"], 6 / 38)
            for k in ("ratio", "windows_per_week", "windows_left_this_week", "weekly_pct_now",
                      "attribution", "preferred_ratio"):
                self.assertIn(k, snap)

    def test_window_file_failure_never_sinks_the_snapshot(self):
        from datetime import timedelta
        rows, end = self.rows()
        snap = us.ratio_snapshot(rows, end + timedelta(minutes=5), windows_path="/nonexistent/dir/w.jsonl")
        self.assertEqual(snap["ratio_windows"]["n"], 1)   # derived from rows instead

    def test_time_split_uses_the_run_spans(self):
        from datetime import timedelta
        rows, end = self.rows()
        old = us.limit_ratio.load_autonomous_spans
        try:
            us.limit_ratio.load_autonomous_spans = lambda rows, now=None, runs_path=None: []
            snap = us.ratio_snapshot(rows, end + timedelta(minutes=5), windows_path="/nonexistent/dir/w.jsonl")
            share = snap["attribution"]["week_share"]
            self.assertEqual((share["method"], share["status"], share["other_share"]), ("time", "ok", 1.0))

            def boom(*a, **k):
                raise OSError("x")
            us.limit_ratio.load_autonomous_spans = boom          # never sinks the sample
            snap = us.ratio_snapshot(rows, end + timedelta(minutes=5), windows_path="/nonexistent/dir/w.jsonl")
            self.assertEqual(snap["attribution"]["week_share"]["status"], "insufficient_data")
        finally:
            us.limit_ratio.load_autonomous_spans = old


if __name__ == "__main__":
    unittest.main()
