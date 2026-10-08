#!/usr/bin/env python3
"""Offline tests for run_metrics.py (D-207: how long an AFClaude run takes to fill the session
limit): the pure compute_run() (full fill, early end with extrapolation, stale rows, a run
crossing the session reset, an ongoing run), fires -> runs, readings, update() on temp files
(backfill, final rows kept), the summary, the watcher's extra readings and the hooks in
keepalive.py / usage_sampler.py / export_quickview.py. Never touches the live data/."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["AFCLAUDE_CONFIG"] = os.devnull   # hermetic: the default config
import run_metrics as rm  # noqa: E402
import keepalive as ka  # noqa: E402

UTC = timezone.utc
F = datetime(2026, 10, 8, 2, 0, 5, tzinfo=UTC)          # a fire at the 04:00 Berlin session-window start
WEND = datetime(2026, 10, 8, 7, 0, tzinfo=UTC)          # its session window ends 5 h later
WRESET = datetime(2026, 10, 8, 17, 0, tzinfo=UTC)       # the weekly reset
SID = "00000000-0000-4000-8000-000000000001"


def M(m):
    return F + timedelta(minutes=m)


def rd(minute, session, weekly, wend=WEND, stale=False, src="sampler", own=None, other=None, fetched=None):
    t = M(minute)
    return {"t": fetched or t, "at": t, "src": src, "session": session,
            "session_resets_at": wend if session else None, "weekly": weekly, "weekly_resets_at": WRESET,
            "stale": stale, "own": own, "other": other, "since": t - timedelta(minutes=15)}


def run(at=F, usage=None, kind="window-start", rid="window-start-2026-10-08-s2"):
    return {"run_id": rid, "at": at, "kind": kind, "session": SID, "usage": usage, "fires": [rid]}


def every(a, b, step=2):
    return [M(x) for x in range(a, b + 1, step)]


class ComputeRun(unittest.TestCase):
    def test_full_fill_ends_at_the_limit_notice(self):
        reads = [rd(-5, 0, 70)] + [rd(m, p, w) for m, p, w in
                                   ((15, 20, 73), (30, 41, 76), (45, 62, 79), (60, 84, 82), (75, 98, 85))]
        reads.append(rd(90, 100, 85.5))                      # the reading after the notice
        hit = M(80)
        r = rm.compute_run(run(), reads, every(0, 80), [(hit, "session")], now=M(200))
        self.assertEqual((r["end_reason"], r["limit_kind"], r["final"], r["ongoing"]), ("limit", "session", True, False))
        self.assertEqual(r["end"], hit.isoformat())
        self.assertEqual((r["session_start_pct"], r["session_end_pct"], r["session_delta"]), (0.0, 100.0, 100.0))
        self.assertEqual((r["t100_min"], r["t100_source"], r["fill_min"]), (80.0, "limit-notice", 80.0))
        # 95% between 84% at 60 min and 98% at 75 min: 60 + 15 * 11/14
        self.assertAlmostEqual(r["t95_min"], 60 + 15 * 11 / 14, places=1)
        self.assertEqual(r["rate_pct_per_h"], 75.0)          # 100 % in 80 min
        self.assertEqual(r["empty_to_full_min"], 80.0)
        self.assertIsNone(r["fill_min_est"])
        self.assertEqual((r["weekly_start_pct"], r["weekly_end_pct"], r["weekly_delta"]), (70, 85.5, 15.5))
        self.assertEqual(r["window_end"], WEND.isoformat())
        self.assertFalse(r["window_end_estimated"])
        self.assertEqual(r["trajectory"][0], [15.0, 20, 73])
        self.assertEqual(r["readings"], 6)

    def test_full_fill_from_readings_without_a_notice(self):
        reads = [rd(-5, 0, 70), rd(30, 50, 76), rd(60, 90, 82), rd(75, 100, 84)]
        r = rm.compute_run(run(), reads, None, [], now=M(300))
        self.assertEqual((r["end_reason"], r["limit_kind"], r["t100_source"]), ("limit", "session", "readings"))
        self.assertEqual(r["fill_min"], 75.0)                # the first reading at 100%
        self.assertAlmostEqual(r["t95_min"], 67.5, places=1)

    def test_early_end_is_extrapolated(self):
        """Idle after its last turn at 40 min with 30%: the reading after the end still counts
        (the meter lags), the rate is 30 %/40 min = 45 %/h, full ~ 133 min, 95% ~ 127 min."""
        reads = [rd(-3, 0, 60), rd(15, 12, 62), rd(30, 25, 64), rd(45, 30, 65), rd(60, 30, 65), rd(120, 55, 70)]
        r = rm.compute_run(run(), reads, every(0, 40), [], now=M(300))
        self.assertEqual((r["end_reason"], r["end"]), ("idle", M(40).isoformat()))
        self.assertEqual(r["duration_min"], 40.0)
        self.assertEqual((r["session_end_pct"], r["weekly_end_pct"], r["weekly_delta"]), (30, 65, 5))
        self.assertEqual(r["rate_pct_per_h"], 45.0)
        self.assertIsNone(r["fill_min"])
        self.assertAlmostEqual(r["fill_min_est"], 133.3, places=1)
        self.assertAlmostEqual(r["t95_min_est"], 126.7, places=1)
        self.assertAlmostEqual(r["empty_to_full_min"], 133.3, places=1)
        self.assertEqual([p[0] for p in r["trajectory"]], [15.0, 30.0, 45.0])   # later user use: not the run's
        self.assertTrue(r["final"])

    def test_stale_rows_are_skipped_and_counted(self):
        """A frozen cache (usage_stale) at 90% must neither fill the run nor end it."""
        reads = [rd(-3, 0, 60), rd(15, 90, 95, stale=True), rd(30, 20, 63), rd(45, 99, 99, stale=True),
                 rd(60, 40, 66)]
        r = rm.compute_run(run(), reads, every(0, 60), [], now=M(120))
        self.assertEqual(r["stale_skipped"], 2)
        self.assertEqual(r["end_reason"], "idle")
        self.assertEqual(r["session_end_pct"], 40)
        self.assertIsNone(r["t95_min"])
        self.assertEqual(r["rate_pct_per_h"], 40.0)
        self.assertEqual([p[1] for p in r["trajectory"]], [20, 40])

    def test_run_crossing_the_session_reset(self):
        """A run that starts in an older window (35% at the fire, window ends at 90 min) and keeps
        working: it is measured up to the window end; the readings of the next window are not
        part of it (the % drops), the rate covers the in-window part only."""
        wend = datetime(2026, 10, 8, 3, 30, tzinfo=UTC)      # 90 min after the fire, a whole minute
        reads = [rd(-2, 35, 50, wend=wend), rd(30, 55, 54, wend=wend), rd(60, 75, 58, wend=wend),
                 rd(85, 92, 61, wend=wend), rd(105, 8, 63, wend=M(390)), rd(120, 15, 64, wend=M(390))]
        r = rm.compute_run(run(), reads, every(0, 200), [], now=M(400))
        self.assertEqual((r["end_reason"], r["end"]), ("window-end", wend.isoformat()))
        self.assertTrue(r["active_after_window_end"])
        self.assertEqual((r["session_start_pct"], r["session_end_pct"]), (35, 92))
        self.assertEqual(r["readings"], 3)
        self.assertEqual(r["rate_pct_per_h"], 40.2)          # 57 % in 85 min
        self.assertAlmostEqual(r["fill_min_est"], 97.0, places=0)   # (100-35) at 40.2 %/h
        self.assertEqual(r["weekly_delta"], 11)
        self.assertIsNone(r["fill_min"])

    def test_reading_before_the_reset_counts_as_zero(self):
        """The last reading before the fire still shows the old window (reset at the fire): 0%."""
        reads = [rd(-10, 80, 50, wend=M(-1)), rd(30, 20, 53, wend=M(299))]
        r = rm.compute_run(run(), reads, every(0, 30), [], now=M(100))
        self.assertEqual(r["session_start_pct"], 0.0)
        self.assertEqual(r["window_end"], datetime(2026, 10, 8, 6, 59, tzinfo=UTC).isoformat())   # to the minute

    def test_fire_snapshot_and_ongoing_run(self):
        usage = {"fetched_at": (F - timedelta(seconds=20)).isoformat(),
                 "session": {"percent": 4.0, "resets_at": None},
                 "weekly": {"percent": 70.0, "resets_at": WRESET.isoformat()}}
        reads = [rd(-14, 0, 69), rd(15, 24, 73), rd(20, 30, 74, src="watcher")]
        r = rm.compute_run(run(usage=usage), reads, every(0, 22), [], now=M(25))
        self.assertTrue(r["ongoing"])
        self.assertFalse(r["final"])
        self.assertEqual((r["end_reason"], r["duration_min"]), ("ongoing", 25.0))
        self.assertEqual((r["session_start_pct"], r["weekly_start_pct"]), (4.0, 70.0))   # the fire's snapshot
        self.assertEqual(r["rate_pct_per_h"], 78.0)          # 26 % in 20 min
        self.assertAlmostEqual(r["fill_min_est"], 73.8, places=1)
        self.assertTrue(r["window_end_estimated"] is False)  # from the 15-min reading

    def test_no_reply_and_no_data(self):
        r = rm.compute_run(run(), [], [], [], now=M(60))
        self.assertEqual((r["end_reason"], r["duration_min"], r["rate_pct_per_h"]), ("idle", 0.0, None))
        self.assertTrue(r["window_end_estimated"])
        r = rm.compute_run(run(), [], None, [], now=M(60))   # no transcript: ongoing until the window end
        self.assertTrue(r["ongoing"])
        r = rm.compute_run(run(), [], None, [], now=M(301))
        self.assertEqual(r["end_reason"], "window-end")

    def test_weekly_limit_and_next_run(self):
        reads = [rd(-2, 0, 96), rd(15, 30, 99), rd(25, 40, 100)]
        r = rm.compute_run(run(), reads, every(0, 24), [(M(24), "weekly")], now=M(120))
        self.assertEqual((r["end_reason"], r["limit_kind"], r["weekly_end_pct"], r["weekly_delta"]),
                         ("limit", "weekly", 100.0, 4.0))
        self.assertIsNone(r["t100_min"])
        r = rm.compute_run(run(), [rd(-2, 0, 50)], every(0, 300), [], now=M(400), next_fire=M(100))
        self.assertEqual((r["end_reason"], r["end"]), ("next-run", M(100).isoformat()))


class FiresAndReadings(unittest.TestCase):
    def test_fire_kind(self):
        self.assertEqual(rm.fire_kind("window-start-2026-10-08-s2"), "window-start")
        self.assertEqual(rm.fire_kind("window-start-2026-10-08", {"reason": "window start (postponed), X"}),
                         "postponed-start")
        self.assertEqual(rm.fire_kind("last-mile-2026-10-08T17:00:00+00:00-s2"), "last-stretch")
        self.assertEqual(rm.fire_kind("manual-now-2026-09-29T09:23:01+00:00"), "manual")
        self.assertEqual(rm.fire_kind("window-start-2026-09-29-manualtest"), "manual")

    def test_fires_and_grouping(self):
        st1 = {"handled": {
            "a": {"at": "2026-10-01T12:00:32+00:00", "result": "continued-in-place", "reason": "x"},
            "b": {"at": "2026-10-01T12:01:48+00:00", "result": "continued-in-place"},   # the duplicate slot fire
            "c": {"at": "2026-10-01T23:00:00+00:00", "result": "dry-run"},
            "d": {"at": "2026-10-01T23:05:00+00:00", "result": "preflight-failed"},
            "e": {"at": "2026-10-02T02:00:00+00:00", "result": "no-reply-within-timeout", "session": "s2"}}}
        st2 = {"handled": {"a": {"at": "2026-10-01T12:00:32+00:00", "result": "continued-in-place"}}}
        fires = rm.fires_from_states([st1, st2], default_session="s1")
        self.assertEqual([f["key"] for f in fires], ["a", "b", "e"])
        self.assertEqual(fires[2]["session"], "s2")
        runs = rm.group_fires(fires)
        self.assertEqual([(r["run_id"], r["fires"]) for r in runs], [("a", ["a", "b"]), ("e", ["e"])])

    def test_readings_stale_and_dedup(self):
        row = {"at": "2026-10-08T02:15:00+00:00",
               "usage": {"fetched_at": "2026-10-08 02:15:03+00:00", "session": {"percent": 20.0, "resets_at": "2026-10-08 07:00:00.3+00:00"},
                         "weekly": {"percent": 73.0, "resets_at": "2026-10-08 17:00:00+00:00"}},
               "own_w_tokens": 100, "other_w_tokens": 5}
        r = rm.reading_from_sample(row)
        self.assertEqual((r["session"], r["weekly"], r["stale"], r["own"]), (20.0, 73.0, False, 100.0))
        old = dict(row, usage=dict(row["usage"], fetched_at="2026-10-07 22:00:00+00:00"))
        self.assertTrue(rm.reading_from_sample(old)["stale"])          # older rows: the age test
        flagged = dict(row, usage=dict(row["usage"], stale=True))
        self.assertTrue(rm.reading_from_sample(flagged)["stale"])
        self.assertIsNone(rm.reading_from_sample({"at": "2026-10-08T02:15:00+00:00", "usage": {}}))
        w = rm.reading_from_watch({"at": "2026-10-08T02:15:30+00:00", "fetched_at": "2026-10-08T02:15:03+00:00",
                                   "session_pct": 20.0, "session_resets_at": "2026-10-08T07:00:00+00:00",
                                   "weekly_pct": 73.0, "stale": False})
        w2 = rm.reading_from_watch({"at": "2026-10-08T02:20:00+00:00", "fetched_at": "2026-10-08T02:20:01+00:00",
                                    "session_pct": 22.0, "weekly_pct": 73.0})
        merged = rm.merge_readings([w, r, w2])
        self.assertEqual([(x["src"], x["session"]) for x in merged], [("sampler", 20.0), ("watcher", 22.0)])


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def sample_row(minute, session, weekly, wend=WEND, stale=False):
    t = M(minute)
    u = {"rc": 0, "fetched_at": str(t + timedelta(seconds=3)),
         "session": {"percent": session, "resets_at": str(wend) if session else None},
         "weekly": {"percent": weekly, "resets_at": str(WRESET)}, "stale": stale}
    return {"at": t.isoformat(), "tag": "cron", "usage": u, "own_w_tokens": 1000, "other_w_tokens": 10}


class Update(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.state = os.path.join(d, "keepalive_state.json")
        self.samples = os.path.join(d, "samples.jsonl")
        self.watch = os.path.join(d, "run_usage.jsonl")
        self.out = os.path.join(d, "afclaude_runs.jsonl")
        self.projects = os.path.join(d, "projects")
        with open(self.state, "w") as fh:
            json.dump({"handled": {
                "window-start-2026-10-08-s2": {"at": F.isoformat(), "result": "continued-in-place", "session": SID},
                "window-start-2026-10-07": {"at": (F - timedelta(hours=5)).isoformat(), "result": "dry-run"}},
                "fires": {}}, fh)
        _write(self.samples, [sample_row(-15, 0, 70), sample_row(0, 0, 70)] +
               [sample_row(m, p, w) for m, p, w in ((15, 25, 74), (30, 50, 78), (45, 75, 82), (60, 95, 85))] +
               [sample_row(75, 100, 86), sample_row(90, 100, 86)])
        _write(self.watch, [{"at": M(50).isoformat(), "fetched_at": M(50).isoformat(), "session_pct": 83.0,
                             "session_resets_at": WEND.isoformat(), "weekly_pct": 83.0,
                             "weekly_resets_at": WRESET.isoformat(), "stale": False}])
        entries = [{"type": "user", "timestamp": F.isoformat(), "message": {"role": "user", "content": "go"}}]
        entries += [{"type": "assistant", "timestamp": M(m).isoformat(),
                     "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "x"}]}}
                    for m in range(1, 70, 3)]
        entries.append({"type": "assistant", "uuid": "lim", "timestamp": M(70).isoformat(), "error": "rate_limit",
                        "isApiErrorMessage": True, "message": {"model": "<synthetic>", "content": [
                            {"type": "text", "text": "You've hit your session limit · resets 7am (UTC)"}]}})
        _write(os.path.join(self.projects, "-proj", f"{SID}.jsonl"), entries)
        _write(os.path.join(self.projects, "-proj", SID, "subagents", "agent-1.jsonl"),
               [{"type": "assistant", "isSidechain": True, "timestamp": M(68).isoformat(), "message": {}}])
        for f in (os.path.join(self.projects, "-proj", f"{SID}.jsonl"),
                  os.path.join(self.projects, "-proj", SID, "subagents", "agent-1.jsonl")):
            os.utime(f, (M(71).timestamp(), M(71).timestamp()))   # written during the run (mtime >= the fire)

    def tearDown(self):
        self.tmp.cleanup()

    def up(self, now, **kw):
        return rm.update(now=now, out_path=self.out, state_paths=[self.state], samples_path=self.samples,
                         watch_path=self.watch, projects_dir=self.projects, session="fallback", **kw)

    def test_backfill_then_keep_final_rows(self):
        rows = self.up(M(30))                                 # while the run goes
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ongoing"])
        rows = self.up(M(200))                                # after it: final, from the transcript notice
        r = rows[0]
        self.assertEqual((r["end_reason"], r["limit_kind"], r["t100_min"], r["t100_source"], r["final"]),
                         ("limit", "session", 70.0, "limit-notice", True))
        self.assertEqual(r["kind"], "window-start")
        self.assertIn([50.0, 83.0, 83.0], r["trajectory"])   # the watcher's extra reading
        self.assertEqual(r["own_w_tokens"], 5000)            # sampler intervals 15..75 min
        self.assertEqual(r["user_share"], 0.01)
        with open(self.out) as fh:
            self.assertEqual(len(fh.readlines()), 1)
        mtime = os.path.getmtime(self.out)
        calls = []
        rows2 = self.up(M(400), activity_fn=lambda *a: calls.append(a) or (None, []))
        self.assertEqual(rows2, rows)                         # final: not recomputed, file not rewritten
        self.assertEqual(calls, [])
        self.assertEqual(os.path.getmtime(self.out), mtime)

    def test_rows_are_never_dropped(self):
        _write(self.out, [{"run_id": "old-run", "start": "2026-09-01T00:00:00+00:00", "final": True}])
        rows = self.up(M(200))
        self.assertEqual([r["run_id"] for r in rows], ["old-run", "window-start-2026-10-08-s2"])

    def test_session_activity(self):
        acts, hits = rm.session_activity(SID, F - timedelta(minutes=1), M(300), self.projects)
        self.assertEqual(hits, [(M(70), "session")])
        self.assertIn(M(68), acts)                            # a subagent entry counts as activity
        self.assertNotIn(M(70), acts)
        self.assertEqual(rm.session_activity("nope", F, M(300), self.projects), (None, []))

    def test_load_readings_range(self):
        r = rm.load_readings(M(10), M(40), self.samples, self.watch)
        self.assertEqual([x["session"] for x in r], [25, 50])


class Summary(unittest.TestCase):
    def test_summary(self):
        self.assertEqual(rm.summary([]), {"n": 0})
        rows = [{"run_id": "a", "start": "2026-10-01T00:00:00+00:00", "empty_to_full_min": 80.0, "fill_min": 80.0,
                 "session_start_pct": 0.0},
                {"run_id": "b", "start": "2026-10-02T00:00:00+00:00", "empty_to_full_min": 100.0, "fill_min": None,
                 "fill_min_est": 100.0},
                {"run_id": "c", "start": "2026-10-03T00:00:00+00:00", "empty_to_full_min": None},
                {"run_id": "d", "start": "2026-10-04T00:00:00+00:00", "empty_to_full_min": 120.0, "fill_min": 70.0,
                 "session_start_pct": 40.0, "ongoing": True}]
        s = rm.summary(rows)
        self.assertEqual((s["n"], s["n_rated"], s["median_empty_to_full_min"]), (4, 3, 100.0))
        self.assertEqual(len(s["p25_p75_empty_to_full_min"]), 2)
        self.assertEqual((s["median_fill_min"], s["n_fills"]), (80.0, 1))   # only fills from ~empty
        self.assertEqual((s["last"]["run_id"], s["last"]["ongoing"]), ("d", True))


class WatcherReadings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "run_usage.jsonl")
        self.olds = (ka.run_active, ka.read_usage_cache, ka.refresh_usage_checked)

    def tearDown(self):
        ka.run_active, ka.read_usage_cache, ka.refresh_usage_checked = self.olds
        self.tmp.cleanup()

    def test_only_during_a_run_and_refresh_only_if_old(self):
        now, refreshed = M(30), []
        cache = {"fetched_at": M(29), "session": {"percent": 40.0, "resets_at": WEND},
                 "weekly": {"percent": 75.0, "resets_at": WRESET}}
        ka.read_usage_cache = lambda: cache
        ka.refresh_usage_checked = lambda now=None, **k: refreshed.append(now) or {"usage": dict(cache, fetched_at=now)}
        ka.run_active = lambda sid, now, st=None: None
        self.assertIsNone(rm.watch_sample(SID, now, path=self.path))
        self.assertFalse(os.path.exists(self.path))
        ka.run_active = lambda sid, now, st=None: F
        row = rm.watch_sample(SID, now, path=self.path)
        self.assertEqual((row["session_pct"], row["stale"], row["run_start"]), (40.0, False, F.isoformat()))
        self.assertEqual(refreshed, [])                      # the cache was fresh: no /usage call
        rm.watch_sample(SID, M(40), path=self.path)          # 11 min old: one /usage
        self.assertEqual(refreshed, [M(40)])
        with open(self.path) as fh:
            rows = [json.loads(x) for x in fh]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rm.reading_from_watch(rows[1])["t"], M(40))

    def test_watcher_hook_never_fails(self):
        def boom(*a, **k):
            raise RuntimeError("x")
        ka.run_active = boom
        logs, old_log = [], ka.log
        ka.log = logs.append
        try:
            ka.watch_run_usage(SID, M(1))
        finally:
            ka.log = old_log
        self.assertTrue(logs and "run usage reading failed" in logs[0])


class Hooks(unittest.TestCase):
    def test_handle_fire_records_session_and_usage(self):
        cache = {"fetched_at": F, "session": {"percent": 3.0, "resets_at": None},
                 "weekly": {"percent": 70.0, "resets_at": WRESET}}
        names = ("STATE_FILE", "PROGRESS_FILE", "PROJECTS_DIR", "preflight", "fire", "verify_reply", "agent_entries",
                 "read_usage_cache", "log", "budget_headroom")
        old = {n: getattr(ka, n) for n in names}
        with tempfile.TemporaryDirectory() as d:
            ka.STATE_FILE, ka.PROGRESS_FILE, ka.PROJECTS_DIR = os.path.join(d, "st.json"), os.path.join(d, "P.md"), d
            _write(os.path.join(d, "-proj", f"{SID}.jsonl"), [{"type": "user", "timestamp": F.isoformat(),
                                                               "message": {"role": "user", "content": "x"}}])
            ka.preflight = lambda sid: (True, [], "send-keys")
            ka.fire = lambda *a, **k: (0, "sent-keys", "")
            ka.verify_reply = lambda path, since, timeout=None: {"message": {"model": "claude-opus-5-5"}}
            ka.agent_entries = lambda s: []
            ka.read_usage_cache = lambda: cache
            ka.log = lambda *a: None
            ka.budget_headroom = lambda u, now: (None, "budget t")

            class A:
                arm = True
            try:
                st = {"handled": {}, "fires": {}}
                ka.handle_fire(SID, {"uuid": "window-start-x", "timestamp": F, "budget": "b"}, "window start, r", st, A())
            finally:
                for n, v in old.items():
                    setattr(ka, n, v)
            rec = st["handled"]["window-start-x"]
        self.assertEqual(rec["result"], "continued-in-place")
        self.assertEqual(rec["session"], SID)
        self.assertEqual(rec["usage"]["session"], {"percent": 3.0, "resets_at": None})
        self.assertEqual(rec["usage"]["weekly"]["resets_at"], WRESET.isoformat())
        f = rm.fires_from_states([st])[0]
        r = rm.compute_run(rm.group_fires([f])[0], [], [], [], now=F + timedelta(minutes=1))
        self.assertEqual((r["session_start_pct"], r["weekly_start_pct"]), (3.0, 70.0))

    def test_sampler_hook_never_sinks_the_sample(self):
        import usage_sampler as us
        old = rm.update

        def boom(**k):
            raise RuntimeError("x")
        rm.update = boom
        try:
            self.assertIsNone(us.update_runs(F))
        finally:
            rm.update = old
        seen = {}
        rm.update = lambda **k: seen.update(k) or [1, 2]
        try:
            self.assertEqual(us.update_runs(F), 2)
        finally:
            rm.update = old
        self.assertEqual((seen["samples_path"], seen["out_path"]), (us.SAMPLES, us.RUNS))

    def test_quickview_fill_block_and_tile(self):
        import export_quickview as qv
        with tempfile.TemporaryDirectory() as d:
            old = qv.RUNS_FILE
            qv.RUNS_FILE = os.path.join(d, "runs.jsonl")
            try:
                self.assertEqual(qv.fill_block(), {"n": 0})
                _write(qv.RUNS_FILE, [{"run_id": "a", "start": "x", "empty_to_full_min": 90.0, "fill_min": 90.0,
                                       "session_start_pct": 0.0}])
                b = qv.fill_block()
            finally:
                qv.RUNS_FILE = old
        self.assertEqual((b["n"], b["median_empty_to_full_min"], b["last"]["fill_min"]), (1, 90.0, 90.0))
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        for needle in ("fillTile(k.fill)", "Session fill time", "median empty → full", "fill_min_est"):
            self.assertIn(needle, page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
