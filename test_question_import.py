#!/usr/bin/env python3
"""Tests for the question import (phase 4b Q3 W3, docs/phase4_plan.md; D-102, D-151, D-181, D-235, D-263, D-279):
`questions.py import` (action question.import) on synthetic OPEN_QUESTIONS.md files in both formats: one question
per item with the text verbatim and the counts, the decision questions added once, the preconditions that refuse
before any write, the dry run, the backup, `--undo` and its refusals, the changed-file alert after the cut-over,
and the refusal through MCP. Synthetic data only.

    python3 -m unittest test_question_import
"""
import glob
import hashlib
import json
import os
import shutil
import tempfile
import unittest

import testenv  # noqa: F401  (hermetic DB/config; before any AFClaude module)
import actions
import questions as qs
import store

DASH = """# Open questions for the user (the manager removes answered ones)

- **Q-first: should the first thing happen?**

Some background in a plain paragraph.

Options: (a) yes; (b) no.

- **Q-second-thing: and the second?**
  An indented continuation line.
  - a nested point
"""

SECTIONS = """# Open questions

<!-- comments before the first item are no question -->

## Q-1: First, in the section format
Body line one.
- a bullet inside the section stays in it

## Q-2: Second
Only a body line.

```
## not a heading, inside a fence
```
"""


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Env(unittest.TestCase):
    """A temp DB with a project, a file to import, and decision entries that have no question yet."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-qimport-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "afclaude.db")
        self.src = os.path.join(self.tmp, "OPEN_QUESTIONS.md")
        self.write(DASH)
        c = store.connect(self.db, create=True)
        store.add_project(c, "Mgr")
        store.update_project(c, "Mgr", manager_session=testenv.SID)
        c.close()
        self.alerts = []

    def write(self, text):
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write(text)

    def conn(self):
        return store.connect(self.db)

    def run_import(self, **kw):
        kw.setdefault("project", "Mgr")
        return qs.import_questions(self.db, self.src, **kw)

    def undo(self, **kw):
        return qs.import_questions(self.db, undo=True, **kw)

    def refused(self, **kw):
        with self.assertRaises(qs.ImportRefused) as cm:
            self.run_import(**kw)
        return str(cm.exception)

    def backups(self):
        return glob.glob(self.db + ".preqimport-*.bak")

    def meta(self, key):
        c = self.conn()
        try:
            return store.get_meta(c, key)
        finally:
            c.close()

    def dump(self):
        c = self.conn()
        try:
            return store._content_hash(c)
        finally:
            c.close()

    def perform(self, name, params, via="cli", actor="cli"):
        c = self.conn()
        try:
            return actions.perform(c, name, params, actor=actor, via=via)
        finally:
            c.close()

    def add_entry(self, state="proposed", author="task-manager", confirmed=False, title=None):
        """A native entry as the seeding writes them: no question asked."""
        c = self.conn()
        try:
            with store.transaction(c):
                did = store.next_decision_id(c)
                row = store.add_decision_entry(c, {
                    "id": did, "title": title or f"Entry {did}", "type": author, "state": state, "author": author,
                    "status": "active" if state == "active" else "proposed", "summary": f"summary of {did}",
                    "words": '"the owner said so"', "body": f"### {did}\n", "slug": f"e-{did.lower()}",
                    "keywords": [], "topics": ["t"]}, "cli")
                if confirmed:
                    c.execute("UPDATE decisions SET confirmed_at = '2026-10-30T10:00:00Z', "
                              "confirmed_by = 'owner' WHERE id = ?", (did,))
            return row["id"]
        finally:
            c.close()

    def questions(self, status=None):
        c = self.conn()
        try:
            rows = [r for r in c.execute("SELECT id FROM tasks WHERE kind = 'question' ORDER BY id")]
            out = [store.get_task(c, r[0]) for r in rows]
        finally:
            c.close()
        return [t for t in out if status is None or t["status"] == status]


class Import(Env):
    def test_one_question_per_item_verbatim_dash_format(self):
        r = self.run_import()
        qq = self.questions("blocked")
        self.assertEqual(len(qq), 2)
        self.assertEqual([t["title"] for t in qq], ["should the first thing happen?", "and the second?"])
        self.assertTrue(qq[0]["blocked_question"].startswith("**Q-first: should the first thing happen?**\n\n"))
        self.assertIn("Options: (a) yes; (b) no.", qq[0]["blocked_question"])
        self.assertEqual(qq[1]["blocked_question"],
                         "**Q-second-thing: and the second?**\n  An indented continuation line.\n  - a nested point")
        self.assertEqual({t["project"] for t in qq}, {"Mgr"})
        self.assertEqual([i["chars"] for i in r["items"]], [len(t["blocked_question"]) for t in qq])
        self.assertEqual([i["title"] for i in r["items"]], [t["title"] for t in qq])
        self.assertEqual(r["counts"], {"items": 2, "proposals": 0, "unconfirmed_entries": 0, "questions": 2})
        self.assertEqual(r["question_ids"], [t["id"] for t in qq])

    def test_one_question_per_item_verbatim_section_format(self):
        self.write(SECTIONS)
        r = self.run_import()
        qq = self.questions("blocked")
        self.assertEqual([t["title"] for t in qq], ["First, in the section format", "Second"])
        self.assertEqual(qq[0]["blocked_question"], "Q-1: First, in the section format\nBody line one.\n"
                                                    "- a bullet inside the section stays in it")
        self.assertEqual(qq[1]["blocked_question"],
                         "Q-2: Second\nOnly a body line.\n\n```\n## not a heading, inside a fence\n```")
        self.assertEqual([i["style"] for i in r["items"]], ["##", "##"])

    def test_title_without_label_and_cut(self):
        self.assertEqual(qs.item_title("**Q-3b-claude: should it?**"), "should it?")
        self.assertEqual(qs.item_title("Q-1: plain"), "plain")
        self.assertEqual(qs.item_title("No label here"), "No label here")
        self.assertEqual(len(qs.item_title("Q-9: " + "x" * 300)), qs.TITLE_MAX)

    def test_meta_and_audit(self):
        r = self.run_import()
        self.assertEqual(self.meta(qs.SOURCE_META), "db")
        self.assertEqual(self.meta(qs.FILE_SHA_META), sha(DASH))
        self.assertEqual(r["sha256"], sha(DASH))
        rec = json.loads(self.meta(qs.IMPORTED_META))
        self.assertEqual(rec["items"], r["question_ids"])
        c = self.conn()
        try:
            rows = [a for a in store.audit_log(c, 50) if a["action"] == "question.import"]
        finally:
            c.close()
        self.assertEqual(len(rows), 1)       # the dry run writes none
        self.assertEqual(rows[0]["actor"], "cli")

    def test_decision_questions_added_once(self):
        p1, p2 = self.add_entry(), self.add_entry()
        t1 = self.add_entry("active")
        self.add_entry("active", confirmed=True)             # confirmed: nobody asks
        self.add_entry("active", author="owner")             # the owner's own: nobody asks
        r = self.run_import()
        self.assertEqual(r["counts"], {"items": 2, "proposals": 2, "unconfirmed_entries": 1, "questions": 5})
        by_decision = {t["decision_id"]: t for t in self.questions("blocked") if t["decision_id"]}
        self.assertEqual(set(by_decision), {p1, p2, t1})
        self.assertEqual(by_decision[p1]["options"], ["approve", "edit", "reject"])
        self.assertEqual(by_decision[t1]["options"], ["confirm", "drop"])
        self.assertEqual(sorted(r["question_ids"]), sorted(t["id"] for t in self.questions("blocked")))
        # the catch-up is idempotent: nothing more is asked for the same entries
        c = self.conn()
        try:
            with store.transaction(c):
                self.assertEqual(actions.ensure_decision_questions(c, actions.Ctx("cli", "cli")), [])
        finally:
            c.close()
        self.assertEqual(len(self.questions("blocked")), 5)

    def test_ensure_asks_only_what_has_no_open_question(self):
        p1 = self.add_entry()
        c = self.conn()
        try:
            with store.transaction(c):
                first = actions.ensure_decision_questions(c, actions.Ctx("cli", "cli"))
            self.assertEqual([(x["decision"], x["kind"]) for x in first], [(p1, "proposal")])
            p2 = self.add_entry()
            with store.transaction(c):
                second = actions.ensure_decision_questions(c, actions.Ctx("cli", "cli"))
            self.assertEqual([x["decision"] for x in second], [p2])
        finally:
            c.close()

    def test_dry_run_writes_nothing(self):
        self.add_entry()
        self.add_entry("active")
        before = self.dump()
        r = self.run_import(dry_run=True)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.backups(), [])
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["counts"], {"items": 2, "proposals": 1, "unconfirmed_entries": 1, "questions": 4})
        self.assertEqual([i["title"] for i in r["items"]], ["should the first thing happen?", "and the second?"])
        self.assertIsNone(self.meta(qs.SOURCE_META))
        real = self.run_import()                  # the real run says the same
        self.assertEqual((real["counts"], real["items"]), (r["counts"], r["items"]))

    def test_second_run_refused(self):
        self.run_import()
        n = len(self.questions())
        self.assertIn("imported already", self.refused())
        self.assertIn("imported already", self.refused(dry_run=True))
        self.assertEqual(len(self.questions()), n)
        self.assertEqual(len(self.backups()), 1)

    def test_backup_made(self):
        self.assertEqual(self.backups(), [])
        r = self.run_import()
        (bak,) = self.backups()
        self.assertEqual(r["backup"], bak)
        old = store.connect(bak)
        try:
            self.assertEqual(old.execute("SELECT COUNT(*) FROM tasks WHERE kind = 'question'").fetchone()[0], 0)
        finally:
            old.close()

    def test_default_project_is_the_checkouts(self):
        c = self.conn()
        try:
            store.add_project(c, "Checkout", path=actions.HERE)
        finally:
            c.close()
        r = qs.import_questions(self.db, self.src)
        self.assertEqual(r["project"], "Checkout")


class Refusals(Env):
    """Every problem is listed; nothing is written (no backup, no row, no meta)."""
    def assert_untouched(self, before):
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.backups(), [])

    def test_text_before_the_first_item(self):
        self.write("# Title\nSome stray text that would be lost\n\n- **Q-a: ok?**\n")
        before = self.dump()
        self.assertIn("would be lost", self.refused())
        self.assert_untouched(before)

    def test_comments_and_title_before_the_first_item_are_fine(self):
        self.write("# Title\n\n<!--\n a multi-line\n comment\n-->\n\n- **Q-a: ok?**\n")
        self.assertEqual(self.run_import()["counts"]["items"], 1)

    def test_no_item(self):
        self.write("# Title only\n")
        self.assertIn("no item", self.refused())

    def test_empty_title_and_every_problem_listed(self):
        self.write("stray\n- **Q-a:**\n- **Q-b: fine?**\n")
        before = self.dump()
        msg = self.refused()
        self.assertIn("2 problems", msg)
        self.assertIn("stray", msg)
        self.assertIn("has no title", msg)
        self.assert_untouched(before)

    def test_an_item_that_only_points_to_a_file(self):
        self.write("- **Q-a: see QUESTIONS_DETAIL.md**\n")
        self.assertIn("points mainly to a file", self.refused())

    def test_unknown_project_and_missing_file(self):
        self.assertIn("no project 'Nope'", self.refused(project="Nope"))
        os.remove(self.src)
        self.assertIn("can't read", self.refused())
        self.assertEqual(self.backups(), [])

    def test_this_checkout_is_no_project(self):
        with self.assertRaisesRegex(qs.ImportRefused, "no project"):
            qs.import_questions(self.db, self.src)

    def test_only_the_cli(self):
        params = {"path": self.src, "project": "Mgr", "dry_run": True}
        for via in ("mcp", "dashboard", "runner"):
            with self.subTest(via):
                with self.assertRaises(actions.Forbidden):
                    self.perform("question.import", params, via=via,
                                 actor="mcp:abc" if via == "mcp" else "user:1" if via == "dashboard" else "runner")
        with self.assertRaises(actions.Forbidden):
            self.perform("question.import", {"undo": True, "dry_run": True}, via="mcp", actor="mcp:abc")
        self.assertIsNone(self.meta(qs.SOURCE_META))
        self.assertEqual(self.questions(), [])


class Undo(Env):
    def test_undo_restores(self):
        pid = self.add_entry()
        before_tasks = len(self.questions())
        r = self.run_import()
        ids = r["question_ids"]
        self.assertEqual(len(ids), 3)
        out = self.undo()
        self.assertEqual(sorted(out["cancelled"]), sorted(ids))
        self.assertEqual({t["status"] for t in self.questions()}, {"cancelled"})
        self.assertEqual(len(self.questions()), before_tasks + 3)
        for k in (qs.FILE_SHA_META, qs.IMPORTED_META):
            self.assertFalse(self.meta(k))
        c = self.conn()
        try:
            self.assertEqual(qs.source_of(c), "file")
            self.assertIsNone(store.open_decision_question(c, pid))
            ev = [e for i in ids for e in store.task_events(c, i) if e["event"] == "cancelled"]
            audit = [a for a in store.audit_log(c, 50) if a["action"] == "question.import"]
        finally:
            c.close()
        self.assertEqual(len(ev), 3)
        self.assertEqual(len(audit), 2)                       # the import and its undo
        self.assertIn("import undone", json.dumps(audit[0]["after"]))
        again = self.run_import()                              # the cut-over can be done again
        self.assertEqual(len(again["question_ids"]), 3)
        self.assertEqual(self.meta(qs.SOURCE_META), "db")

    def test_undo_dry_run_writes_nothing(self):
        self.run_import()
        before = self.dump()
        out = self.undo(dry_run=True)
        self.assertTrue(out["dry_run"])
        self.assertEqual(self.dump(), before)

    def test_undo_without_import_refused(self):
        with self.assertRaisesRegex(qs.ImportRefused, "nothing to undo"):
            self.undo()

    def test_refused_after_an_answer_a_decision_or_a_close_every_reason_listed(self):
        pid = self.add_entry()
        r = self.run_import()
        plain, other = r["question_ids"][0], r["question_ids"][1]
        dq = next(t for t in self.questions() if t["decision_id"] == pid)
        c = self.conn()
        try:
            version = store.get_decision_entry(c, pid)["version"]
        finally:
            c.close()
        self.perform("question.answer", {"task_id": plain, "answer": "yes, do it"}, via="dashboard", actor="owner")
        self.perform("question.close", {"task_id": other, "reason": "settled elsewhere"})
        self.perform("question.decide", {"task_id": dq["id"], "choice": "approve", "decision_version": version},
                     via="dashboard", actor="owner")
        before = self.dump()
        with self.assertRaises(qs.ImportRefused) as cm:
            self.undo()
        msg = str(cm.exception)
        for qid in (plain, other, dq["id"]):
            self.assertIn(f"question #{qid}", msg)
        self.assertEqual(self.dump(), before)                  # nothing changed
        self.assertEqual(self.meta(qs.SOURCE_META), "db")


class FileCheck(Env):
    def alert(self, subject, body):
        self.alerts.append((subject, body))

    def check(self):
        return qs.cron_check(self.db, self.tmp, alert=self.alert)

    def test_before_the_import_nothing(self):
        self.assertEqual(self.check(), (0, ""))
        self.write(DASH + "\n- a new item\n")
        self.assertEqual(self.check(), (0, ""))
        self.assertEqual(self.alerts, [])

    def test_unchanged_file_is_quiet(self):
        self.run_import()
        self.assertEqual(self.check(), (0, ""))
        self.assertEqual(self.alerts, [])

    def test_a_changed_file_alerts_once_per_episode(self):
        self.run_import()
        self.write(DASH + "\n- **Q-new: written after the cut-over?**\n")
        code, line = self.check()
        self.assertEqual(code, 0)
        self.assertIn("OPEN_QUESTIONS.md changed after the cut-over", line)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("ask with `python3 tasks.py question ask`", self.alerts[0][1])
        self.assertEqual(self.check(), (0, ""))                # same episode: quiet
        self.write(DASH + "\n- **Q-new: and again, changed more**\n")
        self.assertEqual(self.check(), (0, ""))
        self.assertEqual(len(self.alerts), 1)
        self.write(DASH)                                       # the imported file again: the episode ends
        self.assertEqual(self.check(), (0, ""))
        self.assertFalse(self.meta(qs.CHANGED_MARK))
        self.write(DASH + "\n- x\n")                           # a new episode alerts again
        self.check()
        self.assertEqual(len(self.alerts), 2)

    def test_a_missing_file_is_no_alert(self):
        self.run_import()
        os.remove(self.src)
        self.assertEqual(self.check(), (0, ""))
        self.assertEqual(self.alerts, [])

    def test_a_non_utf8_save_is_a_change_not_a_traceback(self):
        self.run_import()
        with open(self.src, "wb") as fh:
            fh.write(DASH.encode("utf-8") + b"\n- caf\xe9\n")        # latin-1 save
        code, line = self.check()
        self.assertEqual(code, 0)
        self.assertIn("changed after the cut-over", line)
        self.assertEqual(len(self.alerts), 1)

    def test_an_unreadable_file_alerts_once(self):
        self.run_import()
        os.remove(self.src)
        os.mkdir(self.src)                                     # reading it raises IsADirectoryError
        code, line = self.check()
        self.assertEqual(code, 0)
        self.assertIn("changed after the cut-over", line)
        self.assertEqual(self.check(), (0, ""))
        self.assertEqual(len(self.alerts), 1)

    def test_a_failed_alert_sets_no_mark_and_is_retried(self):
        self.run_import()
        self.write(DASH + "\n- x\n")
        calls = []

        def broken(subject, body):
            calls.append(subject)
            raise RuntimeError("push down")
        with self.assertRaises(RuntimeError):
            qs.cron_check(self.db, self.tmp, alert=broken)
        self.assertFalse(self.meta(qs.CHANGED_MARK))
        code, line = self.check()                              # the next run alerts again
        self.assertIn("changed after the cut-over", line)
        self.assertEqual(len(self.alerts), 1)
        self.assertTrue(self.meta(qs.CHANGED_MARK))


if __name__ == "__main__":
    unittest.main()
