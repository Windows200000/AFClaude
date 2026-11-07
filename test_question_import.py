#!/usr/bin/env python3
"""Tests for the question import (phase 4b Q3 W3, docs/phase4_plan.md; D-102, D-151, D-181, D-235, D-263, D-279):
`questions.py import` (action question.import) on synthetic OPEN_QUESTIONS.md files in both formats: one question
per item with the text verbatim and the counts, the decision questions added once, the preconditions that refuse
before any write, the dry run, the backup, and the refusal through MCP (no undo and no file check since Q5). Synthetic data only.

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
        self.assertEqual(r["counts"], {"items": 2, "proposals": 0, "questions": 2})
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
        self.add_entry("active")                             # the task-manager's own, active (D-322): nobody asks
        self.add_entry("active", confirmed=True)             # confirmed: nobody asks
        self.add_entry("active", author="owner")             # the owner's own: nobody asks
        r = self.run_import()
        self.assertEqual(r["counts"], {"items": 2, "proposals": 2, "questions": 4})
        by_decision = {t["decision_id"]: t for t in self.questions("blocked") if t["decision_id"]}
        self.assertEqual(set(by_decision), {p1, p2})
        self.assertEqual(by_decision[p1]["options"], ["approve", "edit", "reject"])
        self.assertEqual(sorted(r["question_ids"]), sorted(t["id"] for t in self.questions("blocked")))
        # the catch-up is idempotent: nothing more is asked for the same entries
        c = self.conn()
        try:
            with store.transaction(c):
                self.assertEqual(actions.ensure_decision_questions(c, actions.Ctx("cli", "cli")), [])
        finally:
            c.close()
        self.assertEqual(len(self.questions("blocked")), 4)

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
        self.assertEqual(r["counts"], {"items": 2, "proposals": 1, "questions": 3})
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
        self.assertIsNone(self.meta(qs.SOURCE_META))
        self.assertEqual(self.questions(), [])


class Parser(unittest.TestCase):
    """The item parser (moved here from the quickview in Q5): both file formats parse whole, a fenced code
    block starts no item, and D-279's pointer check flags an item that only points to a .md file."""
    FILE = ("# Open questions\n\nsome text before the first item is no question\n\n"
            "## Q-1: First, in the section format\n"
            "Plain text of the first.\n"
            "- option a\n- option b\n\n"
            "## Q-2: Second\nOnly a body line.\n")

    def test_both_formats_parse_whole(self):
        items = qs.parse_open_questions(self.FILE)
        self.assertEqual([s for s, _ in items], ["##", "##"])
        self.assertEqual(items[0][1][0], "Q-1: First, in the section format")
        self.assertIn("- option b", items[0][1])                 # a section's own '- ' lines are its body
        mixed = "- a dash item\n  with a continuation\n## Q-3: then a section\nbody\n"
        parsed = qs.parse_open_questions(mixed)
        self.assertEqual([(s, l[0]) for s, l in parsed], [("-", "a dash item"), ("##", "Q-3: then a section")])
        self.assertEqual(parsed[0][1], ["a dash item", "  with a continuation"])
        self.assertEqual(qs.parse_open_questions("# Title only\n\nno item here\n"), [])

    def test_continuation_and_nested_items_stay_with_one_item(self):
        md = ("# Open questions\n\n"
              "- **3b open questions:** see docs/phase3b_plan.md for details.\n"
              "  1. question one?\n"
              "  2. question two?\n"
              "- A plain single-line question?\n")
        items = qs.parse_open_questions(md)
        self.assertEqual(len(items), 2)
        self.assertIn("  2. question two?", items[0][1])
        self.assertEqual(items[1][1], ["A plain single-line question?"])

    def test_fenced_code_starts_no_item(self):
        md = ("## Q-1: With a code block\n"
              "Run this:\n"
              "```\n## not a heading\n- not an item\n```\n"
              "and then\n"
              "~~~sh\n- nor this\n## nor this\n~~~\n"
              "- inside the section's body\n"
              "- A dash item after fences\n")
        items = qs.parse_open_questions(md)
        self.assertEqual([s for s, _ in items], ["##"])
        self.assertIn("## not a heading", items[0][1])
        self.assertIn("- nor this", items[0][1])
        self.assertIn("- A dash item after fences", items[0][1])         # in a ## section a '- ' line is body
        dash = qs.parse_open_questions("- First\n```\n- fenced\n## fenced\n```\n- Second\n")
        self.assertEqual([(s, l[0]) for s, l in dash], [("-", "First"), ("-", "Second")])
        self.assertEqual(dash[0][1][1:], ["```", "- fenced", "## fenced", "```"])
        unclosed = qs.parse_open_questions("- One\n```\n- never closed\n- Two\n")
        self.assertEqual(len(unclosed), 1)                              # an unclosed fence runs to the end

    def test_pointer_warning(self):
        nested = qs.parse_open_questions("- **3b:** see docs/phase3b_plan.md for details.\n"
                                         "  1. question one?\n  2. question two?\n")
        self.assertIsNone(qs._open_question_warning(nested[0][1]))
        pointer = qs.parse_open_questions("- See docs/phase3b_plan.md for details.\n")
        self.assertEqual(len(pointer), 1)
        self.assertIsNotNone(qs._open_question_warning(pointer[0][1]))
        real = qs.parse_open_questions("- Should the design doc at docs/dashboard_design.md move into phase 4? "
                                       "Recommended: yes, once phase 3b ships.\n")
        self.assertIsNone(qs._open_question_warning(real[0][1]))


if __name__ == "__main__":
    unittest.main()
