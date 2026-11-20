#!/usr/bin/env python3
"""Notices for the owner on the dashboard (D-306): the table and actions (notice.post /
notice.dismiss, audited), the review docs' own notices (doc.set / doc.sync of review:*), the
inbox view, and the CLI. The inbox API's dismiss route and the page: test_inbox.py; the MCP
tool: test_mcp_server.py."""
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402  (hermetic: a temp DB; before the AFClaude imports)
import actions  # noqa: E402
import inbox  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402

REVIEW = """# Usage review 2026-11-01: the prediction model

Analyst run 2026-11-01. Data: the DB only.

## Summary
- H1: predictor_v2 under-predicts by 6 weekly-% (pacing.py:120, commit 986ed7d).

## For the owner: The usage forecast is a bit low
The forecast guessed your weekly use about 6 points too low on average.
A longer look-back of four weeks fixes most of it.

## 1. Baseline
numbers
"""


class Env(unittest.TestCase):
    def setUp(self):
        testenv.fresh_db(self)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        self.p = store.add_project(self.conn, "Proj")

    def do(self, action, /, actor="cli", via="cli", **params):
        return actions.perform(self.conn, action, params, actor=actor, via=via)

    def audit(self, nid):
        return store.audit_log(self.conn, 20, "notice", nid)


class Notices(Env):
    def test_post_list_dismiss(self):
        n = self.do("notice.post", title="Night run done", text="All 4 stages finished.\nNo errors.", project="Proj")
        self.assertEqual((n["title"], n["text"], n["project_name"], n["created_by"], n["dismissed_at"]),
                         ("Night run done", "All 4 stages finished.\nNo errors.", "Proj", "cli", None))
        m = self.do("notice.post", title="Plain", text="not about a project", actor="mcp:x", via="mcp")
        self.assertIsNone(m["project_id"])
        self.assertEqual([x["id"] for x in store.list_notices(self.conn)], [n["id"], m["id"]])
        d = self.do("notice.dismiss", actor="owner", via="dashboard", notice_id=n["id"])
        self.assertIsNotNone(d["dismissed_at"])
        self.assertEqual([x["id"] for x in store.list_notices(self.conn)], [m["id"]])
        self.assertEqual(len(store.list_notices(self.conn, include_dismissed=True)), 2)
        with self.assertRaises(store.InvalidTransition):                      # once
            self.do("notice.dismiss", actor="owner", via="dashboard", notice_id=n["id"])
        with self.assertRaises(store.NotFound):
            self.do("notice.dismiss", notice_id=999)
        rows = self.audit(n["id"])
        self.assertEqual([(r["action"], r["actor"]) for r in rows], [("notice.dismiss", "owner"), ("notice.post", "cli")])
        self.assertEqual(rows[0]["before"], {"dismissed_at": None})

    def test_checks(self):
        for title, text, msg in (("", "x", "title"), ("a\nb", "x", "one line"), ("t" * 201, "x", "one line"),
                                 ("t", "", "text"), ("t", "x" * 4001, "too long"), (None, "x", "title")):
            with self.subTest(title=title), self.assertRaisesRegex(ValueError, msg):
                self.do("notice.post", title=title, text=text)
        with self.assertRaises(store.NotFound):
            self.do("notice.post", title="t", text="x", project="nope")
        self.assertEqual(store.list_notices(self.conn, include_dismissed=True), [])

    def test_unreadable_notices_are_refused(self):
        """D-345/D-347: ids, commands, file names, hashes, branch names, code names and a
        'Nothing to do' line are refused with what to rephrase; plain words pass."""
        bad = ("Answer D-345 first", "see #91", "run tasks.py resume", "fails in store.py:4797", "merged 986ed7d",
               "branch work/AFClaude-wt-notices-n1 is ready", "Merge branch x", "notice_post failed", "finding H1",
               "The run finished.\nNothing to do.\n", "uses git merge main")
        for text in bad:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "owner can't read it"):
                self.do("notice.post", title="Run done", text=text)
        with self.assertRaisesRegex(ValueError, "title: 'P-12'"):
            self.do("notice.post", title="About P-12", text="plain")
        self.assertEqual(store.list_notices(self.conn, include_dismissed=True), [])
        ok = ("The night run finished all four stages. You can open the preview and tell me what to change.",
              "Costs rose 12% (3 of 25 runs). Nothing here needs a hash or path, plan A/B.",
              "Version 2.0.1 of the page is live at 10:30; the end-to-end check passed.")
        for text in ok:
            with self.subTest(text=text):
                self.do("notice.post", title="Plain news", text=text)

    def test_dismiss_is_the_owners(self):
        n = self.do("notice.post", title="t", text="x")
        with self.assertRaises(actions.Forbidden):
            actions.perform(self.conn, "notice.dismiss", {"notice_id": n["id"]}, actor="mcp", via="mcp",
                            autonomous=True)
        self.do("notice.post", title="the task-manager may post", text="x", actor="mcp", via="mcp")

    def test_inbox_view_lists_the_shown_notices(self):
        n = self.do("notice.post", title="Night run done", text="**ok**", project="Proj")
        gone = self.do("notice.post", title="old", text="x")
        self.do("notice.dismiss", actor="owner", via="dashboard", notice_id=gone["id"])
        v = inbox.view(self.conn)
        self.assertEqual([(x["id"], x["title"], x["text"], x["project"], x["source"]) for x in v["notices"]],
                         [(n["id"], "Night run done", "**ok**", "Proj", None)])
        self.assertTrue(v["notices"][0]["posted_berlin"])


class ReviewOwnerPart(unittest.TestCase):
    def test_the_owner_section_only(self):
        title, s = actions.review_owner_part(REVIEW, "x")
        self.assertEqual(title, "The usage forecast is a bit low")
        self.assertEqual(s, "The forecast guessed your weekly use about 6 points too low on average.\n"
                            "A longer look-back of four weeks fixes most of it.")
        self.assertNotIn("H1", s)

    def test_none_without_a_plain_part(self):
        self.assertIsNone(actions.review_owner_part("# T\n\n## Summary\nH1 in store.py\n", "x"))
        self.assertIsNone(actions.review_owner_part("# T\n\n## For the owner\n\n## Next\nz\n", "x"))
        for nothing in ("Nothing for you.", "None.", "Nothing to report, all fine."):
            self.assertIsNone(actions.review_owner_part(f"# T\n\n## For the owner\n{nothing}\n\n## Next\nz\n", "x"), nothing)

    def test_section_ends_at_a_same_level_heading_not_a_deeper_one(self):
        t, s = actions.review_owner_part("# R\n\n### For the owner\nA\n#### Detail\nB\n### Next\nC\n", "x")
        self.assertEqual((t, s), ("R", "A\n#### Detail\nB"))          # no title after the heading: the report's

    def test_title_fallbacks_and_cut(self):
        self.assertEqual(actions.review_owner_part("## For the owner\nA\n", "fb")[0], "fb")
        body = "\n".join(f"- item {i} " + "x" * 80 for i in range(40))
        _, s = actions.review_owner_part("# T\n\n## For the owner\n" + body + "\n", "n")
        self.assertLessEqual(len(s), actions.REVIEW_SUMMARY_MAX)
        self.assertTrue(s.endswith(" \u2026"))
        _, s = actions.review_owner_part("# T\n\n## For the owner\n" + "y" * 5000 + "\n", "n")     # one long line: cut hard
        self.assertEqual(len(s), actions.REVIEW_SUMMARY_MAX)


class ReviewNotices(Env):
    def notices(self):
        return store.list_notices(self.conn, include_dismissed=True)

    def progress(self):
        doc = store.get_doc(self.conn, self.p["id"], "progress")
        return store.doc_render(self.conn, doc["id"]) if doc else ""

    def test_a_report_without_an_owner_part_posts_nothing(self):
        """D-347: the technical summary never reaches the page; the task-manager has the report."""
        self.do("doc.set", project="Proj", name="review:x", text="# R\n\n## Summary\n\nH1 in store.py:12, commit 986ed7d\n")
        self.assertEqual(self.notices(), [])
        self.do("doc.set", project="Proj", name="review:y", text="# R\n\n## For the owner\nNothing for you.\n")
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.progress(), "")

    def test_unreadable_owner_part_becomes_a_fixed_sentence(self):
        """The generated text gets notice_unreadable's check: no refusal (a runner must not crash), the
        original goes to the progress doc, never to the page."""
        self.do("doc.set", project="Proj", name="review:z",
                text="# R (branch k4a @ 986ed7d)\n\n## For the owner\nH1 in store.py:12 needs a fix, see D-300.\n")
        [n] = self.notices()
        self.assertEqual((n["title"], n["text"]), (actions.REVIEW_NOTICE_TITLE, actions.REVIEW_NOTICE_TEXT))
        self.assertEqual(actions.notice_unreadable(n["title"], n["text"]), [])
        prog = self.progress()
        self.assertIn("review:z", prog)
        self.assertIn("store.py:12", prog)
        self.assertIn("986ed7d", prog)
        self.assertEqual(len(store.audit_log(self.conn, 20, "doc", "Proj/progress")), 1)
        before = self.progress()
        self.do("doc.set", project="Proj", name="review:z",
                text="# R (branch k4a @ 986ed7d)\n\n## For the owner\nH1 in store.py:12 needs a fix, see D-300.\n\n## More\nx\n")
        self.assertEqual(self.progress(), before)                       # the same notice again: no new progress line
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual([r["action"] for r in self.audit(n["id"])], ["notice.post"])

    def test_a_readable_title_and_unreadable_text_keep_the_title(self):
        self.do("doc.set", project="Proj", name="review:t", text="# R\n\n## For the owner: All good here\nSee file.py:3.\n")
        [n] = self.notices()
        self.assertEqual((n["title"], n["text"]), ("All good here", actions.REVIEW_NOTICE_TEXT))

    def test_doc_set_posts_then_refreshes(self):
        self.do("doc.set", project="Proj", name="review:2026-11-01", text=REVIEW)
        [n] = self.notices()
        self.assertEqual((n["title"], n["source"], n["project_name"], n["created_by"]),
                         ("The usage forecast is a bit low", "Proj/review:2026-11-01", "Proj", "cli"))
        self.assertIn("four weeks", n["text"])
        self.assertNotIn("H1", n["text"])
        self.assertEqual([r["action"] for r in self.audit(n["id"])], ["notice.post"])
        self.do("doc.set", project="Proj", name="review:2026-11-01", text=REVIEW)    # unchanged: no-op
        self.do("doc.set", project="Proj", name="review:2026-11-01", text=REVIEW.replace("four weeks", "five weeks"))
        [n2] = self.notices()
        self.assertEqual(n2["id"], n["id"])                                     # one notice per report
        self.assertIn("five weeks", n2["text"])
        self.assertIsNotNone(n2["updated_at"])
        rows = self.audit(n["id"])
        self.assertEqual([r["action"] for r in rows], ["notice.update", "notice.post"])
        self.assertEqual(set(rows[0]["after"]), {"text"})                      # (updated_at: audit noise)

    def test_a_dismissed_one_stays_dismissed(self):
        self.do("doc.set", project="Proj", name="review:a", text=REVIEW)
        [n] = self.notices()
        self.do("notice.dismiss", actor="owner", via="dashboard", notice_id=n["id"])
        self.do("doc.set", project="Proj", name="review:a", text=REVIEW + "\nmore\n")
        [n2] = self.notices()
        self.assertIsNotNone(n2["dismissed_at"])
        self.assertEqual(n2["text"], n["text"])

    def test_other_docs_post_nothing(self):
        self.do("doc.set", project="Proj", name="goals", text="# Goals\n")
        self.do("doc.append", project="Proj", name="progress", text="- 10:00 x\n")
        self.do("doc.sync", project="Proj", name="exceptions", text="- e\n")
        self.assertEqual(self.notices(), [])

    def test_doc_sync_of_a_review_file(self):
        """A legacy data/reviews/<name>.md imported (doc.sync) is a review doc written too; the next,
        unchanged import posts nothing more."""
        self.do("doc.sync", project="Proj", name="review:legacy", text=REVIEW, actor="runner:import", via="runner")
        [n] = self.notices()
        self.assertEqual((n["source"], n["created_by"]), ("Proj/review:legacy", "runner:import"))
        self.do("doc.sync", project="Proj", name="review:legacy", text=REVIEW, actor="runner:import", via="runner")
        self.assertEqual(len(self.audit(n["id"])), 1)

    def test_a_failing_notice_rolls_back_the_doc(self):
        """One transaction: the doc write and its notice land together or not at all."""
        orig = store.add_notice

        def boom(*a, **k):
            raise ValueError("no")
        store.add_notice = boom
        try:
            with self.assertRaisesRegex(ValueError, "no"):
                self.do("doc.set", project="Proj", name="review:b", text=REVIEW)
        finally:
            store.add_notice = orig
        self.assertIsNone(store.get_doc(self.conn, self.p["id"], "review:b"))


class CLI(Env):
    def cli(self, *argv, stdin=None):
        out = []
        old, old_in = sys.stdout, sys.stdin
        sys.stdout = types.SimpleNamespace(write=out.append, flush=lambda: None)
        if stdin is not None:
            sys.stdin = types.SimpleNamespace(read=lambda: stdin)
        try:
            rc = tasks.main(["--db", store.DB_PATH] + list(argv))
        finally:
            sys.stdout, sys.stdin = old, old_in
        return rc, "".join(out)

    def test_post_list_dismiss(self):
        rc, out = self.cli("notice", "post", "--title", "Review ready", "--text", "Line 1\nLine 2", "--project", "Proj")
        n = store.list_notices(self.conn)[0]
        self.assertEqual((rc, out.strip()), (0, f"notice #{n['id']}: posted"))
        self.assertEqual((n["title"], n["text"], n["project_name"]), ("Review ready", "Line 1\nLine 2", "Proj"))
        self.cli("notice", "post", "--title", "From stdin", "--text", "-", stdin="piped text\n")
        self.assertEqual(store.list_notices(self.conn)[1]["text"], "piped text")
        rc, out = self.cli("notice", "list")
        self.assertIn("Review ready", out)
        self.assertIn("[Proj]", out)
        self.assertIn("   Line 2", out)
        rc, out = self.cli("notice", "dismiss", str(n["id"]))
        self.assertEqual(out.strip(), f"notice #{n['id']}: dismissed")
        self.assertNotIn("Review ready", self.cli("notice", "list")[1])
        self.assertIn("Review ready", self.cli("notice", "list", "--all")[1])
        self.assertEqual(self.audit(n["id"])[0]["via"], "cli")


if __name__ == "__main__":
    unittest.main()
