#!/usr/bin/env python3
"""Offline tests for the expired-login detection (D-320): the detector on simulated `claude -p` output
(JSON, stream-json, plain text; nothing is run), the state it keeps, and every caller that reacts to it:
the usage read (fail-stop with the plain login subject, the throttle, the self-clearing), the Haiku
usage judgement, the inbox triage, the rc-continue sender, the push notification and the quickview's
auth block. No claude calls, no real ~/.claude.json, no live data/ files."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import afclaude_config  # noqa: E402
import claude_auth  # noqa: E402
import export_quickview as qv  # noqa: E402
import failstop  # noqa: E402
import host  # noqa: E402
import inbox  # noqa: E402
import keepalive as ka  # noqa: E402
import rc_continue  # noqa: E402
import usage_sampler as us  # noqa: E402
from test_dispatcher import sender_out  # noqa: E402
from test_usage_stale import Patch  # noqa: E402

UTC = timezone.utc
# what the 02.11.2026 14:15 token-refresh request printed (data/sampler.log kept the first 60 characters)
EXPIRED_RESULT = "Failed to authenticate: OAuth session expired and could not be refreshed"
EXPIRED_JSON = json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": EXPIRED_RESULT,
                           "total_cost_usd": 0, "usage": {}})
NOT_LOGGED_IN = "Not logged in · Please run /login"
INVALID_KEY = "API Error: 401 Invalid API key · Please run /login"
OK_JSON = json.dumps({"type": "result", "is_error": False, "result": "OK"})
NOT_AUTH = (   # failed calls that are no login problem
    ("Failed to refresh OAuth token: another Claude Code process is refreshing it or exited mid-refresh. This is "
     "usually transient; retry in a minute, and if it persists close other Claude Code processes or sign in again"),
    "OAuth access token could not be refreshed: the disk that holds the Claude Code config directory is full",
    "You've hit your session limit · resets 6pm (Europe/Berlin)",
    "API Error: 529 overloaded_error",
)


def result_json(text):
    return json.dumps({"type": "result", "is_error": True, "result": text})


class Detect(unittest.TestCase):
    def test_the_json_result_of_the_incident(self):
        self.assertEqual(claude_auth.detect(1, EXPIRED_JSON, ""), EXPIRED_RESULT)

    def test_plain_text_on_either_stream_of_a_failed_call(self):
        self.assertEqual(claude_auth.detect(1, NOT_LOGGED_IN + "\n", ""), NOT_LOGGED_IN)
        self.assertEqual(claude_auth.detect(1, "", INVALID_KEY), INVALID_KEY)
        self.assertEqual(claude_auth.detect(1, "", "Your session has expired. Please run /login to sign in again."),
                         "Your session has expired. Please run /login to sign in again.")
        self.assertIn("Authentication required", claude_auth.detect(1, "", "Authentication required · Sign in again"))

    def test_stream_json_events(self):
        evs = [{"type": "system", "subtype": "init"},
               {"type": "assistant", "error": "authentication_failed",
                "message": {"content": [{"type": "text", "text": EXPIRED_RESULT}]}},
               {"type": "result", "is_error": True, "result": EXPIRED_RESULT}]
        out = "\n".join(map(json.dumps, evs))
        self.assertEqual(claude_auth.detect(1, out, ""), EXPIRED_RESULT)
        # an event that only carries the error code (no text) still counts
        self.assertEqual(claude_auth.detect(1, json.dumps({"type": "assistant", "error": "authentication_failed"}), ""),
                         "authentication_failed")
        self.assertEqual(claude_auth.detect(1, json.dumps([{"type": "result", "is_error": True, "result": INVALID_KEY}]), ""),
                         INVALID_KEY)

    def test_an_error_result_with_exit_code_zero_counts(self):
        self.assertEqual(claude_auth.detect(0, EXPIRED_JSON, ""), EXPIRED_RESULT)

    def test_other_failures_are_not_a_login_problem(self):
        for text in NOT_AUTH:
            self.assertIsNone(claude_auth.detect(1, result_json(text), ""), text[:40])
            self.assertIsNone(claude_auth.detect(1, "", text), text[:40])
        self.assertIsNone(claude_auth.detect(1, "", ""))
        self.assertIsNone(claude_auth.detect(1, "not json at all", "boom"))

    def test_unrelated_authentication_and_session_failures_are_not_the_login(self):
        # an MCP server's own auth problem (the call timed out, rc=124), a Remote Control session, a stray /login
        for text in ("MCP server foo: Authentication required", "Remote Control session expired",
                     "MCP server foo: authentication failed", "Not logged in to the registry"):
            self.assertIsNone(claude_auth.detect(124, "", text), text)
            self.assertIsNone(claude_auth.detect(1, result_json(text), ""), text)
        # stderr is searched only when there is no JSON result at all
        self.assertIsNone(claude_auth.detect(1, result_json("API Error: 529 overloaded_error"), INVALID_KEY))
        self.assertIsNone(claude_auth.detect(1, OK_JSON, NOT_LOGGED_IN))

    def test_succeeded_needs_a_clean_result_and_exit_zero(self):
        self.assertTrue(claude_auth.succeeded(0, OK_JSON))
        self.assertFalse(claude_auth.succeeded(1, OK_JSON))
        self.assertFalse(claude_auth.succeeded(0, EXPIRED_JSON))
        self.assertFalse(claude_auth.succeeded(0, result_json("API Error: 529 overloaded_error")))
        self.assertFalse(claude_auth.succeeded(0, "not json"))
        self.assertFalse(claude_auth.succeeded(0, ""))

    def test_a_successful_answer_is_never_searched(self):
        talk = json.dumps({"type": "result", "is_error": False, "result": "If it says Not logged in, please run /login."})
        self.assertIsNone(claude_auth.detect(0, talk, ""))
        self.assertIsNone(claude_auth.detect(0, OK_JSON, "Not logged in noise on stderr"))

    def test_local_usage_output_counts_whatever_the_exit_code(self):
        cost = "Total cost:            $0.0000\nTotal duration (API):  0s\nUsage: 0 input, 0 output"
        self.assertIsNone(claude_auth.detect(0, cost, "", local=True))
        self.assertEqual(claude_auth.detect(0, NOT_LOGGED_IN, "", local=True), NOT_LOGGED_IN)

    def test_the_found_text_is_one_short_line(self):
        long = result_json("Failed to authenticate: " + "x " * 400)
        got = claude_auth.detect(1, long, "")
        self.assertLessEqual(len(got), claude_auth.FOUND_MAX)
        self.assertNotIn("\n", got)

    def test_the_wording(self):
        self.assertIn("claude", claude_auth.SUBJECT)
        self.assertIn("/login", claude_auth.SUBJECT)
        self.assertIn("host", claude_auth.SUBJECT)
        self.assertIn("Remote Control", claude_auth.SUBJECT)
        self.assertIn(EXPIRED_RESULT, claude_auth.failure_detail(EXPIRED_RESULT))


class StateBase(unittest.TestCase):
    def setUp(self):
        self.p = Patch(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p(ka, "USAGE_STATE_FILE", os.path.join(self.tmp.name, "usage_state.json"))
        self.addCleanup(testenv.unpause)


class State(StateBase):
    def test_expired_keeps_its_start_and_ok_clears_it(self):
        t0 = datetime(2026, 11, 2, 13, 15, tzinfo=UTC)
        self.assertEqual(claude_auth.state(ka.USAGE_STATE_FILE), {})
        self.assertFalse(claude_auth.record_ok("x", ka.USAGE_STATE_FILE))          # nothing to clear
        claude_auth.record_expired("usage read", "found 1", ka.USAGE_STATE_FILE, t0)
        st = claude_auth.record_expired("inbox triage", "found 2", ka.USAGE_STATE_FILE, t0 + timedelta(minutes=15))
        self.assertEqual((st["expired"], st["since"], st["source"], st["found"]),
                         (True, t0.isoformat(), "inbox triage", "found 2"))
        self.assertTrue(claude_auth.is_expired(ka.USAGE_STATE_FILE))
        self.assertTrue(claude_auth.record_ok("Haiku", ka.USAGE_STATE_FILE, t0 + timedelta(minutes=20)))
        st = claude_auth.state(ka.USAGE_STATE_FILE)
        self.assertEqual((st["expired"], st["source"]), (False, "Haiku"))
        self.assertFalse(claude_auth.is_expired(ka.USAGE_STATE_FILE))
        # a new episode starts a new `since`
        st = claude_auth.record_expired("usage read", "again", ka.USAGE_STATE_FILE, t0 + timedelta(hours=1))
        self.assertEqual(st["since"], (t0 + timedelta(hours=1)).isoformat())

    def test_it_lives_beside_the_poke_time_in_the_usage_refresh_state(self):
        ka._save_usage_state({"poke_at": "2026-11-02T13:00:00+00:00"})
        claude_auth.record_expired("usage read", "f", ka.USAGE_STATE_FILE)
        doc = ka._load_usage_state()
        self.assertEqual(doc["poke_at"], "2026-11-02T13:00:00+00:00")
        self.assertTrue(doc["auth"]["expired"])


class UsageRead(StateBase):
    """The usage read when the token-refresh request is refused for an expired login (the 02.11. incident)."""
    def setUp(self):
        super().setUp()
        cj = os.path.join(self.tmp.name, "claude.json")                  # never the real one
        with open(cj, "w") as fh:
            json.dump({"cachedUsageUtilization": {"utilization": {"limits": []}}}, fh)
        self.p(ka, "CLAUDE_JSON", cj)
        self.lines = []
        self.p(ka, "log", self.lines.append)
        self.alerts = []
        self.p(ka, "alert", lambda subject, body="": self.alerts.append((subject, body)))
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.cache = {"fetched_at": self.now - timedelta(hours=1),
                      "weekly": {"percent": 29.0, "resets_at": self.now + timedelta(days=3)}}
        self.p(ka, "read_usage_cache", lambda: dict(self.cache))
        self.login_ok = False       # True: the poke and /usage work again
        self.pokes = 0

        def refresh(cwd=ka.HERE):
            if self.login_ok:
                self.cache["fetched_at"] = datetime.now(UTC)
                return 0, "Current session ..."
            return 0, "Total cost:            $0.0000\nTotal duration (API):  0s"   # exit 0, as on 02.11.
        self.p(ka, "refresh_usage", refresh)

        def roh(argv, input=None, cwd=None, env=None, timeout=None):
            self.pokes += 1
            if self.login_ok:
                return types.SimpleNamespace(returncode=0, stdout=OK_JSON, stderr="")
            return types.SimpleNamespace(returncode=1, stdout=EXPIRED_JSON, stderr="")
        self.p(host, "run_on_host", roh)

    def test_refresh_reports_the_login_and_records_it(self):
        r = ka.refresh_usage_checked(now=self.now)
        self.assertFalse(r["ok"])
        self.assertEqual(r["auth"], EXPIRED_RESULT)
        self.assertIn("rc=1", r["poke"])
        self.assertIn(EXPIRED_RESULT, r["poke"])                      # not cut to 60 characters any more
        self.assertEqual(ka.usage_failure_subject(r), claude_auth.SUBJECT)
        st = claude_auth.state(ka.USAGE_STATE_FILE)
        self.assertEqual((st["expired"], st["found"]), (True, EXPIRED_RESULT))
        self.assertIn(EXPIRED_RESULT, ka.usage_failure_detail(r))
        self.assertIn("/login", ka.usage_failure_detail(r))

    def test_fresh_usage_fails_stop_with_the_plain_message(self):
        self.assertIsNone(ka.fresh_usage(self.now, force=True))
        self.assertEqual([a[0] for a in self.alerts], ["AFClaude STOPPED: " + claude_auth.SUBJECT])
        pause = afclaude_config.automation_pause()
        self.assertTrue(pause["paused"])
        self.assertIn("Claude login expired: run `claude`", pause["reason"])
        self.assertIn("/login", pause["reason"])
        self.assertIn("not possible over Remote Control", pause["reason"])
        self.assertIn(EXPIRED_RESULT, self.alerts[0][1])
        self.assertNotIn(ka.USAGE_FAILED, pause["reason"])

    def test_display_only_never_pauses_but_names_the_login(self):
        self.assertIsNone(ka.fresh_usage(self.now, force=True, display=True))
        self.assertFalse(afclaude_config.automation_pause()["paused"])
        self.assertTrue(any(claude_auth.SHORT in ln and "ERROR" in ln for ln in self.lines))

    def test_a_throttled_request_keeps_the_verdict_and_a_re_login_is_noticed_soon(self):
        ka.refresh_usage_checked(now=self.now)
        self.assertEqual(self.pokes, 1)
        r = ka.refresh_usage_checked(now=self.now + timedelta(minutes=2))      # inside POKE_EXPIRED_INTERVAL
        self.assertEqual(self.pokes, 1)
        self.assertIn("skipped", r["poke"])
        self.assertEqual(r["auth"], EXPIRED_RESULT)                            # the state's verdict, not "generic"
        self.assertEqual(ka.usage_failure_subject(r), claude_auth.SUBJECT)
        ka.refresh_usage_checked(now=self.now + ka.POKE_EXPIRED_INTERVAL + timedelta(minutes=1))
        self.assertEqual(self.pokes, 2)                                        # not the 30-minute throttle

    def test_a_later_successful_read_clears_it(self):
        ka.refresh_usage_checked(now=self.now)
        self.assertTrue(claude_auth.is_expired(ka.USAGE_STATE_FILE))
        self.login_ok = True
        r = ka.refresh_usage_checked(now=self.now + timedelta(minutes=16))
        self.assertTrue(r["ok"])
        self.assertIsNone(r["auth"])
        self.assertFalse(claude_auth.is_expired(ka.USAGE_STATE_FILE))
        self.assertTrue(any("works again" in ln for ln in self.lines))

    def test_a_poke_that_works_clears_it_too(self):
        claude_auth.record_expired("usage read", "old", ka.USAGE_STATE_FILE)
        self.login_ok = True
        self.assertIn("is_error=False", ka.poke_auth(now=self.now))
        self.assertFalse(claude_auth.is_expired(ka.USAGE_STATE_FILE))

    def test_a_failure_that_is_no_login_problem_stays_generic(self):
        def roh(argv, input=None, cwd=None, env=None, timeout=None):
            return types.SimpleNamespace(returncode=1, stdout=result_json(NOT_AUTH[0]), stderr="")
        self.p(host, "run_on_host", roh)
        r = ka.refresh_usage_checked(now=self.now)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["auth"])
        self.assertEqual(ka.usage_failure_subject(r), ka.USAGE_FAILED)
        self.assertFalse(claude_auth.is_expired(ka.USAGE_STATE_FILE))

    def test_usage_output_naming_the_login_counts(self):
        self.p(ka, "refresh_usage", lambda cwd=ka.HERE: (1, NOT_LOGGED_IN))
        self.p(host, "run_on_host", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=result_json(NOT_AUTH[3]),
                                                                           stderr=""))
        r = ka.refresh_usage_checked(now=self.now)
        self.assertEqual(r["auth"], NOT_LOGGED_IN)

    def test_an_old_verdict_does_not_outlive_a_poke_that_worked(self):
        outs = iter([(1, NOT_LOGGED_IN), (0, "Total cost:            $0.0000")])   # /usage 1 names the login, /usage 2 does not
        self.p(ka, "refresh_usage", lambda cwd=ka.HERE: next(outs))
        self.p(host, "run_on_host", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=OK_JSON, stderr=""))
        r = ka.refresh_usage_checked(now=self.now)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["auth"])                                   # generic failure, not "login expired"
        self.assertEqual(ka.usage_failure_subject(r), ka.USAGE_FAILED)

    def test_a_throttled_poke_after_the_state_was_cleared_is_generic(self):
        ka._save_usage_state({"poke_at": self.now.isoformat()})        # a poke just ran; the state is not expired
        r = ka.refresh_usage_checked(now=self.now + timedelta(minutes=1))
        self.assertIn("skipped", r["poke"])
        self.assertIsNone(r["auth"])

    def test_the_refresh_keeps_stderr(self):
        self.p(host, "run_on_host", lambda argv, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=NOT_LOGGED_IN))
        self.p(ka, "refresh_usage", type(self).real_refresh)
        self.assertEqual(ka.refresh_usage(), (1, "\n" + NOT_LOGGED_IN))

    def test_the_sampler_and_the_run_readings_fail_with_the_plain_subject(self):
        with self.assertRaises(failstop.Failed) as cm:
            us.usage_now(self.now)
        self.assertEqual(cm.exception.why, claude_auth.SUBJECT)
        self.assertIn(EXPIRED_RESULT, cm.exception.detail)
        import run_metrics
        self.p(ka, "run_active", lambda sid, now, st=None: self.now)
        with self.assertRaises(failstop.Failed) as cm:
            run_metrics.watch_sample("s", self.now)
        self.assertEqual(cm.exception.why, claude_auth.SUBJECT)


UsageRead.real_refresh = staticmethod(ka.refresh_usage)


class HaikuJudgement(StateBase):
    def setUp(self):
        super().setUp()
        self.t = datetime(2026, 11, 2, 13, 0, tzinfo=UTC)
        self.p(us, "HAIKU_LOG", os.path.join(self.tmp.name, "haiku.jsonl"))
        self.st = {"sessions": {"abc": {"last_activity": self.t.isoformat(), "title": "t",
                                        "prompts": [{"text": "hi"}], "gaps": [], "tokens_since_haiku": {}}}}

    def run_with(self, result):
        self.p(host, "run_on_host", lambda argv, **k: result)
        self.st["sessions"]["abc"].pop("last_haiku_at", None)      # judge again
        return us.haiku_judgement(self.st, self.t)

    def test_an_expired_login_is_an_error_row_not_a_raw_answer(self):
        rows = self.run_with(types.SimpleNamespace(returncode=1, stdout=EXPIRED_JSON, stderr=""))
        self.assertEqual(rows[0]["auth"], EXPIRED_RESULT)
        self.assertIn(claude_auth.SHORT, rows[0]["error"])
        self.assertNotIn("raw", rows[0])
        self.assertTrue(claude_auth.is_expired(ka.USAGE_STATE_FILE))

    def test_a_good_answer_clears_the_state(self):
        claude_auth.record_expired("usage read", "old", ka.USAGE_STATE_FILE)
        out = json.dumps({"type": "result", "is_error": False, "result": '{"load": "low"}',
                          "total_cost_usd": 0.001, "usage": {}})
        rows = self.run_with(types.SimpleNamespace(returncode=0, stdout=out, stderr=""))
        self.assertNotIn("error", rows[0])
        self.assertEqual(rows[0]["answer"], {"load": "low"})
        self.assertFalse(claude_auth.is_expired(ka.USAGE_STATE_FILE))

    def no_pause(self, rows, key=None):
        """Nothing the sampler would fail-stop on: no row carries an error; the login state is untouched."""
        self.assertEqual([r for r in rows if r.get("error")], [])
        self.assertFalse(claude_auth.is_expired(ka.USAGE_STATE_FILE))

    def test_a_usage_limit_refusal_is_a_skipped_row(self):
        result = "You've hit your session limit \u00b7 resets 6pm (Europe/Berlin)"
        for rc, out, err in ((1, result_json(result), ""), (1, "{}", result)):
            claude_auth.record_ok("x", ka.USAGE_STATE_FILE)
            rows = self.run_with(types.SimpleNamespace(returncode=rc, stdout=out, stderr=err))
            self.no_pause(rows)
            self.assertIn("usage limit", rows[0]["skipped"])
            self.assertNotIn("answer", rows[0])

    def test_a_transient_api_error_keeps_the_old_raw_row(self):
        for text in ("API Error: 529 overloaded_error", NOT_AUTH[0]):   # overloaded; "another process is refreshing it"
            rows = self.run_with(types.SimpleNamespace(returncode=1, stdout=result_json(text), stderr=""))
            self.no_pause(rows)
            self.assertEqual(rows[0]["raw"], text[:500])
            self.assertNotIn("auth", rows[0])


class Triage(unittest.TestCase):
    def test_parse_triage_raises_the_login_error(self):
        for rc, out, err in ((1, EXPIRED_JSON, ""), (1, "", INVALID_KEY), (0, EXPIRED_JSON, "")):
            with self.assertRaises(inbox.TriageLoginExpired) as cm:
                inbox.parse_triage(rc, out, err)
            self.assertIsInstance(cm.exception, inbox.TriageError)
            self.assertIn(claude_auth.SHORT, str(cm.exception))
        with self.assertRaises(inbox.TriageError) as cm2:                     # another failure: the plain error
            inbox.parse_triage(1, result_json(NOT_AUTH[0]), "")
        self.assertNotIsInstance(cm2.exception, inbox.TriageLoginExpired)


class RcSender(StateBase):
    def test_the_sender_that_cannot_log_in(self):
        self.p(host, "run_on_host", lambda *a, **k: None)
        out = "\n".join(map(json.dumps, [{"type": "system", "subtype": "init"},
                                         {"type": "assistant", "error": "authentication_failed",
                                          "message": {"content": [{"type": "text", "text": EXPIRED_RESULT}]}},
                                         {"type": "result", "is_error": True, "result": EXPIRED_RESULT}]))
        r = subprocess.CompletedProcess(["claude"], 1, out + "\n", "")
        with self.assertRaises(rc_continue.LoginExpired) as cm:
            rc_continue.check_sender(r, "work-a1", "x")
        self.assertIsInstance(cm.exception, rc_continue.RcError)
        self.assertIn(EXPIRED_RESULT, str(cm.exception))
        self.assertTrue(claude_auth.is_expired(ka.USAGE_STATE_FILE))

    def test_a_good_sender_clears_it_and_a_plain_failure_stays_plain(self):
        claude_auth.record_expired("usage read", "old", ka.USAGE_STATE_FILE)
        rc_continue.check_sender(sender_out("work-a1"), "work-a1", "x")
        self.assertFalse(claude_auth.is_expired(ka.USAGE_STATE_FILE))
        with self.assertRaises(rc_continue.RcError) as cm:
            rc_continue.check_sender(subprocess.CompletedProcess(["claude"], 1, "", "boom"), "work-a1", "x")
        self.assertNotIsInstance(cm.exception, rc_continue.LoginExpired)


class Push(StateBase):
    def real_push(self):
        spec = importlib.util.spec_from_file_location("notify_real", os.path.join(HERE, "notify.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)     # testenv replaced notify.push in the imported module only
        return mod.push

    def test_a_push_that_cannot_log_in_says_so(self):
        self.p(host, "run_on_host", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=EXPIRED_JSON, stderr=""))
        seen = []
        self.p(claude_auth, "record_expired", lambda source, found, path=None, now=None: seen.append((source, found)))
        got = self.real_push()("AFClaude: x")
        self.assertIn(claude_auth.SHORT, got)
        self.assertEqual(seen, [("push notification", EXPIRED_RESULT)])


class QuickviewBlock(StateBase):
    def test_the_block_follows_the_state(self):
        b = qv.auth_block()
        self.assertEqual((b["expired"], b["since_berlin"], b["error"]), (False, None, None))
        self.assertEqual(b["fix"], claude_auth.FIX)
        claude_auth.record_expired("usage read", EXPIRED_RESULT, ka.USAGE_STATE_FILE,
                                   datetime(2026, 11, 2, 13, 15, tzinfo=UTC))
        b = qv.auth_block()
        self.assertTrue(b["expired"])
        self.assertEqual((b["found"], b["source"]), (EXPIRED_RESULT, "usage read"))
        self.assertIn("14:15", b["since_berlin"])                     # shown in Berlin time
        claude_auth.record_ok("usage read", ka.USAGE_STATE_FILE)
        self.assertFalse(qv.auth_block()["expired"])

    def test_unknown_only_for_the_auth_state_own_failed_load(self):
        import runner_state

        def failing(component):
            def state(path=None):
                runner_state._LOAD_FAILED[component] = "boom"
                return {}
            return state
        self.addCleanup(runner_state._LOAD_FAILED.clear)
        self.p(claude_auth, "state", failing("sampler"))             # another component's failed load
        self.assertIsNone(qv.auth_block()["error"])
        self.p(claude_auth, "state", failing(claude_auth.COMPONENT))
        self.assertIn("boom", qv.auth_block()["error"])

    def test_the_page_renders_it_first(self):
        with open(os.path.join(HERE, "quickview", "AFClaude.html"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("function authItem(", page)
        self.assertIn("s.auth", page)
        self.assertIn("Claude login EXPIRED", page)
        self.assertIn("var html = authHtml + inputHtml", page)        # before every other needs-input item


if __name__ == "__main__":
    unittest.main()
