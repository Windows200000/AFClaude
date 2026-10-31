#!/usr/bin/env python3
"""Offline tests for D-252: no Haiku triage while the task-manager is active (inbox_worker.
manager_active). A message for a session with a keep-alive run going, a turn running or a request
within ACTIVE_WINDOW is "now" without the Haiku call and goes through the normal delivery; an idle
session's message is triaged as before; a failed activity check is an error on the message and
pauses its project (D-235/D-244), with no triage and no delivery in its place. D-262: the
keep-alive's compaction flow is no activity: from its start and after it messages are triaged, a
"now" verdict made during it goes in after its READY. A temp DB, the fake tmux session and
transcript of test_compaction.Env, a fake Haiku; no real Claude, no tmux."""
import functools
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
        self.verdict = ("note", "a goal for later")
        self._roh = host.run_on_host
        host.run_on_host = lambda argv, **kw: (self.haiku.append(argv), types.SimpleNamespace(
            returncode=0, stdout=haiku_out(*self.verdict), stderr=""))[1]

    def manage(self, d):
        return testenv.managed_project("PA", SID, path=os.path.join(d, "pa"))   # SID's project (tc.Env)

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
        self.typing = "a draft in afc-abcdef01's input box (12 characters)"
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


class DuringAndAfterCompaction(Active):
    """D-262: the compaction flow (save prompt, hand-off, /compact, compact_after, READY) is no
    activity; a "now" verdict made during it waits for the READY."""

    def setUp(self):
        super().setUp()
        self.during = {}                    # a message prefix the flow types -> called once right after it
        ka.fire = self.hooked_fire

    def hooked_fire(self, sid, cwd, message, plan, new=False, **kw):
        out = self.fake_fire(sid, cwd, message, plan, new, **kw)
        for prefix in list(self.during):
            if message.startswith(prefix):
                self.during.pop(prefix)()
        return out

    def flow_record(self, start, end=None, status=None, save_sent=True, sid=SID):
        rec = {"at": start.isoformat(), "status": status or ("running" if end is None else "compacted"),
               "session": sid, "steps": {"save": {"status": "saved"} | ({"sent_at": start.isoformat()}
                                                                          if save_sent else {})}}
        if end is not None:
            rec["done_at"] = end.isoformat()
        st = ka.load_state()
        st.setdefault("compactions", {})[f"compact-{start.isoformat()}"] = rec
        ka.save_state(st)

    def test_triaged_during_the_flow_and_a_now_one_goes_in_after_the_ready(self):
        self.verdict = ("now", "a question")
        # the flow's replies are requests (context 150k, written now): under D-252 alone they'd make
        # the session active
        old = tc.assistant
        tc.assistant = functools.partial(old, ctx=150_000)
        self.addCleanup(setattr, tc, "assistant", old)
        msgs, seen = [], []

        def at_save():                      # the save prompt is out, its reply written: the flow runs
            msgs.append(self.post("What's the state of the run?"))
            seen.append(("save", self.process(), self.row(msgs[0])["hold"]))

        def at_compact():
            seen.append(("compact", self.process(), self.row(msgs[0])["hold"]))
        self.during = {tc.SAVE: at_save, "/compact ": at_compact}
        d, rec = self.compact()
        self.assertEqual((d.due, rec["status"]), (True, "compacted"))
        self.assertEqual(self.haiku, [ka.POKE_ARGV])                            # triaged, not "active"
        r = self.row(msgs[0])
        self.assertEqual((r["verdict"], r["verdict_reason"]), ("now", "a question"))
        self.assertEqual(seen, [("save", {"now": 1, "held": 1}, iw.HOLD_LOCKED),
                                ("compact", {"held": 1}, iw.HOLD_LOCKED)])     # never typed in mid-flow
        self.assertEqual([m[:20] for _, m in self.sent], [tc.SAVE[:20], "/compact Keep what t", tc.AFTER[:20]])
        # READY is in: the next pass delivers it, through the usual checks
        self.assertEqual(self.process(), {"delivered": 1})
        self.assertEqual(self.row(msgs[0])["status"], "delivered")
        self.assertTrue(self.sent[-1][1].startswith(inbox.marker(msgs[0]["id"])))
        self.assertFalse(failstop.paused(session=SID)["paused"])

    def test_after_the_flow_its_turns_are_no_activity(self):
        old = tc.assistant
        tc.assistant = functools.partial(old, ctx=150_000)
        self.addCleanup(setattr, tc, "assistant", old)
        d, rec = self.compact()
        self.assertEqual(rec["status"], "compacted")
        ct._CACHE.clear()
        sc = ct.scan(self.path)
        self.assertLess(datetime.now(UTC) - sc.requests[-1].ts, iw.ACTIVE_WINDOW)   # the READY reply
        m = self.post("Goal for later: a dark mode.")
        self.assertEqual(self.process(), {"note": 1})
        self.assertEqual(self.haiku, [ka.POKE_ARGV])
        self.assertEqual(self.row(m)["status"], "noted")
        # a prompt after the flow: the task-manager works again (D-252)
        t = datetime.now(UTC) + timedelta(seconds=2)
        self.write([tc.owner(t), old(t + timedelta(seconds=1), ctx=160_000)], "a")
        self.make_old()
        self.assertIn("its last request was", iw.manager_active(SID, t + timedelta(minutes=1)))

    def test_activity_before_the_flow_no_longer_counts_after_it(self):
        """The run's last request 8 min ago, then a 4-minute flow: triaged (D-262: after the
        compaction it goes to triage)."""
        now = datetime.now(UTC)
        start, end = now - timedelta(minutes=6), now - timedelta(minutes=2)
        self.write([tc.owner(now - timedelta(minutes=9)), tc.assistant(now - timedelta(minutes=8), ctx=300_000),
                    tc.keepalive_msg(start + timedelta(seconds=5), tc.SAVE + " ..."),
                    tc.assistant(start + timedelta(seconds=30), text="HANDOFF SAVED", ctx=300_500),
                    tc.keepalive_msg(end - timedelta(seconds=30), tc.AFTER + " ..."),
                    tc.assistant(end - timedelta(seconds=10), text="READY", ctx=30_000)])
        self.make_old()
        self.assertEqual(iw.manager_active(SID, now), "its last request was 2 min ago")   # the READY, without the record
        self.flow_record(start, end)
        self.assertIsNone(iw.manager_active(SID, now))
        # a turn still running whose prompt came before the flow's end (a failed flow's tail) is no activity
        self.write([tc.assistant(now, stop="tool_use", ctx=30_100)], "a")
        self.assertIsNone(iw.manager_active(SID, now))

    def test_while_the_flow_runs_nothing_counts(self):
        now = datetime.now(UTC)
        self.write([tc.owner(now - timedelta(minutes=3)), tc.keepalive_msg(now - timedelta(minutes=1), tc.SAVE),
                    tc.assistant(now, stop="tool_use", ctx=200_000)])           # a turn running, a request now
        self.flow_record(now - timedelta(minutes=2))
        self.assertIsNone(iw.manager_active(SID, now))

    def test_a_running_record_holds_the_delivery_without_the_lock(self):
        """A keep-alive whose session lock this process can't see: its "running" record holds it."""
        self.run_going()
        self.flow_record(self.now - timedelta(minutes=1))
        self.verdict = ("now", "a question")
        m = self.post()
        self.assertEqual(self.process(), {"now": 1, "held": 1})
        r = self.row(m)
        self.assertTrue(r["hold"].startswith(iw.HOLD_COMPACTING), r["hold"])
        self.assertEqual(self.haiku, [ka.POKE_ARGV])                            # triaged: the run is over
        self.assertEqual(self.sent, [])

    def test_a_stale_running_record_is_over_and_alerted_once(self):
        now = datetime.now(UTC)
        start = now - iw.COMPACT_FLOW_MAX - timedelta(minutes=3)              # "over" 3 min ago
        self.write([tc.owner(now - timedelta(minutes=2)), tc.assistant(now - timedelta(minutes=1), ctx=200_000)])
        self.make_old()
        self.flow_record(start)
        iw._STALE_FLOWS.clear()
        self.assertEqual(iw.manager_active(SID, now), "its last request was 1 min ago")
        self.assertEqual(iw.compaction_flow(SID, ka.load_state(), now), (start, start + iw.COMPACT_FLOW_MAX))
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("still recorded as running", self.alerts[0])

    def test_a_failed_attempt_without_a_save_prompt_was_no_flow(self):
        now = datetime.now(UTC)
        self.write([tc.owner(now - timedelta(minutes=4)), tc.assistant(now - timedelta(minutes=3), ctx=200_000)])
        self.make_old()
        self.flow_record(now - timedelta(minutes=5), now - timedelta(minutes=1), status="failed", save_sent=False)
        self.assertEqual(iw.manager_active(SID, now), "its last request was 3 min ago")
        self.flow_record(now - timedelta(minutes=5), now - timedelta(minutes=1), status="failed")
        self.assertIsNone(iw.manager_active(SID, now))

    def test_another_sessions_flow_does_not_count(self):
        now = datetime.now(UTC)
        self.write([tc.owner(now - timedelta(minutes=4)), tc.assistant(now - timedelta(minutes=3), ctx=200_000)])
        self.make_old()
        self.flow_record(now - timedelta(minutes=2), sid="ffffffff-0000-4000-8000-000000000000")
        self.assertEqual(iw.manager_active(SID, now), "its last request was 3 min ago")


if __name__ == "__main__":
    unittest.main()
