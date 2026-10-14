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

    def test_derived_fields(self):
        out = us.derived_fields({"limits_raw": [], "text": None}, None, None)
        self.assertEqual((out["breakdown"], out["own_w_tokens"], out["limits_kinds"]), ({}, 0, []))

    def test_derived_failure_raises(self):
        """D-234/D-235: no silently missing field; the sample run is aborted instead."""
        with self.assertRaises(TypeError):
            us.derived_fields({"limits_raw": 5, "text": None}, None, None)

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

    def test_window_file_failure_raises(self):
        """D-234/D-235: no snapshot from the rows alone when the window store fails."""
        from datetime import timedelta
        rows, end = self.rows()
        old = us.limit_ratio.append_new_windows

        def boom(*a, **k):
            raise OSError("window store broken")
        us.limit_ratio.append_new_windows = boom
        try:
            with self.assertRaises(OSError):
                us.ratio_snapshot(rows, end + timedelta(minutes=5), windows_path="/nonexistent/dir/w.jsonl")
        finally:
            us.limit_ratio.append_new_windows = old

    def test_time_split_uses_the_run_spans(self):
        import os
        import tempfile
        from datetime import timedelta
        rows, end = self.rows()
        old = us.limit_ratio.load_autonomous_spans
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "session_windows.jsonl")
            try:
                us.limit_ratio.load_autonomous_spans = lambda rows, now=None, runs_path=None: []
                snap = us.ratio_snapshot(rows, end + timedelta(minutes=5), windows_path=path)
                share = snap["attribution"]["week_share"]
                self.assertEqual((share["method"], share["status"], share["other_share"]), ("time", "ok", 1.0))

                def boom(*a, **k):
                    raise OSError("x")
                us.limit_ratio.load_autonomous_spans = boom      # an error now (D-234/D-235)
                with self.assertRaises(OSError):
                    us.ratio_snapshot(rows, end + timedelta(minutes=5), windows_path=path)
            finally:
                us.limit_ratio.load_autonomous_spans = old


class Main(unittest.TestCase):
    """D-234/D-235: a failure before the sample row aborts the run (no row, no state); one after
    it still saves the state, then is an error; both alert and pause automation."""
    def setUp(self):
        import json
        import os
        import tempfile
        import keepalive as ka
        self.ka = ka
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(d, "projects"))
        cj = os.path.join(d, "claude.json")
        with open(cj, "w") as fh:
            json.dump({"cachedUsageUtilization": {"utilization": {"limits": []}}}, fh)
        self.now = datetime.now(UTC)
        self.refresh = {"ok": True, "rc": 0, "out": "ok", "poke": None, "retried": False,
                        "usage": {"fetched_at": self.now, "session": {"percent": 5.0, "resets_at": None},
                                  "weekly": {"percent": 20.0, "resets_at": None}}}
        self.alerts, self.lines, self.saved = [], [], []
        names = {(us, "PROJECTS"): os.path.join(d, "projects"), (us, "STATE"): os.path.join(d, "sampler_state.json"),
                 (us, "SAMPLES"): os.path.join(d, "samples.jsonl"), (us, "SERIES"): os.path.join(d, "weekly_series.jsonl"),
                 (us, "HAIKU_LOG"): os.path.join(d, "haiku.jsonl"), (ka, "CLAUDE_JSON"): cj,
                 (ka, "refresh_usage_checked"): lambda cwd=None, now=None: self.refresh,
                 (us, "agents_snapshot"): lambda: {}, (us, "ratio_snapshot"): lambda rows, t: {},
                 (us, "track_cycle"): lambda u, t, tag: None, (us, "update_runs"): lambda t: 0,
                 (us, "haiku_judgement"): lambda st, t: [], (us, "log"): self.lines.append,
                 (us.failstop, "_notify"): lambda s, b="": self.alerts.append(s)}
        old = {k: getattr(*k) for k in names}
        for (obj, n), v in names.items():
            setattr(obj, n, v)

        def restore():
            for (obj, n), v in old.items():
                setattr(obj, n, v)
        self.addCleanup(restore)
        self.addCleanup(testenv.unpause)
        self.d = d

    def rows(self):
        import os
        p = os.path.join(self.d, "samples.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return fh.read().splitlines()

    def test_ok(self):
        self.assertEqual(us.main(["--no-haiku"]), 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.alerts, [])

    def test_failed_usage_reading_aborts_the_run(self):
        import os
        import afclaude_config
        self.refresh = dict(self.refresh, ok=False, rc=1, out="Total cost: $0", poke="rc=0")
        self.assertEqual(us.main(["--no-haiku"]), 1)
        self.assertEqual(self.rows(), [])                                  # no row from the old cache
        self.assertFalse(os.path.exists(us.STATE))                         # nothing consumed
        self.assertEqual(self.alerts, ["AFClaude STOPPED: " + self.ka.USAGE_FAILED])
        self.assertTrue(any(ln.startswith("ERROR: sampler: usage reading failed") for ln in self.lines))
        p = afclaude_config.automation_pause()
        self.assertTrue(p["paused"])
        self.assertEqual(p["by"], "runner:sampler")
        self.assertEqual(us.main(["--no-haiku"]), 1)                       # every 15 min: logged,
        self.assertEqual(len(self.alerts), 1)                              # alerted once per pause

    def test_failure_after_the_row_saves_the_state_then_errors(self):
        import os

        def boom(t):
            raise RuntimeError("runs broken")
        us.update_runs = boom
        self.assertEqual(us.main(["--no-haiku"]), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(os.path.exists(us.STATE))                         # no double count next run
        self.assertEqual(self.alerts, ["AFClaude STOPPED: run metrics update failed"])

    def test_agents_snapshot_failure_aborts(self):
        def roh(*a, **k):
            import subprocess
            raise subprocess.TimeoutExpired("claude agents", 60)
        old = us.host.run_on_host, us.agents_snapshot
        us.host.run_on_host = roh
        us.agents_snapshot = type(self).real_agents
        try:
            self.assertEqual(us.main(["--no-haiku"]), 1)
        finally:
            us.host.run_on_host, us.agents_snapshot = old
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.alerts, ["AFClaude STOPPED: `claude agents` snapshot failed"])


Main.real_agents = staticmethod(us.agents_snapshot)


if __name__ == "__main__":
    unittest.main()
