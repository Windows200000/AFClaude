"""Offline tests for phase 3c S2's detector (docs/phase3c_plan.md §3.2, §6 question 1; D-144, D-218):
compact_timing's age test (first_activity, ran_before_window) at the window edges and on a DST night,
and keepalive.long_running_check: a review / request session past the test that is live in tmux alerts
once (its run_log ELIGIBLE row), one whose tmux session is gone is ended once (driven.set) and not
checked again, below the test, ended, a task-manager or an owner's session nothing, a read or the
ending write failing raises (the tick reports it: fail_stop), more rows than one tick checks are the
next ticks', the once-per-tick part runs once over several task-managers, and its calls are in the
compaction_check tick's timeout (not the act's)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import compact_timing as ct  # noqa: E402
import keepalive as ka  # noqa: E402
import runner  # noqa: E402
import runner_jobs  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
RESET = datetime(2026, 10, 30, 21, 0, 0, 400_000, tzinfo=UTC)   # jitters by ms, as the cache's does
START = datetime(2026, 10, 30, 16, 0, tzinfo=UTC)               # the open window's start: RESET - 5 h
NOW = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
REV = "11111111-2222-4333-8444-555555555555"
REQ = "22222222-3333-4444-8555-666666666666"
TM = "33333333-4444-4555-8666-777777777777"


def entry(kind, ts):
    return {"type": kind, "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "message": {"role": kind, "content": "x"}}


class AgeTest(unittest.TestCase):
    def transcript(self, lines):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        self.addCleanup(os.unlink, path)
        return path

    def test_first_activity_is_the_first_user_or_assistant_entry(self):
        t = NOW - timedelta(hours=7)
        path = self.transcript([json.dumps({"type": "summary", "summary": "s"}),
                                json.dumps({"type": "file-history-snapshot", "timestamp": "2026-10-01T00:00:00Z"}),
                                "{not json",
                                json.dumps(entry("user", t)), json.dumps(entry("assistant", NOW))])
        self.assertEqual(ct.first_activity(path), t)
        self.assertIsNone(ct.first_activity(self.transcript([json.dumps({"type": "summary"})])))

    def test_window_edges(self):
        self.assertTrue(ct.ran_before_window(START - timedelta(minutes=1), START))
        self.assertFalse(ct.ran_before_window(START + timedelta(minutes=1), START))
        self.assertFalse(ct.ran_before_window(START, START))
        self.assertFalse(ct.ran_before_window(None, START))                 # no activity: not eligible

    def test_a_dst_night(self):
        # 25.10.2026, Berlin CEST -> CET at 03:00 local: a window 22:30 UTC .. 03:30 UTC
        berlin = ka.BERLIN
        start = datetime(2026, 10, 24, 22, 30, tzinfo=UTC)
        before = datetime(2026, 10, 25, 0, 29, tzinfo=berlin)               # 22:29 UTC (CEST, +2)
        after = datetime(2026, 10, 25, 3, 30, tzinfo=berlin)                # 02:30 UTC (CET, +1)
        self.assertTrue(ct.ran_before_window(before, start))
        self.assertFalse(ct.ran_before_window(after, start))


class Detector(unittest.TestCase):
    NAMES = ("PROJECTS_DIR", "usage_cache_checked", "tmux_has", "alert", "fail_stop", "TICKS",
             "LONG_RUN_TMUX_MAX")

    def setUp(self):
        testenv.fresh_db(self)
        self.old = {n: getattr(ka, n) for n in self.NAMES}
        self.addCleanup(lambda: [setattr(ka, n, v) for n, v in self.old.items()])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ka.PROJECTS_DIR = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, "proj"))
        self.usage = {"session": {"percent": 40.0, "resets_at": RESET}}
        ka.usage_cache_checked = lambda: self.usage
        self.live, self.tmux_calls, self.alerts, self.stops = set(), [], [], []

        def tmux_has(name):
            self.tmux_calls.append(name)
            return name in self.live
        ka.tmux_has = tmux_has
        ka.alert = lambda subject, body="": self.alerts.append((subject, body))
        ka.fail_stop = lambda subject, detail="", session=None: self.stops.append((subject, detail))

    def row(self, sid, kind="review", first=START - timedelta(minutes=1), live=True, ended=None, task_id=7):
        conn = store.connect()
        try:
            with store.transaction(conn):
                store.upsert_driven_session(conn, sid, kind=kind, tmux="afc-" + sid[:8], task_id=task_id,
                                            started_at=store.now_iso(), ended_at=ended)
        finally:
            conn.close()
        if first is not None:
            with open(os.path.join(self.tmp.name, "proj", sid + ".jsonl"), "w") as fh:
                fh.write(json.dumps(entry("user", first)) + "\n" + json.dumps(entry("assistant", NOW)) + "\n")
        if live:
            self.live.add("afc-" + sid[:8])

    def driven(self, sid):
        conn = store.connect()
        try:
            return store.get_driven_session(conn, sid)
        finally:
            conn.close()

    def audits(self, sid):
        conn = store.connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='driven.set' AND target_id=? "
                                "AND actor='keepalive' AND via='runner'", (sid,)).fetchone()[0]
        finally:
            conn.close()

    def rows(self):
        conn = store.connect()
        try:
            return [r for r in store.run_log(conn, "compaction", 100) if r["decision"] == "ELIGIBLE"]
        finally:
            conn.close()

    def test_a_review_past_the_test_alerts_once(self):
        self.row(REV)
        self.assertEqual(ka.long_running_check(NOW, True), [REV[:8]])
        self.assertEqual(len(self.alerts), 1)
        self.assertIn(REV[:8], self.alerts[0][0])
        self.assertIn("phase 3c S2", self.alerts[0][1])
        rows = self.rows()
        self.assertEqual([(r["session_id"], r["decision"]) for r in rows], [(REV, "ELIGIBLE")])
        self.assertTrue(rows[0]["reason"].startswith(f"long-running {REV}: review session"), rows[0]["reason"])
        self.assertEqual(ka.long_running_check(NOW, True), [])                    # the next tick: nothing
        self.assertEqual(ka.long_running_check(NOW + timedelta(hours=3), True), [])
        self.assertEqual((len(self.alerts), len(self.rows())), (1, 1))
        self.assertEqual(self.tmux_calls, ["afc-" + REV[:8]])                       # reported: no tmux check again

    def test_a_request_session_counts_too(self):
        self.row(REQ, kind="request")
        self.assertEqual(ka.long_running_check(NOW, True), [REQ[:8]])

    def test_below_the_test_nothing(self):
        self.row(REV, first=START + timedelta(minutes=1))
        self.assertEqual(ka.long_running_check(NOW, True), [])
        self.assertEqual((self.alerts, self.rows(), self.tmux_calls), ([], [], []))

    def test_the_task_manager_and_owners_sessions_are_never_checked(self):
        for i, kind in enumerate(("task-manager", "stall", "own")):
            self.row(TM[:-1] + str(i), kind=kind)
        self.assertEqual(ka.long_running_check(NOW, True), [])
        self.assertEqual((self.alerts, self.rows(), self.tmux_calls), ([], [], []))

    def test_an_ended_review_or_one_without_transcript_nothing(self):
        self.row(REV, ended=store.now_iso())
        self.row(TM, first=None)                                                   # a review with no transcript
        self.assertEqual(ka.long_running_check(NOW, True), [])
        self.assertEqual((self.alerts, self.rows(), self.tmux_calls), ([], [], []))

    def test_a_dead_tmux_row_is_ended_once_and_not_rechecked(self):
        self.row(REQ, kind="request", live=False)
        self.assertEqual(ka.long_running_check(NOW, True), [])
        self.assertIsNotNone(self.driven(REQ)["ended_at"])
        self.assertEqual(self.audits(REQ), 1)                                      # through driven.set, audited
        self.assertEqual(ka.long_running_check(NOW, True), [])
        self.assertEqual(self.tmux_calls, ["afc-" + REQ[:8]])
        self.assertEqual((self.alerts, self.rows(), self.audits(REQ)), ([], [], 1))

    def test_a_dry_run_ends_no_row(self):
        self.row(REQ, kind="request", live=False)
        ka.long_running_check(NOW, False)
        self.assertIsNone(self.driven(REQ)["ended_at"])

    def test_the_ending_write_failing_raises(self):
        self.row(REQ, kind="request", live=False)
        real = actions.perform

        def refuse(conn, name, params, **kw):
            if name == "driven.set":
                raise RuntimeError("database is locked")
            return real(conn, name, params, **kw)
        actions.perform = refuse
        self.addCleanup(setattr, actions, "perform", real)
        with self.assertRaisesRegex(RuntimeError, "database is locked"):
            ka.long_running_check(NOW, True)
        self.assertIsNone(self.driven(REQ)["ended_at"])
        res = ka.long_running_tick(NOW, ka._pass_args(None))                      # the tick: a fail-stop
        self.assertEqual((res["note"], len(self.stops)), ("the long-running session check failed", 1))

    def test_no_open_window_nothing(self):
        self.row(REV)
        for usage in (None, {"session": {"resets_at": None}}, {"session": {"resets_at": NOW - timedelta(minutes=1)}}):
            self.usage = usage
            self.assertEqual(ka.long_running_check(NOW, True), [])
        self.assertEqual(self.alerts, [])

    def test_a_dry_run_writes_and_alerts_nothing(self):
        self.row(REV)
        self.assertEqual(ka.long_running_check(NOW, False), [])
        self.assertEqual((self.alerts, self.rows()), ([], []))

    def test_read_failures_raise(self):
        self.row(REV)
        os.unlink(os.path.join(self.tmp.name, "proj", REV + ".jsonl"))
        os.mkdir(os.path.join(self.tmp.name, "proj", REV + ".jsonl"))           # the transcript can't be read
        with self.assertRaises(IsADirectoryError):
            ka.long_running_check(NOW, True)
        self.row(REQ, kind="request")

        def cache_failed():
            raise OSError("usage cache read failed")
        ka.usage_cache_checked = cache_failed
        with self.assertRaises(OSError):
            ka.long_running_check(NOW, True)
        ka.usage_cache_checked = lambda: self.usage
        os.rmdir(os.path.join(self.tmp.name, "proj", REV + ".jsonl"))

        def tmux_failed(name):
            raise OSError("tmux has-session failed rc=2")
        ka.tmux_has = tmux_failed
        with self.assertRaises(OSError):
            ka.long_running_check(NOW, True)
        self.assertEqual((self.alerts, self.rows()), ([], []))

    def test_the_tick_reports_a_failure_as_a_fail_stop(self):
        self.row(REV)
        ka.tmux_has = lambda name: (_ for _ in ()).throw(OSError("bridge down"))
        res = ka.long_running_tick(NOW, ka._pass_args(None))
        self.assertEqual(res["note"], "the long-running session check failed")
        self.assertEqual(len(self.stops), 1)
        self.assertIn("bridge down", self.stops[0][1])

    def test_more_rows_than_one_tick_checks_are_the_next_ticks(self):
        ka.LONG_RUN_TMUX_MAX = 1
        self.row(REV, live=False, first=START - timedelta(hours=2))
        self.row(REQ, kind="request", first=START - timedelta(hours=1))
        self.assertEqual(ka.long_running_check(NOW, True), [])                    # the oldest: ended
        self.assertEqual(self.tmux_calls, ["afc-" + REV[:8]])
        self.assertEqual(ka.long_running_check(NOW, True), [REQ[:8]])             # then the next
        self.assertEqual(ka.long_running_check(NOW, True), [])
        self.assertEqual(self.tmux_calls, ["afc-" + REV[:8], "afc-" + REQ[:8]])
        self.assertEqual((len(self.alerts), self.stops), (1, []))

    def test_once_per_tick_over_several_task_managers(self):
        self.row(REV)
        seen = []
        ka.TICKS = dict(ka.TICKS, compaction_check=lambda sid, now, args: seen.append(sid) or {"note": ""})
        tms = [{"session": TM, "name": "a"}, {"session": REQ, "name": "b"}]
        res = ka.run_tick("compaction_check", tms, NOW)
        self.assertEqual(seen, [TM, REQ])
        self.assertEqual(len(self.alerts), 1)
        self.assertIn(f"long-running: {REV[:8]} alerted", res["note"])
        self.assertEqual(len(self.alerts), 1)


class TickBudget(unittest.TestCase):
    def test_the_detector_is_in_the_tick_timeout_not_the_act(self):
        tick = runner_jobs.tick_inner("compaction_check")
        bare = runner_jobs.tick_inner("compaction_check", once=False)
        self.assertEqual(tick["keepalive.tmux_has"] - bare["keepalive.tmux_has"],
                         timedelta(seconds=30 * ka.LONG_RUN_TMUX_MAX))
        self.assertEqual(tick["host.usage_cache"] - bare["host.usage_cache"], timedelta(seconds=30))
        self.assertEqual(runner_jobs.act_inner("compaction")["keepalive.tmux_has"], bare["keepalive.tmux_has"])
        self.assertGreaterEqual(timedelta(minutes=runner.JOBS["compaction_check"].timeout_min),
                                runner_jobs.tick_min_timeout("compaction_check"))


if __name__ == "__main__":
    unittest.main()
