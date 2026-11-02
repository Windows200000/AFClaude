#!/usr/bin/env python3
"""Tests for D-322 (the task-manager's own decisions are active at once; only one that changes an owner's
decision waits for the owner): the migration action decision.activate_own / `decisions.py activate-own`
(dry run, which entries go active, which stay, the owner's own answer, idempotence, who may run it) and
the classifier decisions.replaced_ids. The rule at decision.add is in test_decisions_native. Synthetic data.

    python3 -m unittest test_decisions_d322
"""
import contextlib
import glob
import io
import os
import shutil
import tempfile
import unittest

import testenv  # noqa: F401  (hermetic DB/config; before any AFClaude module)
import actions
import decisions as dec
import store
from testenv import SID

MCP = dict(actor=f"mcp:{SID}", via="mcp", autonomous=True)


class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-d322-")
        self.db = os.path.join(self.tmp, "afclaude.db")
        self.conn = store.connect(self.db, create=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.conn.close)
        self.owner = self.entry("owner", "active", title="Owner's own", words="the owner said so")
        self.owner2 = self.entry("owner", "active", title="Owner's other", words="the owner said more")
        # what the store held before D-322: unconfirmed active entries and proposals, each with its question
        self.m = self.entry("task-manager", "active", title="Mine, active")                       # M-like
        self.p = self.entry("task-manager", "proposed", title="Mine, proposed")
        self.reword = self.entry("task-manager", "proposed", title="Rewording", supersedes=self.owner)
        self.answer = self.entry("task-manager", "proposed", title="Owner's B", words="B")
        self.confirmed = self.entry("task-manager", "active", title="Confirmed", confirmed=True, question=False)
        self.free = self.entry("task-manager", "active", title="Refines", supersedes="none; refines how D-001 works")

    def entry(self, author, state, title, words=None, supersedes=None, confirmed=False, question=True):
        with store.transaction(self.conn):
            did = store.next_decision_id(self.conn)
            status = "proposed" if state == "proposed" else "active"
            if state == "proposed":
                status = dec.PROPOSED_STATUS
            row = store.add_decision_entry(self.conn, {
                "id": did, "title": title, "type": author, "state": state, "author": author, "status": status,
                "summary": f"summary of {did}", "words": words, "body": f"### {did} {title}\n- **Status:** {status}",
                "slug": did.lower(), "keywords": [], "topics": ["t"], "supersedes": supersedes}, "cli")
            if confirmed:
                self.conn.execute("UPDATE decisions SET confirmed_at = '2026-10-30T10:00:00.000Z', "
                                  "confirmed_by = 'owner' WHERE id = ?", (did,))
            if question and state == "proposed":
                actions.ask_decision_question(self.conn, actions.Ctx("cli", "cli"), row)
            elif question:                  # what the old code asked about an active, unconfirmed entry
                store.ask_question(self.conn, f"Confirm {did}?", None, None, f"Confirm {did}", ["confirm", "drop"], did)
        return did

    def migrate(self, *, via="cli", **params):
        return actions.perform(self.conn, "decision.activate_own", params, actor="cli", via=via)

    def get(self, did):
        return store.get_decision_entry(self.conn, did)

    def open_q(self, did):
        return store.open_decision_question(self.conn, did)

    def test_dry_run_lists_and_writes_nothing(self):
        before = self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        r = self.migrate(dry_run=True, owner_entries=[self.answer])
        self.assertEqual((r["active"], r["active_as_owners"]), ([self.m, self.p, self.free], [self.answer]))
        self.assertEqual(r["stay_proposed"], {self.reword: [self.owner]})
        self.assertEqual(len(r["questions_closed"]), 4)
        self.assertEqual(self.get(self.p)["state"], "proposed")
        self.assertIsNotNone(self.open_q(self.p))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], before)

    def test_what_goes_active_and_what_stays(self):
        qs = {d: self.open_q(d)["id"] for d in (self.m, self.p, self.answer, self.reword)}
        r = self.migrate(owner_entries=[self.answer])
        self.assertEqual(r["dry_run"], False)
        for did in (self.m, self.p, self.free):                      # its own: active, still the task-manager's
            e = self.get(did)
            self.assertEqual((e["state"], e["status"], e["confirmed_at"]), ("active", "active", None))
            self.assertIsNone(self.open_q(did))
        e = self.get(self.p)
        self.assertIn("- **Status:** active", e["body"])
        a = self.get(self.answer)                                    # the owner's own answer: active, protected
        self.assertEqual((a["state"], a["confirmed_by"] is not None), ("active", True))
        r = self.get(self.reword)                                    # touches an owner entry: still waiting
        self.assertEqual((r["state"], self.open_q(self.reword)["id"]), ("proposed", qs[self.reword]))
        for did in (self.m, self.p, self.answer):
            self.assertEqual(store.get_task(self.conn, qs[did])["status"], "cancelled")
            self.assertEqual(store.task_events(self.conn, qs[did])[-1]["detail"]["reason"], "D-322")
        self.assertEqual(self.get(self.confirmed)["state"], "active")
        self.assertEqual(sorted(x["target_id"] for x in [a for a in store.audit_log(self.conn, 200) if a["action"] == "decision.activate"]),
                         sorted([self.m, self.p, self.free, self.answer]))
        # the task-manager may change and delete its own, the owner's answer is protected
        self.conn.commit()
        actions.perform(self.conn, "decision.edit", {"decision": self.p, "summary": "Changed."}, **MCP)
        actions.perform(self.conn, "decision.delete", {"decision": self.p, "reason": "x"}, **MCP)
        with self.assertRaises(actions.Forbidden):
            actions.perform(self.conn, "decision.delete", {"decision": self.answer, "reason": "x"}, **MCP)

    def test_idempotent(self):
        self.migrate(owner_entries=[self.answer])
        n = self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        r = self.migrate(owner_entries=[self.answer])
        self.assertEqual((r["active"], r["active_as_owners"], r["questions_closed"]), ([], [], []))
        self.assertEqual(r["stay_proposed"], {self.reword: [self.owner]})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], n)

    def test_who_may_run_it_and_what_an_owner_entry_may_be(self):
        for via in ("mcp", "dashboard"):
            with self.assertRaises(actions.Forbidden):
                self.migrate(via=via)
        for bad in (self.owner, self.reword):              # an owner's own entry, one that replaces something
            with self.assertRaisesRegex(ValueError, "records the owner's words and replaces nothing"):
                self.migrate(owner_entries=[bad])
        with self.assertRaisesRegex(ValueError, "dry_run"):
            self.migrate(dry_run="yes")
        self.assertEqual(self.get(self.p)["state"], "proposed")

    def test_the_cli_backs_up_first_and_a_dry_run_does_not(self):
        self.conn.commit()
        out = dec.activate_own([self.answer], db_path=self.db, dry_run=True)
        self.assertEqual(glob.glob(self.db + ".pred322-*.bak"), [])
        self.assertTrue(out["dry_run"])
        out = dec.activate_own([self.answer], db_path=self.db)
        (bak,) = glob.glob(self.db + ".pred322-*.bak")
        self.assertEqual(out["backup"], bak)
        again = dec.activate_own([self.answer], db_path=self.db)       # nothing left: no second backup
        self.assertEqual(again["active"], [])
        self.assertEqual(len(glob.glob(self.db + ".pred322-*.bak")), 1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(dec.main(["--db", self.db, "activate-own", "--dry-run"]), 0)
        self.assertIn('"stay_proposed"', buf.getvalue())


class Classifier(unittest.TestCase):
    def test_replaced_ids_reads_only_a_plain_id_list(self):
        self.assertEqual(dec.replaced_ids("D-001, D-015"), ["D-001", "D-015"])
        self.assertEqual(dec.replaced_ids("D-001"), ["D-001"])
        self.assertEqual(dec.replaced_ids("none; refines how D-266 measures"), [])
        self.assertEqual(dec.replaced_ids("none"), [])
        self.assertEqual(dec.replaced_ids(None), [])
        self.assertEqual(dec.replaced_ids("D-308's gate part"), [])


if __name__ == "__main__":
    unittest.main()
