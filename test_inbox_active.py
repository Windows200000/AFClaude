#!/usr/bin/env python3
"""Offline tests for D-252: no Haiku triage while the task-manager is active (inbox_worker.
manager_active). A message for a session with a keep-alive run going, a turn running or a request
within ACTIVE_WINDOW is "now" without the Haiku call and goes through the normal delivery; an idle
session's message is triaged as before; a failed activity check is an error on the message and
pauses its project (D-235/D-244), with no triage and no delivery in its place. A temp DB, the fake
tmux session and transcript of test_compaction.Env, a fake Haiku; no real Claude, no tmux."""
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import compact_timing as ct  # noqa: E402
import failstop  # noqa: E402
import host  # noqa: E402
import inbox  # noqa: E402
import inbox_worker as iw  # noqa: E402
import keepalive as ka  # noqa: E402
import runner_state  # noqa: E402
import store  # noqa: E402
import test_compaction as tc  # noqa: E402  (its Env: a fake tmux session and transcript)
from test_inbox import haiku_out  # noqa: E402

UTC = timezone.utc
SID = tc.SID


class Active(tc.Env):
    def setUp(self):
        super().setUp()
        self.conn = store.connect()
        with store.transaction(self.conn):
            self.conn.execute("DELETE FROM inbox_messages")
        self.notified = []
        self._notify = failstop._notify
        failstop._notify = lambda subject, body: self.notified.append(subject)
        self.haiku = []
        self._roh = host.run_on_host
        host.run_on_host = lambda argv, **kw: (self.haiku.append(argv), types.SimpleNamespace(
            returncode=0, stdout=haiku_out("note", "a goal for later"), stderr=""))[1]
        self.addCleanup(testenv.managed_project("PA", SID))

    def tearDown(self):
        host.run_on_host = self._roh
        failstop._notify = self._notify
        self.conn.close()
        super().tearDown()

    def post(self, text="Resume automation as well"):
        return actions.perform(self.conn, "inbox.post", {"text": text, "session_id": SID}, actor="owner",
                               via="dashboard")

    def process(self):
        return iw.process(self.conn, now=datetime.now(UTC), verify=lambda *a: None)

    def row(self, m):
        return store.get_inbox(self.conn, m["id"])

    def run_going(self):
        """A window-start fired 1 h ago, its run still writing (the transcript 10 min old, its last
        request 20 min ago: only the run makes the session active)."""
        fire = self.now - timedelta(hours=1)
        ka.save_state({"handled": {"window-start-x": {"at": fire.isoformat(), "result": "continued-in-place",
                                                      "session": SID}}, "fires": {}})
        self.write([tc.keepalive_msg(fire), tc.assistant(self.now - timedelta(minutes=20), ctx=200_000)])
        self.make_old()

    def assert_delivered_untriaged(self, m, out, why):
        r = self.row(m)
        self.assertEqual(out, {"now": 1, "delivered": 1})
        self.assertEqual((r["status"], r["verdict"], r["triage_cost_usd"]), ("delivered", "now", None))
        self.assertIn("task-manager active", r["verdict_reason"])
        self.assertIn(why, r["verdict_reason"])
        self.assertEqual(self.haiku, [])                                       # no Haiku call (D-252)
        plan, msg = self.sent[0]
        self.assertEqual(plan, "send-keys")                                    # the normal delivery path
        self.assertTrue(msg.startswith(inbox.marker(m["id"])))
        self.assertFalse(failstop.paused(session=SID)["paused"])

    def test_active_because_of_a_run(self):
        self.run_going()
        m = self.post()
        self.assert_delivered_untriaged(m, self.process(), "a keep-alive run is going")

    def test_active_because_of_a_recent_request(self):
        self.write([tc.owner(self.now - timedelta(minutes=4)), tc.assistant(self.now - timedelta(minutes=3),
                                                                             ctx=200_000)])
        self.make_old()
        m = self.post()
        self.assert_delivered_untriaged(m, self.process(), "its last request was 3 min ago")

    def test_active_because_a_turn_is_running(self):
        self.write([tc.owner(self.now - timedelta(minutes=40)),
                    tc.assistant(self.now - timedelta(minutes=30), stop="tool_use", ctx=200_000)])
        self.assertEqual(iw.manager_active(SID, datetime.now(UTC)), "a turn is running")

    def test_a_delivery_check_still_holds_an_untriaged_message(self):
        """No triage is no shortcut past the delivery's checks: the owner typing holds it."""
        self.run_going()
        self.typing = "a draft in ka-abcdef01's input box (12 characters)"
        m = self.post()
        out = self.process()
        r = self.row(m)
        self.assertEqual(out, {"now": 1, "held": 1})
        self.assertEqual((r["status"], r["verdict"], r["hold"]), ("pending", "now", self.typing))
        self.assertEqual((self.sent, self.haiku), ([], []))

    def test_not_active_so_haiku_triages(self):
        """The default transcript: the last request 55 min ago, no run: triaged as before."""
        self.assertGreater(timedelta(minutes=55), iw.ACTIVE_WINDOW)
        m = self.post("Goal for later: a dark mode.")
        out = self.process()
        r = self.row(m)
        self.assertEqual(out, {"note": 1})
        self.assertEqual((r["status"], r["verdict"], r["verdict_reason"], r["triage_cost_usd"]),
                         ("noted", "note", "a goal for later", 0.0012))
        self.assertEqual(self.haiku, [ka.POKE_ARGV])
        self.assertEqual(self.sent, [])

    def test_a_stopped_run_and_an_old_request_are_not_active(self):
        self.run_going()
        ka.mark_run_stopped(SID, self.now - timedelta(minutes=15))
        self.assertIsNone(iw.manager_active(SID, datetime.now(UTC)))

    def assert_check_failed(self, m, out, what):
        r = self.row(m)
        self.assertEqual(out, {"failed": 1})
        self.assertEqual((r["status"], r["verdict"]), ("error", None))
        self.assertIn("inbox activity check failed", r["error"])
        self.assertIn(what, r["error"])
        self.assertEqual((self.haiku, self.sent), ([], []))                    # no triage, no delivery instead
        p = failstop.paused(session=SID)
        self.assertEqual((p["paused"], p["scope"], p["project"]), (True, "project", "PA"))
        self.assertIn("inbox: inbox activity check failed", p["reason"])
        self.assertFalse(failstop.paused()["paused"])                         # only the message's project
        self.assertEqual(self.notified, ["AFClaude STOPPED (PA): inbox activity check failed"])
        self.assertEqual(self.process(), {})                                   # not retried on its own

    def test_an_unreadable_transcript_is_an_error_and_a_pause(self):
        def boom(path):
            raise PermissionError(13, "Permission denied", path)
        old, ct.scan_cached = ct.scan_cached, boom
        self.addCleanup(setattr, ct, "scan_cached", old)
        m = self.post()
        self.assert_check_failed(m, self.process(), "PermissionError")

    def test_an_unreadable_keepalive_state_is_an_error_and_a_pause(self):
        def unavailable(path=None):
            doc = runner_state.StateDoc({"handled": {}, "fires": {}})
            doc.unavailable = True
            return doc
        old, ka.load_state = ka.load_state, unavailable
        self.addCleanup(setattr, ka, "load_state", old)
        m = self.post()
        self.assert_check_failed(m, self.process(), "the keepalive state can't be read")


if __name__ == "__main__":
    unittest.main()
