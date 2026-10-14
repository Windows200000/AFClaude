#!/usr/bin/env python3
"""Offline tests for usage_review.py's one-off review (--only / --at, D-220/D-221): run_once()
fires immediately and logs under st["one_off_runs"] without touching the monthly schedule;
schedule_once() spools an at-shim job for the right UTC time without running or scheduling
anything itself; main() wires --only/--at together. Also the D-228 chain (--only usage,stage-eta):
parse_only() canonicalizes the combination regardless of typing order; session_idle() /
usage_review_finished() / wait_for_usage_review() detect the usage review session finishing
without the (not yet built) D-224 stop-marker CLI; run_usage_then_eta() fires the usage review,
waits, then fires a SEPARATE fresh session for the ETA review pointed at its result -- or alerts
and never fires the ETA leg if the wait times out -- all without touching the monthly schedule."""
import argparse
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import keepalive as ka  # noqa: E402
import usage_review as ur  # noqa: E402
import actions  # noqa: E402
import store  # noqa: E402
import telemetry  # noqa: E402
import afclaude_config  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)


class Base(unittest.TestCase):
    def setUp(self):
        testenv.unpause()            # a failure in an earlier test paused automation (D-234/D-235)
        self.addCleanup(testenv.unpause)
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {"fire": ka.fire, "alert": ka.alert, "log": ka.log, "progress_note": ka.progress_note,
                     "state": ur.STATE, "subprocess_run": ur.subprocess.run}
        self.fires = []
        self.alerts = []
        ka.fire = lambda sid, cwd, msg, plan, new=False, **k: self.fires.append(
            (sid, msg, plan, new, k)) or (0, "started", "")
        ka.alert = lambda subject, body="": self.alerts.append((subject, body))
        ka.log = lambda msg: None
        ka.progress_note = lambda line: None
        ur.STATE = os.path.join(self.tmp.name, "usage_review_state.json")

    def tearDown(self):
        ka.fire, ka.alert, ka.log, ka.progress_note = (self.saved["fire"], self.saved["alert"],
                                                        self.saved["log"], self.saved["progress_note"])
        ur.STATE = self.saved["state"]
        ur.subprocess.run = self.saved["subprocess_run"]
        self.tmp.cleanup()


class RunOnce(Base):
    def test_fires_once_and_logs_under_one_off_runs(self):
        st = {"next_run_at": (NOW + timedelta(days=5)).isoformat(), "runs": []}
        ok = ur.run_once(st, NOW, "stage-eta")
        self.assertTrue(ok)
        self.assertEqual(len(self.fires), 1)
        sid, msg, plan, new, k = self.fires[0]
        self.assertEqual(plan, "resume")
        self.assertTrue(new)
        self.assertIn("one-off", k.get("name", ""))
        # only the one-off prompt + guard_respect + manager: never the regular usage_review.md
        # brief, and the owner's verbatim D-220/D-221 concerns are in it
        self.assertNotIn("periodic usage-model review", msg)
        self.assertIn("Schedule the eta review at end of this week", msg)
        self.assertIn("massive underestimate", msg)
        self.assertIn("Maybe count design chats towards afclaude usage", msg)
        self.assertIn("three categories", msg)
        # the monthly schedule is untouched
        self.assertEqual(st["runs"], [])
        self.assertEqual(st["next_run_at"], (NOW + timedelta(days=5)).isoformat())
        self.assertEqual(len(st["one_off_runs"]), 1)
        self.assertEqual(st["one_off_runs"][0]["only"], "stage-eta")
        self.assertEqual(st["one_off_runs"][0]["session"], sid)
        self.assertEqual(self.alerts, [])

    def test_failed_launch_alerts_and_still_does_not_touch_the_schedule(self):
        ka.fire = lambda *a, **k: (1, "", "boom")
        st = {"next_run_at": "2026-11-01T00:00:00+00:00", "runs": []}
        ok = ur.run_once(st, NOW, "stage-eta")
        self.assertFalse(ok)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("one-off stage-eta review launch failed", self.alerts[0][0])
        self.assertEqual(st["next_run_at"], "2026-11-01T00:00:00+00:00")
        p = afclaude_config.automation_pause()                          # D-234/D-235: an error, paused
        self.assertTrue(p["paused"])
        self.assertIn("usage_review: one-off stage-eta review launch failed", p["reason"])

    def test_paused_automation_starts_no_review(self):
        """While automation is paused (D-234/D-235) neither a one-off nor the monthly review starts;
        the one-off's at job is used up, so the owner is told to schedule it again."""
        conn = store.connect()
        try:
            actions.perform(conn, "automation.set", {"paused": True, "reason": "a test"}, actor="cli", via="cli")
        finally:
            conn.close()
        old_argv = sys.argv
        try:
            for argv in (["--only", "stage-eta", "--now"], ["--now"]):
                sys.argv = ["usage_review.py"] + argv
                self.assertEqual(ur.main(), 0)
        finally:
            sys.argv = old_argv
        self.assertEqual(self.fires, [])
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("one-off stage-eta review not started: automation is paused", self.alerts[0][0])
        self.assertIn("a test", self.alerts[0][1])

    def test_two_one_off_runs_each_fire_once(self):
        st = {"next_run_at": "2026-11-01T00:00:00+00:00", "runs": []}
        ur.run_once(st, NOW, "stage-eta")
        ur.run_once(st, NOW + timedelta(days=1), "stage-eta")
        self.assertEqual(len(self.fires), 2)
        self.assertEqual(len(st["one_off_runs"]), 2)
        self.assertNotEqual(st["one_off_runs"][0]["session"], st["one_off_runs"][1]["session"])


class ScheduleOnce(Base):
    def setUp(self):
        super().setUp()
        self.calls = []

        def fake_run(argv, input=None, text=None, capture_output=None, env=None):   # noqa: A002
            self.calls.append({"argv": argv, "input": input, "env": env})
            return type("R", (), {"returncode": 0, "stdout": "job abc123 at 2026-10-15 14:00 UTC\n",
                                  "stderr": ""})()
        ur.subprocess.run = fake_run

    def test_schedules_the_right_utc_time_and_command(self):
        # Thu 15.10.2026 16:00 Europe/Berlin = 14:00 UTC (still CEST, DST ends 25.10.2026)
        res = ur.schedule_once("stage-eta", "2026-10-15 16:00", now=NOW)
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.calls), 1)
        argv, cmd = self.calls[0]["argv"], self.calls[0]["input"]
        self.assertEqual(argv[:2], ["at", "-t"])
        self.assertEqual(argv[2], "202610151400")
        self.assertIn("usage_review.py --only stage-eta --now", cmd)
        self.assertEqual(self.calls[0]["env"]["TZ"], "UTC")
        self.assertEqual(res["at_utc"], datetime(2026, 10, 15, 14, 0, tzinfo=UTC).isoformat())
        self.assertEqual(self.alerts, [])

    def test_winter_time_is_cet(self):
        # 15.12.2026 16:00 Europe/Berlin (CET, UTC+1) = 15:00 UTC
        ur.schedule_once("stage-eta", "2026-12-15 16:00", now=NOW)
        self.assertEqual(self.calls[0]["argv"][2], "202612151500")

    def test_bad_time_is_rejected_without_calling_at(self):
        res = ur.schedule_once("stage-eta", "not a time", now=NOW)
        self.assertFalse(res["ok"])
        self.assertIn("error", res)
        self.assertEqual(self.calls, [])

    def test_at_failure_alerts(self):
        ur.subprocess.run = lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": "no at"})()
        res = ur.schedule_once("stage-eta", "2026-10-15 16:00", now=NOW)
        self.assertFalse(res["ok"])
        self.assertEqual(len(self.alerts), 1)

    def test_scheduling_never_writes_the_review_state(self):
        self.assertFalse(os.path.exists(ur.STATE))
        ur.schedule_once("stage-eta", "2026-10-15 16:00", now=NOW)
        self.assertFalse(os.path.exists(ur.STATE))   # no state file touched: not st["runs"], not next_run_at


class Main(Base):
    def setUp(self):
        super().setUp()
        self.calls = []
        ur.subprocess.run = lambda argv, **k: self.calls.append((argv, k)) or type(
            "R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()

    def test_at_without_only_errors(self):
        old_argv = sys.argv
        sys.argv = ["usage_review.py", "--at", "2026-10-15 16:00"]
        try:
            with self.assertRaises(SystemExit):
                ur.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(self.calls, [])   # argparse error: never got to scheduling

    def test_only_at_schedules_without_firing(self):
        old_argv = sys.argv
        sys.argv = ["usage_review.py", "--only", "stage-eta", "--at", "2026-10-15 16:00"]
        try:
            ur.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.fires), 0)        # scheduled, not fired
        self.assertFalse(os.path.exists(ur.STATE))  # the monthly state untouched

    def test_only_now_fires_without_touching_next_run_at(self):
        old_argv = sys.argv
        sys.argv = ["usage_review.py", "--only", "stage-eta", "--now"]
        try:
            ur.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(len(self.fires), 1)
        with open(ur.STATE) as fh:
            st = json.load(fh)
        self.assertEqual(st["runs"], [])
        self.assertEqual(len(st["one_off_runs"]), 1)
        self.assertEqual(st["next_run_at"], ur.FIRST_RUN.isoformat())   # default: never advanced


class ParseOnly(unittest.TestCase):
    def test_single_values_pass_through(self):
        self.assertEqual(ur.parse_only("usage"), "usage")
        self.assertEqual(ur.parse_only("stage-eta"), "stage-eta")

    def test_combo_is_canonicalized_regardless_of_typing_order(self):
        self.assertEqual(ur.parse_only("usage,stage-eta"), "usage,stage-eta")
        self.assertEqual(ur.parse_only("stage-eta,usage"), "usage,stage-eta")
        self.assertEqual(ur.parse_only(" stage-eta , usage "), "usage,stage-eta")

    def test_invalid_values_rejected(self):
        for bad in ("bogus", "usage,bogus", "", ","):
            with self.assertRaises(argparse.ArgumentTypeError):
                ur.parse_only(bad)


class SessionIdle(unittest.TestCase):
    """session_idle() stands in for the explicit stop-marker CLI D-224 has not built yet: the
    "last written" signal, adapted for a session usage_review.py fired directly (never gets a
    keepalive st["handled"] entry, so ka.run_active() itself cannot see it)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_transcript_path = ka.transcript_path

    def tearDown(self):
        ka.transcript_path = self.saved_transcript_path
        self.tmp.cleanup()

    def _transcript(self, age):
        path = os.path.join(self.tmp.name, "sid.jsonl")
        with open(path, "w") as fh:
            fh.write("{}\n")
        t = (NOW - age).timestamp()
        os.utime(path, (t, t))
        ka.transcript_path = lambda sid: path
        return path

    def test_quiet_past_the_idle_window_is_idle(self):
        self._transcript(timedelta(minutes=20))
        self.assertTrue(ur.session_idle("sid", NOW))

    def test_quiet_less_than_a_bigger_idle_window_is_not_idle(self):
        self._transcript(timedelta(minutes=20))
        self.assertFalse(ur.session_idle("sid", NOW, idle=timedelta(minutes=30)))

    def test_just_written_is_not_idle(self):
        self._transcript(timedelta(seconds=1))
        self.assertFalse(ur.session_idle("sid", NOW))

    def test_no_transcript_yet_is_not_idle(self):
        ka.transcript_path = lambda sid: None
        self.assertFalse(ur.session_idle("sid", NOW))


def this_project():
    """The test DB's project for this repo (usage_review's report docs live there)."""
    conn = store.connect()
    try:
        p = store.get_project(conn, ur.HERE)
        if p is None:
            store.add_project(conn, "AFC-test", None, ur.HERE)
            p = store.get_project(conn, ur.HERE)
        return p["name"]
    finally:
        conn.close()


def set_doc(name, text):
    conn = store.connect()
    try:
        actions.perform(conn, "doc.set", {"project": this_project(), "name": name, "text": text},
                        actor="cli", via="cli")
    finally:
        conn.close()


class UsageReviewFinished(unittest.TestCase):
    """usage_review_finished() needs BOTH signals: the report could still be mid-write while the
    session looks idle, or the session could be slow to start even once a stale report exists.
    The report is the review doc review:<date> in the DB (design §7.7), or a legacy file."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (ur.session_idle, ur.REVIEWS_DIR)
        ur.REVIEWS_DIR = self.tmp.name
        this_project()

    def tearDown(self):
        ur.session_idle, ur.REVIEWS_DIR = self.saved
        self.tmp.cleanup()

    def test_false_without_the_report_even_if_idle(self):
        ur.session_idle = lambda sid, now, idle=None: True
        self.assertFalse(ur.usage_review_finished("sid", "review:2026-01-01", NOW))

    def test_false_while_not_idle_even_with_the_report(self):
        ur.session_idle = lambda sid, now, idle=None: False
        set_doc("review:2026-01-02", "findings\n")
        self.assertFalse(ur.usage_review_finished("sid", "review:2026-01-02", NOW))

    def test_true_once_both_hold(self):
        ur.session_idle = lambda sid, now, idle=None: True
        set_doc("review:2026-01-03", "findings\n")
        self.assertTrue(ur.usage_review_finished("sid", "review:2026-01-03", NOW))

    def test_a_legacy_report_file_counts_too(self):
        ur.session_idle = lambda sid, now, idle=None: True
        with open(os.path.join(self.tmp.name, "2026-01-04.md"), "w") as fh:
            fh.write("findings")
        self.assertTrue(ur.usage_review_finished("sid", "review:2026-01-04", NOW))
        self.assertEqual(ur.report_doc(NOW), "review:2026-10-09")


class UsageReviewRecords(Base):
    """usage_reviews (§7.2): a record per launch (kind usage.review, keyed by the session), marked
    final by the hourly track_reviews() once the report doc exists and the session is idle, or
    no_report after REVIEW_REPORT_DEADLINE; a failed launch is final at once."""
    def setUp(self):
        super().setUp()
        self.saved_idle = ur.session_idle
        this_project()

    def tearDown(self):
        ur.session_idle = self.saved_idle
        super().tearDown()

    def rec(self, sid):
        return {r["review_id"]: r for r in telemetry.read_records("usage.review")}.get(sid)

    def test_launch_records_and_tracking(self):
        st = {"next_run_at": (NOW + timedelta(days=5)).isoformat(), "runs": []}
        when = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
        self.assertTrue(ur.run_once(st, when, "stage-eta"))
        sid = self.fires[-1][0]
        r = self.rec(sid)
        self.assertEqual((r["status"], r["final"], r["one_off"], r["reviews"], r["report_doc"]),
                         ("started", False, True, ["stage-eta"], "review:2026-07-01"))
        ur.session_idle = lambda s, now, idle=None: True
        self.assertEqual(ur.track_reviews(when + timedelta(hours=1)), [])      # no report yet
        set_doc("review:2026-07-01", "# Review\n")
        done = ur.track_reviews(when + timedelta(hours=2))
        self.assertEqual([(d["review_id"], d["status"]) for d in done], [(sid, "finished")])
        self.assertTrue(self.rec(sid)["final"])
        self.assertEqual(ur.track_reviews(when + timedelta(hours=3)), [])      # final: left alone

    def test_no_report_and_launch_failure(self):
        st = {"next_run_at": (NOW + timedelta(days=5)).isoformat(), "runs": []}
        when = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
        ur.run(st, when)
        sid = self.fires[-1][0]
        self.assertEqual(self.rec(sid)["reviews"], ["usage", "stage-eta"])
        self.assertFalse(self.rec(sid)["one_off"])
        ur.session_idle = lambda s, now, idle=None: False
        self.assertEqual(ur.track_reviews(when + timedelta(hours=23)), [])
        done = ur.track_reviews(when + ur.REVIEW_REPORT_DEADLINE)
        self.assertEqual([d["status"] for d in done if d["review_id"] == sid], ["no_report"])
        ka.fire = lambda *a, **k: (1, "", "boom")
        ur.run_once(st, when, "usage")
        failed = [r for r in telemetry.read_records("usage.review") if r.get("rc") == 1 and r["date"] == "2026-06-01"]
        self.assertEqual([(r["status"], r["final"]) for r in failed], [("launch_failed", True)])


class WaitForUsageReview(unittest.TestCase):
    """The bounded poll itself, with an injectable clock/sleep so it runs instantly in tests
    instead of really waiting hours."""
    def setUp(self):
        self.saved_finished = ur.usage_review_finished

    def tearDown(self):
        ur.usage_review_finished = self.saved_finished

    def _clock(self, start):
        state = {"t": start}

        def now_fn():
            return state["t"]

        def sleep_fn(seconds):
            state["t"] += timedelta(seconds=seconds)
        return now_fn, sleep_fn

    def test_returns_true_as_soon_as_finished_is_seen(self):
        seen = []
        ur.usage_review_finished = lambda sid, report, now: seen.append(now) or len(seen) >= 3
        now_fn, sleep_fn = self._clock(NOW)
        ok = ur.wait_for_usage_review("sid", "report.md", NOW, timeout=timedelta(hours=1),
                                      poll=timedelta(minutes=5), now_fn=now_fn, sleep_fn=sleep_fn)
        self.assertTrue(ok)
        self.assertEqual(len(seen), 3)   # two misses, then a hit -- no more polling after that

    def test_gives_up_after_the_timeout_without_hanging(self):
        ur.usage_review_finished = lambda sid, report, now: False
        now_fn, sleep_fn = self._clock(NOW)
        ok = ur.wait_for_usage_review("sid", "report.md", NOW, timeout=timedelta(minutes=20),
                                      poll=timedelta(minutes=5), now_fn=now_fn, sleep_fn=sleep_fn)
        self.assertFalse(ok)

    def test_already_finished_at_the_first_check_never_sleeps(self):
        ur.usage_review_finished = lambda sid, report, now: True
        sleeps = []
        ok = ur.wait_for_usage_review("sid", "report.md", NOW, now_fn=lambda: NOW,
                                      sleep_fn=lambda s: sleeps.append(s))
        self.assertTrue(ok)
        self.assertEqual(sleeps, [])


class ChainUsageEta(Base):
    """run_usage_then_eta() (--only usage,stage-eta, D-228): fires the usage review, waits for it
    (wait_fn, injected so no real waiting happens here), then fires a SEPARATE fresh session for
    the ETA review pointed at the usage review's result -- in that order, never in one session --
    or alerts and skips the ETA leg if the wait fails. Never touches the monthly schedule."""
    def test_chain_fires_usage_then_a_separate_eta_session_pointed_at_its_result(self):
        st = {"next_run_at": (NOW + timedelta(days=5)).isoformat(), "runs": []}
        waited = []

        def fake_wait(sid, report, start):
            waited.append((sid, report, start))
            return True
        ok = ur.run_usage_then_eta(st, NOW, wait_fn=fake_wait)
        self.assertTrue(ok)
        self.assertEqual(len(self.fires), 2)
        (sid1, msg1, plan1, new1, k1), (sid2, msg2, plan2, new2, k2) = self.fires
        self.assertEqual(plan1, "resume")
        self.assertEqual(plan2, "resume")
        self.assertTrue(new1)
        self.assertTrue(new2)
        # the usage leg: the full monthly brief
        self.assertIn("periodic usage-model review", msg1)
        self.assertNotIn("Schedule the eta review at end of this week", msg1)
        # the ETA leg: a SEPARATE session (different sid), never the usage brief, pointed at the
        # usage review's result instead
        self.assertNotEqual(sid1, sid2)
        self.assertNotIn("periodic usage-model review", msg2)
        self.assertIn("Schedule the eta review at end of this week", msg2)
        d = NOW.astimezone(ka.BERLIN).date().isoformat()
        report = f"review:{d}"
        self.assertIn(f"the review doc {report} in the AFClaude DB", msg2)
        self.assertIn("telemetry.py user-model get", msg2)
        # waited on the usage session specifically, before firing the ETA leg
        self.assertEqual(waited, [(sid1, report, NOW)])
        # order: the usage one-off entry logged before the stage-eta one
        self.assertEqual(len(st["one_off_runs"]), 2)
        self.assertEqual(st["one_off_runs"][0]["only"], "usage")
        self.assertEqual(st["one_off_runs"][0]["session"], sid1)
        self.assertEqual(st["one_off_runs"][1]["only"], "stage-eta")
        self.assertEqual(st["one_off_runs"][1]["session"], sid2)
        # the monthly schedule is untouched
        self.assertEqual(st["runs"], [])
        self.assertEqual(st["next_run_at"], (NOW + timedelta(days=5)).isoformat())
        self.assertEqual(self.alerts, [])

    def test_usage_review_timeout_alerts_and_never_fires_the_eta_leg(self):
        st = {"next_run_at": (NOW + timedelta(days=5)).isoformat(), "runs": []}
        ok = ur.run_usage_then_eta(st, NOW, wait_fn=lambda sid, report, start: False)
        self.assertFalse(ok)
        self.assertEqual(len(self.fires), 1)   # only the usage leg ever fired
        self.assertEqual(len(st["one_off_runs"]), 1)
        self.assertEqual(st["one_off_runs"][0]["only"], "usage")
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("did not finish in time", self.alerts[0][0])
        self.assertEqual(st["runs"], [])
        self.assertEqual(st["next_run_at"], (NOW + timedelta(days=5)).isoformat())

    def test_usage_launch_failure_never_waits_or_fires_the_eta_leg(self):
        ka.fire = lambda *a, **k: (1, "", "boom")
        st = {"next_run_at": "2026-11-01T00:00:00+00:00", "runs": []}
        waited = []
        ok = ur.run_usage_then_eta(st, NOW, wait_fn=lambda *a, **k: waited.append(1) or True)
        self.assertFalse(ok)
        self.assertEqual(waited, [])    # never even started waiting
        self.assertEqual(len(self.fires), 0)
        self.assertEqual(len(st["one_off_runs"]), 1)
        self.assertEqual(len(self.alerts), 1)   # the usage launch-failed alert, from run_once
        self.assertEqual(st["next_run_at"], "2026-11-01T00:00:00+00:00")

    def test_default_wait_fn_is_looked_up_at_call_time(self):
        # a module-level monkeypatch of wait_for_usage_review (not passing wait_fn explicitly)
        # must still take effect -- it must not be bound as a stale default at def time.
        st = {"next_run_at": (NOW + timedelta(days=5)).isoformat(), "runs": []}
        saved = ur.wait_for_usage_review
        ur.wait_for_usage_review = lambda sid, report, start: True
        try:
            ok = ur.run_usage_then_eta(st, NOW)
        finally:
            ur.wait_for_usage_review = saved
        self.assertTrue(ok)
        self.assertEqual(len(self.fires), 2)


class ChainCli(Base):
    """main() wiring for the chained --only value: dispatched to run_usage_then_eta (never to two
    separate run_once() calls), and schedulable via --at like any other --only value."""
    def setUp(self):
        super().setUp()
        self.calls = []
        ur.subprocess.run = lambda argv, **k: self.calls.append((argv, k)) or type(
            "R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
        self.saved_chain = ur.run_usage_then_eta

    def tearDown(self):
        ur.run_usage_then_eta = self.saved_chain
        super().tearDown()

    def _run(self, *argv):
        old_argv = sys.argv
        sys.argv = ["usage_review.py", *argv]
        try:
            ur.main()
        finally:
            sys.argv = old_argv

    def test_only_usage_stage_eta_now_dispatches_to_the_chain(self):
        called = []
        ur.run_usage_then_eta = lambda st, now: called.append((st, now)) or True
        self._run("--only", "usage,stage-eta", "--now")
        self.assertEqual(len(called), 1)
        self.assertEqual(len(self.fires), 0)   # run_usage_then_eta is the spy above, never fired

    def test_only_reverse_order_also_dispatches_to_the_chain(self):
        called = []
        ur.run_usage_then_eta = lambda st, now: called.append((st, now)) or True
        self._run("--only", "stage-eta,usage", "--now")
        self.assertEqual(len(called), 1)

    def test_only_usage_stage_eta_at_schedules_without_firing(self):
        self._run("--only", "usage,stage-eta", "--at", "2026-10-15 16:00")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("usage_review.py --only usage,stage-eta --now", self.calls[0][1]["input"])
        self.assertEqual(len(self.fires), 0)
        self.assertFalse(os.path.exists(ur.STATE))   # the monthly state untouched


if __name__ == "__main__":
    unittest.main()
