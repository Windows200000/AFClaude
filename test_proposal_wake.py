#!/usr/bin/env python3
"""Tests for phase 5a A5 part 2 (docs/phase5a_plan.md §3.8, §5 A5): a separate session's proposal reaches its
task-manager through the inbox, and a task-manager with no run going is woken through run-now (D-188, D-194,
D-186). Temp DBs only; no session, tmux or signal is touched (keepalive's send and start paths are fakes).

  capture   roles_enforce=on: a driven agent session's proposal leaves one inbox notice (origin `proposal`,
            "now" at once) for the project's task-manager and wakes the runner; a subagent of the task-manager
            leaves none (its summary comes through the hooks); a shadow row never leaves one; a replayed key
            leaves no second
  deliver   a run of the task-manager going: the notice is typed in through the inbox's own send path
  wake      no run going: one proposal_review request (no window or budget gate, the preflight applies), the
            notice held until the wake's start takes it along; a second proposal while it is open adds no
            request; a refused wake is not repeated for the same notice; paused: no wake; a failed wake alerts
            and pauses (D-235/D-282)

    python3 -m unittest test_proposal_wake
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (temp DB first)
import actions  # noqa: E402
import failstop  # noqa: E402
import inbox  # noqa: E402
import inbox_worker  # noqa: E402
import keepalive as ka  # noqa: E402
import run_now  # noqa: E402
import runner  # noqa: E402
import store  # noqa: E402
import test_proposals  # noqa: E402
import test_run_now  # noqa: E402
from test_run_now import NOW, SID_A  # noqa: E402


# ---------------------------------------------------------------- capture

class Capture(test_proposals.Proposals):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(runner, "wake", mock.Mock(return_value=True))
        self.wake = p.start()
        self.addCleanup(p.stop)

    def notices(self):
        return [m for m in store.list_inbox(self.conn, limit=100) if m["origin"] == store.PROPOSAL_ORIGIN]

    def test_a_driven_sessions_proposal_leaves_one_notice_for_its_task_manager(self):
        self.go_on()
        before = (inbox.summary(self.conn), [m["id"] for m in inbox.thread(self.conn)])
        pid = self.propose("task.edit", task_id=self.task, title="renamed")
        (m,) = self.notices()
        self.assertEqual((m["session_id"], m["status"], m["verdict"], m["created_by"]),
                         (self.sess["A"], "pending", "now", self.agent))
        self.assertTrue(m["text"].startswith(f"P-{pid} by {self.agent} (task.edit): "), m["text"])
        self.wake.assert_called_once_with()
        # not the owner's: no count, no chat line
        self.assertEqual((inbox.summary(self.conn), [m["id"] for m in inbox.thread(self.conn)]), before)

    def test_a_subagent_of_the_task_manager_leaves_none(self):
        self.go_on()
        with self.assertRaises(actions.Proposed):
            actions.perform(self.conn, "task.edit", {"task_id": self.task, "title": "x"},
                            actor=f"agent:{self.sess['A']}:sub1", via="mcp", autonomous=True)
        self.assertEqual(self.notices(), [])
        self.wake.assert_not_called()

    def test_a_shadow_row_never_notifies_or_wakes(self):
        self.as_agent("task.edit", task_id=self.task, title="renamed")      # shadow: applied, decided at birth
        (row,) = self.rows()
        self.assertTrue(row["shadow"])
        self.assertEqual(self.notices(), [])
        self.wake.assert_not_called()

    def test_a_replayed_key_leaves_no_second_notice(self):
        self.go_on()
        for _ in range(2):
            with self.assertRaises(actions.Proposed):
                actions.perform(self.conn, "task.edit", {"task_id": self.task, "title": "x"}, actor=self.agent,
                                via="mcp", autonomous=True, key="replay-0001")
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.wake.call_count, 1)


# ---------------------------------------------------------------- delivery and wake

class Wake(test_run_now.Base):
    """alpha's task-manager SID_A; keepalive's preflight / stall status / start / run state are test_run_now's
    fakes (self.running, self.held, self.limited, self.reply)."""

    def setUp(self):
        super().setUp()
        fd, self.transcript = tempfile.mkstemp(dir=self.dir, suffix=".jsonl")
        os.close(fd)
        self.sent = []
        self.inbox_alerts = []
        self._notify = failstop._notify
        failstop._notify = lambda subject, body: self.inbox_alerts.append(subject)
        self.addCleanup(setattr, failstop, "_notify", self._notify)
        for obj, n, v in ((ka, "current_session", lambda sid: sid), (ka, "transcript_path", lambda sid: self.transcript),
                          (ka, "session_cwd", lambda path: self.dir), (ka, "fire", self.fake_fire),
                          (inbox_worker, "load_state", lambda: {"handled": {}, "compactions": {}}),
                          (inbox_worker, "delivery_hold", lambda sid, path, now: (None, "send-keys")),
                          (inbox_worker, "log", lambda msg: None), (runner, "wake", mock.Mock(return_value=True))):
            p = mock.patch.object(obj, n, v)
            p.start()
            self.addCleanup(p.stop)
        self.pid = store.get_project(self.conn, "alpha")["id"]

    def fake_fire(self, sid, cwd, msg, plan, **kw):
        self.sent.append((sid, msg, plan))
        return 0, "", ""

    def notice(self, n=1):
        m = store.add_proposal_notice(self.conn, SID_A, f"P-{n} by agent:x (task.edit): task.edit #3: title='x'",
                                      "agent:x")
        self.conn.commit()
        return m["id"]

    def process(self):
        return inbox_worker.process(self.conn, NOW, verify=lambda *a: None)

    def msg(self, mid):
        return store.get_inbox(self.conn, mid)

    def wakes(self):
        return [r for r in store.list_requests(self.conn, limit=100) if r["kind"] == "proposal_review"]

    def test_a_running_task_manager_gets_the_notice_typed_in(self):
        self.running = True
        mid = self.notice()
        self.assertEqual(self.process(), {"delivered": 1})
        ((sid, msg, plan),) = self.sent
        self.assertEqual((sid, plan), (SID_A, "send-keys"))
        self.assertIn(inbox.marker(mid), msg)                        # the post-send check looks for it
        self.assertIn("proposal", msg)
        self.assertIn("tasks.py review", msg)
        self.assertEqual(self.msg(mid)["status"], "delivered")
        self.assertEqual(self.wakes(), [])

    def test_an_idle_task_manager_is_woken_once_without_window_or_budget_gate(self):
        mid = self.notice()
        self.assertEqual(self.process(), {"held": 1})
        (req,) = self.wakes()
        self.assertEqual((req["status"], req["target"], req["actor"]), ("open", str(self.pid), inbox_worker.ACTOR))
        self.assertTrue(self.msg(mid)["hold"].startswith(inbox_worker.WAKE_HOLD))
        self.assertEqual(self.sent, [])                              # nothing typed into an idle session
        runner.wake.assert_called()
        # the second proposal while the request is open, and the next passes: no second request
        mid2 = self.notice(2)
        self.assertEqual(self.process(), {"held": 2})
        self.assertEqual(len(self.wakes()), 1)
        self.assertTrue(inbox_worker.woken(self.msg(mid2)))
        # the runner's request job: a run-now start at midday (outside the window), no budget gate (the
        # fakes raise on one), the preflight applies
        row, _ = self.decide(req)
        self.assertEqual(row["status"], "started")
        self.assertEqual(self.preflights, [SID_A])
        sid, stall, reason = self.fired[0]
        self.assertTrue(stall["run_now"])
        self.assertIn("proposal review request", reason)
        self.assertIn("D-194", reason)

    def test_the_wakes_start_brings_the_notices_along(self):
        a, b = self.notice(1), self.notice(2)
        text, ids = inbox.held_insert(self.conn, SID_A)
        self.assertEqual(ids, [a, b])
        self.assertIn(inbox.marker(a), text)
        self.assertIn("P-2 by agent:x", text)
        self.assertNotIn("The owner's messages", text)              # not framed as the owner's words

    def test_the_preflight_still_applies(self):
        self.notice()
        self.process()
        (req,) = self.wakes()
        self.held = True                                             # the owner holds the session
        row, _ = self.decide(req)
        self.assertEqual(row["status"], "refused")
        self.assertEqual(self.fired, [])

    def test_a_refused_wake_is_not_repeated_for_the_same_notice(self):
        mid = self.notice()
        self.process()
        self.limited = True                                          # at its limit (D-204): refused
        row, _ = self.decide(self.wakes()[0])
        self.assertEqual(row["status"], "refused")
        for _ in range(3):
            self.assertEqual(self.process(), {"held": 1})
        self.assertEqual(len(self.wakes()), 1)                      # no loop
        self.assertIn("which was refused", self.msg(mid)["hold"])
        self.notice(2)                                               # a new proposal: one new try
        self.process()
        self.assertEqual(len(self.wakes()), 2)

    def test_paused_automation_wakes_no_one(self):
        actions.perform(self.conn, "automation.set_project", {"project": "alpha", "paused": True, "reason": "x"},
                        actor="cli", via="cli")
        mid = self.notice()
        self.assertEqual(self.process(), {"held": 1})
        self.assertEqual(self.wakes(), [])
        self.assertIn("automation is paused", self.msg(mid)["hold"])
        # a request already open when the pause came is refused by the handler itself
        r = actions.perform(self.conn, "request.wake", {"project": "alpha"}, actor="runner:inbox", via="runner")
        row, _ = self.decide(r)
        self.assertEqual(row["status"], "refused")
        self.assertIn("paused", self.result(row)["reason"])
        self.assertEqual(self.fired, [])

    def test_a_failed_wake_alerts_and_pauses(self):
        self.notice()
        self.process()
        self.reply = "no-reply-within-timeout"
        row, _ = self.decide(self.wakes()[0])
        self.assertEqual(row["status"], "failed")
        self.assertTrue(self.alerts or self.inbox_alerts)
        self.assertTrue(failstop.paused(project="alpha")["paused"])
        self.notice(2)                                               # a later proposal while paused: no new wake
        self.process()
        self.assertEqual(len(self.wakes()), 1)

    def test_a_wake_that_cant_be_filed_is_an_error_with_alert_and_pause(self):
        mid = self.notice()
        with mock.patch.object(store, "open_request", side_effect=RuntimeError("boom")):
            self.assertEqual(self.process(), {"failed": 1})
        m = self.msg(mid)
        self.assertEqual(m["status"], "error")
        self.assertIn("proposal wake failed", m["error"])
        self.assertTrue(self.inbox_alerts)
        self.assertTrue(failstop.paused(project="alpha")["paused"])
        self.assertEqual(inbox.thread(self.conn)[0]["id"], mid)     # its error shows to the owner (retry)

    def test_request_add_refuses_proposal_review_and_request_wake_is_idempotent(self):
        with self.assertRaises(ValueError):
            self.request("proposal_review", "alpha")
        a = actions.perform(self.conn, "request.wake", {"project": "alpha"}, actor="runner:inbox", via="runner")
        b = actions.perform(self.conn, "request.wake", {"project": self.pid}, actor="runner:inbox", via="runner")
        self.assertEqual(a["id"], b["id"])
        with self.assertRaises(actions.Forbidden):
            actions.perform(self.conn, "request.wake", {"project": "alpha"}, actor="agent:" + SID_A, via="mcp")

    def test_a_notice_into_a_session_that_is_no_live_one_is_an_error(self):
        self.running = True
        mid = self.notice()
        with mock.patch.object(inbox_worker, "delivery_hold", lambda sid, path, now: (None, "resume")):
            self.assertEqual(self.process(), {"failed": 1})
        self.assertIn("running task-manager only", self.msg(mid)["error"])
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
