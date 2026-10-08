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
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import keepalive as ka  # noqa: E402
import schedule  # noqa: E402

UTC = timezone.utc


def Z(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


WEEK_RESET = Z("2026-10-01T16:59:59Z")   # real value from /usage at 00:01 Berlin

import afclaude_config  # noqa: E402

_CFG_DIR = tempfile.TemporaryDirectory()


import pacing as budget  # noqa: E402

_OLD_FILES = (budget.SAMPLES_FILE, ka.DEFER_FILE, budget.FIRE_FILES, ka.USAGE_STATE_FILE, ka.alert)


setcfg = testenv.setcfg     # the DB settings for a test: the code defaults, then kw


def setUpModule():
    # the budget tests below pin the linear rule; BudgetWiring tests the budget model.
    # Hermetic: no real samples (the last mile's ratio = the default), no real deferral file.
    setcfg(usage_model="linear")
    budget.SAMPLES_FILE = os.path.join(_CFG_DIR.name, "no_samples.jsonl")
    ka.DEFER_FILE = os.path.join(_CFG_DIR.name, "deferred.json")
    ka.FILLUP_FILE = os.path.join(_CFG_DIR.name, "fillup.json")   # the fill-up plan (D-212)
    budget.FIRE_FILES = []
    # window-start usage bookkeeping (note_window_usage): a temp state file, never ALERTS.md / a push
    ka.USAGE_STATE_FILE = os.path.join(_CFG_DIR.name, "usage_refresh_state.json")
    ka.alert = lambda subject, body="": None


def tearDownModule():
    testenv.clear_settings()
    (budget.SAMPLES_FILE, ka.DEFER_FILE, budget.FIRE_FILES, ka.USAGE_STATE_FILE, ka.alert) = _OLD_FILES
    _CFG_DIR.cleanup()


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


class WindowStartDedup(unittest.TestCase):
    """The window code itself is schedule.py (test_schedule.py); keepalive dedups per start key."""

    def test_window_start_handled_key_not_fired_again_after_midnight(self):
        """handle_fire skips a key already handled (before preflight): a 23:00 window-start
        and a second --window-start run at 00:30 the same night fire once; the 04:00 start has
        its own key (D-202)."""
        st = {"handled": {schedule.start_key(Z("2026-09-29T21:00:05Z")): {"result": "continued"}},
              "fires": {}}
        old = ka.preflight
        ka.preflight = lambda *a, **k: self.fail("preflight must not run for a handled key")
        try:
            stall = {"uuid": schedule.start_key(Z("2026-09-29T22:30:00Z")), "timestamp": Z("2026-09-29T22:30:00Z")}
            ka.handle_fire(SID, stall, "window start", st, None)
        finally:
            ka.preflight = old
        self.assertNotIn(schedule.start_key(Z("2026-09-30T02:00:05Z")), st["handled"])


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

    def test_cutoff_window_started_the_evening_before(self):
        # 23:30 CEST on 25.09.: the window ends 26.09. 09:00, so the cutoff is 26.09. 11:00 CEST
        now = Z("2026-09-25T21:30:00Z")
        self.assertTrue(ka.budget_decision(usage(95, Z("2026-09-26T09:00:00Z")), now)[0])
        go, why = ka.budget_decision(usage(95, Z("2026-09-26T09:00:01Z")), now)
        self.assertFalse(go)
        self.assertIn("after 2026-09-26 11:00:00 CEST", why)
        # before and after midnight of the same window: the same cutoff
        self.assertIn("after 2026-09-26 11:00:00 CEST",
                      ka.budget_decision(usage(95, Z("2026-09-26T09:00:01Z")), self.NOW)[1])

    def test_cutoff_at_window_start_cet(self):
        now = Z("2026-10-26T22:00:00Z")  # 23:00 CET on 26.10. -> cutoff 27.10. 11:00 CET
        self.assertTrue(ka.budget_decision(usage(95, Z("2026-10-27T10:00:00Z")), now)[0])
        self.assertFalse(ka.budget_decision(usage(95, Z("2026-10-27T10:00:01Z")), now)[0])

    def test_cutoff_dst_nights(self):
        # D-148: the window is 10 h absolute, so it ends 08:00 CET / 10:00 CEST on the DST nights
        # DST end: 23:30 CEST 24.10., the window ends 25.10. 08:00 CET, cutoff 10:00 CET = 09:00Z
        now = Z("2026-10-24T21:30:00Z")
        self.assertTrue(ka.budget_decision(usage(95, Z("2026-10-25T09:00:00Z")), now)[0])
        self.assertFalse(ka.budget_decision(usage(95, Z("2026-10-25T09:00:01Z")), now)[0])
        # DST start: 23:30 CET 27.03., the window ends 28.03. 10:00 CEST, cutoff 12:00 CEST = 10:00Z
        now = Z("2027-03-27T22:30:00Z")
        self.assertTrue(ka.budget_decision(usage(95, Z("2027-03-28T10:00:00Z")), now)[0])
        self.assertFalse(ka.budget_decision(usage(95, Z("2027-03-28T10:00:01Z")), now)[0])

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
        """evaluate() = an APPROVED stall's continue (D-206): at the reset + grace, no budget gate."""
        write_transcript(self.tmp.name, SID, [USER, REPLY, STALL] + TRAILING)
        # reset 02:49Z (04:49 Berlin) + 90s grace
        self.assertEqual(self.ev("2026-09-26T02:50:00Z", usage(10))[0], "WAIT_RESET")
        self.assertEqual(self.ev("2026-09-26T02:50:31Z", usage(10))[0], "FIRE")
        self.assertEqual(self.ev("2026-09-26T02:50:31Z", usage(32))[0], "FIRE")    # no AFClaude budget gate
        self.assertEqual(self.ev("2026-09-26T02:50:31Z", None)[0], "FIRE")         # usage unknown: still the reset
        # live usage says the session limit is still on
        self.assertEqual(self.ev("2026-09-26T02:51:00Z",
                                 usage(10, sess_pct=100, sess_reset=Z("2026-09-26T03:30:00Z")))[0], "WAIT_RESET")
        self.assertEqual(self.ev("2026-09-26T02:51:00Z", usage(100))[0], "WAIT_RESET")   # weekly limit on
        # detection only (the watcher's view): no usage fetched, no decision
        self.assertEqual(ka.stall_status(SID, Z("2026-09-26T02:50:00Z"))[0], "WAIT_RESET")
        act, detail, stall = ka.stall_status(SID, Z("2026-09-26T02:50:31Z"))
        self.assertEqual((act, stall["reset"]), ("RESET_PASSED", Z("2026-09-26T02:49:00Z")))

    def test_approved_stall_fires_at_its_reset_outside_the_window(self):
        """D-206: approved stalls continue at their reset at ANY time of day (the windows are only
        for AFClaude)."""
        late = dict(STALL, timestamp="2026-09-26T05:30:00.000Z",
                    message=dict(STALL["message"], content=[{"type": "text",
                                 "text": "You've hit your session limit · resets 10am (UTC)"}]))
        write_transcript(self.tmp.name, SID, [USER, late])
        self.assertEqual(self.ev("2026-09-26T10:01:00Z", usage(10))[0], "WAIT_RESET")
        act, detail = self.ev("2026-09-26T10:01:31Z", usage(90))           # 12:01 Berlin, week at 90%
        self.assertEqual(act, "FIRE")
        self.assertIn("D-206", detail)

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


class BudgetWiring(unittest.TestCase):
    """budget_eval / budget_decision / budget_headroom with "usage_model": "pacing" (the default)."""
    R = Z("2026-10-08T17:00:00Z")                       # Thu 19:00 Berlin
    NIGHT = Z("2026-10-04T21:00:00Z")                   # Sun 23:00 Berlin, a window start

    def setUp(self):
        import pacing as budget
        self.bm = budget
        self.tmp = tempfile.TemporaryDirectory()
        self._old = (budget.SAMPLES_FILE, budget.decide, ka.DEFER_FILE, ka.STATE_FILE, ka.PROGRESS_FILE)
        budget.SAMPLES_FILE = os.path.join(self.tmp.name, "samples.jsonl")
        ka.DEFER_FILE = os.path.join(self.tmp.name, "deferred.json")
        ka.STATE_FILE = os.path.join(self.tmp.name, "state.json")
        ka.PROGRESS_FILE = os.path.join(self.tmp.name, "PROGRESS.md")
        self.setcfg()

    def tearDown(self):
        (self.bm.SAMPLES_FILE, self.bm.decide, ka.DEFER_FILE, ka.STATE_FILE, ka.PROGRESS_FILE) = self._old
        setcfg(usage_model="linear")
        self.tmp.cleanup()

    def setcfg(self, **kw):
        setcfg(**kw)

    def samples(self, now, prompt_minutes_ago=None, week=15.0, own_prompt_minutes_ago=None, minutes=300):
        """Sampler rows every 15 min up to `now` (weekly `week`%); one prompt in a non-AFClaude
        session `prompt_minutes_ago` ago, one in an AFClaude session `own_prompt_minutes_ago` ago."""
        with open(self.bm.SAMPLES_FILE, "w") as fh:
            for m in range(minutes, -1, -15):
                other = {"human_prompts": 1} if m == prompt_minutes_ago else {}
                own = {"human_prompts": 1, "assistant_turns": 3} if m == own_prompt_minutes_ago else {}
                fh.write(json.dumps({"at": (now - timedelta(minutes=m)).isoformat(),
                                     "usage": {"weekly": {"percent": week, "resets_at": self.R.isoformat()}},
                                     "activity": {"own": own, "other": other}}) + "\n")

    def u(self, week=15.0, sess=0.0, sess_reset=None):
        return usage(week, self.R, sess, sess_reset)

    def test_config_switch(self):
        import store
        self.assertEqual(afclaude_config.usage_model(), "pacing")       # default
        self.setcfg(usage_model="linear")
        self.assertEqual(afclaude_config.usage_model(), "linear")
        for v in ("reserve", "budget", "nonsense"):                     # retired names: refused, and a
            with self.assertRaisesRegex(ValueError, "pacing|linear"):    # row written around actions.py
                testenv.set_setting("usage_model", v)                   # is ignored (the default)
            conn = store.connect()
            store.put_setting(conn, "usage_model", v)
            conn.commit()
            conn.close()
            self.assertEqual(afclaude_config.usage_model(), "pacing")

    def test_one_headroom_number_everywhere(self):
        """Requirement 6: reason, budget_headroom, --decide, the continue message, the quickview."""
        now = self.NIGHT + timedelta(minutes=5)
        self.samples(now)
        d = ka.budget_eval(self.u(), now)
        self.assertTrue(d["go"], d["reason"])
        num = f"+{d['headroom']:.1f}"
        self.assertIn(f"budget for this run {num}%", d["reason"])
        extra, text = ka.budget_headroom(self.u(), now)
        self.assertEqual(extra, d["headroom"])
        self.assertIn(f"{num} weekly %", text)
        self.assertIn("stop before exceeding it", text)
        self.assertEqual(ka.budget_decision(self.u(), now), (d["go"], d["reason"]))
        # --decide prints the same reason and text
        import contextlib
        import io
        olds = (ka.fresh_usage, ka.datetime, sys.argv)
        ka.fresh_usage = lambda n, force=False: self.u()

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        ka.datetime = FakeDT
        sys.argv = ["keepalive.py", "--decide"]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                ka.main()
        finally:
            ka.fresh_usage, ka.datetime, sys.argv = olds
        self.assertIn(d["reason"], out.getvalue())
        self.assertIn(d["text"], out.getvalue())
        self.assertIn("one session window left", out.getvalue())          # pacing.threshold_info()
        self.assertIn("model error", out.getvalue())
        # the continue message (window start, dry-run) carries that text
        msgs = []
        oldm, oldp = ka.session_message, ka.preflight
        ka.session_message = lambda name, **kw: msgs.append(kw.get("reason", "")) or "msg"
        ka.preflight = lambda sid: (True, [], "resume")
        oldf, oldr, oldpd = ka.fresh_usage, ka.read_usage_cache, ka.PROJECTS_DIR
        ka.fresh_usage = lambda n, force=False: self.u()
        ka.read_usage_cache = lambda: self.u()
        ka.PROJECTS_DIR = self.tmp.name
        write_transcript(self.tmp.name, SID, [USER, REPLY])
        try:
            ka.window_start_pass(SID, now, Args())
        finally:
            ka.session_message, ka.preflight, ka.fresh_usage = oldm, oldp, oldf
            ka.read_usage_cache, ka.PROJECTS_DIR = oldr, oldpd
        self.assertEqual(len(msgs), 1)
        self.assertIn(d["text"], msgs[0])
        self.assertIn(f"budget for this run {num}%", msgs[0])
        # the quickview
        import export_quickview as eq
        oldq = (ka.read_usage_cache, eq.watcher_running, ka.tmux_alive, eq.limits_block, eq.usage_review)
        ka.read_usage_cache = lambda: self.u()
        eq.watcher_running, ka.tmux_alive = (lambda: False), (lambda sid: False)
        eq.limits_block, eq.usage_review = (lambda: {}), (lambda: None)
        try:
            q = eq.keepalive_and_usage(now)
        finally:
            ka.read_usage_cache, eq.watcher_running, ka.tmux_alive, eq.limits_block, eq.usage_review = oldq
        self.assertNotIn("budget_rule_now", q)                          # D-166: the next run, not the rule
        nr = q["next_run"]
        self.assertEqual(nr["kind"], "now", nr)
        self.assertIn(f"{num}%", nr["reason"])                            # the same number there too

    def test_own_session_prompt_is_not_user_activity(self):
        """Requirement 1: the user's input to an AFClaude session does not hold AFClaude."""
        now = self.NIGHT + timedelta(minutes=5)
        self.samples(now, own_prompt_minutes_ago=15)
        go, why = ka.budget_decision(self.u(), now)
        self.assertTrue(go, why)
        self.samples(now, prompt_minutes_ago=15)                         # a non-AFClaude session
        d = ka.budget_eval(self.u(), now)
        self.assertFalse(d["go"])
        self.assertTrue(d["postpone"])
        self.assertIn("yield", d["reason"])

    def test_fallback_to_linear_on_error(self):
        now = self.NIGHT
        def boom(*a, **k):
            raise RuntimeError("broken model")
        self.bm.decide = boom
        go, why = ka.budget_decision(self.u(20), now)
        self.assertEqual(go, ka.linear_budget_decision(self.u(20), now)[0])
        self.assertIn("linear fallback", why)
        extra, text = ka.budget_headroom(self.u(20), now)
        self.assertEqual(extra, ka.linear_budget_headroom(self.u(20), now)[0])
        self.assertIn(f"+{extra:.1f}%", why)                             # the same number there too
        self.assertFalse(ka.budget_decision(None, now)[0])

    def test_fallback_holds_in_last_mile(self):
        now = self.R - timedelta(hours=2)                                # linear would CONTINUE to 100%
        self.assertTrue(ka.linear_budget_decision(self.u(50), now)[0])
        def boom(*a, **k):
            raise RuntimeError("broken model")
        self.bm.decide = boom
        go, why = ka.budget_decision(self.u(50), now)
        self.assertFalse(go, why)
        self.assertIn("budget model failed", why)
        self.assertEqual(ka.budget_headroom(self.u(50), now)[0], 0.0)

    def test_unknown_usage_and_stale_sampler_postpone(self):
        now = self.NIGHT + timedelta(minutes=5)
        d = ka.budget_eval(None, now)
        self.assertFalse(d["go"])
        self.assertTrue(d["postpone"])
        d = ka.budget_eval(self.u(), now)                                # no samples file
        self.assertFalse(d["go"])
        self.assertIn("unknown", d["reason"])
        self.assertTrue(d["postpone"])
        self.samples(now - timedelta(minutes=45))                        # sampler stopped 45 min ago
        self.assertFalse(ka.budget_decision(self.u(), now)[0])

    def test_last_stretch_yields_and_fires_once_per_slot(self):
        fired = []
        cache = {"fetched_at": self.R, "weekly": {"percent": 70.0, "resets_at": self.R},
                 "session": {"percent": 0.0, "resets_at": None}}
        olds = (ka.read_usage_cache, ka.fresh_usage, ka.handle_fire)
        ka.read_usage_cache = lambda: cache
        ka.fresh_usage = lambda n, force=False: cache
        def hf(sid, stall, reason, st, args):
            fired.append((stall, reason))
            st["handled"][stall["uuid"]] = {"result": "test"}
        ka.handle_fire = hf
        try:
            st = {"handled": {}, "fires": {}}
            # no ratio data: the conservative default 0.2 -> 30 / 20 = 1.5 -> 2 windows = 10 h
            now = self.R - timedelta(hours=10, minutes=1)
            self.samples(now, week=70)
            self.assertIsNone(ka.last_mile_pass(SID, now, st, None))
            self.assertEqual(fired, [])
            now = self.R - timedelta(hours=9)
            self.samples(now, prompt_minutes_ago=0, week=70)              # the user is active: postpone
            self.assertEqual(ka.last_mile_pass(SID, now, st, None), now + timedelta(minutes=60))
            self.assertEqual(fired, [])
            self.samples(now, week=70)                                    # idle: fires slot 2
            self.assertIsNone(ka.last_mile_pass(SID, now, st, None))
            self.assertEqual(len(fired), 1)
            self.assertTrue(fired[0][0]["uuid"].endswith("-s2"), fired[0][0]["uuid"])
            self.assertIn("last stretch", fired[0][1])
            self.assertIn("+30.0 weekly %", fired[0][0]["budget"])
            ka.last_mile_pass(SID, self.R - timedelta(hours=6), st, None)      # same slot: no second fire
            self.assertEqual(len(fired), 1)
            self.samples(self.R - timedelta(hours=4), week=70)
            ka.last_mile_pass(SID, self.R - timedelta(hours=4), st, None)      # the final slot fires again
            self.assertEqual(len(fired), 2)
            self.assertTrue(fired[1][0]["uuid"].endswith("-s1"))
            cache["weekly"]["percent"] = 100.0                                 # exhausted: no last stretch
            self.assertIsNone(ka.last_mile_pass(SID, self.R - timedelta(hours=3), {"handled": {}, "fires": {}},
                                                None))
        finally:
            ka.read_usage_cache, ka.fresh_usage, ka.handle_fire = olds

    def test_approved_stall_continue_has_no_pacing_gate(self):
        """D-206: an approved stall (the owner's session) is continued at its reset even while the
        owner is active, the week is far above the threshold or it is daytime; no budget text."""
        with tempfile.TemporaryDirectory() as d:
            ka.PROJECTS_DIR, old = d, ka.PROJECTS_DIR
            try:
                stall = dict(STALL, timestamp="2026-10-07T09:00:00.000Z",
                             message=dict(STALL["message"], content=[{"type": "text",
                                          "text": "You've hit your session limit · resets 10am (UTC)"}]))
                write_transcript(d, SID, [USER, stall])
                day = Z("2026-10-07T10:05:00Z")                             # Wed 12:05 Berlin
                cache = self.u(90.0)
                self.samples(day, prompt_minutes_ago=0, week=90)            # the owner is active
                action, detail, st = ka.evaluate(SID, day, lambda n: cache)
                self.assertEqual(action, "FIRE", detail)
                self.assertNotIn("budget", st)
            finally:
                ka.PROJECTS_DIR = old

    def test_stalled_task_manager_waits_for_the_slot_start(self):
        """D-204 in the last stretch: the task-manager stopped at a limit; the slot start waits for
        the limit reset, then decides itself (fires once); nothing else continues it."""
        fired = []
        with tempfile.TemporaryDirectory() as d:
            old = (ka.PROJECTS_DIR, ka.read_usage_cache, ka.fresh_usage, ka.handle_fire)
            ka.PROJECTS_DIR = d
            stall = dict(STALL, timestamp="2026-10-08T07:30:00.000Z",
                         message=dict(STALL["message"], content=[{"type": "text",
                                      "text": "You've hit your session limit · resets 9am (UTC)"}]))
            write_transcript(d, SID, [USER, stall])
            cache = {"fetched_at": self.R, "weekly": {"percent": 70.0, "resets_at": self.R},
                     "session": {"percent": 0.0, "resets_at": None}}
            ka.read_usage_cache = lambda: cache
            ka.fresh_usage = lambda n, force=False: cache

            def hf(sid, stall, reason, st, args):
                fired.append(stall["uuid"])
                st["handled"][stall["uuid"]] = {"result": "test"}
            ka.handle_fire = hf
            try:
                st = {"handled": {}, "fires": {}}
                now = self.R - timedelta(hours=9, minutes=30)               # slot 2, before the 09:00Z reset
                self.samples(now, week=70)
                self.assertEqual(ka.last_mile_pass(SID, now, st, None), Z("2026-10-08T09:00:00Z") + ka.RESET_GRACE)
                self.assertEqual(fired, [])
                self.assertIsNone(ka.last_mile_next_slot(now, st))          # the slot start is still due
                now = Z("2026-10-08T09:02:00Z")
                self.samples(now, week=70)
                self.assertIsNone(ka.last_mile_pass(SID, now, st, None))
                self.assertEqual(len(fired), 1)
                self.assertTrue(fired[0].endswith("-s2"))
                # slot 2 ran: the next run is the slot-1 start, the final one after the reset
                self.assertEqual(ka.last_mile_next_slot(now, st), self.R - timedelta(hours=5))
                ka.last_mile_pass(SID, now + timedelta(hours=1), st, None)
                self.assertEqual(len(fired), 1)
                self.assertIsNone(ka.last_mile_next_slot(self.R - timedelta(hours=4), st))
                self.samples(self.R - timedelta(hours=4), week=70)
                ka.last_mile_pass(SID, self.R - timedelta(hours=4), st, None)
                self.assertEqual(ka.last_mile_next_slot(self.R - timedelta(hours=4), st), False)
            finally:
                ka.PROJECTS_DIR, ka.read_usage_cache, ka.fresh_usage, ka.handle_fire = old

    def test_postponed_window_start_fires_60_min_after_the_last_activity(self):
        """Requirement 2: the window-start HOLD for an active user is deferred within the window
        and fires at last activity + 60 min, under the same once-per-night key."""
        fired = []
        start = self.NIGHT + timedelta(minutes=2)                           # the 23:00 cron run
        activity = self.NIGHT - timedelta(minutes=10)                       # the user at 22:50
        olds = (ka.fresh_usage, ka.handle_fire, ka.read_usage_cache)
        ka.fresh_usage = lambda n, force=False: self.u()
        ka.read_usage_cache = lambda: self.u()
        def hf(sid, stall, reason, st, args):
            fired.append((stall, reason))
            st["handled"][stall["uuid"]] = {"result": "test", "at": "x"}
        ka.handle_fire = hf
        try:
            def rows_until(now):
                with open(self.bm.SAMPLES_FILE, "w") as fh:
                    t = activity - timedelta(hours=2)
                    while t <= now:
                        other = {"human_prompts": 2} if t == activity else {}
                        fh.write(json.dumps({"at": t.isoformat(), "activity": {"own": {}, "other": other},
                                             "usage": {"weekly": {"percent": 30.0,
                                                                  "resets_at": self.R.isoformat()}}}) + "\n")
                        t += timedelta(minutes=5)
            rows_until(start)
            d = ka.window_start_pass(SID, start, Args())
            self.assertTrue(d["postpone"], d["reason"])
            self.assertEqual(fired, [])
            key = schedule.start_key(start)
            ent = ka.load_deferred()[key]
            self.assertEqual(ka.parse_ts(ent["recheck_at"]), activity + timedelta(minutes=60))
            st = {"handled": {}, "fires": {}}
            t = start + timedelta(minutes=20)                               # 23:22: still waiting
            rows_until(t)
            self.assertEqual(ka.deferred_window_start_pass(SID, t, st, Args()), activity + timedelta(minutes=60))
            self.assertEqual(fired, [])
            t = activity + timedelta(minutes=60)                            # 23:50: fires
            rows_until(t)
            self.assertIsNone(ka.deferred_window_start_pass(SID, t, st, Args()))
            self.assertEqual(len(fired), 1)
            self.assertEqual(fired[0][0]["uuid"], key)                       # the same once-per-night key
            self.assertIn("postponed", fired[0][1])
            self.assertEqual(ka.load_deferred(), {})
            self.assertIsNone(ka.deferred_window_start_pass(SID, t + timedelta(minutes=5), st, Args()))
            self.assertEqual(len(fired), 1)
        finally:
            ka.fresh_usage, ka.handle_fire, ka.read_usage_cache = olds

    def test_postponed_window_start_dropped_after_the_window(self):
        start = self.NIGHT + timedelta(minutes=2)
        ka.defer_window_start(schedule.start_key(start), SID, start + timedelta(minutes=30), "r")
        fired = []
        oldh = ka.handle_fire
        ka.handle_fire = lambda *a: fired.append(a)
        try:
            later = Z("2026-10-05T08:00:00Z")                                # Mon 10:00, the window is over
            self.assertIsNone(ka.deferred_window_start_pass(SID, later, {"handled": {}, "fires": {}}, Args()))
        finally:
            ka.handle_fire = oldh
        self.assertEqual(fired, [])
        self.assertEqual(ka.load_deferred(), {})

    # ---- review fix: a DB pause at a window start must be postponed, not silently dropped
    # (nothing else makes a --window-start cron run due again before the next session-window
    # start, unlike a stall or last-mile-slot continue, which the watcher re-decides every tick)

    def test_window_start_db_pause_is_postponed_like_a_budget_hold(self):
        start = self.NIGHT + timedelta(seconds=5)
        calls = []

        def hf(sid, stall, reason, st, args):
            calls.append(stall["uuid"])
            return "db_paused"
        olds = (ka.budget_eval, ka.fresh_usage, ka.handle_fire)
        ka.budget_eval = lambda u, now: {"go": True, "postpone": False, "recheck_at": None,
                                         "reason": "CONTINUE: test", "text": "budget t"}
        ka.fresh_usage = lambda n, force=False: self.u()
        ka.handle_fire = hf
        try:
            ka.window_start_pass(SID, start, Args())
            self.assertEqual(calls, [schedule.start_key(start)])
            ent = ka.load_deferred()[schedule.start_key(start)]
            self.assertEqual(ka.parse_ts(ent["recheck_at"]), start + ka.DB_PAUSE_RECHECK)
            self.assertIn("database problem", self.progress())
        finally:
            ka.budget_eval, ka.fresh_usage, ka.handle_fire = olds

    def test_window_start_db_pause_at_the_last_start_is_not_retried(self):
        """D-202: the night's last session-window start is never postponed past the window end
        -- a DB pause there is reported, not deferred (there is no later start to catch it)."""
        start = self.S2 + timedelta(seconds=5)
        olds = (ka.budget_eval, ka.fresh_usage, ka.handle_fire)
        ka.budget_eval = lambda u, now: {"go": True, "postpone": False, "recheck_at": None,
                                         "reason": "CONTINUE: test", "text": "budget t"}
        ka.fresh_usage = lambda n, force=False: self.u()
        ka.handle_fire = lambda sid, stall, reason, st, args: "db_paused"
        try:
            ka.window_start_pass(SID, start, Args())
            self.assertEqual(ka.load_deferred(), {})
            self.assertIn("not retried before the window ends", self.progress())
        finally:
            ka.budget_eval, ka.fresh_usage, ka.handle_fire = olds

    def test_deferred_window_start_db_pause_is_retried_not_dropped(self):
        """The watcher's postponed-start retry (deferred_window_start_pass) must not lose the
        start when the fire itself hits a DB pause: it must keep (re-postpone) the deferred
        entry instead of popping it before knowing whether handle_fire actually fired."""
        start = self.NIGHT + timedelta(seconds=5)
        key = schedule.start_key(start)
        ka.defer_window_start(key, SID, start, "r", self.S2)
        st = {"handled": {}, "fires": {}}
        calls = []

        def hf(sid, stall, reason, st, args):
            calls.append(stall["uuid"])
            if len(calls) == 1:
                return "db_paused"
            st["handled"][stall["uuid"]] = {"result": "test"}
            return None
        olds = (ka.budget_eval, ka.fresh_usage, ka.handle_fire)
        ka.budget_eval = lambda u, now: {"go": True, "postpone": False, "recheck_at": None,
                                         "reason": "CONTINUE: test", "text": "budget t"}
        ka.fresh_usage = lambda n, force=False: self.u()
        ka.handle_fire = hf
        try:
            nxt = ka.deferred_window_start_pass(SID, start, st, Args())
            self.assertEqual(nxt, start + ka.DB_PAUSE_RECHECK)
            self.assertEqual(ka.parse_ts(ka.load_deferred()[key]["recheck_at"]), nxt)
            self.assertEqual(len(calls), 1)
            # the database is healthy again at the watcher's next recheck: same key fires
            self.assertIsNone(ka.deferred_window_start_pass(SID, nxt, st, Args()))
            self.assertEqual(len(calls), 2)
            self.assertEqual(ka.load_deferred(), {})
            self.assertIn(key, st["handled"])
        finally:
            ka.budget_eval, ka.fresh_usage, ka.handle_fire = olds

    # ---- D-202: the gate at each session-window start (23:00, 04:00), postponed starts end by 09:00

    S2 = Z("2026-10-05T02:00:00Z")                      # Mon 04:00 Berlin, the night's second start

    def _patched(self, decisions, fired):
        """budget_eval returns decisions[i] in turn (a postpone to `recheck_at`, or a go);
        handle_fire records the key and marks it handled."""
        it = iter(decisions)

        def be(u, now):
            r = next(it)
            if r == "go":
                return {"go": True, "postpone": False, "recheck_at": None, "reason": "CONTINUE: test",
                        "text": "budget t"}
            return {"go": False, "postpone": True, "recheck_at": r, "text": "budget t",
                    "reason": f"HOLD: user active 10 min ago (yield); postponed to {ka.berlin(r)}"}

        def hf(sid, stall, reason, st, args):          # the real dedup: a handled key is skipped
            if stall["uuid"] in st["handled"]:
                return
            fired.append(stall["uuid"])
            st["handled"][stall["uuid"]] = {"result": "test", "at": "x"}
            ka.save_state(st)
        olds = (ka.budget_eval, ka.fresh_usage, ka.handle_fire)
        ka.budget_eval, ka.fresh_usage, ka.handle_fire = be, (lambda n, force=False: self.u()), hf
        return olds

    def _restore(self, olds):
        ka.budget_eval, ka.fresh_usage, ka.handle_fire = olds

    def progress(self):
        try:
            with open(ka.PROGRESS_FILE) as fh:
                return fh.read()
        except OSError:
            return ""

    def test_postponed_start_runs_only_until_the_next_session_window_start(self):
        """A 23:00 start postponed to 01:30 is deferred (deadline 04:00, the next start); one
        postponed to 04:10 is skipped (the 04:00 start decides itself); the last start (04:00)
        is never postponed: its run would end after 09:00 (D-202)."""
        fired = []
        start = self.NIGHT + timedelta(seconds=5)
        olds = self._patched([start + timedelta(hours=2, minutes=30),          # 01:30: deferred
                              self.S2 + timedelta(minutes=10),                  # 04:10: skipped
                              self.S2 + timedelta(minutes=40)], fired)          # at 04:00 -> 04:40: skipped
        try:
            ka.window_start_pass(SID, start, Args())
            ent = ka.load_deferred()[schedule.start_key(start)]
            self.assertEqual(ka.parse_ts(ent["deadline"]), self.S2)
            self.assertEqual(ka.parse_ts(ent["recheck_at"]), start + timedelta(hours=2, minutes=30))
            os.remove(ka.DEFER_FILE)
            ka.window_start_pass(SID, start, Args())
            self.assertEqual(ka.load_deferred(), {})
            self.assertIn("skipped to the next session-window start 2026-10-05 04:00:00", self.progress())
            ka.window_start_pass(SID, self.S2 + timedelta(seconds=5), Args())
            self.assertEqual(ka.load_deferred(), {})
            self.assertIn("skipped to the next session-window start 2026-10-05 23:00:00", self.progress())
            self.assertEqual(fired, [])
        finally:
            self._restore(olds)

    def test_deferred_start_dropped_at_the_next_session_window_start(self):
        """The watcher's recheck: re-postponed past 04:00 -> dropped; reached only after 04:00
        (the next start's key) -> dropped as stale, never fired late."""
        fired, st = [], {"handled": {}, "fires": {}}
        start = self.NIGHT + timedelta(seconds=5)
        key = schedule.start_key(start)
        olds = self._patched([self.S2 + timedelta(minutes=20)], fired)         # 03:00 -> 04:20
        try:
            ka.defer_window_start(key, SID, self.S2 - timedelta(hours=1), "r", self.S2)
            self.assertIsNone(ka.deferred_window_start_pass(SID, self.S2 - timedelta(hours=1), st, Args()))
            self.assertEqual((ka.load_deferred(), fired), ({}, []))
            self.assertIn("postponed past the night's next session-window start", self.progress())
            ka.defer_window_start(key, SID, self.S2 - timedelta(minutes=30), "r", self.S2)
            self.assertIsNone(ka.deferred_window_start_pass(SID, self.S2 + timedelta(minutes=5), st, Args()))
            self.assertEqual((ka.load_deferred(), fired), ({}, []))
        finally:
            self._restore(olds)

    def test_second_session_window_start_fires_under_its_own_key(self):
        """The 04:00 check is deduplicated on its own (the 23:00 key does not block it, a second
        04:xx run does not fire again), and the 23:00 check of the next night is new."""
        fired = []
        olds = self._patched(["go"] * 4, fired)
        old_rec = self.bm.record_forecast
        self.bm.record_forecast = lambda *a, **k: None
        try:
            for t in (self.NIGHT, self.S2, self.S2 + timedelta(minutes=30), self.NIGHT + timedelta(days=1)):
                ka.window_start_pass(SID, t + timedelta(seconds=5), Args())
            self.assertEqual(fired, ["window-start-2026-10-05", "window-start-2026-10-05-s2",
                                     "window-start-2026-10-06"])
        finally:
            self.bm.record_forecast = old_rec
            self._restore(olds)

    def test_run_still_going_at_the_second_start_is_not_started_twice(self):
        """A run from 23:00 still going at 04:00 (live in our tmux ka-<id8>): the 04:00 check
        only types into that same session (plan send-keys, no new process, no --new) and the key
        dedups a second 04:xx run."""
        calls = []
        olds = (ka.budget_eval, ka.fresh_usage, ka.tmux_alive, ka.tmux_pids, ka.registry_holders,
                ka.fire, ka.verify_reply, ka.agent_entries, ka.PROJECTS_DIR, self.bm.record_forecast)
        ka.budget_eval = lambda u, now: {"go": True, "postpone": False, "recheck_at": None,
                                         "reason": "CONTINUE: test", "text": "budget t"}
        ka.fresh_usage = lambda n, force=False: self.u()
        ka.tmux_alive, ka.tmux_pids, ka.registry_holders = (lambda s: True), (lambda s: {300}), (lambda s: [])
        ka.fire = lambda sid, cwd, msg, plan, new=False, **k: calls.append((plan, new)) or (0, "sent-keys", "")
        ka.verify_reply = lambda path, since, timeout=None: {"message": {"model": "claude-opus-5-5"}}
        ka.agent_entries = lambda s: []
        ka.PROJECTS_DIR = self.tmp.name
        self.bm.record_forecast = lambda *a, **k: None
        write_transcript(self.tmp.name, SID, [USER, REPLY])

        class Armed(Args):
            arm = True
        try:
            ka.save_state({"handled": {schedule.start_key(self.NIGHT): {"result": "continued-in-place"}},
                           "fires": {}})
            ka.window_start_pass(SID, self.S2 + timedelta(seconds=5), Armed())
            ka.window_start_pass(SID, self.S2 + timedelta(minutes=30), Armed())
        finally:
            (ka.budget_eval, ka.fresh_usage, ka.tmux_alive, ka.tmux_pids, ka.registry_holders,
             ka.fire, ka.verify_reply, ka.agent_entries, ka.PROJECTS_DIR, self.bm.record_forecast) = olds
        self.assertEqual(calls, [("send-keys", False)])
        self.assertEqual(ka.load_state()["handled"]["window-start-2026-10-05-s2"]["plan"], "send-keys")

    def test_run_active_and_pending_deferral(self):
        """The quickview's runner state: a real fire < 5 h ago with a transcript written in the
        last 15 min is a run going; a dry-run, an old fire or an idle transcript is not."""
        now = self.S2 + timedelta(hours=4, minutes=18)                     # Mon 08:18 Berlin
        old = ka.PROJECTS_DIR
        ka.PROJECTS_DIR = self.tmp.name
        try:
            write_transcript(self.tmp.name, SID, [USER, REPLY])
            path = ka.transcript_path(SID)
            fresh = now.timestamp() - 60
            os.utime(path, (fresh, fresh))
            st = {"handled": {"k": {"result": "continued-in-place", "at": (now - timedelta(hours=1)).isoformat()}}}
            self.assertEqual(ka.run_active(SID, now, st), now - timedelta(hours=1))
            st["handled"]["k"]["at"] = (now - timedelta(hours=6)).isoformat()   # the 23:00... fire: over
            self.assertIsNone(ka.run_active(SID, now, st))
            st["handled"]["k"].update(at=(now - timedelta(hours=1)).isoformat(), result="dry-run")
            self.assertIsNone(ka.run_active(SID, now, st))
            st["handled"]["k"]["result"] = "continued-in-place"
            idle = now.timestamp() - 3600
            os.utime(path, (idle, idle))
            self.assertIsNone(ka.run_active(SID, now, st))                  # finished: idle for an hour
            sub = os.path.join(path[:-len(".jsonl")], "subagents")
            os.makedirs(sub)
            with open(os.path.join(sub, "agent-1.jsonl"), "w") as fh:
                fh.write("{}\n")
            os.utime(os.path.join(sub, "agent-1.jsonl"), (fresh, fresh))
            self.assertIsNotNone(ka.run_active(SID, now, st))              # a subagent is working
            write_transcript(self.tmp.name, SID, [USER, REPLY, STALL])      # D-204: the limit ended the run
            os.utime(path, (fresh, fresh))
            self.assertIsNone(ka.run_active(SID, now, st))
        finally:
            ka.PROJECTS_DIR = old
        start = self.NIGHT + timedelta(seconds=5)
        ka.defer_window_start(schedule.start_key(start), SID, start + timedelta(hours=1), "r", self.S2)
        self.assertEqual(ka.pending_deferral(SID, start + timedelta(minutes=30)), start + timedelta(hours=1))
        self.assertIsNone(ka.pending_deferral("other", start + timedelta(minutes=30)))
        self.assertIsNone(ka.pending_deferral(SID, self.S2 + timedelta(minutes=1)))   # the next start's key


class Args:
    arm = False
    now = False


class LastMile(unittest.TestCase):
    R = Z("2026-10-01T16:59:59Z")

    def setUp(self):
        self.ac = afclaude_config
        self.setcfg()

    def tearDown(self):
        setcfg(usage_model="linear")

    def setcfg(self, **kw):
        setcfg(usage_model="linear", **kw)

    def test_default_is_auto(self):
        self.assertEqual(self.ac.last_mile_setting(), "auto")
        # no ratio data -> the conservative default 0.2: 95% -> 0.25 windows -> one session length
        self.assertEqual(ka.last_mile_hours(95), 5.0)
        self.assertEqual(ka.last_mile_hours(70), 10.0)                 # 1.5 -> 2 windows
        self.assertEqual(ka.last_mile_hours(None), 5.0)                # unknown %: one session length
        self.setcfg(last_mile_hours=3)
        self.assertEqual(self.ac.last_mile_setting(), 3.0)
        self.assertEqual(ka.last_mile_hours(10), 3.0)
        self.setcfg(last_mile_hours=0)
        self.assertEqual(ka.last_mile_hours(10), 0.0)

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
            cache["weekly"]["percent"] = 100.0        # exhausted -> no last mile left
            st2 = {"handled": {}, "fires": {}}
            self.assertIsNone(ka.last_mile_pass(SID, self.R - timedelta(hours=2), st2, None))
            cache["weekly"]["percent"] = 95.0
            fails = []
            olde = ka.budget_eval
            ka.budget_eval = lambda u, n: fails.append(1) or {"go": False, "reason": "HOLD: x", "postpone": False}
            try:                                      # a HOLD in the last mile -> re-check later
                nxt = ka.last_mile_pass(SID, self.R - timedelta(hours=2), st2, None)
            finally:
                ka.budget_eval = olde
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
            self.assertEqual(fired[0]["uuid"], "last-mile-2026-10-01T17:00:00+00:00-s1")
            # raw (un-rounded) keys of the same slot already in the state count as handled
            fired.clear()
            st = {"handled": {"last-mile-2026-10-01T17:00:00.320910+00:00-s1": {},
                              "last-mile-2026-10-01T16:59:59.557562+00:00-s1": {}}, "fires": {}}
            for r in (r1, r2):
                cache["weekly"]["resets_at"] = r
                ka.last_mile_pass(SID, r1 - timedelta(hours=2), st, None)
            self.assertEqual(fired, [])
            # a different weekly cycle is not handled by them
            self.assertFalse(ka.last_mile_handled(ka.last_mile_key(r1 + timedelta(days=7), 1), st["handled"]))
            self.assertFalse(ka.last_mile_handled(ka.last_mile_key(r1, 2), st["handled"]))   # another slot
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

    def test_latest_entrypoint_real(self):
        # the manager (tmux, old sdk-cli entries from a former --bg start) vs an rc thread
        for sid, want in (("f2897285-dd97-49d9-b29a-2334b4753dee", "cli"),
                          ("9da84efe-c1ec-516d-9a86-4ecca2aec1f3", "sdk-cli")):
            p = ka.transcript_path(sid)
            if not p:
                continue
            self.assertEqual(ka.latest_entrypoint(p), want, sid)

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
        self.proj = os.path.join(self.tmp.name, "projects", "-x")
        os.makedirs(self.proj)
        self.orig = (ka.PROC_DIR, ka.SESSIONS_DIR, ka.tmux_alive, ka.tmux_pids, ka.agent_entries, ka.TAKE_OVER_IDLE,
                     ka.PROJECTS_DIR)
        ka.PROC_DIR, ka.SESSIONS_DIR = self.proc, self.sess
        ka.PROJECTS_DIR = os.path.dirname(self.proj)
        self.tmux, self.panes, self.rows = False, set(), []
        ka.tmux_alive = lambda s: self.tmux
        ka.tmux_pids = lambda s: set(self.panes)
        ka.agent_entries = lambda s: list(self.rows)
        ka.TAKE_OVER_IDLE = False

    def tearDown(self):
        (ka.PROC_DIR, ka.SESSIONS_DIR, ka.tmux_alive, ka.tmux_pids, ka.agent_entries, ka.TAKE_OVER_IDLE,
         ka.PROJECTS_DIR) = self.orig
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

    def transcript(self, *entrypoints):
        with open(os.path.join(self.proj, self.SID + ".jsonl"), "w") as fh:
            for i, ep in enumerate(entrypoints):
                fh.write(json.dumps({"type": "user" if i % 2 == 0 else "assistant", "entrypoint": ep,
                                     "uuid": f"u{i}", "message": {"content": "x"}}) + "\n")
            fh.write(json.dumps({"type": "system", "content": "meta"}) + "\n")

    def test_idle_rc_thread_refused(self):
        # nothing alive, but the latest turn came from an rc-server/SDK host -> refuse
        self.transcript("cli", "cli", "sdk-cli", "sdk-cli")
        ok, problems, plan = ka.preflight(self.SID)
        self.assertEqual((ok, plan), (False, None))
        self.assertTrue(problems[0].startswith(ka.RC_HELD))
        self.assertIn("rc-server/SDK-hosted thread (idle", problems[0])

    def test_old_sdk_cli_entries_do_not_count(self):
        # e.g. the manager's own transcript: old sdk-cli turns from a former --bg start
        self.transcript("sdk-cli", "sdk-cli", "cli", "cli")
        self.assertEqual(ka.preflight(self.SID), (True, [], "resume"))

    def test_sdk_cli_latest_with_plain_interactive_holder(self):
        # a plain interactive holder is alive: the normal holder rules apply
        self.transcript("sdk-cli", "sdk-cli")
        self.process(107, ["claude", "--resume", self.SID])
        self.registry(107)
        self.rows = [{"sessionId": self.SID, "pid": 107, "kind": "interactive", "status": "idle"}]
        self.assertFalse(ka.preflight(self.SID)[0])
        ka.TAKE_OVER_IDLE = True
        self.assertEqual(ka.preflight(self.SID), (True, [], "take-over:107"))

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


class WatcherNoContinue(unittest.TestCase):
    """D-204: the watcher (run loop) never continues the task-manager at a limit reset; it logs
    the limit hit once (PROGRESS note), and the start passes (last-stretch slot, postponed
    session-window start) still run while the session is stalled."""
    def test_limit_hit_is_logged_never_continued(self):
        with tempfile.TemporaryDirectory() as d:
            write_transcript(d, SID, [USER, REPLY, STALL] + TRAILING)     # reset long passed
            names = ("PROJECTS_DIR", "STATE_FILE", "LOCK_FILE", "STOP_FILE", "PROGRESS_FILE", "DEFER_FILE",
                     "handle_fire", "fire", "fresh_usage", "read_usage_cache", "last_mile_pass",
                     "deferred_window_start_pass", "log")
            old = {n: getattr(ka, n) for n in names}
            calls, logs = [], []
            ka.PROJECTS_DIR = d
            ka.STATE_FILE, ka.LOCK_FILE = os.path.join(d, "st.json"), os.path.join(d, "ka.lock")
            ka.STOP_FILE, ka.PROGRESS_FILE = os.path.join(d, "STOP"), os.path.join(d, "P.md")
            ka.DEFER_FILE = os.path.join(d, "deferred.json")
            ka.handle_fire = lambda *a, **k: calls.append("handle_fire")
            ka.fire = lambda *a, **k: calls.append("fire") or (0, "", "")
            ka.fresh_usage = lambda n, force=False: calls.append("usage")
            ka.read_usage_cache = lambda: None
            ka.last_mile_pass = lambda sid, now, st, args: calls.append("last_mile_pass")
            ka.deferred_window_start_pass = lambda sid, now, st, args: calls.append("deferred")
            ka.log = logs.append

            class A:
                session, arm, once, now = SID, True, True, False
            try:
                ka.run(A())
                ka.run(A())                                                # same stall: noted once
            finally:
                for n, v in old.items():
                    setattr(ka, n, v)
            self.assertEqual(calls, ["last_mile_pass", "deferred"] * 2)     # never handle_fire / fire
            self.assertTrue(any(ln.startswith("LIMIT_HIT:") and "D-204" in ln for ln in logs), logs)
            with open(os.path.join(d, "P.md")) as fh:
                notes = fh.read()
            self.assertEqual(notes.count("hit its session limit"), 1)
            self.assertIn("no continue at the limit reset (D-204)", notes)
            with open(os.path.join(d, "st.json")) as fh:
                st = json.load(fh)
            self.assertEqual(list(st["limit_hits"]), ["stall-1"])
            self.assertEqual(st["handled"], {})


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
