#!/usr/bin/env python3
"""Tests for the decision seeding (phase 4b Q2, docs/phase4_plan.md; D-104, D-158, D-235, D-242, D-284, D-285,
D-286): `decisions.py seed` (action decision.seed) on a synthetic DECISIONS.md: the classes and their counts
against the file's, the preconditions that refuse before any write, the dry run, the backup, `--undo` and its
refusals, and what follows the seeding (the import retired, the changed-file alert, the hook from the DB only,
edit / expire / delete on seeded rows). Synthetic data only.

    python3 -m unittest test_decisions_seed
"""
import contextlib
import glob
import io
import json
import os
import unittest
from unittest import mock

import testenv  # noqa: F401  (hermetic DB/config; before any AFClaude module)
import actions
import decisions as dec
import store
import tasks
from test_decisions import SOURCE, Env

EXTRA = '''
### D-005 The guard
- **Type:** owner
- **Date:** 2026-10-02
- **Owner's words:** "keep the guard"
- **Status:** done (the guard stays active)

### D-006 A one-off that expired
- **Type:** owner
- **Owner's words:** "go now"
- **Status:** expired (one-off, D-278)
'''
TOPICS = {"D-001": ["budget"], "D-003": ["budget", "cap"], "M-001": ["all"]}


class Seed(Env):
    """A database imported from the synthetic file; helpers for the seeding files."""
    def setUp(self):
        super().setUp()
        self.write(SOURCE + EXTRA)
        self.imp()
        self.alerts = []

    def files(self, topics=None, merges=None):
        t, m = os.path.join(self.tmp, "topics.json"), os.path.join(self.tmp, "merges.json")
        with open(t, "w") as fh:
            json.dump(topics if topics is not None else dict(TOPICS, **{"D-005": ["guard"]}), fh)
        with open(m, "w") as fh:
            json.dump(merges if merges is not None else {"keep": ["D-005"]}, fh)
        return t, m

    def seed(self, topics=None, merges=None, **kw):
        t, m = self.files(topics, merges)
        return dec.seed(t, m, db_path=self.db, path=self.src, **kw)

    def undo(self, **kw):
        return dec.seed(None, None, db_path=self.db, path=self.src, undo=True, **kw)

    def backups(self):
        return glob.glob(self.db + ".preseed-*.bak")

    def meta(self, key):
        c = self.conn()
        try:
            return store.get_meta(c, key)
        finally:
            c.close()

    def row(self, did):
        c = self.conn()
        try:
            return store.get_decision_entry(c, did)
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

    def refused(self, *a, **kw):
        with self.assertRaises(dec.SeedRefused) as cm:
            self.seed(*a, **kw)
        return str(cm.exception)

    MERGE = {"title": "Session cap, reworded", "summary": "Up to 95% at night, as the owner said.",
             "topics": ["cap"], "supersedes": ["D-003"]}


class Classes(Seed):
    def test_dry_run_writes_nothing(self):
        before = self.dump()
        r = self.seed(dry_run=True)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.backups(), [])
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["counts"]["become_db_rows"], 4)               # D-001, D-003, M-001, D-005 (kept)
        self.assertEqual(r["delete"], {"D-002": "superseded", "D-004": "done", "D-006": "expired"})

    def test_counts_add_up_to_the_file(self):
        r = self.seed()
        c = r["counts"]
        entries = len(dec.parse(SOURCE + EXTRA))
        self.assertEqual(c["file"]["entries"], entries)
        self.assertEqual(c["classes"], {"active": 2, "partial": 1, "done": 2, "expired": 1, "superseded": 1})
        self.assertEqual(c["become_db_rows"] + c["deleted"], entries)
        self.assertTrue(c["balanced"])
        self.assertEqual((c["file"]["drift_entries"], c["audit_only"]), (1, 3))   # DR-01, preamble, Purpose and rules
        self.assertEqual(c["file"]["headings_only"], 3)                  # 1. Budget, Drift log, Manager decisions

    def test_survivors_unchanged_deleted_in_the_audit_log(self):
        before = {i: self.row(i) for i in ("D-001", "D-003", "M-001", "D-005")}
        self.seed()
        c = self.conn()
        try:
            self.assertEqual({r["id"]: r["origin"] for r in store.list_decision_entries(c)},
                             {"D-001": "db", "D-003": "db", "M-001": "db", "D-005": "db"})
            for i, old in before.items():
                new = store.get_decision_entry(c, i)
                for k in ("body", "state", "status", "words", "date", "title", "author", "section", "ord"):
                    self.assertEqual(new[k], old[k], (i, k))
            self.assertEqual(store.get_decision_entry(c, "D-003")["state"], "partial")      # stays partial
            self.assertEqual(store.get_decision_entry(c, "D-003")["topics"], ["budget", "cap"])
            log = store.audit_log(c, 500, target_type="decision")
        finally:
            c.close()
        gone = {r["target_id"]: r for r in log if r["action"] == "decision.delete"}
        self.assertEqual(sorted(gone), ["D-002", "D-004", "D-006"])
        self.assertIn("Old cap of 50%", gone["D-002"]["before"]["body"])                    # the full text
        self.assertIn("seeding: superseded", gone["D-002"]["after"]["reason"])
        self.assertIn("seeding: expired", gone["D-006"]["after"]["reason"])
        arch = {r["target_id"]: r for r in log if r["action"] == "decision.archive"}
        self.assertIn("DR-01", arch)
        self.assertIn("Forgot something", arch["DR-01"]["before"]["text"])
        self.assertIn("Some text with **Bold:** that is no entry.", arch["section: Purpose and rules"]["before"]["text"])
        self.assertIn("(before the first heading)", " ".join(arch))
        self.assertEqual(len(arch), 3)

    def test_meta_backup_and_audit(self):
        r = self.seed()
        self.assertEqual(self.meta(dec.SOURCE_META), "db")
        self.assertEqual(self.meta(dec.SEED_SHA_META), dec.read_source(self.src)[1])
        self.assertEqual(int(self.meta("decision_id_floor")), 6)         # the file's highest D-number
        seeded = json.loads(self.meta(dec.SEEDED_META))
        self.assertEqual(sorted(seeded["rows"]), ["D-001", "D-003", "D-005", "M-001"])
        self.assertEqual(seeded["deleted"], {"D-002": "superseded", "D-004": "done", "D-006": "expired"})
        bak = self.backups()
        self.assertEqual(len(bak), 1)
        self.assertEqual(r["backup"], bak[0])
        old = store.connect(bak[0])                                      # the backup is the pre-seed database
        try:
            self.assertEqual({d["origin"] for d in store.list_decision_entries(old)}, {"file"})
            self.assertEqual(len(store.list_decision_entries(old)), 7)
        finally:
            old.close()
        seed_rows = [a for a in self.audit("decision.seed")]
        self.assertEqual(len(seed_rows), 1)
        self.assertEqual(seed_rows[0]["after"]["source"], "db")

    def test_second_run_refused(self):
        self.seed()
        before = self.dump()
        with self.assertRaises(dec.SeedRefused) as cm:
            self.seed()
        self.assertIn("seeded already", str(cm.exception))
        self.assertEqual(self.dump(), before)

    def test_merge_becomes_a_proposal(self):
        r = self.seed(merges={"merges": [self.MERGE], "keep": ["D-005"]})
        (pid,) = r["proposal_ids"]
        self.assertEqual(pid, "D-007")                                   # above the file's highest id
        p = self.row(pid)
        self.assertEqual((p["state"], p["author"], p["origin"], p["supersedes"]), ("proposed", "task-manager", "db",
                                                                                   "D-003"))
        self.assertEqual(p["topics"], ["cap"])
        self.assertIn('D-003 (2026-10-17): chose "95"', p["words"])      # the original's words, verbatim, dated
        self.assertIn("Up to 95% at night", p["body"])
        self.assertEqual(self.row("D-003")["state"], "partial")          # the original stays in force
        c = self.conn()
        try:
            self.assertNotIn(pid, [d["id"] for d in store.decision_titles(c, dec.CURRENT)])
            self.assertEqual(r["counts"]["proposals"], 1)
            self.assertEqual(r["counts"]["of_which_merge_originals"], 1)
        finally:
            c.close()
        # confirming it deletes the original in one go (Q1/W1 behaviour on top of the seeded rows)
        out = self.perform("decision.confirm", {"decision": pid})
        self.assertEqual(out["replaced"], ["D-003"])
        self.assertIsNone(self.row("D-003"))


class Refusals(Seed):
    def test_missing_topics(self):
        before = self.dump()
        msg = self.refused(topics={"D-001": ["budget"], "D-005": ["guard"]})
        self.assertIn("D-003 has no topics", msg)
        self.assertIn("M-001 has no topics", msg)
        self.assertEqual(self.dump(), before)                            # nothing written
        self.assertEqual(self.backups(), [])                             # not even a backup

    def test_topic_count_and_stray_topics(self):
        msg = self.refused(topics=dict(TOPICS, **{"D-005": ["guard"], "D-001": ["a", "b", "c", "d"],
                                                  "D-002": ["x"]}))
        self.assertIn("D-001: 4 topics, needs 1-3", msg)
        self.assertIn("topics for D-002, which does not survive", msg)
        self.assertIn("topics must be", self.refused(topics=dict(TOPICS, **{"D-005": ["guard"], "D-001": "budget"})))

    def test_done_with_an_active_part_must_be_covered(self):
        msg = self.refused(merges={"keep": []}, topics=dict(TOPICS))
        self.assertIn("D-005 is done but its Status names a part that stays", msg)
        self.assertNotIn("D-004", msg)                                   # plain done: deleted
        self.assertNotIn("D-006", msg)                                   # an expired one-off: deleted
        # a merge covers it as well
        m = {"title": "Guard", "summary": "The guard stays.", "topics": ["guard"], "supersedes": ["D-005"]}
        r = self.seed(merges={"merges": [m]}, topics=dict(TOPICS, **{"D-005": ["guard"]}))
        self.assertEqual(r["counts"]["of_which_merge_originals"], 1)

    def test_merge_and_keep_shapes(self):
        top = dict(TOPICS, **{"D-005": ["guard"]})
        for merges, want in (
                ({"merges": [dict(self.MERGE, supersedes=["D-002"])], "keep": ["D-005"]}, "D-002 is superseded already"),
                ({"merges": [dict(self.MERGE, supersedes=["D-099"])], "keep": ["D-005"]}, "D-099 is not in the file"),
                ({"merges": [self.MERGE, dict(self.MERGE, title="again")], "keep": ["D-005"]}, "D-003 is in another merge"),
                ({"merges": [dict(self.MERGE, topics=[])], "keep": ["D-005"]}, "0 topics, needs 1-3"),
                ({"merges": [{"title": "x"}], "keep": ["D-005"]}, "an object with title, summary"),
                ({"keep": ["D-005", "D-001"]}, "keep D-001: it is active"),
                ({"keep": ["D-005", "D-099"]}, "keep D-099: not in the file"),
                ({"merges": [dict(self.MERGE, supersedes=["D-005"])], "keep": ["D-005"]}, "keep D-005: it is in a merge"),
        ):
            with self.subTest(want):
                self.assertIn(want, self.refused(merges=merges, topics=top))
        with self.assertRaises(dec.SeedRefused):
            self.seed(merges={"merges": [], "stray": 1})

    def test_file_not_imported_refuses(self):
        self.write((SOURCE + EXTRA).replace("### D-004", "### D-008 Brand new\n- **Keywords:** new\n\n### D-004"))
        self.assertIn("run `python3 tasks.py decision import` first", self.refused())
        self.write((SOURCE + EXTRA).replace("Nights carry the week.", "Nights carry the week, changed."))
        self.assertIn("differing: D-001", self.refused())
        self.assertEqual(self.backups(), [])

    def test_a_missing_file_refuses(self):
        os.remove(self.src)
        self.assertIn("can't read", self.refused())

    def test_only_the_cli(self):
        t, m = self.files()
        params = {"path": self.src, "topics": TOPICS, "merges": [], "keep": [], "dry_run": True}
        for via in ("mcp", "dashboard", "runner"):
            with self.subTest(via):
                with self.assertRaises(actions.Forbidden):
                    self.perform("decision.seed", params, via=via, actor="mcp:abc" if via == "mcp" else "user:1")
        with self.assertRaises(actions.Forbidden):
            self.perform("decision.seed", {"undo": True, "dry_run": True}, via="mcp", actor="mcp:abc")
        self.assertEqual(self.meta(dec.SOURCE_META), None)


class Undo(Seed):
    def test_undo_restores(self):
        r = self.seed(merges={"merges": [self.MERGE], "keep": ["D-005"]})
        pid = r["proposal_ids"][0]
        out = self.undo()
        self.assertEqual((out["restored"], out["deleted_proposals"]), (4, [pid]))
        self.assertIsNone(self.row(pid))
        self.assertEqual(self.meta(dec.SOURCE_META), "file")
        self.assertEqual({d["origin"] for d in self.all_rows()}, {"file"})
        self.assertEqual([a["after"]["reason"] for a in self.audit("decision.delete") if a["target_id"] == pid],
                         ["seeding undone"])
        res = self.imp()                                                 # the next import re-adds what was deleted
        self.assertEqual(sorted(res["added"]), ["D-002", "D-004", "D-006"])
        self.assertEqual(len(self.all_rows()), 7)
        self.assertFalse(res.get("retired"))
        self.seed()                                                      # and it can run again
        self.assertEqual(self.meta(dec.SOURCE_META), "db")

    def test_undo_closes_the_questions_of_what_it_deletes_or_resets(self):
        """F5 (phase 4b Q3): a question about a seeded proposal (or entry) is closed with the seeding."""
        r = self.seed(merges={"merges": [self.MERGE], "keep": ["D-005"]})
        pid = r["proposal_ids"][0]
        c = self.conn()
        try:
            with store.transaction(c):
                q = actions.ask_decision_question(c, actions.Ctx("cli", "cli"), store.get_decision_entry(c, pid))
        finally:
            c.close()
        self.undo()
        c = self.conn()
        try:
            t = store.get_task(c, q["id"])
            self.assertEqual(t["status"], "cancelled")
            self.assertIn("seeding undone", store.task_events(c, q["id"])[-1]["detail"]["reason"])
            self.assertIsNone(store.open_decision_question(c, pid))
        finally:
            c.close()

    def all_rows(self):
        c = self.conn()
        try:
            return store.list_decision_entries(c)
        finally:
            c.close()

    def test_native_rows_written_since_stay(self):
        self.seed()
        out = self.perform("decision.add", {"title": "Written after", "topics": ["x"], "author": "task-manager"})
        self.assertEqual(out["id"], "D-007")
        self.undo()
        self.assertEqual(self.row("D-007")["origin"], "db")
        self.imp()
        self.assertEqual(self.row("D-007")["state"], "proposed")

    def test_refused_after_an_edit(self):
        self.seed()
        self.perform("decision.edit", {"decision": "D-001", "title": "Night runs fill the week, edited"})
        before = self.dump()
        with self.assertRaises(dec.SeedRefused) as cm:
            self.undo()
        self.assertIn("D-001 changed since the seeding", str(cm.exception))
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.meta(dec.SOURCE_META), "db")

    def test_refused_after_a_topic_change(self):
        self.seed()
        self.perform("decision.topics", {"decision": "D-001", "op": "add", "topics": ["extra"]})
        with self.assertRaises(dec.SeedRefused):
            self.undo()

    def test_refused_after_a_confirm(self):
        r = self.seed(merges={"merges": [self.MERGE], "keep": ["D-005"]})
        self.perform("decision.confirm", {"decision": r["proposal_ids"][0]})
        with self.assertRaises(dec.SeedRefused) as cm:
            self.undo()
        msg = str(cm.exception)
        self.assertIn("D-003 is gone", msg)
        self.assertIn(f"{r['proposal_ids'][0]} changed since the seeding", msg)

    def test_refused_when_not_seeded_and_dry_run(self):
        with self.assertRaises(dec.SeedRefused) as cm:
            self.undo()
        self.assertIn("not seeded", str(cm.exception))
        self.seed()
        before = self.dump()
        r = self.undo(dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(self.dump(), before)


class After(Seed):
    def setUp(self):
        super().setUp()
        self.seed()

    def cron(self):
        return dec.cron_import(self.db, self.tmp, alert=lambda s, b: self.alerts.append((s, b)))

    def test_import_is_a_no_op(self):
        ids, audited = [d["id"] for d in self.all_rows()], len(self.audit("decision.import"))
        self.write(SOURCE + EXTRA + "\n### D-099 Added to the file\n- **Keywords:** x\n")
        with mock.patch.object(dec, "_alert") as alert:                  # (the real alert goes to the owner)
            r = self.imp()
        self.assertTrue(r["retired"])
        self.assertTrue(r["differs"])
        self.assertEqual(alert.call_count, 1)
        self.assertEqual([d["id"] for d in self.all_rows()], ids)        # nothing added, nothing re-added
        self.assertIsNone(self.row("D-099"))
        self.assertIsNone(self.row("D-002"))
        self.assertEqual(self.perform("decision.import", {"path": self.src})["retired"], True)
        self.assertEqual(len(self.audit("decision.import")), audited)    # no audit row either

    def all_rows(self):
        c = self.conn()
        try:
            return store.list_decision_entries(c)
        finally:
            c.close()

    def test_changed_file_alerts_once_per_episode(self):
        self.assertEqual(self.cron(), (0, ""))                           # the seeded file: quiet
        self.assertEqual(self.alerts, [])
        self.write(SOURCE + EXTRA + "\n### D-099 Added to the file\n- **Keywords:** x\n")
        self.assertEqual(self.cron(), (0, ""))
        self.cron()
        self.assertEqual([a[0] for a in self.alerts], ["DECISIONS.md changed after the cut-over"])
        self.assertIn("tasks.py decision add", self.alerts[0][1])
        self.write(SOURCE + EXTRA + "\n### D-098 Again\n- **Keywords:** x\n")           # still the same episode
        self.cron()
        self.assertEqual(len(self.alerts), 1)
        self.write(SOURCE + EXTRA)                                       # the seeded file again: the episode ends
        self.cron()
        self.write("# changed again\n")
        self.cron()
        self.assertEqual(len(self.alerts), 2)                            # a new episode
        self.assertIsNone(self.row("D-099"))

    def test_hook_reads_the_db_only(self):
        self.write("# no entries at all\n")                              # an unparseable file does not matter
        out = dec.hook_output(1, 3, {"source": "compact", "cwd": dec.HERE}, self.src, self.db)
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("D-001 Night runs fill the week", text)
        self.assertIn("D-003 Session cap 95% at night [partly superseded]", text)
        self.assertNotIn("D-002", text)
        self.assertNotIn("NOTE", text)
        os.remove(self.src)
        self.assertIn("D-001", dec.hook_output(1, 3, {"source": "compact", "cwd": dec.HERE}, self.src,
                                               self.db)["hookSpecificOutput"]["additionalContext"])

    def test_hook_failure_names_find_not_the_file(self):
        with mock.patch.object(dec, "tldr_lines", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(io.StringIO()):
            text = dec.hook_output(1, 3, {"source": "compact", "cwd": dec.HERE}, self.src,
                                   self.db)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("could NOT be built", text)
        self.assertIn("python3 tasks.py decision find", text)
        self.assertNotIn(self.src, text)

    def test_edit_expire_delete_work_on_seeded_rows(self):
        self.perform("decision.edit", {"decision": "D-001", "title": "Renamed"})
        self.assertEqual(self.row("D-001")["title"], "Renamed")
        self.perform("decision.expire", {"decision": "M-001"})
        self.perform("decision.delete", {"decision": "D-003", "reason": "no longer applies"})
        self.assertEqual((self.row("M-001"), self.row("D-003")), (None, None))
        self.perform("decision.topics", {"decision": "D-001", "op": "add", "topics": ["more"]})   # and topics
        self.assertEqual(self.row("D-001")["topics"], ["budget", "more"])

    def test_new_ids_come_from_the_floor(self):
        out = self.perform("decision.add", {"title": "Next", "topics": ["x"], "author": "task-manager"})
        self.assertEqual(out["id"], "D-007")


class Cli(Seed):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dec.main(["--db", self.db, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_seed_dry_run_then_real_then_undo(self):
        t, m = self.files()
        code, out, _ = self.run_cli("seed", "--topics", t, "--merges", m, "--file", self.src, "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["counts"]["balanced"])
        self.assertEqual(self.meta(dec.SOURCE_META), None)
        code, out, _ = self.run_cli("seed", "--topics", t, "--merges", m, "--file", self.src)
        self.assertEqual(code, 0)
        self.assertEqual(self.meta(dec.SOURCE_META), "db")
        code, _, err = self.run_cli("seed", "--topics", t, "--merges", m, "--file", self.src)
        self.assertEqual(code, 1)
        self.assertIn("seeded already", err)
        code, out, _ = self.run_cli("seed", "--undo")
        self.assertEqual(code, 0)
        self.assertEqual(self.meta(dec.SOURCE_META), "file")

    def test_refusal_is_an_error_exit(self):
        code, out, err = self.run_cli("seed")
        self.assertEqual(code, 1)
        self.assertIn("--topics", err)

    def test_tasks_import_says_it_is_retired(self):
        self.seed()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tasks.main(["--db", self.db, "decision", "import", "--file", self.src])
        self.assertIn("nothing imported", out.getvalue())
        self.assertIn("The file is the seeded one", out.getvalue())


class Blocks(unittest.TestCase):
    def test_split_blocks(self):
        bs = dec.split_blocks(SOURCE)
        self.assertEqual([(b.kind, b.id, b.empty) for b in bs],
                         [("section", dec.PREAMBLE, False), ("section", "Purpose and rules", False),
                          ("section", "1. Budget", True), ("section", "Drift log", True), ("drift", "DR-01", False),
                          ("section", "Manager decisions", True)])
        self.assertTrue(bs[4].text.startswith("### DR-01 Forgot something"))


if __name__ == "__main__":
    unittest.main()
