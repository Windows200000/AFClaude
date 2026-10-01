#!/usr/bin/env python3
"""Offline tests for keepalive.py (no claude calls, no resumes).
Real-transcript tests only READ files under ~/.claude/projects."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keepalive as ka  # noqa: E402

UTC = timezone.utc


def Z(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


WEEK_RESET = Z("2026-10-01T16:59:59Z")   # real value from /usage at 00:01 Berlin


def usage(week_pct, week_reset=WEEK_RESET, sess_pct=0, sess_reset=None, fetched=None):
    return {"fetched_at": fetched or Z("2026-09-26T00:00:00Z"),
            "weekly": {"percent": week_pct, "resets_at": week_reset},
            "session": {"percent": sess_pct, "resets_at": sess_reset}}


class ResetText(unittest.TestCase):
    def test_pm_hour_same_day(self):
        self.assertEqual(ka.parse_reset_text("You've hit your session limit · resets 5pm (UTC)",
                                             Z("2026-09-25T15:39:44Z")), Z("2026-09-25T17:00:00Z"))

    def test_rolls_to_next_day(self):
        self.assertEqual(ka.parse_reset_text("resets 1:30am (UTC)", Z("2026-09-24T21:26:27Z")),
                         Z("2026-09-25T01:30:00Z"))

    def test_relative_to_notice_not_now(self):
        # notice at 07:54Z says 9:30am; evaluated much later it must still be the same instant
        self.assertEqual(ka.parse_reset_text("resets 9:30am (UTC)", Z("2026-09-25T07:54:32Z")),
                         Z("2026-09-25T09:30:00Z"))

    def test_with_date(self):
        self.assertEqual(ka.parse_reset_text("resets Oct 1, 4:59pm (UTC)", Z("2026-09-25T22:00:00Z")),
                         Z("2026-10-01T16:59:00Z"))

    def test_year_wrap(self):
        self.assertEqual(ka.parse_reset_text("resets Jan 2, 9am (UTC)", Z("2026-12-30T10:00:00Z")),
                         Z("2027-01-02T09:00:00Z"))

    def test_midnight_noon(self):
        self.assertEqual(ka.parse_reset_text("resets 12am (UTC)", Z("2026-09-25T15:00:00Z")),
                         Z("2026-09-26T00:00:00Z"))
        self.assertEqual(ka.parse_reset_text("resets 12pm (UTC)", Z("2026-09-25T08:00:00Z")),
                         Z("2026-09-25T12:00:00Z"))

    def test_non_utc_tz(self):
        self.assertEqual(ka.parse_reset_text("resets 3am (Europe/Berlin)", Z("2026-09-25T20:00:00Z")),
                         Z("2026-09-26T01:00:00Z"))

    def test_garbage(self):
        self.assertIsNone(ka.parse_reset_text("no reset here", Z("2026-09-25T20:00:00Z")))


class Window(unittest.TestCase):
    cases = [
        ("2026-09-25T21:59:59Z", False),  # 23:59:59 CEST
        ("2026-09-25T22:00:00Z", True),   # 00:00 CEST
        ("2026-09-26T05:59:59Z", True),   # 07:59:59 CEST
        ("2026-09-26T06:00:00Z", False),  # 08:00 CEST
        ("2026-10-26T22:59:59Z", False),  # 23:59:59 CET
        ("2026-10-26T23:00:00Z", True),   # 00:00 CET
        ("2026-10-27T06:59:59Z", True),   # 07:59:59 CET
        ("2026-10-27T07:00:00Z", False),  # 08:00 CET
        # DST end night (2026-10-25, 03:00 CEST -> 02:00 CET): window is 9 real hours
        ("2026-10-24T22:00:00Z", True),   # 00:00 CEST
        ("2026-10-25T06:59:59Z", True),   # 07:59:59 CET
        ("2026-10-25T07:00:00Z", False),  # 08:00 CET
        # DST start night (2027-03-28, 02:00 CET -> 03:00 CEST): window is 7 real hours
        ("2027-03-27T23:00:00Z", True),   # 00:00 CET
        ("2027-03-28T05:59:59Z", True),   # 07:59:59 CEST
        ("2027-03-28T06:00:00Z", False),  # 08:00 CEST
    ]

    def test_in_window(self):
        for ts, want in self.cases:
            with self.subTest(ts=ts):
                self.assertEqual(ka.in_window(Z(ts)), want)

    def test_window_end_and_next_start(self):
        self.assertEqual(ka.current_window_end(Z("2026-09-25T23:30:00Z")), Z("2026-09-26T06:00:00Z"))
        self.assertEqual(ka.current_window_end(Z("2026-09-26T10:00:00Z")), Z("2026-09-27T06:00:00Z"))
        self.assertEqual(ka.next_window_start(Z("2026-09-26T10:00:00Z")), Z("2026-09-26T22:00:00Z"))
        self.assertEqual(ka.next_window_start(Z("2026-10-26T10:00:00Z")), Z("2026-10-26T23:00:00Z"))
        self.assertEqual(ka.current_window_end(Z("2026-10-24T22:30:00Z")), Z("2026-10-25T07:00:00Z"))


class Budget(unittest.TestCase):
    NOW = Z("2026-09-26T03:00:00Z")   # 05:00 Berlin, inside window

    def test_tonight_real_numbers_hold(self):
        go, why = ka.budget_decision(usage(32), self.NOW)
        self.assertFalse(go, why)
        self.assertIn("projected 158%", why)

    def test_low_usage_continue(self):
        go, why = ka.budget_decision(usage(10), self.NOW)
        self.assertTrue(go, why)

    def test_just_under_threshold(self):
        # projected = p * 7d / elapsed  (elapsed = 34h00m01s here)
        frac = (self.NOW - (WEEK_RESET - ka.WEEK)) / ka.WEEK
        self.assertTrue(ka.budget_decision(usage(89.99 * frac), self.NOW)[0])
        self.assertFalse(ka.budget_decision(usage(90.0 * frac), self.NOW)[0])

    def test_high_but_reset_before_cutoff(self):
        go, why = ka.budget_decision(usage(95, Z("2026-09-26T08:30:00Z")), self.NOW)  # 10:30 Berlin
        self.assertTrue(go, why)
        self.assertIn("weekly reset <=", why)

    def test_high_reset_exactly_cutoff(self):
        self.assertTrue(ka.budget_decision(usage(95, Z("2026-09-26T09:00:00Z")), self.NOW)[0])  # 11:00

    def test_high_reset_after_cutoff(self):
        go, why = ka.budget_decision(usage(95, Z("2026-09-26T09:00:01Z")), self.NOW)
        self.assertFalse(go, why)

    def test_cutoff_in_cet(self):
        now = Z("2026-10-27T05:00:00Z")  # 06:00 CET
        self.assertTrue(ka.budget_decision(usage(95, Z("2026-10-27T10:00:00Z")), now)[0])  # 11:00 CET
        self.assertFalse(ka.budget_decision(usage(95, Z("2026-10-27T10:00:01Z")), now)[0])

    def test_unknown_usage_fails_safe(self):
        self.assertFalse(ka.budget_decision(None, self.NOW)[0])
        self.assertFalse(ka.budget_decision({"fetched_at": self.NOW}, self.NOW)[0])

    def test_weekly_exhausted(self):
        go, why = ka.budget_decision(usage(100), self.NOW)
        self.assertFalse(go)
        self.assertIn("exhausted", why)

    def test_elapsed_floor(self):
        # 2h into a fresh week, 5% used: unfloored linear would say 420%
        now = WEEK_RESET - ka.WEEK + timedelta(hours=2)
        self.assertAlmostEqual(ka.project_weekly(5, WEEK_RESET, now), 5 + 5 * 166 / 24, places=3)
        self.assertTrue(ka.budget_decision(usage(5), now)[0])


def write_transcript(d, sid, entries):
    proj = os.path.join(d, "-proj")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, f"{sid}.jsonl"), "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


STALL = {"type": "assistant", "uuid": "stall-1", "timestamp": "2026-09-26T01:10:00.000Z",
         "isApiErrorMessage": True, "error": "rate_limit",
         "message": {"role": "assistant", "model": "<synthetic>",
                     "content": [{"type": "text", "text": "You've hit your session limit · resets 2:49am (UTC)"}]}}
USER = {"type": "user", "uuid": "u1", "timestamp": "2026-09-26T01:00:00.000Z",
        "message": {"role": "user", "content": "do things"}}
REPLY = {"type": "assistant", "uuid": "a1", "timestamp": "2026-09-26T01:05:00.000Z",
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "done"}]}}
TRAILING = [{"type": "system", "timestamp": "2026-09-26T01:10:00.100Z"},
            {"type": "last-prompt"}, {"type": "cost-state"}, {"type": "atis-latch"}]
SID = "00000000-0000-4000-8000-000000000001"


class Evaluate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ka.PROJECTS_DIR = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def ev(self, now, u=None):
        return ka.evaluate(SID, Z(now), lambda n: u)[:2]

    def test_trailing_metadata_still_detected(self):
        write_transcript(self.tmp.name, SID, [USER, REPLY, STALL] + TRAILING)
        self.assertEqual(self.ev("2026-09-26T01:20:00Z")[0], "WAIT_RESET")

    def test_resumed_after_stall_is_not_stalled(self):
        later = dict(USER, uuid="u2", timestamp="2026-09-26T03:00:00.000Z")
        write_transcript(self.tmp.name, SID, [USER, STALL] + TRAILING + [later])
        self.assertEqual(self.ev("2026-09-26T03:01:00Z")[0], "NO_STALL")

    def test_sequence(self):
        write_transcript(self.tmp.name, SID, [USER, REPLY, STALL] + TRAILING)
        # reset 02:49Z (04:49 Berlin) + 90s grace
        self.assertEqual(self.ev("2026-09-26T02:50:00Z", usage(10))[0], "WAIT_RESET")
        self.assertEqual(self.ev("2026-09-26T02:50:31Z", usage(10))[0], "FIRE")
        self.assertEqual(self.ev("2026-09-26T02:50:31Z", usage(32))[0], "HOLD")
        self.assertEqual(self.ev("2026-09-26T02:50:31Z", None)[0], "HOLD")
        # live usage says the session limit is still on
        self.assertEqual(self.ev("2026-09-26T02:51:00Z",
                                 usage(10, sess_pct=100, sess_reset=Z("2026-09-26T03:30:00Z")))[0], "WAIT_RESET")

    def test_reset_after_window_waits_for_next_night(self):
        late = dict(STALL, timestamp="2026-09-26T05:30:00.000Z",
                    message=dict(STALL["message"], content=[{"type": "text",
                                 "text": "You've hit your session limit · resets 10am (UTC)"}]))
        write_transcript(self.tmp.name, SID, [USER, late])
        act, detail = self.ev("2026-09-26T10:05:00Z", usage(10))
        self.assertEqual(act, "WAIT_WINDOW")
        self.assertIn("2026-09-27 00:00:00 CEST", detail)
        self.assertEqual(self.ev("2026-09-26T22:00:10Z", usage(10))[0], "FIRE")

    def test_other_api_error_not_fired(self):
        err = dict(STALL, error="overloaded", message=dict(STALL["message"], content=[
            {"type": "text", "text": "API Error: 529 overloaded"}]))
        write_transcript(self.tmp.name, SID, [USER, err])
        self.assertEqual(self.ev("2026-09-26T03:00:00Z", usage(10))[0], "OTHER_ERROR")


class Files(unittest.TestCase):
    def test_scripts_executable(self):
        # 2026-09-29 00:00: the window-start fire died on "Permission denied: ka_resume.sh"
        d = os.path.dirname(ka.__file__)
        for f in ("ka_resume.sh", "start_keepalive.sh", "notify.py"):
            self.assertTrue(os.access(os.path.join(d, f), os.X_OK), f)


class LastMile(unittest.TestCase):
    R = Z("2026-10-01T16:59:59Z")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self.tmp.name, "afclaude.json")
        import afclaude_config
        self.ac = afclaude_config
        self._old = afclaude_config.CONFIG_FILE
        afclaude_config.CONFIG_FILE = self.cfg

    def tearDown(self):
        self.ac.CONFIG_FILE = self._old
        self.tmp.cleanup()

    def setcfg(self, **kw):
        with open(self.cfg, "w") as fh:
            json.dump(kw, fh)

    def test_default_is_one_session_length(self):
        self.assertEqual(self.ac.last_mile(), timedelta(hours=5))
        self.setcfg(last_mile_hours=3)
        self.assertEqual(self.ac.last_mile(), timedelta(hours=3))
        self.setcfg(last_mile_hours=0)
        self.assertEqual(self.ac.last_mile(), timedelta(0))

    def test_budget_edges(self):
        u = usage(99, self.R)
        # before the last mile the old rules decide (here the reset-before-cutoff rule)
        self.assertNotIn("last mile", ka.budget_decision(u, self.R - timedelta(hours=5, minutes=1))[1])
        # early in the week at 99%: HOLD without the last mile
        self.assertFalse(ka.budget_decision(usage(99, Z("2026-10-05T16:59:59Z")), Z("2026-10-01T18:00:00Z"))[0])
        go, why = ka.budget_decision(u, self.R - timedelta(hours=4, minutes=59))
        self.assertTrue(go, why)
        self.assertIn("last mile", why)
        self.assertFalse(ka.budget_decision(usage(100, self.R), self.R - timedelta(hours=1))[0])
        self.assertFalse(ka.budget_decision(None, self.R - timedelta(hours=1))[0])
        self.setcfg(last_mile_hours=0)
        self.assertNotIn("last mile", ka.budget_decision(u, self.R - timedelta(hours=1))[1])

    def test_dst_day(self):
        r = Z("2026-10-25T16:59:59Z")          # reset on the CEST->CET day
        self.assertIsNotNone(ka.last_mile_left(r, r - timedelta(hours=4, minutes=59)))
        self.assertIsNone(ka.last_mile_left(r, r - timedelta(hours=5, minutes=1)))

    def test_evaluate_window_exempt_in_last_mile(self):
        with tempfile.TemporaryDirectory() as d:
            ka.PROJECTS_DIR, old = d, ka.PROJECTS_DIR
            try:
                stall = dict(STALL, timestamp="2026-10-01T11:00:00.000Z",
                             message=dict(STALL["message"], content=[{"type": "text",
                                          "text": "You've hit your session limit · resets 12pm (UTC)"}]))
                write_transcript(d, SID, [USER, stall])
                cache = {"fetched_at": self.R, "weekly": {"percent": 95.0, "resets_at": self.R},
                         "session": {"percent": 0.0, "resets_at": None}}
                ka.read_usage_cache, oldc = (lambda: cache), ka.read_usage_cache
                try:
                    now = Z("2026-10-01T12:05:00Z")        # 14:05 Berlin, outside the night window
                    self.assertEqual(ka.evaluate(SID, now, lambda n: cache)[0], "FIRE")
                    self.setcfg(last_mile_hours=0)
                    self.assertEqual(ka.evaluate(SID, now, lambda n: cache)[0], "WAIT_WINDOW")
                finally:
                    ka.read_usage_cache = oldc
            finally:
                ka.PROJECTS_DIR = old

    def test_last_mile_pass_once_per_cycle(self):
        fired = []
        cache = {"fetched_at": self.R, "weekly": {"percent": 95.0, "resets_at": self.R},
                 "session": {"percent": 0.0, "resets_at": None}}
        olds = (ka.read_usage_cache, ka.fresh_usage, ka.handle_fire)
        ka.read_usage_cache = lambda: cache
        ka.fresh_usage = lambda n, force=False: cache
        def hf(sid, stall, reason, st, args):
            fired.append(stall)
            st["handled"][stall["uuid"]] = {"result": "test"}
        ka.handle_fire = hf
        try:
            st = {"handled": {}, "fires": {}}
            self.assertIsNone(ka.last_mile_pass(SID, self.R - timedelta(hours=6), st, None))
            self.assertEqual(fired, [])
            ka.last_mile_pass(SID, self.R - timedelta(hours=4), st, None)
            ka.last_mile_pass(SID, self.R - timedelta(hours=3), st, None)
            self.assertEqual(len(fired), 1)
            self.assertEqual(fired[0]["prompt"], "last_mile")
            self.assertTrue(fired[0]["uuid"].startswith("last-mile-"))
            cache["weekly"]["percent"] = 100.0        # exhausted -> HOLD, re-check later
            st2 = {"handled": {}, "fires": {}}
            nxt = ka.last_mile_pass(SID, self.R - timedelta(hours=2), st2, None)
            self.assertEqual(nxt, self.R - timedelta(hours=2) + ka.LAST_MILE_RECHECK)
        finally:
            ka.read_usage_cache, ka.fresh_usage, ka.handle_fire = olds

    def test_last_mile_jittered_reset_fires_once(self):
        # regression 01.10.: resets_at jittered between fetches (16:59:59.557 vs
        # 17:00:00.320) and the raw ISO key let the watcher fire twice
        fired = []
        r1 = Z("2026-10-01T16:59:59.557562Z")
        r2 = Z("2026-10-01T17:00:00.320910Z")
        cache = {"fetched_at": r1, "weekly": {"percent": 85.0, "resets_at": r1},
                 "session": {"percent": 0.0, "resets_at": None}}
        olds = (ka.read_usage_cache, ka.fresh_usage, ka.handle_fire)
        ka.read_usage_cache = lambda: cache
        ka.fresh_usage = lambda n, force=False: cache
        def hf(sid, stall, reason, st, args):
            fired.append(stall)
            st["handled"][stall["uuid"]] = {"result": "test"}
        ka.handle_fire = hf
        try:
            st = {"handled": {}, "fires": {}}
            ka.last_mile_pass(SID, r1 - timedelta(hours=4), st, None)
            cache["weekly"]["resets_at"] = r2
            ka.last_mile_pass(SID, r1 - timedelta(hours=3), st, None)
            self.assertEqual(len(fired), 1)
            self.assertEqual(fired[0]["uuid"], "last-mile-2026-10-01T17:00:00+00:00")
            # the raw keys already in today's state count as handled
            fired.clear()
            st = {"handled": {"last-mile-2026-10-01T17:00:00.320910+00:00": {},
                              "last-mile-2026-10-01T16:59:59.557562+00:00": {}}, "fires": {}}
            for r in (r1, r2):
                cache["weekly"]["resets_at"] = r
                ka.last_mile_pass(SID, r1 - timedelta(hours=2), st, None)
            self.assertEqual(fired, [])
            # a different weekly cycle is not handled by them
            self.assertFalse(ka.last_mile_handled(ka.last_mile_key(r1 + timedelta(days=7)), st["handled"]))
        finally:
            ka.read_usage_cache, ka.fresh_usage, ka.handle_fire = olds

    def test_budget_headroom(self):
        now = Z("2026-09-30T22:00:00Z")                       # 149 h into the week
        extra, text = ka.budget_headroom(usage(77, self.R), now)
        self.assertAlmostEqual(extra, 90 * 149 / 168 - 77, places=1)   # ~2.8 %
        self.assertAlmostEqual(ka.project_weekly(77 + extra, self.R, now), 90, places=1)
        self.assertEqual(ka.budget_headroom(usage(95, self.R), now)[0], 0.0)
        extra, text = ka.budget_headroom(usage(80, self.R), self.R - timedelta(hours=2))
        self.assertEqual(extra, 20.0)
        self.assertIn("last mile", text)
        self.assertIsNone(ka.budget_headroom(None, now)[0])

    def test_prompt_file(self):
        m = ka.session_message("last_mile", reason="r", progress=ka.PROGRESS_FILE)
        self.assertIn("Last mile", m)
        self.assertNotIn("\n", m)


class Archived(unittest.TestCase):
    def test_archived_notice_after_last_message(self):
        with tempfile.TemporaryDirectory() as d:
            sysmsg = {"type": "system", "subtype": "informational",
                      "content": "Remote Control disconnected — this session was ended or archived from another device or app (code 4090)"}
            write_transcript(d, SID, [USER, REPLY, sysmsg])
            p = os.path.join(d, "-proj", SID + ".jsonl")
            self.assertTrue(ka.archived_since_last_message(p))
            write_transcript(d, SID, [USER, sysmsg, REPLY])
            self.assertFalse(ka.archived_since_last_message(p))
            self.assertFalse(ka.archived_since_last_message(None))


class RealTranscripts(unittest.TestCase):
    """Read-only checks against real stalls on this host."""
    def setUp(self):
        ka.PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

    def test_a954f3c8_is_stalled(self):
        p = ka.transcript_path("a954f3c8-e15e-41dd-9b25-b2f8b1a88a06")
        if not p:
            self.skipTest("transcript gone")
        s = ka.stall_info(ka.last_message(p))
        self.assertIsNotNone(s)
        self.assertEqual(s["kind"], "session")
        self.assertEqual(s["reset_from_text"], Z("2026-09-25T17:00:00Z"))

    def test_planning_session_not_stalled(self):
        p = ka.transcript_path("37c51d64-4516-578d-af3f-b34feea826ee")
        if not p:
            self.skipTest("transcript gone")
        self.assertIsNone(ka.stall_info(ka.last_message(p)))


class FakeProcHolders(unittest.TestCase):
    """preflight against a fake /proc + ~/.claude/sessions (paths injected): rc-server
    children are detected by --sdk-url or entrypoint sdk-cli, and any live registry
    holder of the uuid that is not our tmux ka-<id8> process blocks a send/resume."""
    SID = "9da84efe-c1ec-516d-9a86-4ecca2aec1f3"
    CHILD = ["/h/.local/share/claude/versions/2.1.283", "--print", "--sdk-url",
             "https://api.anthropic.com/v1/code/sessions/cse_01X", "--session-id", "cse_01X"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = os.path.join(self.tmp.name, "proc")
        self.sess = os.path.join(self.tmp.name, "sessions")
        os.makedirs(self.proc)
        os.makedirs(self.sess)
        self.orig = (ka.PROC_DIR, ka.SESSIONS_DIR, ka.tmux_alive, ka.tmux_pids, ka.agent_entries, ka.TAKE_OVER_IDLE)
        ka.PROC_DIR, ka.SESSIONS_DIR = self.proc, self.sess
        self.tmux, self.panes, self.rows = False, set(), []
        ka.tmux_alive = lambda s: self.tmux
        ka.tmux_pids = lambda s: set(self.panes)
        ka.agent_entries = lambda s: list(self.rows)
        ka.TAKE_OVER_IDLE = False

    def tearDown(self):
        (ka.PROC_DIR, ka.SESSIONS_DIR, ka.tmux_alive, ka.tmux_pids, ka.agent_entries, ka.TAKE_OVER_IDLE) = self.orig
        self.tmp.cleanup()

    def process(self, pid, argv, ppid=1, start=1000):
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "cmdline"), "wb") as fh:
            fh.write(b"\0".join(a.encode() for a in argv) + b"\0")
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 10
        with open(os.path.join(d, "stat"), "w") as fh:
            fh.write(f"{pid} (x y) " + " ".join(fields) + "\n")

    def registry(self, pid, sid=None, entrypoint="cli", start=1000, **kw):
        d = {"pid": pid, "sessionId": sid or self.SID, "procStart": str(start), "kind": "interactive",
             "entrypoint": entrypoint, **kw}
        with open(os.path.join(self.sess, f"{pid}.json"), "w") as fh:
            json.dump(d, fh)

    def test_proc_parsing(self):
        self.process(7, ["claude", "rc"], ppid=3, start=4242)
        self.assertEqual(ka.proc_argv(7), ["claude", "rc"])
        self.assertEqual((ka.proc_ppid(7), ka.proc_starttime(7)), (3, "4242"))
        self.assertTrue(ka.pid_alive(7))
        self.assertFalse(ka.pid_alive(8))
        self.assertEqual((ka.proc_argv(8), ka.proc_ppid(8)), ([], None))

    def test_sdk_url_child_is_rc_held_even_without_rc_parent(self):
        self.process(100, self.CHILD, ppid=1)                        # reparented: no rc server above it
        self.assertTrue(ka.is_sdk_child(self.CHILD))
        self.assertTrue(ka.is_sdk_child(["x", "--print", "--sdk-url=https://x"]))
        self.assertFalse(ka.is_sdk_child(["claude", "--resume", self.SID, "--remote-control"]))
        self.assertTrue(ka.rc_held(100))
        self.rows = [{"sessionId": self.SID, "pid": 100, "kind": "interactive", "status": "idle"}]
        ok, problems, plan = ka.preflight(self.SID)
        self.assertEqual((ok, plan), (False, None))
        self.assertTrue(problems[0].startswith(ka.RC_HELD))

    def test_sdk_cli_entrypoint_is_rc_held(self):
        self.process(101, ["claude"], ppid=1)                         # argv tells nothing
        self.registry(101, entrypoint="sdk-cli")
        self.assertTrue(ka.rc_held(101))
        ka.TAKE_OVER_IDLE = True
        self.rows = [{"sessionId": self.SID, "pid": 101, "kind": "interactive", "status": "idle"}]
        self.assertTrue(ka.preflight(self.SID)[1][0].startswith(ka.RC_HELD))   # never taken over

    def test_registry_rc_child_not_listed_by_agents(self):
        self.process(50, ["claude", "rc"])
        self.process(102, self.CHILD, ppid=50)
        self.registry(102, entrypoint="sdk-cli")
        self.rows = []                                                # `claude agents` does not list it
        ok, problems, _ = ka.preflight(self.SID)
        self.assertFalse(ok)
        self.assertTrue(problems[0].startswith(ka.RC_HELD))
        self.assertIn("102", problems[0])

    def test_registry_other_holder_not_listed_by_agents(self):
        self.process(103, ["claude", "--resume", self.SID])
        self.registry(103)
        ok, problems, plan = ka.preflight(self.SID)
        self.assertEqual((ok, plan), (False, None))
        self.assertTrue(problems[0].startswith(ka.OTHER_HELD))
        self.registry(103, sid="00000000-0000-4000-8000-000000000000")   # a different session: free
        self.assertEqual(ka.preflight(self.SID), (True, [], "resume"))

    def test_registry_stale_entries_are_ignored(self):
        self.registry(104)                                            # dead pid
        self.process(105, ["claude"], start=2000)
        self.registry(105, start=1000)                                # pid reused by another process
        self.assertEqual(ka.registry_holders(self.SID), set())
        self.assertEqual(ka.preflight(self.SID), (True, [], "resume"))

    def test_our_tmux_process_is_not_a_foreign_holder(self):
        self.tmux, self.panes = True, {300}
        self.process(300, ["bash", "run.sh"])
        self.process(301, ["claude", "--resume", self.SID, "--remote-control"], ppid=300)
        self.registry(301)
        self.assertEqual(ka.preflight(self.SID), (True, [], "send-keys"))
        self.panes = {301}                                           # claude is the pane itself
        self.assertEqual(ka.preflight(self.SID), (True, [], "send-keys"))

    def test_tmux_alive_but_foreign_holder_refuses(self):
        self.tmux, self.panes = True, {300}
        self.process(300, ["claude", "--resume", self.SID])
        self.registry(300)
        self.process(50, ["claude", "rc"])
        self.process(102, self.CHILD, ppid=50)
        self.registry(102, entrypoint="sdk-cli")
        self.assertTrue(ka.preflight(self.SID)[1][0].startswith(ka.RC_HELD))
        os.remove(os.path.join(self.sess, "102.json"))
        self.process(106, ["claude", "--resume", self.SID])          # someone's terminal
        self.registry(106)
        self.assertTrue(ka.preflight(self.SID)[1][0].startswith(ka.OTHER_HELD))

    def test_listed_plain_idle_holder_still_taken_over(self):
        self.process(200, ["claude", "--resume", self.SID, "--remote-control"], ppid=10)
        self.process(10, ["-bash"])
        self.registry(200)                                            # listed AND in the registry
        self.rows = [{"sessionId": self.SID, "pid": 200, "kind": "interactive", "status": "idle"}]
        self.assertFalse(ka.preflight(self.SID)[0])                  # opt-in only
        ka.TAKE_OVER_IDLE = True
        self.assertEqual(ka.preflight(self.SID), (True, [], "take-over:200"))


class DryRunLoop(unittest.TestCase):
    """Full CLI pass in a sandbox: detection -> decision -> handle_fire in
    dry-run. `claude` is shadowed by a stub on PATH that records calls and
    fails loudly on anything resume-like."""
    def test_dry_run_never_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            write_transcript(d, SID, [USER, REPLY, STALL] + TRAILING)
            bindir = os.path.join(d, "bin")
            os.makedirs(bindir)
            calls = os.path.join(d, "calls.log")
            with open(os.path.join(bindir, "claude"), "w") as fh:
                fh.write(f"#!/bin/sh\necho \"$@\" >> {calls}\n"
                         "case \"$*\" in *--resume*) echo RESUME-CALLED >&2; exit 99;; esac\n"
                         "case \"$*\" in *agents*) echo '[]';; esac\nexit 0\n")
            os.chmod(os.path.join(bindir, "claude"), 0o755)
            env = dict(os.environ, KEEPALIVE_PROJECTS_DIR=d, KEEPALIVE_STATE_DIR=d,
                       KEEPALIVE_PROGRESS_FILE=os.path.join(d, "P.md"))
            # stub must win over the real claude even inside SCRUBBED_ENV's PATH
            code = ("import keepalive as ka, sys;"
                    f"ka.SCRUBBED_ENV['PATH']={bindir!r}+':'+ka.SCRUBBED_ENV['PATH'];"
                    "from datetime import datetime,timezone;"
                    "ka.LAUNCH.clear();"
                    f"ka.fresh_usage=lambda n, force=False: {{'fetched_at': n, 'weekly': {{'percent': 5.0, 'resets_at': ka.parse_ts('2026-10-01T16:59:59Z')}}, 'session': {{'percent': 0.0, 'resets_at': None}}}};"
                    "st=ka.load_state();"
                    f"a,det,s=ka.evaluate({SID!r}, ka.parse_ts('2026-09-26T03:00:00Z'), ka.fresh_usage);"
                    "print(a,det);"
                    "import types;ka.handle_fire(" + repr(SID) + ", s, det, st, types.SimpleNamespace(arm=False))")
            r = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(ka.__file__),
                               env=dict(env, PATH=bindir + ":" + env["PATH"]),
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("FIRE", r.stdout)
            self.assertIn("WOULD FIRE (dry-run)", r.stdout)
            logged = open(calls).read() if os.path.exists(calls) else ""  # noqa: SIM115
            self.assertNotIn("--resume", logged)
            state = json.load(open(os.path.join(d, "keepalive_state.json")))
            self.assertEqual(state["handled"]["stall-1"]["result"], "dry-run")


if __name__ == "__main__":
    unittest.main(verbosity=2)
