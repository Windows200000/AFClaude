#!/usr/bin/env python3
"""Tests for phase 5a A4 part 1 (docs/phase5a_plan.md §3.7, §5 A4): agent proposals and their review. Temp DBs only.

  direct     an agent's approved direct writes (D-185) apply, in shadow and with roles_enforce=on, and store nothing
  capture    every other agent write is stored as a proposal: `on` applies nothing and raises Proposed; `shadow`
             applies it under the old rule and records a decided shadow row; a replayed key stores one proposal
  review     approve / edit / reject / postpone through perform (via `proposal`, audit origin), who may review,
             a stale version is a Conflict that leaves the proposal open, a decided proposal can't be reviewed again
  entries    tasks.py proposals|review and MCP afclaude_proposals|afclaude_review

    python3 -m unittest test_proposals
"""
import contextlib
import io
import json
import os
import socket
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (temp DB first)
import actions  # noqa: E402
import proposals  # noqa: E402
import roles  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402
from test_permissions import Base  # noqa: E402


class Proposals(Base):
    def setUp(self):
        super().setUp()
        self.agent, self.tm = self.actor["agent"], self.actor["tm_own"]
        self.conn.execute("DELETE FROM agent_proposals")       # the matrix tests' fixture row
        self.conn.commit()

    def go_on(self):
        """roles_enforce=on (D-330: this host's machine user must hold an owner grant first)."""
        code = roles.new_registration_code()
        self.cli("machine.code", code_sha256=roles.code_sha256(code))
        r = actions.register_machine(self.conn, code, socket.gethostname(), actor="cli", via="cli")
        self.cli("grant.set", user_id=r["user_id"], scope="owner")
        self.cli("setting.set", key="roles_enforce", value="on")

    def as_agent(self, name, /, **params):
        return actions.perform(self.conn, name, params, actor=self.agent, via="mcp", autonomous=True)

    def as_tm(self, name, /, **params):
        return actions.perform(self.conn, name, params, actor=self.tm, via="mcp", autonomous=True)

    def propose(self, name="task.edit", /, **params):
        with self.assertRaises(actions.Proposed) as e:
            self.as_agent(name, **params)
        return e.exception.proposal_id

    def title(self, tid=None):
        return store.get_task(self.conn, tid or self.task)["title"]

    def rows(self, **kw):
        return store.list_proposals(self.conn, include_shadow=True, **kw)


class Direct(Proposals):
    def direct_writes(self):
        """Each approved direct write of an agent (§3.7); returns the proposals stored."""
        t = self.cli("task.add", title="own", project="alpha")["id"]
        r = self.as_agent("task.start", task_id=t, session=self.sess["AG"])
        self.assertEqual(r["status"], "in_progress")
        self.as_agent("task.update", task_id=t, status="blocked", note="which one?")
        self.assertEqual(store.get_task(self.conn, t)["status"], "blocked")
        self.cli("task.answer", task_id=t, answer="this one")
        self.as_agent("task.start", task_id=t, session=self.sess["AG"])
        self.as_agent("task.finish", task_id=t, summary="done")
        self.assertEqual(store.get_task(self.conn, t)["status"], "done")
        self.as_agent("doc.append", project=self.A, name="alerts", text="the DB failed\n")
        self.assertEqual(self.conn.execute("SELECT body FROM doc_entries WHERE body LIKE '%the DB failed%'").fetchone()[0],
                         "the DB failed\n")
        body = {"summary": "s", "working_notes": ["tests: none"]}
        self.as_agent("handoff.write", session_id=self.sess["AG"], body=body, project=self.A)
        return self.rows()

    def test_direct_writes_apply_in_shadow_and_store_nothing(self):
        self.assertEqual(self.direct_writes(), [])

    def test_direct_writes_apply_with_enforcement_on_and_store_nothing(self):
        self.go_on()
        self.assertEqual(self.direct_writes(), [])


class Capture(Proposals):
    WRITES = (("task.add", {"title": "new", "project": "alpha"}),
              ("task.edit", {"title": "renamed"}),
              ("task.priority", {"priority": "low"}),
              ("task.ask", {"question": "which?", "project": "alpha"}),
              ("doc.set", {"project": "alpha", "name": "goals", "text": "# new goals\n"}),
              ("decision.add", {"title": "d2", "topics": ["x"], "author": "task-manager", "project": "alpha"}),
              ("notice.post", {"title": "n2", "text": "x", "project": None}),
              ("prompt_var.set", {"project": "alpha", "name": "v1", "value": "x"}))

    def fill(self, name, p):
        p = dict(p)
        if name in ("task.edit", "task.priority"):
            p["task_id"] = self.task
        if name == "notice.post":
            p["project"] = self.A
        return p

    def snapshot(self):
        c = self.conn
        return (c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], self.title(),
                c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM notices").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])

    def test_on_stores_a_proposal_and_applies_nothing(self):
        self.go_on()
        for name, p in self.WRITES:
            p = self.fill(name, p)
            before = self.snapshot()
            with self.subTest(name):
                with self.assertRaises(actions.Proposed) as e:
                    self.as_agent(name, **p)
                self.assertEqual(self.snapshot(), before)
                row = store.get_proposal(self.conn, e.exception.proposal_id)
                self.assertEqual((row["action"], row["actor"], row["status"], row["project_id"], row["shadow"]),
                                 (name, self.agent, "proposed", self.A, False))
                self.assertEqual(row["payload"], p)
                self.assertTrue(row["summary"].startswith(name))
                self.assertIn(f"P-{row['id']}", e.exception.message)
        self.assertEqual(len(store.list_proposals(self.conn, open_only=True)), len(self.WRITES))

    def test_nothing_applies_while_the_task_manager_is_down(self):
        """No timeout, no sweep: the proposal waits whether or not anyone reviews (§3.7)."""
        self.go_on()
        self.cli("project.edit", project=self.A, manager_session=None)       # no task-manager session at all
        pid = self.propose(task_id=self.task, title="while away")
        self.assertEqual(store.get_proposal(self.conn, pid)["status"], "proposed")
        self.assertNotEqual(self.title(), "while away")
        self.assertEqual(self.title(), "t")

    def test_an_agent_without_a_project_is_refused_not_proposed(self):
        self.go_on()
        with self.assertRaises(actions.Forbidden):
            actions.perform(self.conn, "task.add", {"title": "x", "project": "beta"}, actor=self.agent, via="mcp",
                            autonomous=True)
        self.assertEqual(self.rows(), [])

    def test_a_replayed_key_stores_one_proposal(self):
        self.go_on()
        for _ in range(2):
            with self.assertRaises(actions.Proposed) as e:
                actions.perform(self.conn, "task.add", {"title": "k", "project": "alpha"}, actor=self.agent,
                                via="mcp", autonomous=True, key="proposal-key-1")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(e.exception.proposal_id, self.rows()[0]["id"])

    def test_shadow_applies_and_records_a_decided_shadow_row(self):
        r = self.as_agent("task.edit", task_id=self.task, title="in shadow")["title"]
        self.assertEqual((r, self.title()), ("in shadow", "in shadow"))        # the old rule let it through
        (row,) = self.rows()
        self.assertEqual((row["action"], row["shadow"], row["status"], row["reviewer"]),
                         ("task.edit", True, "approved", "shadow"))
        self.assertEqual(store.list_proposals(self.conn, open_only=True), [])  # never pending
        self.assertEqual(store.list_proposals(self.conn), [])                  # hidden unless asked for
        shadow = self.conn.execute("SELECT outcome, action FROM roles_shadow").fetchall()
        self.assertEqual([tuple(s) for s in shadow], [("proposal", "task.edit")])  # A3's log is unchanged
        with self.assertRaises(actions.Conflict) as e:
            self.cli("proposal.review", proposal_id=row["id"], decision="approve")
        self.assertIn("shadow", str(e.exception))

    def test_shadow_that_the_old_rule_refuses_stores_nothing(self):
        with self.assertRaises(actions.Forbidden):
            self.as_agent("prompt_var.set", project="alpha", name="v", value="x")      # owner_only today
        self.assertEqual(self.rows(), [])

    def test_a_proposal_links_to_the_agents_open_run(self):
        self.go_on()
        run = self.conn.execute("INSERT INTO agent_runs(project_id, session_id, kind, started_at) "
                                "VALUES (?, ?, 'session', '2026-11-07T00:00:00.000Z')", (self.A, self.sess["AG"])).lastrowid
        self.conn.commit()
        pid = self.propose(task_id=self.task, title="x")
        self.assertEqual(store.get_proposal(self.conn, pid)["run_id"], run)

    def test_the_summary_names_the_action_target_and_fields(self):
        s = proposals.summarize("task.edit", {"task_id": 12, "title": "a" * 100, "version": 3, "x": None})
        self.assertTrue(s.startswith("task.edit #12: title='aaa"), s)
        self.assertLessEqual(len(s), proposals.SUMMARY_MAX)
        self.assertEqual(proposals.version_seen({"version": 3}), 3)
        self.assertIsNone(proposals.version_seen({"version": True}))


class Review(Proposals):
    def setUp(self):
        super().setUp()
        self.go_on()
        self.pid = self.propose(task_id=self.task, title="proposed title")

    def review(self, decision, who="tm", pid=None, **kw):
        actor = {"tm": self.tm, "cli": "cli"}.get(who, who)
        return actions.perform(self.conn, "proposal.review", dict(proposal_id=pid or self.pid, decision=decision, **kw),
                               actor=actor, via="mcp", autonomous=True)

    def test_approve_applies_as_the_reviewer_with_an_origin(self):
        r = self.review("approve", note="ok")
        self.assertEqual((r["status"], r["applied"]["title"]), ("approved", "proposed title"))
        self.assertEqual(self.title(), "proposed title")
        p = store.get_proposal(self.conn, self.pid)
        self.assertEqual((p["status"], p["reviewer"], p["note"]), ("approved", self.tm, "ok"))
        audit = store.audit_log(self.conn, limit=5)
        write = next(a for a in audit if a["action"] == "task.edit")
        self.assertEqual((write["actor"], write["via"], write["origin"]),
                         (self.tm, "proposal", {"proposal": self.pid, "agent": self.agent}))
        self.assertTrue(any(a["action"] == "proposal.review" and a["target_id"] == str(self.pid) for a in audit))
        self.assertEqual(store.list_proposals(self.conn, open_only=True), [])

    def test_edit_lays_the_payload_over_the_proposed_fields(self):
        r = self.review("edit", payload={"title": "edited title"})
        self.assertEqual((r["status"], self.title()), ("edited", "edited title"))
        p = store.get_proposal(self.conn, self.pid)
        self.assertEqual(p["payload"], {"task_id": self.task, "title": "edited title"})
        with self.assertRaises(ValueError):
            self.review("edit", pid=self.propose(task_id=self.task, title="y"))      # no payload
        with self.assertRaises(ValueError):
            self.review("approve", pid=self.propose(task_id=self.task, title="z"), payload={"title": "x"})

    def test_reject_needs_a_note_and_applies_nothing(self):
        with self.assertRaises(ValueError):
            self.review("reject")
        r = self.review("reject", note="not now")
        self.assertEqual((r["status"], r["applied"], self.title()), ("rejected", None, "t"))
        self.assertEqual(store.get_proposal(self.conn, self.pid)["note"], "not now")

    def test_postpone_keeps_it_open_and_it_can_be_approved_later(self):
        self.assertEqual(self.review("postpone", note="next session")["status"], "postponed")
        self.assertEqual(self.title(), "t")
        self.assertEqual([p["id"] for p in store.list_proposals(self.conn, open_only=True)], [self.pid])
        self.review("approve")
        self.assertEqual(self.title(), "proposed title")

    def test_a_decided_proposal_cannot_be_reviewed_again(self):
        self.review("reject", note="no")
        with self.assertRaises(actions.Conflict):
            self.review("approve")
        with self.assertRaises(store.NotFound):
            self.review("approve", pid=9999)

    def test_a_stale_version_is_a_conflict_and_leaves_the_proposal_open(self):
        v = store.get_task(self.conn, self.task)["version"]
        pid = self.propose(task_id=self.task, title="from v%d" % v, version=v)
        self.cli("task.edit", task_id=self.task, title="owner changed it")       # the version moves on
        with self.assertRaises(actions.Conflict) as e:
            self.review("approve", pid=pid)
        self.assertIn("changed since it was read", str(e.exception))
        self.assertEqual((self.title(), store.get_proposal(self.conn, pid)["status"]),
                         ("owner changed it", "proposed"))
        now = store.get_task(self.conn, self.task)["version"]
        self.review("edit", pid=pid, payload={"version": now})                  # edit onto the current state
        self.assertEqual(self.title(), "from v%d" % v)
        self.assertEqual(store.get_proposal(self.conn, pid)["version_seen"], v)

    def test_a_failing_write_rolls_the_review_back(self):
        pid = self.propose("task.priority", task_id=self.task, priority="nonsense")
        with self.assertRaises(ValueError):
            self.review("approve", pid=pid)
        self.assertEqual(store.get_proposal(self.conn, pid)["status"], "proposed")

    def test_who_may_review(self):
        for who in ("agent", "tm_other", "viewer", "runner", "machine_ungranted"):
            with self.subTest(who):
                with self.assertRaises(actions.Forbidden):
                    actions.perform(self.conn, "proposal.review", {"proposal_id": self.pid, "decision": "approve"},
                                    actor=self.actor[who], via="mcp", autonomous=True)
        self.assertEqual(self.title(), "t")
        self.review("approve", who=self.actor["editor"])                          # D-190: editors review too
        self.assertEqual(self.title(), "proposed title")

    def test_the_reviewer_needs_the_right_to_make_the_write(self):
        """An edit that points the write at another project is checked against the reviewer, not the agent."""
        other = self.cli("task.add", title="b's", project="beta")["id"]
        pid = self.propose(task_id=self.task, title="x")
        with self.assertRaises(actions.Forbidden) as e:
            self.review("edit", pid=pid, payload={"task_id": other})
        self.assertIn("as the reviewer", str(e.exception))
        self.assertEqual(store.get_task(self.conn, other)["title"], "b's")

    def test_via_proposal_is_not_for_callers(self):
        with self.assertRaises(ValueError):
            actions.perform(self.conn, "task.edit", {"task_id": self.task, "title": "x"}, actor="cli", via="proposal")


class Entries(Proposals):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tasks.main(["--db", self.db, *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_tasks_py_lists_shows_and_reviews(self):
        self.go_on()
        pid = self.propose(task_id=self.task, title="via cli")
        rc, out, _ = self.run_cli("proposals")
        self.assertEqual(rc, 0)
        self.assertIn(f"P-{pid}", out)
        self.assertIn("task.edit #", out)
        rc, out, _ = self.run_cli("proposals", "--show", str(pid), "--json")
        self.assertEqual(json.loads(out)["payload"], {"task_id": self.task, "title": "via cli"})
        rc, out, err = self.run_cli("review", str(pid), "reject")
        self.assertEqual((rc, "needs a note" in err), (1, True))
        rc, out, _ = self.run_cli("review", str(pid), "edit", "--set", "title=edited by cli", "--note", "tweaked")
        self.assertEqual((rc, out.strip()), (0, f"P-{pid}: edited"))
        self.assertEqual(self.title(), "edited by cli")
        rc, out, _ = self.run_cli("proposals")
        self.assertIn("no open proposals", out)
        rc, out, _ = self.run_cli("proposals", "--all")
        self.assertIn("edited", out)

    def test_tasks_py_prints_a_proposed_write_as_an_answer(self):
        with mock.patch.object(actions, "perform", side_effect=actions.Proposed(7, self.A, "task.edit #1: title='x'")):
            rc, out, err = self.run_cli("prio", str(self.task), "low")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("proposed as P-7", out)


if __name__ == "__main__":
    unittest.main()
