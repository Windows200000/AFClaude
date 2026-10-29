#!/usr/bin/env python3
"""Offline tests for the owner's message box to the task-manager (D-178, D-186, D-241; inbox.py,
inbox_worker.py, inbox_api.py): the table and its actions (verbatim text, audit rows without the
text, the status transitions), the Haiku verdict parser, the worker's failure paths (a failed
triage is an error + an alert + automation paused and nothing is delivered, D-244; one at the
account's usage limit is held until the limit's reset, then sorted once more), the delivery's
gates (the session lock, the owner holding or typing, a usage limit, an unreadable pane, a failed
send, the transcript check), a hold until the next session-window start (D-244: the start takes
the message along, else an error), the noted messages going along with the next start and the
post-compaction message, the endpoint's key check and limits, the CLI and the quickview. A temp DB, a fake tmux / transcript
(test_compaction.Env), a fake Haiku; no real Claude, no tmux, no live session."""
import html.parser
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import export_quickview  # noqa: E402
import failstop  # noqa: E402
import host  # noqa: E402
import inbox  # noqa: E402
import inbox_api  # noqa: E402
import inbox_worker as iw  # noqa: E402
import keepalive as ka  # noqa: E402
import schedule  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402
import test_compaction as tc  # noqa: E402  (its Env: a fake tmux session and transcript)

UTC = timezone.utc
SID = tc.SID
HERE = os.path.dirname(os.path.abspath(__file__))


LIMIT_NOTICE = "You've hit your session limit · resets 2pm (UTC)"


def limit_out(notice=LIMIT_NOTICE):
    """What `claude -p --output-format json` printed for inbox #30 (22.10., exit code 1): no usage."""
    return json.dumps({"duration_api_ms": 0, "stop_reason": "stop_sequence", "total_cost_usd": 0,
                       "usage": {"input_tokens": 0, "output_tokens": 0}, "type": "result", "is_error": True,
                       "result": notice})


def haiku_out(verdict="now", reason="a question", cost=0.0012, raw=None, is_error=False):
    result = raw if raw is not None else json.dumps({"verdict": verdict, "reason": reason})
    return json.dumps({"type": "result", "is_error": is_error, "result": result, "total_cost_usd": cost,
                       "usage": {"input_tokens": 310, "output_tokens": 22}})


class DB(unittest.TestCase):
    """A clean inbox table per test; alerts recorded instead of sent."""
    def setUp(self):
        testenv.clear_settings()
        self.conn = store.connect()
        with store.transaction(self.conn):
            self.conn.execute("DELETE FROM inbox_messages")
        self.alerts = []
        self._notify = failstop._notify
        failstop._notify = lambda subject, body: self.alerts.append((subject, body))

    def tearDown(self):
        failstop._notify = self._notify
        self.conn.close()
        testenv.unpause()

    def post(self, text="Is the dashboard login done?", actor="owner", via="dashboard", sid=SID):
        return actions.perform(self.conn, "inbox.post", {"text": text, "session_id": sid}, actor=actor, via=via)

    def run_(self, name, **params):
        return actions.perform(self.conn, name, params, actor="runner:inbox", via="runner")

    def row(self, mid):
        return store.get_inbox(self.conn, mid)


# ---------------------------------------------------------------- the table and the actions

class Actions(DB):
    def test_post_keeps_the_text_verbatim_and_audits_only_its_digest(self):
        text = "  Two lines:\n  keep   the spacing\n"
        m = self.post(text)
        self.assertEqual((m["text"], m["status"], m["verdict"], m["session_id"]), (text, "pending", None, SID))
        audit = store.audit_log(self.conn, 1, "inbox", m["id"])[0]
        self.assertEqual((audit["action"], audit["actor"], audit["via"]), ("inbox.post", "owner", "dashboard"))
        self.assertNotIn(text, json.dumps(audit))                           # D-241: the row is the only copy
        self.assertEqual(audit["after"]["text"]["chars"], len(text))

    def test_post_defaults_to_the_first_projects_task_manager(self):
        m = actions.perform(self.conn, "inbox.post", {"text": "hi"}, actor="owner", via="dashboard")
        self.assertEqual(m["session_id"], store.manager_sessions(self.conn)[0]["session"])

    def test_post_targets_a_project_s_task_manager(self):
        """3b S3: the target is a project (its task-manager), not a machine setting."""
        sid2 = "00000000-0000-4000-8000-0000000000b2"
        store.add_project(self.conn, "inbox-two")
        store.add_project(self.conn, "inbox-none")
        store.update_project(self.conn, "inbox-two", manager_session=sid2)
        self.conn.commit()
        m = actions.perform(self.conn, "inbox.post", {"text": "to two", "project": "inbox-two"}, actor="owner",
                            via="dashboard")
        self.assertEqual(m["session_id"], sid2)
        for params in ({"project": "inbox-none"}, {"project": "nope"}, {"project": "inbox-two", "session_id": sid2}):
            with self.assertRaises(ValueError, msg=str(params)):      # no guess: no session yet, unknown, both
                actions.perform(self.conn, "inbox.post", {"text": "x", **params}, actor="owner", via="dashboard")
        self.assertEqual([r["text"] for r in store.list_inbox(self.conn)], ["to two"])

    def test_post_validation(self):
        for bad in ("", "   \n", None, 7, "x" * (store.INBOX_TEXT_MAX + 1), "a\0b"):
            with self.assertRaises(ValueError, msg=repr(bad)[:40]):
                self.post(bad)
        with self.assertRaises(ValueError):
            self.post("hi", sid="not-a-session")
        self.assertEqual(store.list_inbox(self.conn), [])

    def test_triage_note_and_now(self):
        a, b = self.post("note this"), self.post("answer this")
        n = self.run_("inbox.triage", message_id=a["id"], verdict="note", reason="a goal", cost_usd=0.001,
                      usage={"output_tokens": 9})
        self.assertEqual((n["status"], n["verdict"], n["verdict_reason"], n["triage_usage"]),
                         ("noted", "note", "a goal", {"output_tokens": 9}))
        w = self.run_("inbox.triage", message_id=b["id"], verdict="now", reason="a question")
        self.assertEqual((w["status"], w["verdict"]), ("pending", "now"))
        with self.assertRaises(store.InvalidTransition):                      # once only
            self.run_("inbox.triage", message_id=b["id"], verdict="note", reason="x")
        with self.assertRaises(ValueError):
            self.run_("inbox.triage", message_id=a["id"], verdict="later", reason="x")

    def test_runner_steps_are_not_for_mcp(self):
        m = self.post()
        for name, params in (("inbox.triage", {"verdict": "now", "reason": "x"}), ("inbox.hold", {"reason": "x"}),
                             ("inbox.deliver", {"plan": "x"}), ("inbox.fail", {"error": "x"})):
            with self.assertRaises(actions.Forbidden, msg=name):
                actions.perform(self.conn, name, dict(params, message_id=m["id"]), actor="mcp:x", via="mcp")

    def test_hold_deliver_fail_retry_reply(self):
        m = self.post()
        self.run_("inbox.triage", message_id=m["id"], verdict="now", reason="q")
        h = self.run_("inbox.hold", message_id=m["id"], reason="the owner is typing")
        since = h["hold_since"]
        n_audit = len(store.audit_log(self.conn, 100, "inbox", m["id"]))
        self.run_("inbox.hold", message_id=m["id"], reason="the owner is typing")   # same reason: a no-op
        self.assertEqual(len(store.audit_log(self.conn, 100, "inbox", m["id"])), n_audit)
        h2 = self.run_("inbox.hold", message_id=m["id"], reason="a compaction")
        self.assertEqual((h2["hold"], h2["hold_since"]), ("a compaction", since))     # held since the first
        d = self.run_("inbox.deliver", message_id=m["id"], plan="send-keys")
        self.assertEqual((d["status"], d["delivered_via"], d["hold"]), ("delivered", "send-keys", None))
        with self.assertRaises(store.InvalidTransition):
            self.run_("inbox.deliver", message_id=m["id"], plan="send-keys")
        e = self.run_("inbox.fail", message_id=m["id"], error="not in the transcript")
        self.assertEqual((e["status"], e["error"]), ("error", "not in the transcript"))
        r = actions.perform(self.conn, "inbox.retry", {"message_id": m["id"]}, actor="owner", via="dashboard")
        self.assertEqual((r["status"], r["error"], r["verdict"]), ("pending", None, "now"))
        a = actions.perform(self.conn, "inbox.reply", {"message_id": m["id"], "answer": "Yes, done."},
                            actor="cli", via="cli")
        a = actions.perform(self.conn, "inbox.reply", {"message_id": m["id"], "answer": "And deployed."},
                            actor="cli", via="cli")
        self.assertEqual((a["status"], a["answer"]), ("answered", "Yes, done.\n\nAnd deployed."))
        with self.assertRaises(store.InvalidTransition):                      # only a failed one is retried
            actions.perform(self.conn, "inbox.retry", {"message_id": m["id"]}, actor="owner", via="dashboard")

    def test_dismiss_answered_thread(self):
        """D-304: the owner dismisses an answered thread (audited); only an answered one, once; a later
        reply brings it back (owner-only: test_actions.Autonomous)."""
        m = self.post()
        with self.assertRaises(store.InvalidTransition):                      # not answered yet
            actions.perform(self.conn, "inbox.dismiss", {"message_id": m["id"]}, actor="owner", via="dashboard")
        actions.perform(self.conn, "inbox.reply", {"message_id": m["id"], "answer": "Yes."}, actor="cli", via="cli")
        d = actions.perform(self.conn, "inbox.dismiss", {"message_id": m["id"]}, actor="owner", via="dashboard")
        self.assertEqual(d["status"], "answered")
        self.assertIsNotNone(d["dismissed_at"])
        row = store.audit_log(self.conn, 1, "inbox", m["id"])[0]
        self.assertEqual((row["action"], row["actor"], row["before"]["dismissed_at"]), ("inbox.dismiss", "owner", None))
        with self.assertRaises(store.InvalidTransition):                      # already dismissed
            actions.perform(self.conn, "inbox.dismiss", {"message_id": m["id"]}, actor="owner", via="dashboard")
        r = actions.perform(self.conn, "inbox.reply", {"message_id": m["id"], "answer": "More."}, actor="cli", via="cli")
        self.assertIsNone(r["dismissed_at"])

    def test_include_only_noted(self):
        a, b = self.post("n1"), self.post("n2")
        for m in (a, b):
            self.run_("inbox.triage", message_id=m["id"], verdict="note", reason="r")
        out = self.run_("inbox.include", message_ids=[a["id"], b["id"]], by="window-start-x")
        self.assertEqual([(m["status"], m["included_by"]) for m in out], [("included", "window-start-x")] * 2)
        with self.assertRaises(store.InvalidTransition):
            self.run_("inbox.include", message_ids=[a["id"]], by="again")

    def test_triage_hold_then_triage_or_fail_or_retry(self):
        """D-244: a triage held at a usage limit: pending, untriaged, retry_after set; the triage
        clears it; a failure and the owner's retry too."""
        a, b = self.post("q1"), self.post("q2")
        h = self.run_("inbox.triage_hold", message_id=a["id"], reason="session limit", retry_after="2026-10-22T14:00:00+00:00")
        self.assertEqual((h["status"], h["verdict"], h["hold"], h["retry_after"]),
                         ("pending", None, "session limit", "2026-10-22T14:00:00Z"))
        self.assertIsNotNone(h["hold_since"])
        t = self.run_("inbox.triage", message_id=a["id"], verdict="now", reason="q")
        self.assertEqual((t["hold"], t["hold_since"], t["retry_after"]), (None, None, None))
        with self.assertRaises(store.InvalidTransition):                      # triaged: no triage hold
            self.run_("inbox.triage_hold", message_id=a["id"], reason="x", retry_after="2026-10-22T14:00:00Z")
        for bad in ("soon", "2026-10-22T14:00:00", None):
            with self.assertRaises(ValueError, msg=bad):
                self.run_("inbox.triage_hold", message_id=b["id"], reason="x", retry_after=bad)
        self.run_("inbox.triage_hold", message_id=b["id"], reason="x", retry_after="2026-10-22T14:00:00Z")
        e = self.run_("inbox.fail", message_id=b["id"], error="limit again")
        self.assertEqual((e["status"], e["hold"], e["retry_after"]), ("error", None, None))
        r = actions.perform(self.conn, "inbox.retry", {"message_id": b["id"]}, actor="owner", via="dashboard")
        self.assertEqual((r["status"], r["verdict"], r["hold_since"], r["retry_after"]), ("pending", None, None, None))
        with self.assertRaises(actions.Forbidden):
            actions.perform(self.conn, "inbox.triage_hold", {"message_id": b["id"], "reason": "x",
                            "retry_after": "2026-10-22T14:00:00Z"}, actor="mcp:x", via="mcp")

    def test_retry_of_a_failed_triage_triages_again(self):
        m = self.post()
        self.run_("inbox.fail", message_id=m["id"], error="Haiku timed out")
        r = actions.perform(self.conn, "inbox.retry", {"message_id": m["id"]}, actor="owner", via="dashboard")
        self.assertEqual((r["status"], r["verdict"]), ("pending", None))


# ---------------------------------------------------------------- the verdict parser

class ParseTriage(unittest.TestCase):
    def test_good(self):
        t = inbox.parse_triage(0, haiku_out("note", "a goal for later"))
        self.assertEqual((t["verdict"], t["reason"], t["cost_usd"]), ("note", "a goal for later", 0.0012))
        self.assertEqual(t["usage"]["output_tokens"], 22)

    def test_tolerant_of_prose_and_case(self):
        t = inbox.parse_triage(0, haiku_out(raw='Sure:\n```json\n{"verdict": "NOW", "reason": "asks"}\n```'))
        self.assertEqual(t["verdict"], "now")

    def test_failures(self):
        for rc, out in ((1, ""), (0, "not json"), (0, "[1]"), (0, haiku_out(is_error=True)),
                        (0, haiku_out(raw="I think now.")), (0, haiku_out("soon")),
                        (0, haiku_out(raw='{"verdict": ')), (0, json.dumps({"result": None}))):
            with self.assertRaises(inbox.TriageError, msg=out[:60]):
                inbox.parse_triage(rc, out, "boom")

    def test_prompt(self):
        p = inbox.triage_prompt('Is {this} "done"?')
        self.assertIn('Is {this} "done"?', p)
        self.assertIn('{"verdict": "now|note"', p)


# ---------------------------------------------------------------- the worker: triage

class FakeHaiku(DB):
    """A fake Haiku call (self.answer) and a fake send (self.fired)."""
    def setUp(self):
        super().setUp()
        self.calls = []
        self.answer = lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=haiku_out("note", "a goal"),
                                                               stderr="")
        self._roh = host.run_on_host
        host.run_on_host = lambda argv, **kw: (self.calls.append((argv, kw)), self.answer(argv, **kw))[1]
        self.fired = []
        self._fire = ka.fire
        ka.fire = lambda *a, **kw: (self.fired.append(a), (0, "sent-keys", ""))[1]

    def tearDown(self):
        host.run_on_host, ka.fire = self._roh, self._fire
        super().tearDown()


class Triage(FakeHaiku):
    def test_note(self):
        m = self.post("Goal for later: a dark mode.")
        out = iw.process(self.conn, verify=lambda *a: None)
        r = self.row(m["id"])
        self.assertEqual((out, r["status"], r["verdict"], r["triage_cost_usd"]), ({"note": 1}, "noted", "note", 0.0012))
        argv, kw = self.calls[0]
        self.assertEqual(argv, ka.POKE_ARGV)                                  # the bridge-whitelisted Haiku call
        self.assertIn("Goal for later: a dark mode.", kw["input"])
        self.assertEqual(self.fired, [])

    def test_haiku_timeout_is_an_error_and_nothing_is_delivered(self):
        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 120)
        self.answer = boom
        m = self.post("Stop the run now!")
        out = iw.process(self.conn, verify=lambda *a: None)
        r = self.row(m["id"])
        self.assertEqual((out, r["status"], r["verdict"]), ({"failed": 1}, "error", None))
        self.assertIn("timed out", r["error"])
        self.assertEqual(self.fired, [])                                      # never delivered silently
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("inbox message triage failed", self.alerts[0][0])
        p = failstop.paused()                                                 # an inbox error stops automation (D-244)
        self.assertTrue(p["paused"])
        self.assertIn("inbox: inbox message triage failed", p["reason"])
        self.assertEqual(iw.process(self.conn, verify=lambda *a: None), {})   # not retried on its own

    def test_an_inbox_error_pauses_only_the_messages_project(self):
        """D-244: the message's session is the task-manager of PA: only PA is paused (its runner
        checks hold), a second project PB and every install-wide check go on."""
        other = "bbbbbbbb-1111-4222-8333-444444444444"
        self.addCleanup(testenv.managed_project("PA", SID))
        self.addCleanup(testenv.managed_project("PB", other))
        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 120)
        self.answer = boom
        m = self.post("Stop the run now!")
        iw.process(self.conn, verify=lambda *a: None)
        self.assertEqual(self.row(m["id"])["status"], "error")
        p = failstop.paused(session=SID)
        self.assertEqual((p["paused"], p["scope"], p["project"]), (True, "project", "PA"))
        self.assertIn("inbox: inbox message triage failed", p["reason"])
        self.assertFalse(failstop.paused(session=other)["paused"])
        self.assertFalse(failstop.paused()["paused"])
        self.assertEqual([a[0] for a in self.alerts], ["AFClaude STOPPED (PA): inbox message triage failed"])
        self.assertTrue(ka.automation_paused("the start", SID))
        self.assertFalse(ka.automation_paused("the start", other))

    def test_a_failed_pass_is_an_error_and_pauses(self):
        """A DB failure in a pass (D-244): alerted, automation paused; the worker goes on."""
        wake, stop = threading.Event(), threading.Event()
        def boom(conn, now=None, verify=None):
            stop.set()
            wake.set()                                                        # no POLL wait after the pass
            raise store.DBUnavailable("database is locked", "error")
        old, iw.process = iw.process, boom
        try:
            iw.run_forever(wake, stop)
        finally:
            iw.process = old
        self.assertEqual([a[0] for a in self.alerts], ["AFClaude STOPPED: inbox worker pass failed: DBUnavailable"])
        self.assertIn("inbox worker pass failed", failstop.paused()["reason"])

    def test_bad_answer_is_an_error(self):
        self.answer = lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=haiku_out(raw="hmm"), stderr="")
        m = self.post()
        iw.process(self.conn, verify=lambda *a: None)
        self.assertEqual(self.row(m["id"])["status"], "error")
        self.assertEqual(self.fired, [])

    def test_a_failed_claude_call_is_an_error(self):
        self.answer = lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="not logged in")
        m = self.post()
        iw.process(self.conn, verify=lambda *a: None)
        self.assertIn("not logged in", self.row(m["id"])["error"])
        self.assertTrue(failstop.paused()["paused"])                          # at once, no hold (D-235)
        self.assertIsNone(self.row(m["id"])["retry_after"])


# ---------------------------------------------------------------- the worker: triage at a usage limit (D-244)

class TriageAtLimit(FakeHaiku):
    """Inbox #30 (22.10.): the triage call hit the account's session limit and exited 1. Held until
    the limit's reset, no error, no pause; sorted once more after it; failing then is an error."""
    NOW = datetime(2026, 10, 22, 9, 53, tzinfo=UTC)
    RESET = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)

    def limit(self, out=None, rc=1, stderr=""):
        self.answer = lambda argv, **kw: types.SimpleNamespace(returncode=rc, stdout=limit_out() if out is None else out,
                                                               stderr=stderr)

    def note(self):
        self.answer = lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=haiku_out("note", "a goal"), stderr="")

    def held(self, m, reset=None):
        out = iw.process(self.conn, now=self.NOW, verify=lambda *a: None)
        r = self.row(m["id"])
        self.assertEqual((out, r["status"], r["verdict"]), ({"held": 1}, "pending", None))
        self.assertEqual(r["retry_after"], store.iso(reset or self.RESET))
        self.assertIn("usage limit", r["hold"])
        self.assertEqual((self.alerts, failstop.paused()["paused"]), ([], False))   # no error, no pause
        return r

    def test_held_until_the_reset_and_shown_as_held(self):
        self.limit()
        m = self.post()
        r = self.held(m)
        self.assertIn("session usage limit", r["hold"])
        self.assertEqual(len(self.calls), 1)
        for t in (self.NOW + timedelta(hours=1), self.RESET + iw.TRIAGE_RETRY_MARGIN - timedelta(seconds=1)):
            self.assertEqual(iw.process(self.conn, now=t, verify=lambda *a: None), {"held": 1})
        self.assertEqual(len(self.calls), 1)                                  # no call before the reset
        v = [x for x in inbox.view(self.conn, self.NOW)["messages"] if x["id"] == m["id"]][0]
        until = self.RESET.astimezone(inbox.BERLIN).strftime(inbox.VIEW_FMT)
        self.assertEqual((v["state"], v["hold_until_berlin"]), ("held", until))
        self.assertTrue(v["tip"].startswith("Held: ") and v["tip"].endswith(f"sorted after {until}"), v["tip"])
        self.assertEqual(self.fired, [])

    def test_sorted_after_the_reset(self):
        self.limit()
        m = self.post()
        self.held(m)
        self.note()
        out = iw.process(self.conn, now=self.RESET + iw.TRIAGE_RETRY_MARGIN, verify=lambda *a: None)
        r = self.row(m["id"])
        self.assertEqual((out, r["status"], r["verdict"], r["hold"], r["retry_after"]), ({"note": 1}, "noted", "note", None, None))
        self.assertEqual((len(self.calls), self.alerts, failstop.paused()["paused"]), (2, [], False))

    def test_still_failing_after_the_reset_is_an_error_and_pauses(self):
        for again in (self.limit, lambda: setattr(self, "answer", lambda argv, **kw: types.SimpleNamespace(
                returncode=0, stdout=haiku_out(raw="hmm"), stderr=""))):
            testenv.unpause()
            self.alerts.clear()
            self.limit()
            m = self.post()
            self.held(m)
            again()
            out = iw.process(self.conn, now=self.RESET + timedelta(minutes=5), verify=lambda *a: None)
            r = self.row(m["id"])
            self.assertEqual((out, r["status"]), ({"failed": 1}, "error"))
            self.assertIn("after the usage limit's reset", r["error"])
            self.assertEqual([a[0] for a in self.alerts],
                             ["AFClaude STOPPED: inbox message triage failed after the usage limit's reset"])
            self.assertTrue(failstop.paused()["paused"])
            self.assertEqual(self.fired, [])

    def test_weekly_limit_without_a_reset_in_the_notice_waits_for_the_usage_cache_reset(self):
        reset = self.NOW + timedelta(days=2)
        old = ka.usage_cache_checked
        ka.usage_cache_checked = lambda: {"weekly": {"percent": 100.0, "resets_at": reset}}
        self.addCleanup(setattr, ka, "usage_cache_checked", old)
        self.limit(limit_out("You've hit your weekly limit"))
        m = self.post()
        self.assertIn("weekly usage limit", self.held(m, reset)["hold"])

    def test_no_reset_to_wait_for_is_an_error(self):
        for cache in (lambda: None, lambda: {"session": {"percent": 100.0, "resets_at": self.NOW - timedelta(minutes=1)}},
                      lambda: (_ for _ in ()).throw(OSError("bridge down"))):
            testenv.unpause()
            old = ka.usage_cache_checked
            ka.usage_cache_checked = cache
            try:
                self.limit(limit_out("You've hit your session limit"))
                m = self.post()
                out = iw.process(self.conn, now=self.NOW, verify=lambda *a: None)
            finally:
                ka.usage_cache_checked = old
            self.assertEqual((out, self.row(m["id"])["status"]), ({"failed": 1}, "error"))
            self.assertTrue(failstop.paused()["paused"])

    def test_the_notice_on_stderr_counts_too(self):
        self.limit(out="", stderr=LIMIT_NOTICE)
        self.held(self.post())

    def test_other_limits_and_quoted_notices_are_no_hold(self):
        """A spend limit has no reset to wait for (an error at once); a successful call whose answer
        quotes a limit notice is a normal verdict."""
        self.assertIsNone(iw.limit_notice(1, limit_out("You've hit your monthly spend limit · resets Nov 1, 12am (UTC)"), ""))
        self.assertIsNone(iw.limit_notice(0, haiku_out(raw='{"verdict": "now", "reason": "You\'ve hit your session limit?"}'), ""))
        self.assertEqual(iw.limit_notice(1, limit_out("You've hit your limit · resets 2pm (UTC)"), ""),
                         ("", "You've hit your limit · resets 2pm (UTC)"))
        self.assertEqual(iw.limit_notice(0, limit_out(), "")[0], "session")   # is_error with exit code 0
        self.limit(limit_out("You've hit your monthly spend limit"))
        m = self.post()
        self.assertEqual(iw.process(self.conn, now=self.NOW, verify=lambda *a: None), {"failed": 1})
        self.assertEqual(self.row(m["id"])["status"], "error")
        self.assertTrue(failstop.paused()["paused"])


# ---------------------------------------------------------------- the worker: delivery

class Delivery(tc.Env):
    """A "now" message into the fake live tmux session of test_compaction.Env."""
    def setUp(self):
        super().setUp()
        self.conn = store.connect()
        with store.transaction(self.conn):
            self.conn.execute("DELETE FROM inbox_messages")
        self.notified = []
        self._notify = failstop._notify
        failstop._notify = lambda subject, body: self.notified.append(subject)
        self.verified = []

    def tearDown(self):
        failstop._notify = self._notify
        self.conn.close()
        super().tearDown()

    def now_msg(self, text="Is the login page live?"):
        m = actions.perform(self.conn, "inbox.post", {"text": text, "session_id": SID}, actor="owner", via="dashboard")
        actions.perform(self.conn, "inbox.triage", {"message_id": m["id"], "verdict": "now", "reason": "a question"},
                        actor="runner:inbox", via="runner")
        return m

    def note(self, text):
        m = actions.perform(self.conn, "inbox.post", {"text": text, "session_id": SID}, actor="owner", via="dashboard")
        actions.perform(self.conn, "inbox.triage", {"message_id": m["id"], "verdict": "note", "reason": "later"},
                        actor="runner:inbox", via="runner")
        return m

    def deliver(self, m, now=None):
        m = store.get_inbox(self.conn, m["id"])
        return iw.deliver(self.conn, m, now or datetime.now(UTC), verify=lambda *a: self.verified.append(a))

    def row(self, m):
        return store.get_inbox(self.conn, m["id"])

    def test_delivered_into_the_live_session_with_the_notes(self):
        n = self.note("Line one\nline two of a goal")
        m = self.now_msg()
        self.assertEqual(self.deliver(m), "delivered")
        plan, msg = self.sent[0]
        self.assertEqual(plan, "send-keys")
        self.assertTrue(msg.startswith(inbox.marker(m["id"])))
        self.assertIn('"Is the login page live?"', msg)
        self.assertIn(f"python3 tasks.py inbox reply {m['id']}", msg)
        self.assertIn('"Line one ⏎ line two of a goal"', msg)                # the note went along, one line
        self.assertNotIn("\n", msg)
        r = self.row(m)
        self.assertEqual((r["status"], r["delivered_via"]), ("delivered", "send-keys"))
        self.assertEqual((self.row(n)["status"], self.row(n)["included_by"]), ("included", f"inbox #{m['id']}"))
        self.assertEqual(self.verified[0][0], m["id"])
        self.assertTrue(iw.verify(m["id"], self.path, 0, timedelta(0)))      # the fake session wrote it

    def test_held_by_a_compaction_or_start_lock(self):
        m = self.now_msg()
        lock = ka.session_lock(SID)
        try:
            self.assertEqual(self.deliver(m), "held")
        finally:
            ka.release_lock(lock)
        self.assertEqual((self.row(m)["status"], self.row(m)["hold"]), ("pending", iw.HOLD_LOCKED))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.deliver(m), "delivered")                        # free again: it goes out
        self.assertIsNone(self.row(m)["hold"])

    def test_held_while_the_owner_types(self):
        self.typing = "a draft in ka-abcdef01's input box (5 characters)"
        m = self.now_msg()
        self.assertEqual(self.deliver(m), "held")
        self.assertIn("a draft", self.row(m)["hold"])
        self.assertEqual(self.sent, [])

    def test_held_while_the_owner_holds_the_session(self):
        ka.preflight = lambda sid: (False, [f"{ka.RC_HELD} thread (pid [1])"], None)
        m = self.now_msg()
        self.assertEqual(self.deliver(m), "held")
        self.assertIn("the owner holds the session", self.row(m)["hold"])
        self.assertEqual(self.notified, [])
        self.assertFalse(failstop.paused()["paused"])                         # a hold is no error

    def test_held_at_a_usage_limit(self):
        at = datetime.now(UTC) - timedelta(minutes=5)
        self.write([tc.entry("assistant", at, uuid="lim", error="rate_limit",
                             message={"role": "assistant", "model": "<synthetic>",
                                      "content": [{"type": "text", "text": "You've hit your session limit · resets 11pm"}]})],
                   "a")
        m = self.now_msg()
        self.assertEqual(self.deliver(m), "held")
        self.assertIn("usage limit", self.row(m)["hold"])

    def test_held_until_the_next_window_start_then_an_error(self):
        """D-244: no 2 h limit; the hold lasts until the next session-window start (+ START_GRACE for
        that start to take it along); still pending then: an error, alerted, automation paused.
        A fixed clock: hold_since is store.now_iso() wall time, so a real clock made the gap to the
        next session start (and so this test) depend on the time of day it happened to run -
        anchored just after a window start on the 2026 DST fall-back night, which also proves
        hold_until spans the fold (D-031/D-148) rather than using a fixed UTC offset."""
        since = datetime(2026, 10, 24, 21, 5, tzinfo=UTC)      # 23:05 CEST: just after the window opens
        old_utcnow = store._utcnow
        store._utcnow = lambda: since
        self.addCleanup(setattr, store, "_utcnow", old_utcnow)
        self.typing = "the owner typed in a terminal attached to ka-abcdef01 1 min ago"
        m = self.now_msg()
        self.assertEqual(self.deliver(m, since), "held")
        self.assertEqual(store.parse_iso(self.row(m)["hold_since"]), since)
        deadline = schedule.next_session_start(since)
        self.assertEqual(deadline, datetime(2026, 10, 25, 2, 0, tzinfo=UTC))   # 03:00 CET: across the fold
        self.assertEqual(inbox.hold_until(self.row(m)), deadline)
        view = inbox.message_view(self.row(m))
        self.assertEqual(view["hold_until_berlin"], deadline.astimezone(inbox.BERLIN).strftime("%a %d.%m. %H:%M"))
        self.assertEqual(view["state"], "held")                              # D-246: the box's tooltip says until when
        self.assertEqual(view["tip"], f"Held: {self.typing} · goes out with the window start "
                                      f"{view['hold_until_berlin']} at the latest")
        for later in (since + timedelta(hours=3), deadline + iw.START_GRACE - timedelta(minutes=1)):
            self.assertEqual(self.deliver(m, later), "held")                  # no 2 h limit any more
        self.assertEqual(self.notified, [])
        self.assertEqual(self.deliver(m, deadline + iw.START_GRACE), "failed")
        r = self.row(m)
        self.assertEqual(r["status"], "error")
        self.assertIn("held past its window start", r["error"])
        self.assertIn("did not take it along", r["error"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.notified, ["AFClaude STOPPED: inbox message held past its window start"])
        self.assertIn("inbox message held past its window start", failstop.paused()["reason"])
        self.assertIsNone(inbox.message_view(r)["hold_until_berlin"])
        self.assertEqual(inbox.message_view(r)["state"], "error")
        self.assertTrue(inbox.message_view(r)["tip"].startswith("Failed "), inbox.message_view(r)["tip"])

    def test_a_hold_without_any_window_start_is_an_error(self):
        self.typing = "a draft in ka-abcdef01's input box (5 characters)"
        testenv.set_setting("window_days", {d: None for d in schedule.DAYS})
        m = self.now_msg()
        self.assertEqual(self.deliver(m), "failed")
        self.assertIn("without a window start", self.row(m)["error"])
        self.assertTrue(failstop.paused()["paused"])

    def test_not_sent_again_after_a_start_took_it_along(self):
        m = self.now_msg()
        stale = self.row(m)                                                  # the pass read it as pending ...
        actions.perform(self.conn, "inbox.deliver", {"message_id": m["id"], "plan": "send-keys, with the start k"},
                        actor="keepalive", via="runner")                    # ... then a start sent it
        self.assertEqual(iw.deliver(self.conn, stale, datetime.now(UTC), verify=lambda *a: None), "gone")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.row(m)["status"], "delivered")

    def test_errors(self):
        """A refused preflight (not the owner), an unreadable pane, a failed send: an error on the
        message and an alert; nothing (more) is sent."""
        def unreadable(sid, now):
            raise RuntimeError("can't read ka-abcdef01's pane")
        cases = [("preflight", lambda: setattr(ka, "preflight", lambda sid: (False, ["host process info unavailable"], None)),
                  "preflight refused"),
                 ("pane", lambda: setattr(ka, "owner_typing", unreadable), "can't read"),
                 ("send", lambda: setattr(ka, "fire", lambda *a, **kw: (1, "", "send-keys to ka-abcdef01 failed")),
                  "rc=1")]
        for what, arrange, want in cases:
            with self.subTest(what):
                testenv.unpause()                                             # each one pauses (D-244)
                old = (ka.preflight, ka.owner_typing, ka.fire)
                arrange()
                try:
                    m = self.now_msg()
                    self.assertEqual(self.deliver(m), "failed")
                finally:
                    ka.preflight, ka.owner_typing, ka.fire = old
                r = self.row(m)
                self.assertEqual(r["status"], "error")
                self.assertIn(want, r["error"])
                self.assertEqual(self.notified[-1], "AFClaude STOPPED: inbox message not delivered")
                self.assertTrue(failstop.paused()["paused"])
        self.assertEqual(self.sent, [])

    def test_not_in_the_transcript_is_an_error(self):
        m = self.now_msg()
        ka.fire = lambda *a, **kw: (0, "sent-keys ka-abcdef01", "")          # typed, but nothing arrives
        old, iw.VERIFY_POLL = iw.VERIFY_POLL, 0
        try:
            self.assertEqual(iw.deliver(self.conn, self.row(m), datetime.now(UTC),
                                        verify=lambda mid, path, off: iw.verify(mid, path, off, timedelta(0))),
                             "delivered")
        finally:
            iw.VERIFY_POLL = old
        r = self.row(m)
        self.assertEqual(r["status"], "error")
        self.assertIn("not in its transcript", r["error"])
        self.assertTrue(failstop.paused()["paused"])

    def test_a_resumed_session_gets_the_rules_pointer(self):
        ka.preflight = lambda sid: (True, [], "resume")
        m = self.now_msg()
        self.assertEqual(self.deliver(m), "delivered")
        plan, msg = self.sent[0]
        self.assertEqual(plan, "resume")
        self.assertTrue(msg.startswith(inbox.marker(m["id"])))
        self.assertTrue(len(msg) > len(inbox.now_message(self.row(m), None)))   # + the standing rules / pointer

    def test_a_held_message_is_tried_again_by_the_next_pass(self):
        self.typing = "a dialog or menu is open on ka-abcdef01's pane"
        m = self.now_msg()
        self.assertEqual(iw.process(self.conn, verify=lambda *a: None), {"held": 1})
        self.typing = None
        self.assertEqual(iw.process(self.conn, verify=lambda *a: None), {"delivered": 1})
        self.assertEqual(self.row(m)["status"], "delivered")


# ---------------------------------------------------------------- the noted messages go along with the next message

class NotesInStarts(tc.Env):
    def setUp(self):
        super().setUp()
        conn = store.connect()
        try:
            with store.transaction(conn):
                conn.execute("DELETE FROM inbox_messages")
            self.ids = []
            for text in ("Goal: a dark mode, later.", "FYI: the host reboots on Sunday."):
                m = actions.perform(conn, "inbox.post", {"text": text, "session_id": SID}, actor="owner", via="dashboard")
                actions.perform(conn, "inbox.triage", {"message_id": m["id"], "verdict": "note", "reason": "r"},
                                actor="runner:inbox", via="runner")
                self.ids.append(m["id"])
        finally:
            conn.close()

    def statuses(self):
        conn = store.connect()
        try:
            return [(store.get_inbox(conn, i)["status"], store.get_inbox(conn, i)["included_by"]) for i in self.ids]
        finally:
            conn.close()

    def test_the_next_start_lists_them_and_marks_them_included(self):
        key = "window-start-2026-10-07"
        at = tc.NIGHT + timedelta(days=1)
        ka.handle_fire(SID, {"uuid": key, "timestamp": at, "budget": "b", "headroom": 14.0, "start_at": at},
                       "window start, CONTINUE: test", ka.load_state(), tc.Armed())
        msg = self.sent[0][1]
        self.assertTrue(msg.startswith("[keep-alive] A new run starts"))
        self.assertIn(f'#{self.ids[0]} (', msg)
        self.assertIn('"Goal: a dark mode, later."', msg)
        self.assertIn('"FYI: the host reboots on Sunday."', msg)
        self.assertLess(msg.index("dark mode"), msg.index("reboots"))          # oldest first
        self.assertEqual(self.statuses(), [("included", key)] * 2)
        self.assertEqual(ka.load_state()["handled"][key]["inbox_notes"], self.ids)
        # the next start has nothing more to list
        self.sent.clear()
        ka.handle_fire(SID, {"uuid": key + "-s2", "timestamp": at, "budget": "b", "headroom": 14.0, "start_at": at},
                       "window start, CONTINUE: test", ka.load_state(), tc.Armed())
        self.assertNotIn("dark mode", self.sent[0][1])

    def test_a_dry_run_marks_nothing(self):
        at = tc.NIGHT + timedelta(days=1)
        ka.handle_fire(SID, {"uuid": "k-dry", "timestamp": at, "budget": "b", "start_at": at}, "test",
                       ka.load_state(), tc.DryRun())
        self.assertEqual(self.statuses(), [("noted", None)] * 2)

    def test_the_post_compaction_message_lists_them(self):
        d, rec = self.compact()
        self.assertEqual(rec["status"], "compacted")
        after = [m for p, m in self.sent if m.startswith(tc.AFTER)][0]
        self.assertIn('"Goal: a dark mode, later."', after)
        self.assertLess(after.index("dark mode"), after.index("No run is going on now"))
        self.assertEqual([s for s, _ in self.statuses()], ["included"] * 2)
        self.assertEqual(rec["inbox_notes"], self.ids)

    def test_a_long_note_is_cut_with_a_pointer(self):
        conn = store.connect()
        try:
            long = []
            for c in "xy":
                m = actions.perform(conn, "inbox.post", {"text": c * 3000, "session_id": SID}, actor="owner",
                                    via="dashboard")
                actions.perform(conn, "inbox.triage", {"message_id": m["id"], "verdict": "note", "reason": "r"},
                                actor="runner:inbox", via="runner")
                long.append(m["id"])
            text, ids = inbox.notes_insert(conn, SID)
        finally:
            conn.close()
        self.assertEqual(ids, self.ids + long[:1])                            # the second long one waits (NOTES_MAX)
        self.assertIn("1 more: python3 tasks.py inbox list", text)
        self.assertIn(f"full text: python3 tasks.py inbox show {long[0]}", text)
        self.assertNotIn("x" * 1501, text)


# ---------------------------------------------------------------- held "now" messages go along with the next start (D-244)

class HeldInStarts(tc.Env):
    KEY = "window-start-2026-10-07"

    def setUp(self):
        super().setUp()
        conn = store.connect()
        try:
            with store.transaction(conn):
                conn.execute("DELETE FROM inbox_messages")
            self.held = self.msg(conn, "Why did the 04:00 run stop?\nPlease check.", "now")
            actions.perform(conn, "inbox.hold", {"message_id": self.held, "reason": "the owner holds the session (x)"},
                            actor="runner:inbox", via="runner")
            self.noted = self.msg(conn, "FYI: the host reboots on Sunday.", "note")
        finally:
            conn.close()

    def msg(self, conn, text, verdict):
        m = actions.perform(conn, "inbox.post", {"text": text, "session_id": SID}, actor="owner", via="dashboard")
        actions.perform(conn, "inbox.triage", {"message_id": m["id"], "verdict": verdict, "reason": "r"},
                        actor="runner:inbox", via="runner")
        return m["id"]

    def row(self, mid):
        conn = store.connect()
        try:
            return store.get_inbox(conn, mid)
        finally:
            conn.close()

    def start(self, args=None, key=KEY):
        at = tc.NIGHT + timedelta(days=1)
        return ka.handle_fire(SID, {"uuid": key, "timestamp": at, "budget": "b", "headroom": 14.0, "start_at": at},
                              "window start, CONTINUE: test", ka.load_state(), args or tc.Armed())

    def test_the_start_takes_it_along_marked_as_needing_an_answer(self):
        self.start()
        msg = self.sent[0][1]
        self.assertTrue(msg.startswith("[keep-alive] A new run starts"))
        self.assertIn(f'{inbox.marker(self.held)} (', msg)
        self.assertIn('"Why did the 04:00 run stop? ⏎ Please check."', msg)
        self.assertIn('python3 tasks.py inbox reply <id> "<your answer>"', msg)
        self.assertIn('"FYI: the host reboots on Sunday."', msg)
        self.assertLess(msg.index(inbox.marker(self.held)), msg.index("reboots"))   # held first, then the notes
        r = self.row(self.held)
        self.assertEqual((r["status"], r["delivered_via"], r["hold"]),
                         ("delivered", f"send-keys, with the start {self.KEY}", None))
        self.assertEqual(self.row(self.noted)["status"], "included")
        rec = ka.load_state()["handled"][self.KEY]
        self.assertEqual((rec["inbox_now"], rec.get("inbox_unseen")), ([self.held], None))
        self.assertEqual(self.alerts, [])
        self.assertFalse(failstop.paused()["paused"])
        self.sent.clear()                                                    # the next start: nothing more
        self.start(key=self.KEY + "-s2")
        self.assertNotIn(inbox.marker(self.held), self.sent[0][1])

    def test_not_in_the_transcript_is_an_error(self):
        def fire(sid, cwd, message, plan, new=False, **kw):                 # a reply, but the message never shows
            self.sent.append((plan, message))
            self.write([tc.assistant(datetime.now(UTC))], "a")
            return 0, "sent-keys ka-abcdef01", ""
        ka.fire = fire
        self.start()
        r = self.row(self.held)
        self.assertEqual(r["status"], "error")
        self.assertIn("not in the session's transcript", r["error"])
        self.assertEqual(ka.load_state()["handled"][self.KEY]["inbox_unseen"], [self.held])
        self.assertTrue(any("not in its transcript" in a for a in self.alerts))
        self.assertTrue(failstop.paused()["paused"])

    def test_not_in_the_transcript_pauses_only_its_project(self):
        """D-244: the keep-alive's inbox helpers pause the project of the session (PA), not all."""
        self.addCleanup(testenv.managed_project("PA", SID))
        def fire(sid, cwd, message, plan, new=False, **kw):
            self.sent.append((plan, message))
            self.write([tc.assistant(datetime.now(UTC))], "a")
            return 0, "sent-keys ka-abcdef01", ""
        ka.fire = fire
        self.start()
        self.assertEqual(self.row(self.held)["status"], "error")
        p = failstop.paused(session=SID)
        self.assertEqual((p["paused"], p["scope"], p["project"]), (True, "project", "PA"), p)
        self.assertIn("not in its transcript", p["reason"])
        self.assertFalse(failstop.paused()["paused"])

    def test_a_failed_send_leaves_it_held(self):
        ka.fire = lambda *a, **kw: (self.sent.append(a[2:4]), (1, "", "send-keys failed"))[1]
        self.start()
        r = self.row(self.held)
        self.assertEqual((r["status"], r["hold"] is not None), ("pending", True))   # the worker's deadline decides

    def test_a_dry_run_marks_nothing(self):
        self.start(tc.DryRun())
        self.assertEqual(self.row(self.held)["status"], "pending")

    def test_insert_cuts_long_texts_but_takes_all(self):
        conn = store.connect()
        try:
            long = [self.msg(conn, c * 2000, "now") for c in "xyz"]
            text, ids = inbox.held_insert(conn, SID)
        finally:
            conn.close()
        self.assertEqual(ids, [self.held] + long)                            # every one owes an answer
        self.assertIn(f"full text: python3 tasks.py inbox show {long[0]}", text)
        self.assertNotIn("x" * 1501, text)
        self.assertIn(f"{inbox.marker(long[2])} (", text)
        self.assertIn(f"its text: python3 tasks.py inbox show {long[2]}", text)   # past HELD_MAX: the id only
        self.assertNotIn("z" * 10, text)


class Watchdog(DB):
    def test_api_not_running_is_an_error_once_per_outage(self):
        """The */5 watchdog found the API down (D-244): alert + automation paused, once per outage."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        started = []
        saved = (inbox_api.take_lock, inbox_api.LOG, inbox_api.DOWN, inbox_api.subprocess.Popen)
        inbox_api.take_lock = lambda: open(os.devnull)
        inbox_api.LOG, inbox_api.DOWN = os.path.join(tmp.name, "api.log"), os.path.join(tmp.name, ".down")
        inbox_api.subprocess.Popen = lambda argv, **kw: started.append(argv)
        try:
            self.assertEqual(inbox_api.ensure(boot=True), 0)                  # the container start: no error
            self.assertEqual((len(started), self.alerts), (1, []))
            inbox_api.ensure(boot=False)
            inbox_api.ensure(boot=False)                                      # still down: not alerted again
        finally:
            inbox_api.take_lock, inbox_api.LOG, inbox_api.DOWN, inbox_api.subprocess.Popen = saved
        self.assertEqual(len(started), 3)
        self.assertEqual([a[0] for a in self.alerts], ["AFClaude STOPPED: inbox API was not running"])
        self.assertTrue(failstop.paused()["paused"])


# ---------------------------------------------------------------- the dashboard view (D-178)

class View(DB):
    def at(self, m, created, **more):
        with store.transaction(self.conn):
            self.conn.execute("UPDATE inbox_messages SET created_at=? WHERE id=?", (store.iso(created), m["id"]))
            for k, v in more.items():
                self.conn.execute(f"UPDATE inbox_messages SET {k}=? WHERE id=?", (store.iso(v) if isinstance(v, datetime) else v, m["id"]))

    def test_thread_and_reset(self):
        now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
        old, a, b = self.post("old"), self.post("q1"), self.post("q2")
        self.at(old, now - timedelta(hours=12), status="answered")
        self.at(a, now - timedelta(hours=4), status="answered", answered_at=now - timedelta(hours=3))
        self.at(b, now - timedelta(minutes=10), status="delivered")
        v = inbox.view(self.conn, now)
        self.assertEqual([m["text"] for m in v["messages"]], ["q1", "q2"])   # the 12 h old one: a new thread
        self.assertEqual(v["reset_hours"], 5.0)
        self.assertEqual(inbox.view(self.conn, now + timedelta(hours=6))["messages"], [])   # box empty again
        testenv.set_setting("ui_chat_reset_hours", 24)
        self.assertEqual(len(inbox.view(self.conn, now)["messages"]), 3)

    def test_each_state_has_its_mark_and_tooltip(self):
        """D-246: the box shows a mark per message, its tooltip says the rest (no labels on the page)."""
        now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
        at = now - timedelta(minutes=5)
        ms = [self.post(t) for t in ("sorting", "sending", "delivered", "noted", "failed")]
        self.at(ms[1], at, verdict="now")
        self.at(ms[2], at, verdict="now", status="delivered", delivered_at=at)
        self.at(ms[3], at, verdict="note", status="noted", triaged_at=at)
        self.at(ms[4], at, status="error", error="Haiku timed out", error_at=at)
        self.at(ms[0], at)
        v = inbox.view(self.conn, now)
        when = at.astimezone(inbox.BERLIN).strftime("%a %d.%m. %H:%M")
        self.assertEqual([(x["text"], x["state"], x["tip"]) for x in v["messages"]], [
            ("sorting", "sorting", f"Sent {when} · sorting"),
            ("sending", "sending", f"Passing it on to the task-manager · sent {when}"),
            ("delivered", "delivered", f"Delivered {when} · waiting for the answer"),
            ("noted", "noted", f"Saved for the next session · {when}"),
            ("failed", "error", f"Failed {when}: Haiku timed out")])
        self.assertEqual((v["busy"], v["chat"]), (True, False))

    def test_an_answer_makes_it_a_chat_until_the_reset(self):
        """D-246: a passed-on message's answer turns the box into a chat, a later one continues it,
        and D-178's reset empties it after ui_chat_reset_hours of quiet."""
        now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
        a, b = self.post("q1"), self.post("q2")
        self.at(a, now - timedelta(hours=2), verdict="now", status="answered", answer="a1", answered_at=now - timedelta(hours=1))
        self.at(b, now - timedelta(minutes=1), verdict="now", status="delivered", delivered_at=now - timedelta(minutes=1))
        v = inbox.view(self.conn, now)
        self.assertEqual([(x["text"], x["state"], x["answer"]) for x in v["messages"]],
                         [("q1", "answered", "a1"), ("q2", "delivered", None)])
        self.assertEqual((v["chat"], v["busy"]), (True, True))
        self.at(b, now - timedelta(minutes=1), status="answered", answer="a2", answered_at=now)
        v = inbox.view(self.conn, now + timedelta(hours=5, minutes=1))
        self.assertEqual((v["messages"], v["chat"], v["busy"]), ([], False, False))

    def test_a_noted_message_stays_until_it_is_included(self):
        """D-248: a noted message waits in the box (whatever the chat's reset) and clears once a
        message to the task-manager took it along; answered later, it shows in the chat."""
        now = datetime.now(UTC)
        n, q = self.post("note it"), self.post("q")
        self.at(n, now - timedelta(hours=9), verdict="note", status="noted", triaged_at=now - timedelta(hours=9))
        self.at(q, now - timedelta(hours=20), verdict="now", status="answered", answer="a", answered_at=now - timedelta(hours=19))
        v = inbox.view(self.conn, now)
        self.assertEqual([(x["text"], x["state"]) for x in v["messages"]], [("note it", "noted")])   # the chat reset
        self.assertEqual((v["chat"], v["busy"]), (False, False))
        self.run_("inbox.include", message_ids=[n["id"]], by="start")
        self.assertEqual(inbox.view(self.conn, now)["messages"], [])
        actions.perform(self.conn, "inbox.reply", {"message_id": n["id"], "answer": "done"}, actor="cli", via="cli")
        v = inbox.view(self.conn)                                             # its answer is the latest activity
        self.assertEqual([(x["text"], x["state"], x["answer"]) for x in v["messages"]], [("note it", "answered", "done")])
        self.assertTrue(v["chat"])

    def test_errors_stay_in_view(self):
        now = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
        m = self.post("failed one")
        self.at(m, now - timedelta(days=2), status="error", error="Haiku timed out", error_at=now - timedelta(days=2))
        v = inbox.view(self.conn, now)
        self.assertEqual([(x["text"], x["status"], x["error"]) for x in v["messages"]],
                         [("failed one", "error", "Haiku timed out")])

    def test_summary_has_no_texts(self):
        self.post("secret plans")
        s = inbox.summary(self.conn)
        self.assertEqual((s["waiting"], s["errors"]), (1, 0))
        self.assertNotIn("secret plans", json.dumps(s))

    def test_rate_limit(self):
        self.assertTrue(inbox.rate_ok(self.conn))
        for i in range(inbox.RATE_MAX):
            self.post(f"m{i}")
        self.assertFalse(inbox.rate_ok(self.conn))
        self.assertTrue(inbox.rate_ok(self.conn, datetime.now(UTC) + timedelta(hours=1, minutes=1)))


# ---------------------------------------------------------------- the endpoint

class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=10)
        self.path_ = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.path_)


class Endpoint(DB):
    KEY = "k" * 40

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.keyfile = os.path.join(self.tmp.name, "quickview.key")
        with open(self.keyfile, "w") as fh:
            fh.write(self.KEY + "\n")
        self.sock = os.path.join(self.tmp.name, "api", "inbox.sock")
        self.wake = threading.Event()
        self.srv = inbox_api.bind(self.sock, self.keyfile, self.wake)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()
        super().tearDown()

    def call(self, method, path="/afclaude/api/inbox", body=None, key=KEY, ctype="application/json", raw=None):
        c = UnixHTTP(self.sock)
        headers = {}
        if key is not None:
            headers["X-AFClaude-Key"] = key
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = ctype
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        out = (r.status, json.loads(r.read() or b"null"))
        c.close()
        return out

    def test_socket_mode(self):
        self.assertEqual(os.stat(self.sock).st_mode & 0o777, 0o666)
        self.assertEqual(os.stat(os.path.dirname(self.sock)).st_mode & 0o777, 0o755)

    def test_key_check(self):
        for key in (None, "", "wrong", self.KEY[:-1], self.KEY + "x"):
            with self.subTest(key=key):
                self.assertEqual(self.call("POST", body={"text": "hi"}, key=key)[0], 401)
                self.assertEqual(self.call("GET", key=key)[0], 401)
        self.assertEqual(store.list_inbox(self.conn), [])
        self.assertFalse(self.wake.is_set())

    def test_no_key_file_refuses_everything(self):
        os.unlink(self.keyfile)
        self.assertEqual(self.call("POST", body={"text": "hi"}, key="")[0], 503)
        self.assertEqual(self.call("GET")[0], 503)
        self.assertEqual(store.list_inbox(self.conn), [])

    def test_post_get_retry(self):
        st, m = self.call("POST", body={"text": "Line 1\nLine 2"})
        self.assertEqual((st, m["status"], m["text"]), (201, "pending", "Line 1\nLine 2"))
        self.assertTrue(self.wake.is_set())
        row = store.get_inbox(self.conn, m["id"])
        self.assertEqual((row["created_by"], row["session_id"]), ("owner", ka.afclaude_config.manager_session()))
        self.assertEqual(store.audit_log(self.conn, 1, "inbox", m["id"])[0]["via"], "dashboard")
        st, v = self.call("GET")
        self.assertEqual((st, [x["id"] for x in v["messages"]], v["max_chars"]), (200, [m["id"]], store.INBOX_TEXT_MAX))
        self.assertEqual(self.call("POST", f"/afclaude/api/inbox/{m['id']}/retry", {})[0], 409)   # not failed
        self.run_("inbox.fail", message_id=m["id"], error="x")
        st, r = self.call("POST", f"/afclaude/api/inbox/{m['id']}/retry", {})
        self.assertEqual((st, r["status"]), (200, "pending"))
        self.assertEqual(self.call("POST", "/afclaude/api/inbox/999/retry", {})[0], 404)

    def test_threads_and_dismiss(self):
        """D-304: GET lists every answered, undismissed thread, however old; POST .../dismiss removes it."""
        a, b, c = self.post("q1"), self.post("q2"), self.post("q3")
        for m in (a, b):
            self.run_("inbox.reply", message_id=m["id"], answer=f"a{m['id']}")
        self.conn.execute("UPDATE inbox_messages SET created_at='2026-01-01T00:00:00Z', "
                          "answered_at='2026-01-01T01:00:00Z' WHERE id=?", (a["id"],))   # long out of the chat
        self.conn.commit()
        st, v = self.call("GET")
        self.assertEqual([(t["id"], t["text"], t["answer"]) for t in v["threads"]],
                         [(a["id"], "q1", f"a{a['id']}"), (b["id"], "q2", f"a{b['id']}")])
        self.assertNotIn(a["id"], [x["id"] for x in v["messages"]])
        self.wake.clear()
        st, r = self.call("POST", f"/afclaude/api/inbox/{a['id']}/dismiss", {})
        self.assertEqual((st, r["id"]), (200, a["id"]))
        self.assertFalse(self.wake.is_set())                                  # nothing for the worker
        self.assertEqual(store.audit_log(self.conn, 1, "inbox", a["id"])[0]["via"], "dashboard")
        self.assertEqual([t["id"] for t in self.call("GET")[1]["threads"]], [b["id"]])
        self.assertEqual(self.call("POST", f"/afclaude/api/inbox/{a['id']}/dismiss", {})[0], 409)
        self.assertEqual(self.call("POST", f"/afclaude/api/inbox/{c['id']}/dismiss", {})[0], 409)   # unanswered
        self.assertEqual(self.call("POST", f"/afclaude/api/inbox/{b['id']}/dismiss", {}, key="wrong")[0], 401)
        self.assertEqual(self.call("GET", f"/afclaude/api/inbox/{b['id']}/dismiss")[0], 405)

    def test_notices_and_dismiss(self):
        """D-306: GET lists the shown notices; POST .../notice/<id>/dismiss runs notice.dismiss (owner, audited)."""
        with store.transaction(self.conn):
            self.conn.execute("DELETE FROM notices")
        n = actions.perform(self.conn, "notice.post", {"title": "Review ready", "text": "the summary"},
                            actor="cli", via="cli")
        st, v = self.call("GET")
        self.assertEqual([(x["id"], x["title"], x["text"]) for x in v["notices"]], [(n["id"], "Review ready", "the summary")])
        self.assertEqual(self.call("GET", f"/afclaude/api/inbox/notice/{n['id']}/dismiss")[0], 405)
        self.assertEqual(self.call("POST", f"/afclaude/api/inbox/notice/{n['id']}/dismiss", {}, key="wrong")[0], 401)
        st, r = self.call("POST", f"/afclaude/api/inbox/notice/{n['id']}/dismiss", {})
        self.assertEqual((st, r["id"]), (200, n["id"]))
        audit = store.audit_log(self.conn, 1, "notice", n["id"])[0]
        self.assertEqual((audit["action"], audit["actor"], audit["via"]), ("notice.dismiss", "owner", "dashboard"))
        self.assertEqual(self.call("GET")[1]["notices"], [])
        self.assertEqual(self.call("POST", f"/afclaude/api/inbox/notice/{n['id']}/dismiss", {})[0], 409)
        self.assertEqual(self.call("POST", "/afclaude/api/inbox/notice/999/dismiss", {})[0], 404)

    def test_request_checks(self):
        self.assertEqual(self.call("POST", raw=b"text=hi", ctype="application/x-www-form-urlencoded")[0], 415)
        self.assertEqual(self.call("POST", raw=b"text=hi", ctype="text/plain")[0], 415)
        self.assertEqual(self.call("POST", raw=b"{not json")[0], 400)
        self.assertEqual(self.call("POST", raw=b"[1]")[0], 400)
        self.assertEqual(self.call("POST", body={"text": "hi", "session_id": SID})[0], 400)   # no session choice
        self.assertEqual(self.call("POST", body={"text": "   "})[0], 400)
        self.assertEqual(self.call("POST", raw=json.dumps({"text": "x" * 20000}).encode())[0], 413)
        self.assertEqual(self.call("PUT", body={"text": "hi"})[0], 405)
        self.assertEqual(self.call("GET", "/afclaude/api/other")[0], 404)
        self.assertEqual(self.call("GET", "/afclaude/api/inbox/1/retry")[0], 405)
        self.assertEqual(store.list_inbox(self.conn), [])

    def test_rate_limit(self):
        for i in range(inbox.RATE_MAX):
            self.assertEqual(self.call("POST", body={"text": f"m{i}"})[0], 201)
        st, err = self.call("POST", body={"text": "one more"})
        self.assertEqual(st, 429)
        self.assertIn("last hour", err["error"])

    def test_serve_refuses_to_start_without_a_key(self):
        old = inbox_api.KEY_FILE, inbox_api.LOCK
        inbox_api.KEY_FILE = os.path.join(self.tmp.name, "missing.key")
        inbox_api.LOCK = os.path.join(self.tmp.name, "lock")
        try:
            self.assertEqual(inbox_api.serve(os.path.join(self.tmp.name, "x.sock")), 1)
        finally:
            inbox_api.KEY_FILE, inbox_api.LOCK = old
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "x.sock")))


# ---------------------------------------------------------------- the CLI and the quickview

class CLI(DB):
    def setUp(self):
        testenv.fresh_db(self)       # `inbox` also lists blocked tasks: none from another module
        super().setUp()

    def cli(self, *argv):
        out = []
        old = sys.stdout
        sys.stdout = types.SimpleNamespace(write=out.append, flush=lambda: None)
        try:
            rc = tasks.main(["--db", store.DB_PATH] + list(argv))
        finally:
            sys.stdout = old
        return rc, "".join(out)

    def test_list_show_reply(self):
        m = self.post("Did the 23:00 run start?")
        rc, out = self.cli("inbox", "list")
        self.assertEqual(rc, 0)
        self.assertIn(f"#{m['id']}  pending", out)
        rc, out = self.cli("inbox", "reply", str(m["id"]), "Yes, at 23:00.")
        self.assertEqual((rc, out.strip()), (0, f"inbox #{m['id']}: answered"))
        r = store.get_inbox(self.conn, m["id"])
        self.assertEqual((r["answer"], r["status"]), ("Yes, at 23:00.", "answered"))
        self.assertEqual(store.audit_log(self.conn, 1, "inbox", m["id"])[0]["actor"], "cli")
        rc, out = self.cli("inbox", "show", str(m["id"]))
        self.assertIn("answer (", out)
        rc, out = self.cli("inbox", "list")
        self.assertIn("no open messages", out)                                # answered: not open
        self.assertIn(f"#{m['id']}", self.cli("inbox", "list", "--all")[1])

    def test_old_inbox_still_works(self):
        rc, out = self.cli("inbox", "--no-scan")
        self.assertEqual((rc, out.strip()), (0, "nothing needs your input"))


class Quickview(DB):
    def test_export_has_only_counts(self):
        self.post("my private note")
        b = export_quickview.inbox_block()
        self.assertEqual(b["waiting"], 1)
        self.assertNotIn("my private note", json.dumps(b))

    def test_page_has_the_box(self):
        with open(os.path.join(HERE, "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        box = page.index('id="s-msg"')
        self.assertLess(page.index('id="s-input"'), box)                      # right after "needs your input"
        self.assertLess(box, page.index("Keep-alive &amp; usage"))
        for s in ('id="msg-log"', 'id="msg-form"', 'id="msg-text"', 'id="msg-send"', 'API = BASE + "api/inbox"',
                  '"Content-Type": "application/json"', "data-retry", "/retry", "m.answer", "m.error", "m.state", "m.tip",
                  "data-tip", "box.chat", "box.busy", "s-spin", "s-check", "s-err", "Ctrl+Enter"):
            self.assertIn(s, page, s)
        for s in ("box.threads", "data-dismiss", '"/dismiss"', "m.answered_berlin"):   # D-304: answered threads
            self.assertIn(s, page, s)
        for s in ("box.notices", "data-notice", '"/notice/"', "n.title", "n.text"):   # D-306: notices
            self.assertIn(s, page, s)
        for s in ("m.verdict_reason", "m.hold", "msg-list", "noted for the next session"):   # D-246: no thread list, no labels
            self.assertNotIn(s, page, s)
        self.assertNotIn("arguments.callee", page)                            # "use strict"

    def test_page_marks_every_state(self):
        """Each state inbox.view sends has a mark on the page (MARK) except answered (the chat)."""
        with open(os.path.join(HERE, "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        line = next(x for x in page.splitlines() if "var MARK = {" in x)
        marked = set(re.findall(r"(\w+): \"s-", line))
        self.assertEqual(marked | {"answered"}, set(inbox.BUSY_STATES) | {"noted", "error", "answered"})

    def test_page_markup_is_balanced(self):
        """The page's static HTML: every element closed in order, ids unique (html.parser)."""
        class P(html.parser.HTMLParser):
            VOID = {"meta", "link", "br", "img", "input", "hr", "source", "path"}

            def __init__(self):
                super().__init__()
                self.stack, self.ids, self.bad = [], [], []

            def handle_starttag(self, tag, attrs):
                self.ids += [v for k, v in attrs if k == "id"]
                if tag not in self.VOID:
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if not self.stack or self.stack.pop() != tag:
                    self.bad.append(tag)

        with open(os.path.join(HERE, "quickview", "AFClaude.html")) as fh:
            p = P()
            p.feed(fh.read())
        self.assertEqual((p.stack, p.bad), ([], []))
        self.assertEqual(len(p.ids), len(set(p.ids)))
        self.assertIn("msg-log", p.ids)

    def test_nginx_proxies_only_the_api(self):
        with open(os.path.join(HERE, "quickview", "nginx.conf")) as fh:
            conf = fh.read()
        self.assertIn("location /afclaude/api/ {", conf)
        self.assertIn("proxy_pass http://unix:/run/afclaude-api/inbox.sock;", conf)
        self.assertLess(conf.index("if ($afc_key_ok = 0)"), conf.index("location /afclaude/api/"))   # key first
        self.assertIn("limit_except GET HEAD { deny all; }", conf)           # the rest stays read-only


if __name__ == "__main__":
    unittest.main()
