#!/usr/bin/env python3
"""Tests for the question model (phase 4b Q3 W1; D-102, D-104, D-151, D-152, D-176, D-196, D-235, D-279, D-285,
D-300, D-304): question.decide (approve / edit / reject, confirm / drop; stale version; who may), the decision
question kept in step by decision.add|edit|confirm|delete|expire, task.answer refusing a decision question,
confirmed = protected, plain questions (options, `tasks.py question ...`, MCP afclaude_ask) and the added
columns on an older database (the deploy path). Synthetic data only.

    python3 -m unittest test_questions
"""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import testenv  # noqa: F401  (hermetic DB/config; before any AFClaude module)
import actions
import decisions as dec
import store
import tasks
from testenv import SID

OWNER_WORDS = "keep the titles short, always"
MCP = dict(actor=f"mcp:{SID}", via="mcp", autonomous=True)


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-questions-")
        self.db = os.path.join(self.tmp, "afclaude.db")
        self.conn = store.connect(self.db, create=True)
        patch = mock.patch.object(dec, "DEFAULT_SOURCE", os.path.join(self.tmp, "none.md"))
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, True)

    def do(self, name, *, actor="cli", via="cli", autonomous=False, **params):
        return actions.perform(self.conn, name, params, actor=actor, via=via, autonomous=autonomous)

    def dash(self, name, **params):
        return self.do(name, actor="owner", via="dashboard", **params)

    def manager(self, name="Mgr", sid="00000000-0000-4000-8000-0000000000b1"):
        """A project with a task-manager session: what an answer's inbox message goes to."""
        store.add_project(self.conn, name)
        store.update_project(self.conn, name, manager_session=sid)
        self.conn.commit()
        return sid

    def owner_entry(self, title="Short titles", **kw):
        return self.do("decision.add", title=title, topics=["lists"], author="owner", words=OWNER_WORDS,
                       summary=kw.pop("summary", "Titles stay short."), **kw)

    def proposal(self, title="Reworded titles", supersedes=None, **kw):
        return self.do("decision.add", title=title, topics=["lists"], author="task-manager",
                       summary=kw.pop("summary", "A merged rewording."), supersedes=supersedes, **kw)

    def question_of(self, did):
        return store.open_decision_question(self.conn, did)

    def entry(self, did):
        return store.get_decision_entry(self.conn, did)

    def audit(self, **kw):
        return store.audit_log(self.conn, 200, **kw)

    def task(self, tid):
        return store.get_task(self.conn, tid)


class Decide(Env):
    def test_approve_confirms_and_deletes_the_originals_in_one_transaction(self):
        a, b = self.owner_entry("First"), self.owner_entry("Second", summary="Other.")
        p = self.proposal(supersedes=[a["id"], b["id"]])
        q = self.question_of(p["id"])
        self.assertEqual((q["decision_id"], q["options"], q["status"]), (p["id"], ["approve", "edit", "reject"],
                                                                           "blocked"))
        r = self.dash("question.decide", task_id=q["id"], choice="approve", decision_version=p["version"])
        self.assertEqual((r["choice"], r["decision"]["state"], r["decision"]["replaced"]), ("approve", "active",
                                                                                              [a["id"], b["id"]]))
        e = self.entry(p["id"])
        self.assertEqual((e["state"], e["confirmed_by"] is not None, e["confirmed_at"] is not None), ("active", True, True))
        self.assertIsNone(self.entry(a["id"]))
        self.assertIsNone(self.entry(b["id"]))
        t = self.task(q["id"])
        self.assertEqual((t["status"], t["answer"]), ("done", "approve"))
        acts = [x["action"] for x in self.audit()]
        self.assertEqual(acts.count("question.decide"), 1)
        self.assertEqual(acts.count("decision.confirm"), 1)
        self.assertEqual(acts.count("decision.delete"), 2)                    # the originals, "replaced by"
        self.assertEqual([x["actor"] for x in self.audit() if x["action"] == "question.decide"], ["owner"])

    def test_approve_is_refused_and_writes_nothing_when_an_original_is_gone(self):
        a = self.owner_entry("First")
        p = self.proposal(supersedes=[a["id"]])
        q = self.question_of(p["id"])
        self.do("decision.delete", decision=a["id"], reason="gone")             # the owner's CLI
        n = len(self.audit())
        with self.assertRaisesRegex(store.NotFound, "no longer exists"):
            self.dash("question.decide", task_id=q["id"], choice="approve", decision_version=self.entry(p["id"])["version"])
        self.assertEqual(len(self.audit()), n)
        self.assertEqual((self.entry(p["id"])["state"], self.task(q["id"])["status"]), ("proposed", "blocked"))

    def test_edit_replaces_the_summary_then_confirms(self):
        self.manager()
        p = self.proposal(title="Old title")
        q = self.question_of(p["id"])
        r = self.dash("question.decide", task_id=q["id"], choice="edit", text="The owner's own wording.",
                      title="New title", note="closer", decision_version=p["version"])
        e = self.entry(p["id"])
        self.assertEqual((e["state"], e["summary"], e["title"]), ("active", "The owner's own wording.", "New title"))
        self.assertIn("The owner's own wording.", e["body"])
        self.assertEqual(r["decision"]["state"], "active")
        acts = [x["action"] for x in self.audit(target_type="decision", target_id=p["id"])]
        self.assertEqual(acts.count("decision.edit"), 1)
        self.assertEqual(acts.count("decision.confirm"), 1)
        with self.assertRaisesRegex(ValueError, "needs the new summary"):
            p2 = self.proposal(title="Two")
            self.dash("question.decide", task_id=self.question_of(p2["id"])["id"], choice="edit",
                      decision_version=p2["version"])

    def test_reject_deletes_the_proposal_and_keeps_the_originals(self):
        self.manager()
        a = self.owner_entry("First")
        p = self.proposal(supersedes=[a["id"]])
        q = self.question_of(p["id"])
        self.dash("question.decide", task_id=q["id"], choice="reject", note="not what I said",
                  decision_version=p["version"])
        self.assertIsNone(self.entry(p["id"]))
        self.assertEqual(self.entry(a["id"])["state"], "active")
        d = [x for x in self.audit(target_type="decision", target_id=p["id"]) if x["action"] == "decision.delete"][0]
        self.assertEqual(d["after"]["reason"], "rejected by the owner: not what I said")
        self.assertEqual(self.task(q["id"])["answer"], "reject")

    def test_confirm_and_drop_an_active_task_manager_entry(self):
        p = self.proposal(title="Mine")
        self.do("decision.confirm", decision=p["id"])                     # active and confirmed already
        # an active, unconfirmed entry (what the seeding left): write it as the old code would have
        self.conn.execute("UPDATE decisions SET confirmed_at = NULL, confirmed_by = NULL WHERE id = ?", (p["id"],))
        self.conn.commit()
        e = self.entry(p["id"])
        t = self.do("task.ask", question="x")                              # not a decision question: refused
        with self.assertRaisesRegex(ValueError, "isn't a decision question"):
            self.dash("question.decide", task_id=t["id"], choice="confirm", decision_version=e["version"])
        with self.conn:
            q = actions.ask_decision_question(self.conn, actions.Ctx("cli", "cli"), e)
        self.assertEqual(q["options"], ["confirm", "drop"])
        self.assertIn("recorded on its own", q["blocked_question"])
        self.dash("question.decide", task_id=q["id"], choice="confirm", decision_version=e["version"])
        e = self.entry(p["id"])
        self.assertEqual((e["state"], e["confirmed_by"]), ("active", "owner"))
        self.assertIsNotNone(e["confirmed_at"])
        self.assertEqual(self.task(q["id"])["answer"], "confirm")
        # drop
        p2 = self.proposal(title="Other")
        self.do("decision.confirm", decision=p2["id"])
        self.conn.execute("UPDATE decisions SET confirmed_at = NULL, confirmed_by = NULL WHERE id = ?", (p2["id"],))
        self.conn.commit()
        e2 = self.entry(p2["id"])
        with self.conn:
            q2 = actions.ask_decision_question(self.conn, actions.Ctx("cli", "cli"), e2)
        self.dash("question.decide", task_id=q2["id"], choice="drop", decision_version=e2["version"])
        self.assertIsNone(self.entry(p2["id"]))
        d = [x for x in self.audit(target_type="decision", target_id=p2["id"]) if x["action"] == "decision.delete"][0]
        self.assertEqual(d["after"]["reason"], "dropped by the owner")

    def test_a_stale_version_and_wrong_choices_are_refused(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        self.do("decision.topics", decision=p["id"], op="add", topics=["more"])      # bumps the version
        with self.assertRaises(actions.Conflict):
            self.dash("question.decide", task_id=q["id"], choice="approve", decision_version=p["version"])
        for kw, msg in (({"choice": "confirm"}, "choice must be one of approve|edit|reject"),
                        ({"choice": "approve", "text": "x"}, "belong to an edit"),
                        ({"choice": "approve", "decision_version": "1"}, "an integer")):
            params = dict(task_id=q["id"], decision_version=self.entry(p["id"])["version"])
            params.update(kw)
            with self.assertRaisesRegex(ValueError, msg):
                self.dash("question.decide", **params)
        self.assertEqual((self.entry(p["id"])["state"], self.task(q["id"])["status"]), ("proposed", "blocked"))

    def test_only_the_owner_decides(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        v = p["version"]
        with self.assertRaises(actions.Forbidden):                               # MCP
            self.do("question.decide", task_id=q["id"], choice="approve", decision_version=v, actor="mcp:abc",
                    via="mcp")
        with self.assertRaises(actions.Forbidden):                               # an autonomous session's CLI
            self.do("question.decide", task_id=q["id"], choice="approve", decision_version=v, autonomous=True)
        with self.assertRaises(actions.Forbidden):                               # the runner
            self.do("question.decide", task_id=q["id"], choice="approve", decision_version=v, actor="runner",
                    via="runner")
        self.assertEqual(self.entry(p["id"])["state"], "proposed")
        self.do("question.decide", task_id=q["id"], choice="approve", decision_version=v)   # the owner's own CLI

    def test_a_second_answer_is_refused(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        self.dash("question.decide", task_id=q["id"], choice="approve", decision_version=p["version"])
        with self.assertRaisesRegex(store.InvalidTransition, "answered or closed already"):
            self.dash("question.decide", task_id=q["id"], choice="approve", decision_version=p["version"])


class Invariant(Env):
    def test_a_new_proposal_asks_once_in_full(self):
        a = self.owner_entry("First", date="2026-10-01 10:00")
        p = self.proposal(supersedes=[a["id"]], summary="Merged text.")
        self.assertEqual(p["question"], self.question_of(p["id"])["id"])
        q = self.question_of(p["id"])
        for part in (p["id"], "Merged text.", a["id"], "First", OWNER_WORDS, "2026-10-01 10:00", "approve"):
            self.assertIn(part, q["blocked_question"])
        self.assertTrue(q["title"].startswith(f"Approve {p['id']}"))
        self.assertEqual(q["kind"], "question")
        with self.assertRaisesRegex(store.InvalidTransition, "already has an open question"):
            with self.conn:
                actions.ask_decision_question(self.conn, actions.Ctx("cli", "cli"), self.entry(p["id"]))
        n = self.conn.execute("SELECT COUNT(*) FROM tasks WHERE decision_id = ?", (p["id"],)).fetchone()[0]
        self.assertEqual(n, 1)
        with self.assertRaisesRegex(ValueError, "needs nothing from the owner"):     # an owner's entry
            actions.ask_decision_question(self.conn, actions.Ctx("cli", "cli"), self.entry(a["id"]))
        self.assertIsNone(self.question_of(a["id"]))                                  # and it got none

    def test_the_unique_index_allows_one_open_question_per_decision(self):
        p = self.proposal()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO tasks(title, status, kind, created_at, updated_at, decision_id) "
                              "VALUES ('x', 'blocked', 'question', 'now', 'now', ?)", (p["id"],))
        self.conn.rollback()

    def test_an_edit_renders_the_question_again(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        r = self.do("decision.edit", decision=p["id"], summary="A better summary.", title="Better")
        self.assertEqual(r["question"], q["id"])
        q2 = self.task(q["id"])
        self.assertIn("A better summary.", q2["blocked_question"])
        self.assertTrue(q2["title"].startswith(f"Approve {p['id']}: Better"))
        self.assertEqual(q2["status"], "blocked")
        self.assertEqual(store.task_events(self.conn, q["id"])[-1]["event"], "reworded")
        self.do("decision.edit", decision=p["id"], scope="x")                          # not shown: text unchanged
        self.assertEqual(self.task(q["id"])["blocked_question"], q2["blocked_question"])

    def test_confirm_delete_and_expire_close_the_question(self):
        for how in ("confirm", "delete", "expire"):
            p = self.proposal(title=how)
            q = self.question_of(p["id"])
            r = self.do(f"decision.{how}", decision=p["id"], **({"reason": "r"} if how == "delete" else {}))
            self.assertEqual(r["question"], q["id"], how)
            t = self.task(q["id"])
            self.assertEqual(t["status"], "cancelled", how)
            self.assertIn(p["id"], store.task_events(self.conn, q["id"])[-1]["detail"]["reason"])
            self.assertIsNone(self.question_of(p["id"]))

    def test_a_confirm_from_chat_evidence_closes_it(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        store.add_inbox(self.conn, f"Yes, {p['id']} is fine.", SID, "owner")
        self.conn.commit()
        r = self.do("decision.confirm", decision=p["id"], **MCP)
        self.assertEqual((r["state"], r["question"]), ("active", q["id"]))
        self.assertEqual(self.task(q["id"])["status"], "cancelled")
        self.assertIsNotNone(self.entry(p["id"])["confirmed_at"])

    def test_confirming_a_replacing_proposal_closes_the_questions_of_the_replaced_ones(self):
        old = self.proposal(title="Old proposal")
        q_old = self.question_of(old["id"])
        new = self.proposal(title="New", supersedes=[old["id"]])
        self.do("decision.confirm", decision=new["id"])
        self.assertIsNone(self.entry(old["id"]))
        self.assertEqual(self.task(q_old["id"])["status"], "cancelled")

    def test_task_answer_and_hand_closing_refuse_a_decision_question(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        for call in (lambda: self.do("task.answer", task_id=q["id"], answer="yes"),
                     lambda: self.do("task.answer", task_id=q["id"], answer="yes", actor="owner", via="dashboard"),
                     lambda: self.do("task.answer", task_id=q["id"], answer="yes", actor="mcp:abc", via="mcp")):
            with self.assertRaisesRegex(ValueError, f"asks about decision {p['id']}"):
                call()
        with self.assertRaises(actions.Forbidden):
            self.do("question.close", task_id=q["id"], reason="x")
        with self.assertRaises(actions.Forbidden):
            self.do("task.cancel", task_id=q["id"])
        with self.assertRaises(actions.Forbidden):
            self.do("task.update", task_id=q["id"], status="cancelled")
        with self.assertRaisesRegex(store.InvalidTransition, "isn't reopened"):
            self.do("task.reopen", task_id=q["id"])
        with self.assertRaisesRegex(ValueError, "its kind doesn't change"):
            self.do("task.edit", task_id=q["id"], kind="task")
        self.assertEqual(self.task(q["id"])["status"], "blocked")

    def test_task_update_and_task_edit_cant_reopen_or_rewrite_a_decision_question(self):
        """F1/F2: every path to store.reopen_task / store.update_task meets the same refusal."""
        p = self.proposal()
        q = self.question_of(p["id"])
        self.dash("question.decide", task_id=q["id"], choice="reject", decision_version=p["version"])   # done
        p2 = self.proposal(title="Second")                                  # another open question exists
        self.assertIsNotNone(self.question_of(p2["id"]))
        with self.assertRaisesRegex(store.InvalidTransition, "isn't reopened"):
            self.do("task.update", task_id=q["id"], status="pending")
        self.assertEqual(self.task(q["id"])["status"], "done")
        q2 = self.question_of(p2["id"])
        for params in ({"kind": "task"}, {"title": "anything"}, {"description": "x"}):
            with self.assertRaisesRegex(ValueError, "is a decision question"):
                self.do("task.edit", task_id=q2["id"], **params)
            with self.assertRaisesRegex(ValueError, "is a decision question"):
                self.do("task.update", task_id=q2["id"], fields=params)
        self.do("task.edit", task_id=q2["id"], priority="low")             # other fields stay editable
        self.assertEqual(self.task(q2["id"])["priority"], "low")

    def test_a_question_whose_proposal_an_older_code_confirmed_is_not_stuck(self):
        """F3: the entry is active and unconfirmed now (kind confirm), the question still offers approve."""
        p = self.proposal()
        q = self.question_of(p["id"])
        body = self.entry(p["id"])["body"]
        self.conn.execute("UPDATE decisions SET state = 'active', status = 'active' WHERE id = ?", (p["id"],))
        self.conn.commit()                                                   # what older code's confirm wrote
        e = self.entry(p["id"])
        with self.assertRaisesRegex(ValueError, "choice must be one of confirm\\|drop"):
            self.dash("question.decide", task_id=q["id"], choice="approve", decision_version=e["version"])
        self.do("question.close", task_id=q["id"], reason="confirmed by older code")
        self.assertEqual(self.task(q["id"])["status"], "cancelled")
        self.assertIn(p["id"], body)

    def test_an_edit_refreshes_the_choices_of_a_stale_question(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        self.conn.execute("UPDATE decisions SET state = 'active', status = 'active' WHERE id = ?", (p["id"],))
        self.conn.commit()
        self.do("decision.edit", decision=p["id"], title="Retitled")
        q2 = self.task(q["id"])
        self.assertEqual(q2["options"], ["confirm", "drop"])
        self.assertIn("recorded on its own", q2["blocked_question"])
        self.dash("question.decide", task_id=q["id"], choice="confirm", decision_version=self.entry(p["id"])["version"])
        self.assertIsNotNone(self.entry(p["id"])["confirmed_at"])

    def test_a_dangling_question_can_be_closed(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        self.conn.execute("DELETE FROM decision_topics WHERE decision_id = ?", (p["id"],))
        self.conn.execute("DELETE FROM decisions WHERE id = ?", (p["id"],))   # an older code version deleted it
        self.conn.commit()
        with self.assertRaisesRegex(store.NotFound, "close question"):
            self.dash("question.decide", task_id=q["id"], choice="approve", decision_version=0)
        self.do("question.close", task_id=q["id"], reason="the entry is gone")
        self.assertEqual(self.task(q["id"])["status"], "cancelled")


class Protection(Env):
    def test_a_confirmed_task_manager_entry_is_protected_like_an_owners(self):
        p = self.proposal(title="Mine")
        self.do("decision.edit", decision=p["id"], summary="Still mine.", **MCP)          # unconfirmed: fine
        self.do("decision.confirm", decision=p["id"])                                      # the owner
        for call in (lambda: self.do("decision.edit", decision=p["id"], title="x", **MCP),
                     lambda: self.do("decision.delete", decision=p["id"], reason="r", **MCP),
                     lambda: self.do("decision.expire", decision=p["id"], **MCP),
                     lambda: self.do("decision.edit", decision=p["id"], title="x", autonomous=True)):
            with self.assertRaises(actions.Forbidden):
                call()
        with self.assertRaisesRegex(ValueError, "not edited, by anyone"):
            self.do("decision.edit", decision=p["id"], summary="The owner's CLI can't either")
        self.do("decision.edit", decision=p["id"], scope="x")                              # other fields: the owner
        self.do("decision.topics", decision=p["id"], op="add", topics=["x"], **MCP)        # topics stay open
        self.assertEqual(self.entry(p["id"])["state"], "active")

    def test_an_unconfirmed_one_stays_editable_and_confirm_is_idempotent(self):
        o = self.owner_entry()
        r = self.do("decision.confirm", decision=o["id"])                                  # owner's: a no-op
        self.assertEqual(len(self.audit(target_type="decision", target_id=o["id"])), 1)    # just the add
        self.assertIsNone(self.entry(o["id"])["confirmed_at"])
        self.assertEqual(r["state"], "active")
        p = self.proposal()
        self.do("decision.confirm", decision=p["id"])
        n = len(self.audit())
        self.do("decision.confirm", decision=p["id"])
        self.assertEqual(len(self.audit()), n)


class Plain(Env):
    def test_options_are_validated_and_stored(self):
        t = self.do("task.ask", question="Which one?", options=["a", "b"], title="Pick")
        self.assertEqual((t["options"], t["decision_id"], t["title"]), (["a", "b"], None, "Pick"))
        self.assertEqual(self.task(t["id"])["options"], ["a", "b"])
        self.assertIsNone(self.do("task.ask", question="free text")["options"])
        self.assertEqual(self.do("task.ask", question="one", options="only")["options"], ["only"])
        for bad in (["a", "a"], [""], [str(i) for i in range(9)], ["y" * 121], [1]):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.do("task.ask", question="q", options=bad)

    def test_a_plain_answer_still_closes_it(self):
        t = self.do("task.ask", question="Which one?", options=["a", "b"])
        r = self.do("task.answer", task_id=t["id"], answer="b", actor="owner", via="dashboard")
        self.assertEqual((r["status"], r["answer"]), ("done", "b"))


class Cli(Env):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tasks.main(["--db", self.db, "question", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_ask_list_close(self):
        actions.perform(self.conn, "project.add", {"name": "Proj"}, actor="cli", via="cli")
        code, out, _ = self.cli("ask", "Which database should it be?\nLong text here.", "--title", "Database",
                                "--option", "sqlite", "--option", "postgres", "--project", "Proj")
        self.assertEqual((code, out.strip()), (0, "question #1: blocked"))
        t = self.task(1)
        self.assertEqual((t["kind"], t["options"], t["project"], t["title"]), ("question", ["sqlite", "postgres"],
                                                                                "Proj", "Database"))
        code, out, _ = self.cli("list")
        self.assertIn("#1  blocked  Proj  Database", out)
        self.assertIn("Long text here.", out)
        self.assertIn("options: sqlite | postgres", out)
        self.assertEqual(json.loads(self.cli("list", "--json")[1])[0]["options"], ["sqlite", "postgres"])
        self.assertIn("no answered or closed", self.cli("list", "--answered")[1])
        with self.assertRaises(SystemExit):                                  # --reason is required
            self.cli("close", "1")
        code, out, _ = self.cli("close", "1", "--reason", "settled in chat")
        self.assertEqual((code, out.strip()), (0, "question #1: cancelled"))
        self.assertEqual(self.task(1)["status"], "cancelled")
        self.assertIn("settled in chat", store.task_events(self.conn, 1)[-1]["detail"]["reason"])
        self.assertIn("#1  cancelled", self.cli("list", "--answered")[1])
        self.assertIn("no open questions", self.cli("list")[1])
        code, _, err = self.cli("close", "1", "--reason", "again")
        self.assertEqual(code, 1)
        self.assertIn("only an open one", err)

    def test_close_refuses_a_task_and_a_waiting_decision_question(self):
        t = self.do("task.add", title="plain task")
        code, _, err = self.cli("close", str(t["id"]), "--reason", "x")
        self.assertEqual(code, 1)
        self.assertIn("isn't a question", err)
        p = self.proposal()
        q = self.question_of(p["id"])
        code, _, err = self.cli("close", str(q["id"]), "--reason", "x")
        self.assertEqual(code, 1)
        self.assertIn("still waits for the owner", err)
        out = self.cli("list")[1]
        self.assertIn(f"[about {p['id']}]", out)

    def test_answer_refuses_a_decision_question(self):
        p = self.proposal()
        q = self.question_of(p["id"])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tasks.main(["--db", self.db, "answer", str(q["id"]), "yes"])
        self.assertEqual(code, 1)
        self.assertIn("asks about decision", err.getvalue())


class DeployPath(unittest.TestCase):
    """The columns of Q3 are additive (no schema bump): a database made before them gains them at connect(),
    the first write of the host's newer code (tools/deploy.py) works against it, and the older container's
    explicit-column writes still work afterwards (it knows none of the new columns)."""
    NEW = {"tasks": {"options", "decision_id"}, "inbox_messages": {"question_id"},
           "decisions": {"confirmed_at", "confirmed_by"}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-questions-gap-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "afclaude.db")
        old = {t: [c for c in cols if c[0] not in self.NEW.get(t, ())] for t, cols in store.COLUMNS.items()}
        with mock.patch.object(store, "COLUMNS", old), mock.patch.object(store, "AFTER_COLUMNS", []):
            conn = store.connect(self.path, create=True)       # a database as the older code left it
            actions.perform(conn, "task.add", {"title": "t", "project": "P"}, actor="cli", via="cli")
            conn.close()

    def cols(self, conn, table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    def test_the_database_before_has_none_of_them(self):
        raw = sqlite3.connect(self.path)
        for table, names in self.NEW.items():
            self.assertFalse(names & self.cols(raw, table), table)
        raw.close()

    def test_the_first_write_of_newer_code_adds_them_and_everything_works(self):
        conn = store.connect(self.path)                          # deploy.py's first write connects first
        self.addCleanup(conn.close)
        for table, names in self.NEW.items():
            self.assertTrue(names <= self.cols(conn, table), table)
        self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'tasks_open_decision_question'")
                             .fetchone())
        self.assertEqual(store.get_meta(conn, "schema_version"), str(store.SCHEMA_VERSION))   # no bump
        actions.perform(conn, "doc.append", {"project": "P", "name": "progress", "text": "- deploy\n"},
                        actor="cli", via="cli")
        p = actions.perform(conn, "decision.add", {"title": "Prop", "topics": ["x"], "author": "task-manager"},
                            actor="cli", via="cli")
        self.assertEqual(store.open_decision_question(conn, p["id"])["id"], p["question"])
        store.connect(self.path).close()                         # a second connect changes nothing, raises nothing

    def test_deploy_py_gets_through_its_first_writes(self):
        """tools/deploy.py on the host (newer code) against the database the new code hasn't touched yet:
        its connect adds the columns, then the drain and the rebuild call go through."""
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
        try:
            import deploy
        finally:
            sys.path.pop(0)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(store, "DB_PATH", self.path), \
                mock.patch.object(deploy, "check_start_guard", return_value=True), \
                mock.patch.object(deploy, "check_scheduler_mode", return_value=True), \
                mock.patch.object(deploy, "wait_clear", return_value=True), \
                mock.patch.object(deploy, "code_version", return_value="abc1234"), \
                mock.patch.object(deploy.runner, "wake", return_value=True), \
                mock.patch.object(deploy.subprocess, "run", side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(deploy.main([]), 0)
        self.assertIn("drained", out.getvalue())
        self.assertEqual(calls[-1][:2], ["docker", "compose"])
        conn = store.connect(self.path)
        self.addCleanup(conn.close)
        self.assertTrue(self.NEW["tasks"] <= self.cols(conn, "tasks"))
        self.assertIsNotNone(actions.runner_drain(conn))

    def test_the_older_codes_writes_still_work_on_the_new_columns(self):
        store.connect(self.path).close()
        raw = sqlite3.connect(self.path)                         # the older container: explicit column lists
        raw.execute("INSERT INTO tasks(title, description, project_id, stage_seq, priority, status, kind, created_at, "
                    "updated_at, created_by_session) VALUES ('old', NULL, NULL, 1, 'high', 'blocked', 'question', "
                    "'2026-10-31T00:00:00Z', '2026-10-31T00:00:00Z', NULL)")
        raw.execute("INSERT INTO inbox_messages(created_at, created_by, session_id, text) VALUES "
                    "('2026-10-31T00:00:00Z', 'owner', 's', 'hi')")
        raw.commit()
        raw.close()
        conn = store.connect(self.path)
        self.addCleanup(conn.close)
        t = [x for x in store.list_tasks(conn, kind="question")][0]
        self.assertEqual((t["title"], t["options"], t["decision_id"]), ("old", None, None))
        r = actions.perform(conn, "task.answer", {"task_id": t["id"], "answer": "ok"}, actor="owner", via="dashboard")
        self.assertEqual(r["status"], "done")                    # a question the older code asked is a plain one


if __name__ == "__main__":
    unittest.main()
