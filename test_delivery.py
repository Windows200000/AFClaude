#!/usr/bin/env python3
"""Tests for phase 5a A5 part 1 (docs/phase5a_plan.md §3.8, §5 A5): a finished subagent run's summary reaches its
task-manager's own session. Temp DBs only.

  hook      the PostToolUse hook on Agent: a foreground summary once; a background launch only the pending count; an
            empty run nothing; a stranger, a subagent's own call and a payload without agentId deliver nothing
  notice    the one-line notice on a tasks.py / MCP result and in the UserPromptSubmit hook, once, to the
            project's task-manager only
  bounded   the hooks' DB waits are short and tried once (a lock: a short note, the reminder still goes); the
            claim comes after the text; a driven session's own run is noticed too; runs closed before the
            delivered_at column are backfilled as delivered
  postponed the continue prompt's insert, only when a proposal is postponed

    python3 -m unittest test_delivery
"""
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (temp DB first)
import actions  # noqa: E402
import delivery  # noqa: E402
import identity  # noqa: E402
import keepalive  # noqa: E402
import proposals  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402
from test_proposals import Proposals  # noqa: E402

spec = importlib.util.spec_from_file_location("delegation_reminder", os.path.join(HERE, "hooks", "delegation_reminder.py"))
reminder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reminder)


class Delivery(Proposals):
    def setUp(self):
        super().setUp()
        self.tm_session = self.sess["A"]            # project alpha's task-manager
        self.tm_id = identity.Identity(actor=self.actor["tm_own"], kind="task-manager", session=self.tm_session)

    def hook(self, name, /, **params):
        return actions.perform(self.conn, name, params, actor="runner:hook", via="runner")

    def sub_write(self, agent, name="task.edit", /, **params):
        """A subagent of the task-manager writes: applied under shadow, or stored as a proposal once enforced."""
        return actions.perform(self.conn, name, params, actor=f"agent:{self.tm_session}:{agent}", via="mcp",
                               autonomous=True, session=self.tm_session)

    def finished_run(self, agent="sub1", propose=False):
        """A subagent run that wrote one task field, closed. -> the run row."""
        self.hook("run.open", session=self.tm_session, agent_id=agent)
        if propose:
            with self.assertRaises(actions.Proposed):
                self.sub_write(agent, task_id=self.task, title="proposed")
        else:
            self.sub_write(agent, task_id=self.task, title=f"by {agent}")
        return self.hook("run.close", session=self.tm_session, agent_id=agent, reason="SubagentStop")

    def payload(self, agent, status=None, **over):
        resp = {"agentId": agent} | ({"status": status} if status else {})
        return {"session_id": self.tm_session, "tool_name": "Agent", "tool_response": resp} | over

    def delivered(self, run_id):
        return store.get_agent_run(self.conn, run_id)["delivered_at"]

    def agent_hook(self, payload):
        return delivery.agent_hook(self.conn, payload)


class Hook(Delivery):
    def test_a_foreground_summary_reaches_the_context_once(self):
        self.go_on()
        run = self.finished_run(propose=True)
        self.assertIsNone(self.delivered(run["id"]))
        context, note = self.agent_hook(self.payload("sub1"))
        self.assertIn(f"Run {run['id']}: 0 write(s), 1 proposal(s) pending", context)
        self.assertIn("task.edit", context)
        self.assertIn("title='proposed'", context)
        self.assertNotIn("report", context)
        self.assertIsNotNone(self.delivered(run["id"]))
        self.assertEqual(self.agent_hook(self.payload("sub1"))[0], "")                      # once
        self.assertEqual(delivery.notice(self.conn, self.tm_id), "")                        # no second path either

    def test_direct_writes_are_in_a_foreground_summary_in_shadow(self):
        run = self.finished_run()
        context, _ = self.agent_hook(self.payload("sub1"))
        self.assertIn(f"Run {run['id']}: 1 write(s)", context)
        self.assertIn("task.edit task#", context)
        self.assertNotIn("pending", context)

    def test_a_background_launch_gives_only_the_pending_count(self):
        self.go_on()
        self.hook("run.open", session=self.tm_session, agent_id="bg1")                       # the launch: still open
        self.assertEqual(self.agent_hook(self.payload("bg1", "async_launched"))[0], "")      # nothing pending: nothing
        self.finished_run("earlier", propose=True)
        self.hook("run.open", session=self.tm_session, agent_id="bg2")
        context, _ = self.agent_hook(self.payload("bg2", "async_launched"))
        self.assertEqual(context, "1 proposal(s) pending from earlier runs: tasks.py proposals")
        self.assertIsNone(self.delivered(store.open_run(self.conn, self.tm_session, "bg2")))   # nothing delivered

    def test_an_empty_run_delivers_nothing(self):
        self.hook("run.open", session=self.tm_session, agent_id="quiet")
        run = self.hook("run.close", session=self.tm_session, agent_id="quiet")
        self.assertIsNotNone(self.delivered(run["id"]))                                      # delivered at close
        self.assertEqual(self.agent_hook(self.payload("quiet")), ("", f"no delivery: run {run['id']} has nothing "
                                                                      "to deliver or was delivered"))
        self.assertEqual(delivery.notice(self.conn, self.tm_id), "")
        self.assertFalse(store.any_undelivered_run(self.conn))

    def test_a_stranger_a_subagents_own_call_and_odd_payloads_deliver_nothing(self):
        run = self.finished_run()
        other = self.payload("sub1", session_id=self.sess["B"])                              # beta's task-manager
        self.assertIn("no run for subagent", self.agent_hook(other)[1])
        self.assertIn("not a task-manager", self.agent_hook(self.payload("sub1", session_id=self.sess["AG"]))[1])
        self.assertIn("subagent's own", self.agent_hook(self.payload("sub1", agent_id="x"))[1])
        self.assertIn("no agentId", self.agent_hook({"session_id": self.tm_session, "tool_response": "failed"})[1])
        self.assertIn("no run for subagent", self.agent_hook(self.payload("nobody"))[1])
        self.assertIsNone(self.delivered(run["id"]))                                         # none of it consumed it
        for bad in (None, [], {"tool_response": {}}, {"session_id": "a b"}):
            with self.assertRaisesRegex(ValueError, "needs session_id"):                     # D-235: no silent skip
                self.agent_hook(bad)

    def test_the_hook_cli_prints_json_context_or_a_plain_note(self):
        run = self.finished_run()

        def cli(payload):
            out = io.StringIO()
            args = tasks.parser().parse_args(["run-hook", "deliver"])
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
                tasks.run_hook(self.conn, args)
            return out.getvalue()
        got = json.loads(cli(self.payload("sub1")))["hookSpecificOutput"]
        self.assertEqual(got["hookEventName"], "PostToolUse")
        self.assertIn(f"Run {run['id']}", got["additionalContext"])
        self.assertIn("nothing to deliver or was delivered", cli(self.payload("sub1")))      # once
        with self.assertRaises(ValueError):
            cli({"tool_response": {"agentId": "sub1"}})
        with mock.patch.dict(os.environ, {"AFCLAUDE_AGENT_ID": "x"}), self.assertRaisesRegex(ValueError, "harness"):
            cli(self.payload("sub1"))


class Notice(Delivery):
    def test_a_finished_background_run_is_noticed_once_by_its_task_manager_only(self):
        self.go_on()
        self.hook("run.open", session=self.tm_session, agent_id="bg1")
        self.assertEqual(delivery.notice(self.conn, self.tm_id), "")                         # still running
        self.hook("run.close", session=self.tm_session, agent_id="bg1")                      # an empty run: silent
        run = self.finished_run("bg2", propose=True)
        agent = identity.Identity(actor=self.actor["agent"], kind="agent", session=self.sess["AG"])
        sub = identity.Identity(actor=self.actor["agent"], kind="agent", session=self.tm_session, agent_id="bg2")
        other = identity.Identity(actor=self.actor["tm_other"], kind="task-manager", session=self.sess["B"])
        for who in (agent, sub, other, identity.Identity(actor="cli", kind="host")):
            self.assertEqual(delivery.notice(self.conn, who), "", who)
        self.assertIsNone(self.delivered(run["id"]))
        line = delivery.notice(self.conn, self.tm_id)
        self.assertIn(f"1 subagent run(s) finished ({run['id']})", line)
        self.assertIn("1 proposal(s) pending, run `tasks.py proposals`", line)
        self.assertNotIn("\n", line)
        self.assertEqual(delivery.notice(self.conn, self.tm_id), "")                         # once
        self.assertIsNotNone(self.delivered(run["id"]))
        rows = self.conn.execute("SELECT actor FROM audit_log WHERE action='run.deliver'").fetchall()
        self.assertEqual([r[0] for r in rows], ["runner:delivery"])                          # audited, once

    def test_several_runs_make_one_line(self):
        a, b = self.finished_run("a1"), self.finished_run("a2")
        line = delivery.notice(self.conn, self.tm_id)
        self.assertIn(f"2 subagent run(s) finished ({a['id']}, {b['id']}): 2 write(s)", line)
        self.assertNotIn("proposal", line)

    def test_a_foreground_run_whose_hook_never_fired_is_noticed_instead(self):
        run = self.finished_run()
        self.assertIn(f"({run['id']})", delivery.notice(self.conn, self.tm_id))
        self.assertEqual(self.agent_hook(self.payload("sub1"))[0], "")                       # and not twice

    def test_tasks_py_says_it_once_on_stderr_and_keeps_stdout_clean(self):
        self.go_on()
        self.finished_run("bg1", propose=True)
        env = {"CLAUDE_CODE_SESSION_ID": self.tm_session, "CLAUDECODE": "1"}

        def run():
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = tasks.main(["--db", self.db, "runs", "--json"])
            return rc, out.getvalue(), err.getvalue()
        rc, out, err = run()
        self.assertEqual(rc, 0)
        json.loads(out)
        self.assertIn("subagent run(s) finished", err)
        rc, out, err = run()
        self.assertEqual((rc, err), (0, ""))
        run_hook = io.StringIO()
        self.finished_run("bg2", propose=True)
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(run_hook), \
                contextlib.redirect_stderr(io.StringIO()) as err2:
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(
                    {"session_id": self.tm_session, "agent_id": "x", "agent_type": ""}))):
                tasks.main(["--db", self.db, "run-hook", "start"])
        self.assertEqual(err2.getvalue(), "")                                                # a hook is not a result
        self.assertTrue(store.any_undelivered_run(self.conn))

    def test_a_failing_notice_is_shown_and_the_command_still_succeeds(self):
        self.finished_run()
        env = {"CLAUDE_CODE_SESSION_ID": self.tm_session, "CLAUDECODE": "1"}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(delivery, "notice", side_effect=OSError("disk")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(tasks.main(["--db", self.db, "runs"]), 0)
        self.assertIn("the run-summary notice failed: OSError: disk", err.getvalue())

    def test_the_user_prompt_hook_carries_it_once(self):
        self.finished_run()
        with mock.patch.dict(os.environ, {"AFCLAUDE_DB": self.db}):
            self.assertEqual(reminder.run_notice(self.sess["B"]), "")                        # not its project's
            self.assertEqual(reminder.run_notice(None), "")
            line = reminder.run_notice(self.tm_session)
            self.assertIn("1 subagent run(s) finished", line)
            self.assertEqual(reminder.run_notice(self.tm_session), "")
        self.assertFalse(store.any_undelivered_run(self.conn))

    def test_the_user_prompt_hook_main_adds_the_notice_to_the_reminder_and_shows_a_failure(self):
        self.finished_run()
        payload = json.dumps({"session_id": self.tm_session, "cwd": HERE})
        out = io.StringIO()
        guard = {"AFCLAUDE_DB": self.db, "CLAUDE_GUARD_DISABLE": "1"}       # an AFClaude session
        with mock.patch.dict(os.environ, guard), mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                contextlib.redirect_stdout(out):
            reminder.main()
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn(reminder.REMINDER, ctx)
        self.assertIn("subagent run(s) finished", ctx)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, guard), mock.patch.object(reminder, "run_notice", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            self.assertEqual(reminder.main(), 0)
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("the run-summary notice failed: RuntimeError: boom", ctx)
        self.assertIn("boom", err.getvalue())

    def test_the_user_prompt_hook_without_a_run_waiting_does_not_load_the_repo_modules(self):
        with mock.patch.dict(os.environ, {"AFCLAUDE_DB": self.db}), \
                mock.patch.object(store, "connect", side_effect=AssertionError("connected")):
            self.assertEqual(reminder.run_notice(self.tm_session), "")


class Bounded(Delivery):
    """The A5 review round: short DB waits in the hooks, the claim last, driven sessions' own runs, the backfill."""

    def test_the_prompt_hook_fast_path_is_this_sessions_project_only(self):
        self.finished_run()
        with mock.patch.dict(os.environ, {"AFCLAUDE_DB": self.db}), \
                mock.patch.object(store, "connect", side_effect=AssertionError("connected")):
            self.assertEqual(reminder.run_notice(self.sess["B"]), "")                    # another task-manager
            self.assertEqual(reminder.run_notice(self.sess["AG"]), "")                   # a driven session

    def test_the_prompt_hook_waits_briefly_and_once(self):
        self.finished_run()
        seen = []

        def notice(conn, session):
            seen.append(store._QUICK.get())
            return ""
        with mock.patch.dict(os.environ, {"AFCLAUDE_DB": self.db}), \
                mock.patch.object(delivery, "notice_for_session", side_effect=notice):
            reminder.run_notice(self.tm_session)
        self.assertEqual(seen, [reminder.BUSY])
        calls = []

        def locked():
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")
        with store.quick_waits(1.0), mock.patch.object(store, "_sleep") as sleep, \
                self.assertRaises(store.DBUnavailable):
            store.retrying(locked)
        self.assertEqual((len(calls), sleep.call_count), (1, 0))                         # one try, no backoff

    def test_a_locked_db_is_a_short_note_and_the_reminder_still_goes(self):
        payload = json.dumps({"session_id": self.tm_session, "cwd": HERE})
        out = io.StringIO()
        locked = store.DBUnavailable("database locked: database is locked", "locked", "transient")
        with mock.patch.dict(os.environ, {"AFCLAUDE_DB": self.db, "CLAUDE_GUARD_DISABLE": "1"}), \
                mock.patch.object(reminder, "run_notice", side_effect=locked), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reminder.main(), 0)
        ctx = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx, reminder.REMINDER + "\n" + reminder.BUSY_NOTE)

    def test_the_post_tool_use_hook_waits_briefly_and_once(self):
        seen = []

        def hook(conn, payload):
            seen.append(store._QUICK.get())
            return "", "no delivery"
        with mock.patch.object(delivery, "agent_hook", side_effect=hook), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": self.tm_session}))), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tasks.main(["--db", self.db, "run-hook", "deliver"]), 0)
        self.assertEqual(seen, [tasks.HOOK_BUSY])
        self.assertIsNone(store._QUICK.get())                                            # only inside

    def test_a_text_that_cant_be_built_leaves_the_run_unclaimed(self):
        run = self.finished_run()
        with mock.patch.object(proposals, "notice_text", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            delivery.notice(self.conn, self.tm_id)
        self.assertIsNone(self.delivered(run["id"]))
        with mock.patch.object(proposals, "delivery_text", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            self.agent_hook(self.payload("sub1"))
        self.assertIsNone(self.delivered(run["id"]))
        self.assertIn(f"({run['id']})", delivery.notice(self.conn, self.tm_id))

    def test_a_driven_sessions_own_run_reaches_the_task_manager(self):
        run = self.hook("run.open", session=self.sess["AG"], kind="session")
        self.as_agent("doc.append", project=self.A, name="alerts", text="the DB failed\n")
        closed = self.hook("run.close", session=self.sess["AG"], reason="session ended")
        self.assertIsNone(closed["delivered_at"])
        line = delivery.notice(self.conn, self.tm_id)
        self.assertIn(f"1 agent run(s) finished ({run['id']}): 1 write(s)", line)
        self.assertEqual(delivery.notice(self.conn, self.tm_id), "")
        empty = self.hook("run.open", session=self.sess["AG"], kind="session")
        self.assertIsNotNone(self.hook("run.close", session=self.sess["AG"])["delivered_at"])    # nothing to say
        self.assertIsNotNone(self.delivered(empty["id"]))

    def test_runs_closed_before_delivery_existed_are_marked_delivered_once(self):
        path = os.path.join(os.path.dirname(self.db), "old.db")
        c = store.connect(path, create=True)
        cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runs)") if r[1] != "delivered_at"]
        c.execute("PRAGMA foreign_keys=OFF")
        c.executescript(f"CREATE TABLE old AS SELECT {', '.join(cols)} FROM agent_runs; DROP TABLE agent_runs; "
                        "ALTER TABLE old RENAME TO agent_runs;")
        c.execute("INSERT INTO agent_runs(project_id, session_id, agent_id, kind, started_at, ended_at) "
                  "VALUES (NULL, 's', 'a', 'subagent', '2026-11-01T00:00:00.000Z', '2026-11-01T01:00:00.000Z')")
        c.execute("INSERT INTO agent_runs(project_id, session_id, agent_id, kind, started_at) "
                  "VALUES (NULL, 's', 'b', 'subagent', '2026-11-01T00:00:00.000Z')")
        c.commit()
        c.close()
        c = store.connect(path)                                          # adds the column, with the backfill
        try:
            rows = c.execute("SELECT agent_id, delivered_at FROM agent_runs ORDER BY id").fetchall()
            self.assertEqual([tuple(r) for r in rows], [("a", "2026-11-01T01:00:00.000Z"), ("b", None)])
            self.assertFalse(store.any_undelivered_run(c))
        finally:
            c.close()


class Postponed(Delivery):
    def continue_prompt(self):
        insert = delivery.postponed_insert(self.conn, self.A)
        return keepalive.session_message("continue", reason="r", progress="P.md", insert=insert, hooked=True)

    def test_the_continue_prompt_names_postponed_proposals_only_when_there_are_any(self):
        self.assertIsNone(delivery.postponed_insert(self.conn, self.A))
        self.assertNotIn("postponed", self.continue_prompt())
        self.go_on()
        pid = self.propose("task.edit", task_id=self.task, title="later")
        self.assertIsNone(delivery.postponed_insert(self.conn, self.A))                      # proposed, not postponed
        self.as_tm("proposal.review", proposal_id=pid, decision="postpone")
        text = self.continue_prompt()
        self.assertIn(f"1 postponed (P-{pid} task.edit #{self.task}: title='later')", text)
        self.assertIn("tasks.py review ID approve|edit|reject|postpone", text)
        self.assertIsNone(delivery.postponed_insert(self.conn, self.B))                      # another project's
        self.as_tm("proposal.review", proposal_id=pid, decision="reject", note="no")
        self.assertNotIn("postponed", self.continue_prompt())

    def test_the_insert_names_five_and_counts_the_rest(self):
        rows = [{"id": i, "summary": f"s{i}"} for i in range(1, 8)]
        self.assertEqual(proposals.postponed_text(rows), "7 postponed (P-1 s1; P-2 s2; P-3 s3; P-4 s4; P-5 s5; and 2 more)")
        self.assertEqual(proposals.postponed_text([]), "")


class Texts(unittest.TestCase):
    def test_a_long_run_is_cut_with_a_pointer(self):
        s = {"run": 9, "write_count": 14, "pending": [], "writes": {"tasks": [{"line": f"w{i}"} for i in range(14)]}}
        text = proposals.delivery_text(s)
        self.assertEqual(len(text.splitlines()), 1 + proposals.DELIVERY_LINES + 1)
        self.assertIn("4 more: tasks.py runs --show 9", text)
        self.assertEqual(proposals.pending_text(0), "")
        self.assertEqual(proposals.notice_text([]), "")


if __name__ == "__main__":
    unittest.main()
