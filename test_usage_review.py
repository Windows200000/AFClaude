#!/usr/bin/env python3
"""Offline tests for usage_review.py's one-off review (--only / --at, D-220/D-221): run_once()
fires immediately and logs under st["one_off_runs"] without touching the monthly schedule;
schedule_once() spools an at-shim job for the right UTC time without running or scheduling
anything itself; main() wires --only/--at together. Also the D-228 chain (--only usage,stage-eta):
parse_only() canonicalizes the combination regardless of typing order; session_idle() /
usage_review_finished() detect the usage review session finishing; start_chain() fires the usage
review and keeps the pending step in the state (no long-lived waiter: a container restart can't
lose it), chain_pass() then fires a SEPARATE fresh session for the ETA review pointed at its
result. The quota gate (no review starts without REVIEW_NEEDS free: postponed to the reset) and
the dead-review detection (review_death: a usage limit is retried once after the reset, any
other death is an error at once), with fixture transcripts. The monthly review (D-254): due ->
a nightly task (nightly_tasks.py, its own tests: test_nightly_tasks.py) with no session, its
record finished once the task is done, a missed task an error that stays due."""
import argparse
import json
import os
import sys
import tempfile
import time
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
import runner_state  # noqa: E402
import nightly_tasks  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)


def usage(weekly=20.0, session=10.0, now=None):
    """A usage reading as keepalive.fresh_usage returns it (weekly resets Thu 17:00 UTC, the
    session window 2 h from `now`)."""
    now = now or NOW
    return {"fetched_at": now,
            "weekly": {"percent": weekly, "resets_at": datetime(2026, 10, 15, 17, 0, tzinfo=UTC)},
            "session": {"percent": session, "resets_at": now + timedelta(hours=2)}}


class Base(unittest.TestCase):
    def setUp(self):
        testenv.unpause()            # a failure in an earlier test paused automation (D-234/D-235)
        self.addCleanup(testenv.unpause)
        finalize_records()           # an earlier test's started reviews are not this test's
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {"fire": ka.fire, "alert": ka.alert, "log": ka.log, "progress_note": ka.progress_note,
                      "state": ur.STATE, "subprocess_run": ur.subprocess.run, "fresh_usage": ka.fresh_usage,
                      "projects": ka.PROJECTS_DIR, "tmux_alive": ka.tmux_alive}
        self.fires = []
        self.alerts = []
        self.at_calls = []
        self.usage = usage()
        ka.fire = lambda sid, cwd, msg, plan, new=False, **k: self.fires.append(
            (sid, msg, plan, new, k)) or (0, "started", "")
        ka.alert = lambda subject, body="": self.alerts.append((subject, body))
        ka.log = lambda msg: None
        ka.progress_note = lambda line: None
        ka.fresh_usage = lambda now, force=False, display=False: self.usage
        ka.PROJECTS_DIR = os.path.join(self.tmp.name, "projects")    # no transcript unless a test writes one
        ka.tmux_alive = lambda sid: True
        ur.STATE = os.path.join(self.tmp.name, "usage_review_state.json")
        ur.subprocess.run = lambda argv, input=None, **k: self.at_calls.append((argv, input)) or type(  # noqa: A002
            "R", (), {"returncode": 0, "stdout": "", "stderr": "job x at y"})()

    def tearDown(self):
        ka.fire, ka.alert, ka.log, ka.progress_note = (self.saved["fire"], self.saved["alert"],
                                                        self.saved["log"], self.saved["progress_note"])
        ka.fresh_usage, ka.PROJECTS_DIR, ka.tmux_alive = (self.saved["fresh_usage"], self.saved["projects"],
                                                          self.saved["tmux_alive"])
        ur.STATE = self.saved["state"]
        ur.subprocess.run = self.saved["subprocess_run"]
        self.tmp.cleanup()

    def paused(self):
        return afclaude_config.automation_pause()["paused"]


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


def finalize_records():
    """Mark every started usage_reviews record of the shared test DB final."""
    for r in telemetry.read_records("usage.review"):
        if not r.get("final"):
            r.update(final=True, status="test-closed")
            telemetry.write_records("usage.review", [r], actor="cli")


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


def clear_nightly_tasks():
    """No nightly tasks in the shared test DB (a test's names are the next one's too; the runners never delete)."""
    conn = store.connect()
    try:
        with store.transaction(conn):
            conn.execute("DELETE FROM runner_state WHERE component=?", (nightly_tasks.COMPONENT,))
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
        ur.run_once(st, when, "usage")
        sid = self.fires[-1][0]
        self.assertEqual(self.rec(sid)["reviews"], ["usage"])
        self.assertTrue(self.rec(sid)["one_off"])
        ur.session_idle = lambda s, now, idle=None: False
        self.assertEqual(ur.track_reviews(when + timedelta(hours=23)), [])
        done = ur.track_reviews(when + ur.REVIEW_REPORT_DEADLINE)
        self.assertEqual([d["status"] for d in done if d["review_id"] == sid], ["no_report"])
        ka.fire = lambda *a, **k: (1, "", "boom")
        ur.run_once(st, when, "usage")
        failed = [r for r in telemetry.read_records("usage.review") if r.get("rc") == 1 and r["date"] == "2026-06-01"]
        self.assertEqual([(r["status"], r["final"]) for r in failed], [("launch_failed", True)])


LIMIT_TEXT = "You've hit your weekly limit · resets 5pm (UTC)"
T0 = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)        # the 15.10. case: 16:00 Berlin, reset 17:00 UTC


def entry(kind, at, **kw):
    """A transcript line: user / assistant (end_turn) / limit (the synthetic notice) / turn_end
    (turn_duration with `pending` background agents) / notify (a task notification)."""
    ts = at.isoformat().replace("+00:00", "Z")
    if kind == "user":
        return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": kw.get("text", "go")}}
    if kind == "assistant":
        return {"type": "assistant", "timestamp": ts,
                "message": {"model": "claude-opus-5-5", "stop_reason": kw.get("stop", "end_turn"),
                            "content": [{"type": "text", "text": kw.get("text", "ok")}]}}
    if kind == "limit":
        return {"type": "assistant", "timestamp": ts, "error": "rate_limit", "isApiErrorMessage": True,
                "message": {"model": "<synthetic>", "stop_reason": "stop_sequence",
                            "content": [{"type": "text", "text": LIMIT_TEXT}]}}
    if kind == "turn_end":
        e = {"type": "system", "subtype": "turn_duration", "timestamp": ts, "durationMs": 1000}
        if kw.get("pending"):
            e["pendingBackgroundAgentCount"] = kw["pending"]
        return e
    if kind == "notify":
        return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": (
            "<task-notification>\n<task-id>a1</task-id>\n<status>failed</status>\n<summary>Agent \"Usage review\" "
            f"failed: Agent terminated early due to an API error: {kw['text']}</summary>\n</task-notification>")}}
    raise ValueError(kind)


class Transcripts(Base):
    """Fixture transcripts under a temp PROJECTS_DIR; the report docs a set (the shared test DB
    keeps every doc an earlier test wrote)."""
    def setUp(self):
        super().setUp()
        self.reports = set()
        self.saved_ready = ur.report_ready
        ur.report_ready = lambda report: report in self.reports
        ur.STATE = runner_state.live_path("usage_review")    # the DB, as live (merge saves, no key deleted)
        st = ur.load()
        st.update(chain=None, retry_attempt=None)
        self.assertTrue(ur.save(st))

    def tearDown(self):
        ur.report_ready = self.saved_ready
        super().tearDown()

    def transcript(self, sid, entries, age=timedelta(minutes=20), now=None):
        """The session's transcript under the test PROJECTS_DIR, last written `age` before `now`."""
        d = os.path.join(ka.PROJECTS_DIR, "proj")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{sid}.jsonl")
        with open(path, "w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        t = ((now or self.now) - age).timestamp()
        os.utime(path, (t, t))
        return path

    def rec(self, sid):
        return ur.review_record(sid)

    def at_jobs(self):
        """(UTC time, command) of every at job spooled."""
        return [(argv[2], cmd) for argv, cmd in self.at_calls]


class QuotaGate(Transcripts):
    """No review session starts without REVIEW_NEEDS free (the 15.10. one started at 99 % weekly):
    it is postponed to the reset that frees it, recorded and logged; a postponement is no error.
    Enough quota: the start is unchanged."""
    now = T0

    def test_one_off_at_99_percent_weekly_is_postponed_to_the_weekly_reset(self):
        self.usage = usage(weekly=99.0, now=T0)
        st = ur.load()
        self.assertFalse(ur.start_one_off(st, T0, "usage"))
        self.assertEqual(self.fires, [])                      # no session started
        self.assertEqual(self.at_jobs(), [("202610151702", self.at_jobs()[0][1])])   # 17:00 UTC + 90 s grace
        self.assertIn("usage_review.py --only usage --now", self.at_jobs()[0][1])
        self.assertNotIn("--attempt", self.at_jobs()[0][1])
        p = st["postponed"][-1]
        self.assertEqual((p["review"], p["until"]), ("one-off usage", "2026-10-15T17:02:00+00:00"))
        self.assertIn("weekly 99% used", p["why"])
        self.assertEqual(self.alerts, [])                      # not an error
        self.assertFalse(self.paused())

    def test_chain_without_quota_is_postponed_as_a_whole(self):
        self.usage = usage(weekly=99.0, now=T0)
        st = ur.load()
        self.assertFalse(ur.start_chain(st, T0))
        self.assertEqual(self.fires, [])
        self.assertIn("usage_review.py --only usage,stage-eta --now", self.at_jobs()[0][1])
        self.assertIsNone(ur.load().get("chain"))

    def test_session_short_postpones_to_the_session_reset(self):
        self.usage = usage(weekly=20.0, session=70.0, now=T0)
        st = ur.load()
        self.assertFalse(ur.start_one_off(st, T0, "stage-eta"))
        self.assertEqual(self.fires, [])
        self.assertEqual(self.at_jobs()[0][0], "202610151602")                # the session reset + grace
        self.assertEqual(ur.load()["postponed"][-1]["review"], "one-off stage-eta")
        self.assertEqual(self.alerts, [])

    def test_enough_quota_starts_as_before(self):
        self.usage = usage(weekly=50.0, session=40.0, now=T0)
        st = ur.load()
        before = (st["next_run_at"], len(st.get("postponed") or []))
        self.assertTrue(ur.start_one_off(st, T0, "stage-eta"))
        self.assertEqual(len(self.fires), 1)
        self.assertEqual(self.at_calls, [])
        st = ur.load()
        self.assertEqual((st["next_run_at"], len(st.get("postponed") or [])), before)
        self.assertEqual(st["one_off_runs"][-1]["session"], self.fires[0][0])

    def test_no_reset_to_postpone_to_is_an_error(self):
        self.usage = usage(weekly=99.0, now=T0)
        self.usage["weekly"]["resets_at"] = None
        with self.assertRaises(ur.failstop.Failed) as cm:
            ur.start_one_off(ur.load(), T0, "usage")
        self.assertIn("can't be scheduled", cm.exception.why)
        self.assertEqual(self.fires, [])

    def test_a_failed_usage_reading_starts_nothing(self):
        self.usage = None
        with self.assertRaises(ur.failstop.Failed) as cm:
            ur.start_chain(ur.load(), T0)
        self.assertIn("usage reading failed", cm.exception.why)
        self.assertEqual(self.fires, [])

    def test_main_postpones_without_an_error(self):
        now = datetime.now(UTC)
        self.usage = usage(weekly=99.5, now=now)
        self.usage["weekly"]["resets_at"] = now + timedelta(hours=3)
        old_argv = sys.argv
        sys.argv = ["usage_review.py", "--only", "usage", "--now"]
        try:
            ur.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(self.fires, [])
        self.assertEqual(len(self.at_calls), 1)
        self.assertFalse(self.paused())
        self.assertEqual(self.alerts, [])


class Chain(Transcripts):
    """The D-228 chain without a long-lived waiter: start_chain() fires the usage leg and saves the
    pending step; chain_pass() (its own at job / the hourly run, here called directly, also from a
    freshly loaded state as after a container restart) checks it every CHAIN_POLL."""
    now = T0 + timedelta(minutes=30)

    def start(self, attempt=1):
        st = ur.load()
        self.next_run_at = st["next_run_at"]
        self.assertTrue(ur.start_chain(st, T0, attempt))
        self.at_calls.clear()
        return st["chain"]["session"]

    def test_start_fires_usage_and_keeps_the_step_in_the_state(self):
        sid = self.start()
        self.assertEqual(len(self.fires), 1)
        self.assertIn("periodic usage-model review", self.fires[0][1])
        st = ur.load()                                       # a new process: the step survived
        self.assertEqual((st["chain"]["step"], st["chain"]["session"]), ("usage", sid))
        r = self.rec(sid)
        self.assertEqual((r["chain"], r["attempt"]), ("usage", 1))

    def test_start_spools_its_first_step(self):
        ur.start_chain(ur.load(), T0)
        self.assertEqual(self.at_jobs()[-1][0], "202610151405")
        self.assertIn("usage_review.py --chain-step", self.at_jobs()[-1][1])

    def test_a_second_chain_while_one_is_pending_is_an_error(self):
        self.start()
        with self.assertRaises(ur.failstop.Failed):
            ur.start_chain(ur.load(), T0)
        self.assertEqual(len(self.fires), 1)

    def test_normal_path_fires_a_separate_eta_session_pointed_at_the_report(self):
        sid = self.start()
        st = ur.load()
        self.assertEqual(ur.chain_pass(st, T0 + timedelta(minutes=5)), "waiting")   # no transcript yet
        self.assertIn("--chain-step", self.at_jobs()[-1][1])
        self.transcript(sid, [entry("user", T0), entry("assistant", T0 + timedelta(minutes=10)),
                              entry("turn_end", T0 + timedelta(minutes=10))])
        self.reports.add("review:2026-10-15")
        st = ur.load()
        self.assertEqual(ur.chain_pass(st, self.now), "eta_started")
        self.assertEqual(len(self.fires), 2)
        sid2, msg2 = self.fires[1][0], self.fires[1][1]
        self.assertNotEqual(sid, sid2)
        self.assertNotIn("periodic usage-model review", msg2)
        self.assertIn("Schedule the eta review at end of this week", msg2)
        self.assertIn("the review doc review:2026-10-15 in the AFClaude DB", msg2)
        self.assertEqual(self.rec(sid)["status"], "finished")
        r2 = self.rec(sid2)
        self.assertEqual((r2["chain"], r2["after_report"], r2["attempt"]), ("stage-eta", "review:2026-10-15", 1))
        self.assertIsNone(ur.load()["chain"])
        self.assertIsNone(ur.chain_pass(ur.load(), self.now))   # a second pass fires nothing
        self.assertEqual(len(self.fires), 2)
        self.assertEqual(self.alerts, [])
        st = ur.load()
        self.assertEqual([r["only"] for r in st["one_off_runs"][-2:]], ["usage", "stage-eta"])
        self.assertEqual(st["next_run_at"], self.next_run_at)   # the monthly schedule untouched

    def test_limit_death_is_seen_early_and_retried_once_after_the_reset(self):
        """15.10.: the worker died at the weekly limit 3 min in, the session sat idle on the
        notice: seen at the next check, the chain retried at 17:02 UTC, an alert, no pause."""
        sid = self.start()
        t = T0 + timedelta(minutes=3)
        self.transcript(sid, [entry("user", T0), entry("assistant", T0 + timedelta(seconds=30)),
                              entry("turn_end", T0 + timedelta(seconds=31), pending=1),
                              entry("notify", t, text=LIMIT_TEXT), entry("limit", t)],
                        age=timedelta(minutes=2), now=t + timedelta(minutes=2))
        self.assertEqual(ur.chain_pass(ur.load(), t + timedelta(minutes=4)), "dead")
        self.assertEqual(self.rec(sid)["status"], "limit")
        self.assertEqual(len(self.fires), 1)                 # no ETA leg
        self.assertIsNone(ur.load()["chain"])
        self.assertEqual(self.at_jobs(), [("202610151702", self.at_jobs()[0][1])])
        self.assertIn("usage_review.py --only usage,stage-eta --now --attempt 2", self.at_jobs()[0][1])
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("usage limit", self.alerts[0][0])
        self.assertFalse(self.paused())

    def test_a_worker_failing_at_the_limit_counts_as_a_limit_death(self):
        sid = self.start()
        t = T0 + timedelta(minutes=3)
        self.transcript(sid, [entry("user", T0), entry("assistant", T0 + timedelta(seconds=30)),
                              entry("notify", t, text=LIMIT_TEXT),
                              entry("assistant", t + timedelta(seconds=5), text="the worker failed")],
                        age=timedelta(minutes=1), now=t + timedelta(minutes=1))
        self.assertEqual(ur.chain_pass(ur.load(), t + timedelta(minutes=1)), "dead")
        self.assertEqual(self.rec(sid)["status"], "limit")
        self.assertIn("--attempt 2", self.at_jobs()[0][1])
        self.assertFalse(self.paused())

    def test_the_retry_hitting_the_limit_again_is_an_error(self):
        sid = self.start(attempt=2)
        t = T0 + timedelta(minutes=3)
        self.transcript(sid, [entry("user", T0), entry("limit", t)], age=timedelta(minutes=1),
                        now=t + timedelta(minutes=1))
        self.assertEqual(ur.chain_pass(ur.load(), t + timedelta(minutes=1)), "dead")
        self.assertEqual(self.at_calls, [])                  # no third attempt
        self.assertTrue(self.paused())
        self.assertIn("usage limit again", self.alerts[-1][0])

    def test_idle_without_report_and_no_agent_is_an_error_at_once(self):
        sid = self.start()
        t = T0 + timedelta(minutes=20)
        self.transcript(sid, [entry("user", T0), entry("assistant", t), entry("turn_end", t)],
                        age=timedelta(minutes=1), now=t + timedelta(minutes=1))
        self.assertEqual(ur.chain_pass(ur.load(), t + timedelta(minutes=1)), "dead")   # not after 6 h
        self.assertEqual(self.rec(sid)["status"], "died")
        self.assertTrue(self.paused())
        self.assertIn("died", self.alerts[-1][0])
        self.assertEqual(len(self.fires), 1)
        self.assertEqual(self.at_calls, [])
        self.assertIsNone(ur.load()["chain"])

    def test_a_running_worker_agent_keeps_waiting(self):
        sid = self.start()
        t = T0 + timedelta(minutes=20)
        self.transcript(sid, [entry("user", T0), entry("assistant", t), entry("turn_end", t, pending=1)],
                        age=timedelta(minutes=1), now=t + timedelta(minutes=1))
        self.assertEqual(ur.chain_pass(ur.load(), t + timedelta(minutes=1)), "waiting")
        self.assertFalse(self.paused())
        self.assertIn("--chain-step", self.at_jobs()[-1][1])

    def test_a_gone_tmux_session_mid_turn_is_dead(self):
        sid = self.start()
        t = T0 + timedelta(minutes=20)
        self.transcript(sid, [entry("user", T0), entry("assistant", t, stop="tool_use")],
                        age=timedelta(minutes=10), now=t + timedelta(minutes=10))
        self.assertEqual(ur.chain_pass(ur.load(), t + timedelta(minutes=10)), "waiting")   # tmux alive
        ka.tmux_alive = lambda s: False
        self.assertEqual(ur.chain_pass(ur.load(), t + timedelta(minutes=10)), "dead")
        self.assertIn("tmux", self.rec(sid)["why"])
        self.assertTrue(self.paused())

    def test_still_running_after_the_timeout_is_an_error(self):
        sid = self.start()
        t = T0 + ur.USAGE_DONE_TIMEOUT
        self.transcript(sid, [entry("user", T0), entry("assistant", t, stop="tool_use")],
                        age=timedelta(seconds=10), now=t)
        self.assertEqual(ur.chain_pass(ur.load(), t), "timed_out")
        self.assertEqual(self.rec(sid)["status"], "timed_out")
        self.assertIn("did not finish in time", self.alerts[-1][0])
        self.assertEqual(len(self.fires), 1)

    def test_eta_leg_without_quota_is_postponed(self):
        sid = self.start()
        self.transcript(sid, [entry("user", T0), entry("assistant", T0 + timedelta(minutes=10))])
        self.reports.add("review:2026-10-15")
        self.usage = usage(weekly=95.0, now=self.now)
        self.assertEqual(ur.chain_pass(ur.load(), self.now), "postponed")
        st = ur.load()
        self.assertEqual((st["chain"]["step"], st["chain"]["not_before"]), ("stage-eta", "2026-10-15T17:02:00+00:00"))
        self.assertEqual(self.at_jobs()[-1][0], "202610151702")
        self.assertEqual(len(self.fires), 1)
        self.assertEqual(ur.chain_pass(ur.load(), self.now + timedelta(minutes=30)), "waiting")   # not before
        self.usage = usage(weekly=0.0, now=T0 + timedelta(hours=3))
        self.assertEqual(ur.chain_pass(ur.load(), T0 + timedelta(hours=3, minutes=3)), "eta_started")
        self.assertEqual(len(self.fires), 2)

    def test_eta_leg_limit_retry_points_at_the_same_report(self):
        sid = self.start()
        self.transcript(sid, [entry("user", T0), entry("assistant", T0 + timedelta(minutes=10))])
        self.reports.add("review:2026-10-15")
        ur.chain_pass(ur.load(), self.now)
        eta = self.fires[-1][0]
        self.reports.clear()                               # the ETA leg has not written its part yet
        t = self.now + timedelta(minutes=5)
        self.transcript(eta, [entry("user", self.now), entry("limit", t)], age=timedelta(minutes=1),
                        now=t + timedelta(minutes=1))
        ur.track_reviews(t + timedelta(minutes=1))
        self.assertEqual(self.rec(eta)["status"], "limit")
        st = ur.load()
        self.assertEqual((st["chain"]["step"], st["chain"]["report"], st["chain"]["attempt"]),
                         ("stage-eta", "review:2026-10-15", 2))
        self.assertIn("--chain-step", self.at_jobs()[-1][1])
        self.assertFalse(self.paused())

    def test_paused_automation_holds_the_chain(self):
        sid = self.start()
        conn = store.connect()
        try:
            actions.perform(conn, "automation.set", {"paused": True, "reason": "a test"}, actor="cli", via="cli")
        finally:
            conn.close()
        self.assertEqual(ur.chain_pass(ur.load(), self.now), "paused")
        self.assertEqual(ur.load()["chain"]["session"], sid)
        self.assertEqual(self.at_calls, [])

    def test_a_pending_chain_step_job_is_not_spooled_twice(self):
        job = {"id": "chainstep01", "at": T0.isoformat(), "cmd": "python3 usage_review.py --chain-step\n", "cwd": "/"}
        self.assertTrue(runner_state.job_add(job))
        try:
            self.assertIsNone(ur.spool_chain_step(T0))
            self.assertEqual(self.at_calls, [])
        finally:
            runner_state.job_claim("chainstep01")

    def test_cli_chain_step_advances_a_saved_chain(self):
        """After a restart, the at job (or the hourly run) is a new process: it reads the pending step
        from the DB-backed state and goes on."""
        sid = self.start()
        self.transcript(sid, [entry("user", T0), entry("assistant", T0 + timedelta(minutes=10))],
                        now=datetime.now(UTC))
        self.reports.add("review:2026-10-15")
        old_argv = sys.argv
        sys.argv = ["usage_review.py", "--chain-step"]
        try:
            ur.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(len(self.fires), 2)
        self.assertIsNone(ur.load()["chain"])


class TrackDeaths(Transcripts):
    """The hourly check finds a dead solo or monthly review too (no chain waits for them)."""
    now = T0 + timedelta(hours=1)

    def test_solo_one_off_limit_death_is_retried_once(self):
        st = ur.load()
        ur.run_once(st, T0, "usage")
        sid = self.fires[-1][0]
        self.transcript(sid, [entry("user", T0), entry("limit", T0 + timedelta(minutes=3))])
        done = ur.track_reviews(self.now)
        self.assertEqual([(d["review_id"], d["status"]) for d in done], [(sid, "limit")])
        self.assertIn("usage_review.py --only usage --now --attempt 2", self.at_jobs()[0][1])
        self.assertFalse(self.paused())

    def test_a_dead_solo_review_is_an_error(self):
        st = ur.load()
        ur.run_once(st, T0, "stage-eta")
        sid = self.fires[-1][0]
        t = T0 + timedelta(minutes=5)
        self.transcript(sid, [entry("user", T0), {"type": "assistant", "timestamp": t.isoformat(),
                                                   "isApiErrorMessage": True,
                                                   "message": {"model": "<synthetic>", "content": "API Error: 500"}}])
        ur.track_reviews(self.now)
        self.assertEqual(self.rec(sid)["status"], "died")
        self.assertTrue(self.paused())
        self.assertEqual(self.at_calls, [])


# ---------------------------------------------------------------- the monthly review (D-254)

class Monthly(Base):
    """D-254: the due monthly review starts no session; it is a nightly task (nightly_tasks.py)
    named by st["monthly"], its record pending until the task is done; a missed task is an error."""
    def setUp(self):
        super().setUp()
        this_project()
        clear_nightly_tasks()
        self.addCleanup(clear_nightly_tasks)
        ur.STATE = runner_state.live_path("usage_review")    # the DB, as live
        st = ur.load()
        st.update(monthly=None, chain=None, next_run_at=(NOW - timedelta(minutes=1)).isoformat())
        self.assertTrue(ur.save(st))
        self.t0 = datetime.now(UTC)      # wall clock: a report doc's updated_at is the real time
        self.saved_ka_state = ka.STATE_FILE
        ka.STATE_FILE = os.path.join(self.tmp.name, "keepalive_state.json")   # the night starts' handled entries

    def tearDown(self):
        ka.STATE_FILE = self.saved_ka_state
        st = ur.load()
        st["monthly"] = None
        ur.save(st)
        super().tearDown()

    def cron(self, *argv):
        old_argv = sys.argv
        sys.argv = ["usage_review.py", *argv]
        try:
            return ur.main()
        finally:
            sys.argv = old_argv

    def carried(self, p, at, result="continued-in-place"):
        """A night start at `at` that took the task along (keepalive._fire_start's handled entry)."""
        st = ka.load_state()
        st["handled"][f"window-start-{at.isoformat()}"] = {
            "at": at.isoformat(), "result": result, "plan": "send-keys", "nightly_tasks": [p["task"]]}
        ka.save_state(st)

    def test_due_is_a_nightly_task_with_no_session(self):
        self.cron()
        self.assertEqual((self.fires, self.at_calls, self.alerts), ([], [], []))
        st = ur.load()
        p = st["monthly"]
        self.assertEqual(p["report"], ur.report_doc(datetime.now(UTC)))
        self.assertTrue(p["task"].startswith("monthly-usage-review-"))
        self.assertGreater(datetime.fromisoformat(st["next_run_at"]), datetime.now(UTC) + timedelta(days=27))
        task = nightly_tasks.get(p["task"])
        self.assertEqual((task["status"], task["prompts"], task["report"], task["fields"], task["by"]),
                         ("pending", ["usage_review", "stage_eta_review"], p["report"],
                          {"date": p["date"], "since": p["since"]}, ur.ACTOR))
        self.assertEqual(nightly_tasks.start_insert(datetime.now(UTC))[1], [p["task"]])
        rec = ur.review_record(p["review_id"])
        self.assertEqual((rec["status"], rec["final"], rec["one_off"], rec["reviews"], rec["session"]),
                         ("pending", False, False, ["usage", "stage-eta"], None))
        self.cron()                                         # the next hourly run: still pending, nothing new
        self.assertEqual(ur.load()["monthly"], p)
        self.assertEqual(len([t for t in nightly_tasks.load()["tasks"].values() if t["status"] == "pending"]), 1)
        self.assertEqual(self.fires, [])

    def test_now_marks_it_due_and_the_brief_is_the_filled_in_prompts(self):
        st = ur.load()
        st["next_run_at"] = (datetime.now(UTC) + timedelta(days=5)).isoformat()
        ur.save(st)
        self.cron("--now")
        p = ur.load()["monthly"]
        brief = nightly_tasks.brief(p["task"])
        self.assertIn(f"[AFClaude nightly task {p['task']}: the monthly usage review {p['date']}]", brief)
        self.assertIn(f"[AFClaude usage review {p['date']}]", brief)
        self.assertIn(f"since {p['since']}", brief)
        self.assertIn(f"[AFClaude stage-ETA review {p['date']}]", brief)
        self.assertNotIn("{date}", brief)
        self.assertNotIn("{since}", brief)
        self.assertEqual(self.fires, [])

    def test_done_once_the_report_exists(self):
        p = ur.mark_due(ur.load(), self.t0)
        self.carried(p, self.t0 + timedelta(hours=11))
        ur.track_nightly(ur.load(), self.t0 + timedelta(hours=12))     # no report yet
        self.assertEqual(ur.load()["monthly"], p)
        time.sleep(0.01)
        set_doc(p["report"], "# Review\n")
        [(task, event, _)] = ur.track_nightly(ur.load(), self.t0 + timedelta(hours=13))
        self.assertEqual((task["name"], event), (p["task"], "done"))
        self.assertIsNone(ur.load()["monthly"])
        rec = ur.review_record(p["review_id"])
        self.assertEqual((rec["status"], rec["final"], rec["runs"]), ("finished", True, 1))
        self.assertFalse(self.paused())

    def test_a_cancelled_task_ends_its_record(self):
        p = ur.mark_due(ur.load(), self.t0)
        nightly_tasks.cancel(p["task"], "the owner skipped it")
        ur.track_nightly(ur.load(), self.t0 + timedelta(hours=1))
        self.assertIsNone(ur.load()["monthly"])
        self.assertEqual(ur.review_record(p["review_id"])["status"], "cancelled")

    def test_a_lost_task_is_added_again(self):
        p = ur.mark_due(ur.load(), self.t0)
        clear_nightly_tasks()                   # e.g. the add failed after the state was saved
        ur.track_nightly(ur.load(), self.t0 + timedelta(minutes=5))
        self.assertEqual(nightly_tasks.get(p["task"])["status"], "pending")

    def test_missed_after_max_runs_is_an_error_and_it_stays_due(self):
        p = ur.mark_due(ur.load(), self.t0)
        night = self.t0 + timedelta(hours=11)
        for i in range(nightly_tasks.MAX_RUNS):
            self.carried(p, night + timedelta(days=i))
        self.carried(p, night + timedelta(days=5), result="start-failed")      # never started a run: not counted
        last = night + timedelta(days=nightly_tasks.MAX_RUNS - 1)
        self.assertEqual(ur.track_nightly(ur.load(), last + timedelta(hours=2)), [])   # its run may still be going
        self.assertFalse(self.paused())
        [(task, event, _)] = ur.track_nightly(ur.load(), last + nightly_tasks.RUN_SPAN)
        self.assertEqual(event, "missed")
        self.assertTrue(self.paused())
        self.assertIn(f"the nightly task {p['task']} was not done", self.alerts[-1][0])
        self.assertEqual(ur.load()["monthly"], p)                                   # still pending
        self.assertEqual(nightly_tasks.get(p["task"])["status"], "pending")
        self.assertEqual(ur.review_record(p["review_id"])["status"], "pending")

    def test_one_offs_keep_their_own_sessions(self):
        ur.mark_due(ur.load(), self.t0)
        self.usage = usage(weekly=20.0, session=10.0, now=datetime.now(UTC))
        self.cron("--only", "usage", "--now")
        self.assertEqual(len(self.fires), 1)
        sid, msg, plan, new, k = self.fires[0]
        self.assertTrue(new)
        self.assertIn("(one-off)", k.get("name", ""))
        rec = ur.review_record(sid)
        self.assertEqual((rec["status"], rec["one_off"]), ("started", True))
        self.assertIsNotNone(ur.load()["monthly"])          # the pending monthly one is untouched


class ChainCli(Base):
    """main() wiring for the chained --only value: dispatched to start_chain (never to two
    separate run_once() calls), and schedulable via --at like any other --only value."""
    def setUp(self):
        super().setUp()
        self.saved_chain = ur.start_chain

    def tearDown(self):
        ur.start_chain = self.saved_chain
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
        ur.start_chain = lambda st, now, attempt=1: called.append((now, attempt)) or True
        self._run("--only", "usage,stage-eta", "--now")
        self.assertEqual([a for _, a in called], [1])
        self.assertEqual(len(self.fires), 0)   # start_chain is the spy above, never fired

    def test_only_reverse_order_also_dispatches_to_the_chain(self):
        called = []
        ur.start_chain = lambda st, now, attempt=1: called.append((now, attempt)) or True
        self._run("--only", "stage-eta,usage", "--now", "--attempt", "2")
        self.assertEqual([a for _, a in called], [2])

    def test_only_usage_stage_eta_at_schedules_without_firing(self):
        self._run("--only", "usage,stage-eta", "--at", "2026-10-15 16:00")
        self.assertEqual(len(self.at_calls), 1)
        self.assertIn("usage_review.py --only usage,stage-eta --now", self.at_calls[0][1])
        self.assertEqual(len(self.fires), 0)
        self.assertFalse(os.path.exists(ur.STATE))   # the monthly state untouched


if __name__ == "__main__":
    unittest.main()
