#!/usr/bin/env python3
"""Tests for phase 5a A4 part 2 (docs/phase5a_plan.md §3.8, §5 A4): agent runs, their summaries, the proposal alert
tick and the agent-blocked task held back from the inbox. Temp DBs only.

  summary    every write of the run is listed, direct ones included; proposals pending / shadow
  runs       run.open / run.close, the SubagentStart/Stop hook CLI, driven.set's session runs, /clear moves a run
  alert      the proposal_alert tick fires once after proposal_alert_hours, not at every tick
  blocked    a task an agent blocked stays out of the inbox until its task-manager forwards it

    python3 -m unittest test_runs
"""
import contextlib
import io
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
import proposals  # noqa: E402
import runner_jobs  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402
from test_proposals import Proposals  # noqa: E402


class Runs(Proposals):
    def setUp(self):
        super().setUp()
        self.ag = self.sess["AG"]

    def hook(self, name, /, **params):
        return actions.perform(self.conn, name, params, actor="runner:hook", via="runner")

    def run_of(self, run_id):
        return store.get_agent_run(self.conn, run_id)


class Summary(Runs):
    def agent_session(self):
        t = self.cli("task.add", title="own", project="alpha")["id"]
        self.as_agent("task.start", task_id=t, session=self.ag)
        self.as_agent("task.finish", task_id=t, summary="done")
        self.as_agent("doc.append", project=self.A, name="alerts", text="the DB failed\n")
        return t

    def test_the_summary_lists_every_write_of_the_run_direct_ones_too(self):
        self.go_on()
        run = self.hook("run.open", session=self.ag, kind="session")
        self.agent_session()
        pid = self.propose("task.edit", task_id=self.task, title="other")
        closed = self.hook("run.close", session=self.ag, reason="session ended")
        s = closed["summary"]
        self.assertEqual(s["write_count"], 3)
        self.assertEqual([w["action"] for w in s["writes"]["tasks"]], ["task.start", "task.finish"])
        self.assertEqual([w["action"] for w in s["writes"]["alerts"]], ["doc.append"])
        # a separate session's proposal reached the task-manager as an inbox notice (A5): named, not pending
        self.assertEqual((s["pending"], [(p["id"], p["status"]) for p in s["noticed"]]), ([], [(pid, "proposed")]))
        self.assertEqual(store.get_proposal(self.conn, pid)["payload"], {"task_id": self.task, "title": "other"})
        self.assertEqual((s["shadow"], s["end_reason"], s["run"]), ([], "session ended", run["id"]))
        self.assertEqual(store.get_proposal(self.conn, pid)["run_id"], run["id"])
        self.assertEqual(self.run_of(run["id"])["summary"], s)
        self.assertIn(f"sent as a notice P-{pid}", proposals.summary_text(s))

    def test_a_write_after_the_close_or_before_the_open_is_not_in_it(self):
        before = self.hook("run.open", session=self.ag, kind="session")
        self.hook("run.close", session=self.ag)
        self.agent_session()
        self.assertEqual(self.run_of(before["id"])["summary"]["write_count"], 0)

    def test_shadow_mode_lists_the_applied_write_as_a_would_be_proposal(self):
        self.hook("run.open", session=self.ag, kind="session")
        self.as_agent("task.edit", task_id=self.task, title="shadowed")        # applied under the old rule
        self.assertEqual(self.title(), "shadowed")
        s = self.hook("run.close", session=self.ag)["summary"]
        self.assertEqual((s["write_count"], len(s["shadow"]), s["pending"]), (1, 1, []))
        self.assertIn("applied under shadow", proposals.summary_text(s))

    def test_a_subagents_run_is_its_own(self):
        self.go_on()
        sub = f"{self.agent}:sub1"
        run = self.hook("run.open", session=self.ag, agent_id="sub1")
        self.assertEqual((run["kind"], run["project_id"]), ("subagent", self.A))
        with self.assertRaises(actions.Proposed) as e:
            actions.perform(self.conn, "task.edit", {"task_id": self.task, "title": "z"}, actor=sub, via="mcp",
                            autonomous=True, session=self.ag)
        self.assertEqual(store.get_proposal(self.conn, e.exception.proposal_id)["run_id"], run["id"])
        self.as_agent("doc.append", project=self.A, name="alerts", text="the main agent's\n")   # not the subagent's
        s = self.hook("run.close", session=self.ag, agent_id="sub1", reason="SubagentStop")["summary"]
        self.assertEqual((s["write_count"], len(s["pending"]), len(s["noticed"])), (0, 0, 1))


class RunRows(Runs):
    def test_open_is_idempotent_and_close_without_open_is_a_noop(self):
        a = self.hook("run.open", session=self.ag, agent_id="s1")
        self.assertEqual(self.hook("run.open", session=self.ag, agent_id="s1")["id"], a["id"])
        self.assertEqual(len(store.list_agent_runs(self.conn)), 1)
        self.assertIsNone(self.hook("run.close", session=self.ag, agent_id="nobody"))
        self.assertEqual(self.hook("run.close", session=self.ag, agent_id="s1")["id"], a["id"])
        self.assertIsNone(self.hook("run.close", session=self.ag, agent_id="s1"))        # twice

    def test_a_session_that_belongs_to_no_project_is_no_run(self):
        with self.assertRaisesRegex(ValueError, "neither a task-manager nor a driven session"):
            self.hook("run.open", session="eeeeeeee-0000-4000-8000-000000000001", agent_id="s1")
        with self.assertRaises(actions.Forbidden):           # the runner's step, not MCP's
            actions.perform(self.conn, "run.open", {"session": self.ag}, actor="runner:hook", via="mcp")

    def run_hook(self, which, payload):
        out = io.StringIO()
        args = tasks.parser().parse_args(["run-hook", which])
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            tasks.run_hook(self.conn, args)
        return out.getvalue()

    def test_the_hook_cli_opens_and_closes_a_subagents_run(self):
        p = {"session_id": self.ag, "agent_id": "s9", "agent_type": "afclaude-worker"}
        self.assertIn("open", self.run_hook("start", p))
        self.assertEqual([r["agent_id"] for r in store.list_agent_runs(self.conn, open_only=True)], ["s9"])
        self.assertIn("closed", self.run_hook("stop", p))
        self.assertEqual(store.list_agent_runs(self.conn, open_only=True), [])
        self.assertIn("no open run", self.run_hook("stop", p))                         # a stop with no start
        self.assertIn("helper agent", self.run_hook("start", dict(p, agent_type="")))
        self.assertIn("neither", self.run_hook("start", dict(p, session_id="eeeeeeee-0000-4000-8000-000000000001")))
        self.assertEqual(len(store.list_agent_runs(self.conn)), 1)
        with self.assertRaises(ValueError):                                             # D-235: no silent skip
            self.run_hook("start", {"session_id": self.ag})
        with mock.patch.dict(os.environ, {"AFCLAUDE_AGENT_ID": "x"}), self.assertRaisesRegex(ValueError, "harness"):
            self.run_hook("start", p)

    def test_the_hook_takes_a_non_dict_payload_as_an_error_and_a_strangers_stop_writes_nothing(self):
        for bad in ([], "x", 3, None):
            with self.assertRaisesRegex(ValueError, "needs session_id"):
                self.run_hook("stop", bad)
        stranger = {"session_id": "eeeeeeee-0000-4000-8000-000000000001", "agent_id": "s1", "agent_type": "w"}
        with mock.patch.object(actions, "perform", side_effect=AssertionError("no transaction")):
            self.assertIn("neither", self.run_hook("stop", stranger))

    def test_the_hook_is_inert_in_a_non_live_checkout_without_a_db_only(self):
        missing = os.path.join(testenv.DIR, "no", "afclaude.db")
        args = tasks.parser().parse_args(["--db", missing, "run-hook", "start"])
        with mock.patch.object(os.path, "isdir", return_value=False):            # a worktree: .git is a file
            self.assertTrue(tasks.hook_is_inert(args))
            args.db = self.db
            self.assertFalse(tasks.hook_is_inert(args))                          # a DB it can reach: records as usual
        args.db = missing
        with mock.patch.object(os.path, "isdir", return_value=True):             # the live repo: D-235 stays
            self.assertFalse(tasks.hook_is_inert(args))
        out = io.StringIO()                                                      # main(): exit 0, no DB touched
        with mock.patch.object(os.path, "isdir", return_value=False), \
                mock.patch.object(store, "connect", side_effect=AssertionError("connected")), \
                mock.patch.object(store, "report_db_error", side_effect=AssertionError("alerted")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(tasks.main(["--db", missing, "run-hook", "start"]), 0)
        self.assertIn("not the live install", out.getvalue())
        with mock.patch.object(os.path, "isdir", return_value=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tasks.main(["--db", missing, "run-hook", "start"]), 1)   # live + missing: error

    def test_a_linked_worktree_without_a_db_fails_locally_and_never_alerts(self):
        # 16.11.: tasks.py run in a worktree (no data/, no DB) pushed a "database missing" alert to the owner
        with tempfile.TemporaryDirectory() as d:
            wt, live, missing = (os.path.join(d, n) for n in ("wt", "live", "none/afclaude.db"))
            os.makedirs(live)
            os.mkdir(wt)
            os.mkdir(os.path.join(live, ".git"))
            with open(os.path.join(wt, ".git"), "w") as f:
                f.write(f"gitdir: {live}/.git/worktrees/wt\n")
            alert = mock.Mock(side_effect=AssertionError("alerted"))
            def main(here, *argv):
                err = io.StringIO()
                with mock.patch.object(tasks, "HERE", here), mock.patch.object(store, "report_db_error", alert), \
                        mock.patch.object(actions, "deliver_alerts", alert), \
                        mock.patch.object(store, "connect", side_effect=AssertionError("connected")), \
                        contextlib.redirect_stderr(err):
                    return tasks.main(["--db", missing, *argv]), err.getvalue()
            rc, err = main(wt, "list")
            self.assertEqual(rc, 1)
            self.assertIn(f"run tasks.py from the main checkout {live}", err)
            self.assertFalse(os.path.exists(os.path.dirname(missing)))           # nothing written anywhere
            alert.assert_not_called()
            args = tasks.parser().parse_args(["--db", self.db, "list"])      # an existing DB: not this case
            with mock.patch.object(tasks, "HERE", wt):
                self.assertIsNone(tasks.linked_worktree_without_db(args))
            with open(os.path.join(wt, ".git"), "w") as f:                       # an unreadable pointer: an error
                f.write("garbage\n")
            args.db = missing
            with mock.patch.object(tasks, "HERE", wt), self.assertRaisesRegex(ValueError, "no linked-worktree"):
                tasks.linked_worktree_without_db(args)

    def test_the_live_checkout_and_a_plain_copy_with_a_missing_db_still_alert(self):
        with tempfile.TemporaryDirectory() as d:
            live, copy = os.path.join(d, "live"), os.path.join(d, "copy")
            os.makedirs(os.path.join(live, ".git"))
            os.mkdir(copy)                                                       # no .git at all (image, copy)
            missing = os.path.join(d, "none", "afclaude.db")
            for here in (live, copy):
                alert = mock.Mock(return_value=None)
                with mock.patch.object(tasks, "HERE", here), mock.patch.object(store, "report_db_error", alert), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(tasks.main(["--db", missing, "list"]), 1)
                alert.assert_called_once()

    def test_the_audit_log_has_an_actor_ts_index(self):
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_log'")
        self.assertIn("audit_log_actor", {r[0] for r in rows})
        plan = " ".join(r[3] for r in self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM audit_log WHERE actor IN (?) AND ts>=? AND ts<=? ORDER BY id",
            ("a", "x", "y")))
        self.assertIn("audit_log_actor", plan)

    def test_driven_set_opens_and_closes_a_driven_agent_sessions_run(self):
        new = "bbbbbbbb-0000-4000-8000-0000000000b1"
        kw = {"actor": "keepalive", "via": "runner"}
        actions.perform(self.conn, "driven.set", {"session_id": new, "kind": "review", "tmux": "afc-rv",
                                                  "project": self.A}, **kw)
        run_id = store.open_run(self.conn, new, None)
        self.assertEqual(self.run_of(run_id)["kind"], "session")
        actions.perform(self.conn, "driven.set", {"session_id": new, "ended_at": store.now_iso(),
                                                  "end_reason": "stalled"}, **kw)
        r = self.run_of(run_id)
        self.assertEqual((r["end_reason"], r["summary"]["write_count"]), ("stalled", 0))
        self.assertIsNone(store.open_run(self.conn, new, None))
        self.assertEqual(len(store.list_agent_runs(self.conn)), 1)

    def test_a_clear_moves_the_open_run_to_the_new_session_id(self):
        old, new = "bbbbbbbb-0000-4000-8000-0000000000c1", "bbbbbbbb-0000-4000-8000-0000000000c2"
        kw = {"actor": "keepalive", "via": "runner"}
        actions.perform(self.conn, "driven.set", {"session_id": old, "kind": "stall", "tmux": "afc-st",
                                                  "project": self.A}, **kw)
        run_id = store.open_run(self.conn, old, None)
        actions.perform(self.conn, "session.cleared", {"old_session": old, "new_session": new}, **kw)
        self.assertEqual(store.open_run(self.conn, new, None), run_id)
        self.assertEqual(len(store.list_agent_runs(self.conn, open_only=True)), 1)


class Alert(Runs):
    def test_the_tick_fires_once_after_proposal_alert_hours_not_again(self):
        self.go_on()
        now = store._utcnow()
        self.propose("task.edit", task_id=self.task, title="late")
        sent = []
        real_connect = store.connect
        with mock.patch("notify.notify", side_effect=lambda s, b="": sent.append((s, b))), \
                mock.patch.object(store, "connect", side_effect=lambda *a, **k: real_connect(self.db)):
            self.assertEqual(runner_jobs.proposal_alert_tick(), "no open proposal is overdue")    # 0 h
            with mock.patch.object(store, "_utcnow", return_value=now + timedelta(hours=23)):
                runner_jobs.proposal_alert_tick()
            self.assertEqual(sent, [])
            with mock.patch.object(store, "_utcnow", return_value=now + timedelta(hours=25)):
                self.assertIn("alerted", runner_jobs.proposal_alert_tick())
                runner_jobs.proposal_alert_tick()                                    # the next tick
            with mock.patch.object(store, "_utcnow", return_value=now + timedelta(hours=60)):
                runner_jobs.proposal_alert_tick()
        self.assertEqual(len(sent), 1)
        self.assertIn("waiting for review for over 24 h", sent[0][0])
        self.assertIn("task.edit", sent[0][1])
        self.conn.commit()
        self.assertEqual(self.rows(open_only=True)[0]["alerted_at"] is not None, True)
        self.assertEqual(self.title(), "t")                      # the alert changes nothing: no timeout applies

    def test_a_failed_notify_leaves_the_proposals_marked_and_raises(self):
        self.go_on()
        now = store._utcnow()
        self.propose("task.edit", task_id=self.task, title="late")
        real_connect = store.connect
        with mock.patch("notify.notify", side_effect=OSError("mail down")) as notify, \
                mock.patch.object(store, "connect", side_effect=lambda *a, **k: real_connect(self.db)), \
                mock.patch.object(store, "_utcnow", return_value=now + timedelta(hours=25)):
            with self.assertRaisesRegex(RuntimeError, "marked sent but notify failed: mail down"):
                runner_jobs.proposal_alert_tick()          # D-235: the run fails, the owner is told
            self.assertEqual(runner_jobs.proposal_alert_tick(), "no open proposal is overdue")   # at most once
        self.assertEqual(notify.call_count, 1)

    def test_a_decided_or_shadow_proposal_never_alerts_and_the_hours_are_a_setting(self):
        self.go_on()
        pid = self.propose("task.edit", task_id=self.task, title="late")
        self.as_tm("proposal.review", proposal_id=pid, decision="reject", note="no")
        late = store._utcnow() + timedelta(hours=100)
        self.assertIsNone(actions.overdue_proposal_alert(self.conn, late))
        self.propose("task.edit", task_id=self.task, title="again")
        self.cli("setting.set", key="proposal_alert_hours", value=2)
        self.assertIsNotNone(actions.overdue_proposal_alert(self.conn, store._utcnow() + timedelta(hours=3)))
        self.assertIsNone(actions.overdue_proposal_alert(self.conn, store._utcnow() + timedelta(hours=1)))


class Blocked(Runs):
    def own_task(self):
        t = self.cli("task.add", title="own", project="alpha")["id"]
        self.as_agent("task.start", task_id=t, session=self.ag)
        return t

    def inbox(self):
        return [t["id"] for t in store.pending_user_input(self.conn)["blocked_tasks"]]

    def test_an_agent_blocked_task_stays_out_of_the_inbox_until_forwarded(self):
        self.go_on()
        t = self.own_task()
        self.as_agent("task.block", task_id=t, question="which one?")
        self.assertEqual(store.get_task(self.conn, t)["blocked_by"], self.agent)
        self.assertEqual(self.inbox(), [])
        self.assertEqual([x["id"] for x in store.held_blocked_tasks(self.conn)], [t])
        with self.assertRaises(actions.Forbidden):                      # an agent can't forward its own question
            self.as_agent("task.forward", task_id=t)
        self.as_tm("task.forward", task_id=t)
        self.assertEqual(self.inbox(), [t])
        self.assertEqual(store.held_blocked_tasks(self.conn), [])
        with self.assertRaisesRegex(store.InvalidTransition, "already"):
            self.as_tm("task.forward", task_id=t)
        self.cli("task.answer", task_id=t, answer="this one")           # answered: pending, the flags go on a new block
        self.cli("task.block", task_id=t, question="again?")
        self.assertEqual((store.get_task(self.conn, t)["blocked_by"], self.inbox()), (None, [t]))

    def test_a_human_or_task_manager_block_goes_straight_to_the_inbox(self):
        self.go_on()
        t = self.cli("task.add", title="h", project="alpha")["id"]
        self.cli("task.block", task_id=t, question="q?")
        self.assertEqual(self.inbox(), [t])
        with self.assertRaisesRegex(store.InvalidTransition, "not blocked by an agent"):
            self.as_tm("task.forward", task_id=t)

    def test_in_shadow_the_agents_block_reaches_the_inbox_as_before(self):
        t = self.own_task()
        self.as_agent("task.block", task_id=t, question="which one?")
        self.assertEqual((store.get_task(self.conn, t)["blocked_by"], self.inbox()), (None, [t]))


if __name__ == "__main__":
    unittest.main()
