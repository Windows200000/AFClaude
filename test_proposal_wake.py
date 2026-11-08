#!/usr/bin/env python3
"""Tests for phase 5a A5 part 2 (docs/phase5a_plan.md §3.8, §5 A5): a separate session's proposal reaches its
task-manager through the inbox, and a task-manager with no run going is woken through run-now (D-188, D-194,
D-186). Temp DBs only; no session, tmux or signal is touched (keepalive's send and start paths are fakes).

  capture   roles_enforce=on: a driven agent session's proposal leaves one inbox notice (origin `proposal`,
            "now" at once) for the project's task-manager and wakes the runner; a subagent of the task-manager
            leaves none (its summary comes through the hooks); a shadow row never leaves one; a replayed key
            leaves no second
  deliver   a run of the task-manager going: the notice is typed in through the inbox's own send path
  continue  a start's continue names the project's postponed proposals (part 1's insert, wired in
            keepalive._fire_start); none: no line; a DB failure aborts the start (D-235)
  together  a separate session's subagent run is delivered with its direct writes, its proposals named by id
            only (the inbox notices had them); a notice
            whose proposal was reviewed meanwhile carries only its marker (named once, by the postponed insert)
  wake      no run going: one proposal_review request (no window or budget gate, the preflight applies), the
            notice held until the wake's start takes it along; a second proposal while it is open adds no
            request; a refused wake is not repeated for the same notice; a notice that shared a request whose
            start had already been built gets one wake of its own; paused: no wake; a failed wake alerts and
            pauses (D-235/D-282)

    python3 -m unittest test_proposal_wake
"""
import json
import os
import sqlite3
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
import proposals  # noqa: E402
import run_now  # noqa: E402
import runner  # noqa: E402
import store  # noqa: E402
import test_compaction as tc  # noqa: E402  (its Env: a fake tmux session and transcript, for a start)
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

    def test_a_notice_that_shared_a_started_request_gets_one_wake_of_its_own(self):
        first = self.notice(1)
        self.process()
        (req,) = self.wakes()
        late = self.notice(2)                                        # shares the open request ...
        self.process()
        self.assertTrue(inbox_worker.woken(self.msg(late)))
        self.conn.execute("UPDATE action_requests SET ts=? WHERE id=?", ("2000-01-01T00:00:00.000Z", req["id"]))
        self.conn.execute("UPDATE inbox_messages SET created_at=? WHERE id=?", ("1999-01-01T00:00:00.000Z", first))
        self.conn.commit()                                           # ... and came after it was made
        row, _ = self.decide(req)                                    # its start (a fake) took neither along
        self.assertEqual(row["status"], "started")
        self.process()                                               # no run going: one new wake, for `late`
        self.assertEqual(len(self.wakes()), 2)
        self.assertIn(f"#{req['id']}, which was started", self.msg(first)["hold"])
        new = self.wakes()[0]
        self.assertEqual(new["status"], "open")
        self.assertIn(f"#{new['id']}", self.msg(late)["hold"])
        self.decide(new)
        for _ in range(3):                                           # the loop guard: no third
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


# ---------------------------------------------------------------- A5 parts 1 + 2 together

class PostponedInStarts(tc.Env):
    """keepalive._fire_start: the continue carries delivery.postponed_insert for the task-manager's project."""

    def project_id(self):
        conn = store.connect()
        try:
            return next(int(t["project_id"]) for t in store.task_managers(conn) if t["session"] == tc.SID)
        finally:
            conn.close()

    def postpone(self, title="x"):
        conn = store.connect()
        try:
            p = store.add_proposal(conn, self.project_id(), None, "agent:" + tc.SID + ":s1", "task.edit",
                                   {"task_id": 1, "title": title}, None, f"task.edit #1: title='{title}'")
            conn.execute("UPDATE agent_proposals SET status='postponed' WHERE id=?", (p["id"],))
            conn.commit()
            return p["id"]
        finally:
            conn.close()

    def start(self, key):
        at = tc.NIGHT + timedelta(days=1)
        ka.handle_fire(tc.SID, {"uuid": key, "timestamp": at, "budget": "b", "headroom": 14.0, "start_at": at},
                       "test start", ka.load_state(), tc.Armed())
        return ka.load_state()["handled"][key]

    def test_the_continue_names_the_postponed_proposals(self):
        self.assertEqual(self.start("window-start-p0")["result"], "continued-in-place")
        self.assertNotIn("postponed", self.sent[-1][1])                  # none: no line
        pid = self.postpone()
        self.assertEqual(self.start("window-start-p1")["result"], "continued-in-place")
        self.assertIn(f"P-{pid} task.edit #1: title='x'", self.sent[-1][1])
        self.assertIn("tasks.py review ID", self.sent[-1][1])

    def test_a_failing_insert_aborts_the_start(self):
        import delivery
        n = len(self.sent)
        with mock.patch.object(delivery, "postponed_insert", side_effect=RuntimeError("db gone")):
            rec = self.start("window-start-p2")
        self.assertEqual(rec["result"], "start-failed")
        self.assertIn("db gone", rec["error"])
        self.assertEqual(len(self.sent), n)                              # nothing typed


class Together(test_proposals.Proposals):
    def separate_run(self, direct):
        """A subagent run of the driven session AG: one proposal (a notice for the task-manager), and with
        `direct` a direct write (a hand-off) too. -> (run, proposal id)."""
        self.go_on()
        sub = f"agent:{self.sess['AG']}:s1"
        run = actions.perform(self.conn, "run.open", {"session": self.sess["AG"], "agent_id": "s1", "project": self.A},
                              actor="runner:hook", via="runner")
        with mock.patch.object(runner, "wake"), self.assertRaises(actions.Proposed) as cm:
            actions.perform(self.conn, "task.edit", {"task_id": self.task, "title": "x"}, actor=sub, via="mcp",
                            autonomous=True)
        if direct:
            actions.perform(self.conn, "handoff.write", {"session_id": self.sess["AG"], "project": self.A,
                                                         "body": {"summary": "next: step 2", "working_notes": ["tests: none"]}}, actor=sub, via="mcp", autonomous=True)
        actions.perform(self.conn, "run.close", {"session": self.sess["AG"], "agent_id": "s1", "reason": "SubagentStop"},
                        actor="runner:hook", via="runner")
        return store.get_agent_run(self.conn, run["id"]), cm.exception.proposal_id

    def test_a_separate_sessions_run_summary_names_its_noticed_proposals_by_id_only(self):
        run, pid = self.separate_run(direct=True)
        self.assertIsNone(run["delivered_at"])                                            # delivered as usual
        self.assertEqual(run["summary"]["pending"], [])
        self.assertEqual([p["id"] for p in run["summary"]["noticed"]], [pid])
        note = delivery_notice(self.conn, self.sess["A"])
        self.assertIn(f"proposals P-{pid}: sent as notices", note)
        self.assertIn("1 write(s)", note)                                                 # the direct write stays
        self.assertNotIn("pending", note)
        text = proposals.delivery_text(run["summary"])
        self.assertIn("handoff.write", text)
        self.assertNotIn("title='x'", text)                                               # the notice had it
        self.assertEqual(len([m for m in store.list_inbox(self.conn, limit=50)
                              if m["origin"] == store.PROPOSAL_ORIGIN]), 1)

    def test_a_separate_run_with_only_noticed_proposals_has_nothing_new_to_deliver(self):
        run, _ = self.separate_run(direct=False)
        self.assertIsNotNone(run["delivered_at"])
        self.assertEqual(delivery_notice(self.conn, self.sess["A"]), "")

    def test_a_failing_notice_lookup_fails_the_close(self):
        self.go_on()
        actions.perform(self.conn, "run.open", {"session": self.sess["AG"], "agent_id": "s1", "project": self.A},
                        actor="runner:hook", via="runner")
        with mock.patch.object(store, "proposal_notices", side_effect=sqlite3.OperationalError("db gone")), \
                self.assertRaises(store.DBUnavailable):
            actions.perform(self.conn, "run.close", {"session": self.sess["AG"], "agent_id": "s1",
                                                     "reason": "SubagentStop"}, actor="runner:hook", via="runner")
        self.assertIsNotNone(store.open_run(self.conn, self.sess["AG"], "s1"))           # still open, not closed

    def test_the_task_managers_own_subagent_run_is_still_delivered(self):
        self.go_on()
        actions.perform(self.conn, "run.open", {"session": self.sess["A"], "agent_id": "s2", "project": self.A},
                        actor="runner:hook", via="runner")
        with self.assertRaises(actions.Proposed):
            actions.perform(self.conn, "task.edit", {"task_id": self.task, "title": "y"},
                            actor=f"agent:{self.sess['A']}:s2", via="mcp", autonomous=True)
        actions.perform(self.conn, "run.close", {"session": self.sess["A"], "agent_id": "s2", "reason": "SubagentStop"},
                        actor="runner:hook", via="runner")
        self.assertIn("pending", delivery_notice(self.conn, self.sess["A"]))

    def test_notices_stay_inside_the_held_budget(self):
        long = "y" * (inbox.NOTE_ITEM_MAX + 500)
        for n in range(1, 151):
            store.add_proposal_notice(self.conn, self.sess["A"], f"P-{1000 + n} by agent:x (task.edit): {long}", "agent:x")
        self.conn.commit()
        text, ids = inbox.held_insert(self.conn, self.sess["A"])
        self.assertEqual(len(ids), 150)                                                  # all of them go
        self.assertLess(len(text.encode()), inbox.MESSAGE_MAX_BYTES // 2)
        self.assertIn("... (cut; the full proposal: python3 tasks.py proposals)", text)
        self.assertIn("(P-1003: python3 tasks.py proposals)", text)                      # past the budget: marker
        self.assertIn(f"and {150 - 1 - inbox.NOTICE_MARKERS} more: python3 tasks.py proposals", text)

    def test_a_reviewed_notice_carries_only_its_marker(self):
        self.go_on()
        with mock.patch.object(runner, "wake"):
            pid = self.propose("task.edit", task_id=self.task, title="z")
        actions.perform(self.conn, "proposal.review", {"proposal_id": pid, "decision": "postpone"},
                        actor=self.tm, via="mcp", autonomous=True)
        (m,) = [m for m in store.list_inbox(self.conn, limit=50) if m["origin"] == store.PROPOSAL_ORIGIN]
        text, ids = inbox.held_insert(self.conn, self.sess["A"])
        self.assertEqual(ids, [m["id"]])
        self.assertIn(f"{inbox.marker(m['id'])} (P-{pid} is postponed already)", text)
        self.assertNotIn("title='z'", text)                              # named once: in the postponed insert
        import delivery
        self.assertIn(f"P-{pid} ", delivery.postponed_insert(self.conn, self.A))


def delivery_notice(conn, session):
    import delivery
    return delivery.notice_for_session(conn, session)


if __name__ == "__main__":
    unittest.main()
