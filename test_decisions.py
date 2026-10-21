#!/usr/bin/env python3
"""Tests for the decision store in the DB (D-242, decisions.py): the parser of the Markdown source,
the import (decision.import: idempotent, audited, the refusals), the task-manager's keywords
(decision.keywords, kept across re-imports), the lookup (keywords first, then FTS5 or LIKE), the
TLDR and its split into hook parts, the SessionStart hook (which sources, which directory, the
failure text), the 10-min cron import and the CLI (tasks.py decision). Synthetic sources only
(the real DECISIONS.md is local and never read here).

    python3 -m unittest test_decisions
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
import textwrap
from datetime import datetime, timezone
from unittest import mock
import unittest

import testenv  # noqa: F401  (hermetic DB/config; before any AFClaude module)
import actions
import decisions as dec
import store
import tasks

HERE = os.path.dirname(os.path.abspath(__file__))

SOURCE = textwrap.dedent('''\
    # Decision store (synthetic test file)

    ## Purpose and rules

    Some text with **Bold:** that is no entry.

    ## 1. Budget

    ### D-001 Night runs fill the week
    - **Type:** owner
    - **Date:** 2026-09-24 15:59
    - **Summary:** Nights carry the week.
    - **Keywords:** budget, night-window, Pacing
    - **Owner's words:** "fill it at night,
      with some wiggle room"
    - **Source:** [X] #1
    - **Scope:** everything
    - **Status:** active
    - **Supersedes:** none
    - **Interpretation (manager, not binding):** the founding goal.
    - **Addendum (owner, 07.10.):** "and the last days too"

    ---

    ### D-002 Old cap of 50%
    - **Type:** owner
    - **Keywords:** session cap
    - **Owner's words:** "50 for now"
    - **Status:** superseded by D-003

    ### D-003 Session cap 95% at night
    - **Type:** owner · **Date:** 2026-10-17 · **Status:** partially superseded. The day part is gone. · **Supersedes:** D-002
    - **Owner's words:** chose "95"
    - **Summary:** Up to 95% at night.
    - **Keywords:** session cap, night, compaction

    ## Drift log

    ### DR-01 Forgot something (not a decision)
    - **Keywords:** budget

    ## Manager decisions

    ### M-001 The exception is retired
    **Type:** manager decision · **Keywords:** budget, cutoff · **Summary:** Retired by the forecast.
    **Why:** the forecast replaces it.

    ### D-004 A one-off that was carried out
    - **Type:** owner
    - **Keywords:** deploy
    - **Owner's words:** "deploy it now"
    - **Status:** done (2026-10-01)
    ''')


class Env(unittest.TestCase):
    """A fresh DB and a source file in a temp dir."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-decisions-")
        self.db = os.path.join(self.tmp, "afclaude.db")
        self.src = os.path.join(self.tmp, "DECISIONS.md")
        self.write(SOURCE)
        store.connect(self.db, create=True).close()

    def tearDown(self):
        shutil.rmtree(self.tmp, True)

    def write(self, text):
        with open(self.src, "w", encoding="utf-8") as fh:
            fh.write(text)

    def conn(self):
        return store.connect(self.db)

    def imp(self, **kw):
        return dec.import_file(self.src, self.db, actor="cli", via="cli", **kw)

    def audit(self, action):
        c = self.conn()
        try:
            return [r for r in store.audit_log(c, 100, target_type="decision") if r["action"] == action]
        finally:
            c.close()


class Parse(unittest.TestCase):
    def test_entries_and_fields(self):
        es = {e.id: e for e in dec.parse(SOURCE)}
        self.assertEqual(list(es), ["D-001", "D-002", "D-003", "M-001", "D-004"])   # no DR-01, no headings
        d1 = es["D-001"]
        self.assertEqual((d1.title, d1.section, d1.type, d1.status, d1.supersedes),
                         ("Night runs fill the week", "1. Budget", "owner", "active", "none"))
        self.assertEqual(d1.keywords, ["budget", "night-window", "Pacing"])
        self.assertEqual(d1.words, '"fill it at night,\n  with some wiggle room"')   # multi-line, verbatim
        self.assertEqual(d1.interpretation, "the founding goal.")
        self.assertTrue(d1.body.startswith("### D-001 Night runs fill the week\n"))
        self.assertIn("Addendum (owner, 07.10.)", d1.body)                            # in the full text only
        self.assertFalse(d1.body.rstrip().endswith("---"))
        d3 = es["D-003"]                                                              # inline " · " fields
        self.assertEqual((d3.date, d3.supersedes), ("2026-10-17", "D-002"))
        self.assertEqual(d3.status, "partially superseded. The day part is gone.")
        m1 = es["M-001"]
        self.assertEqual((m1.type, m1.summary, m1.keywords, m1.status),
                         ("manager decision", "Retired by the forecast.", ["budget", "cutoff"], None))
        self.assertEqual(m1.section, "Manager decisions")

    def test_states(self):
        for status, state in (("active", "active"), ("active (to build)", "active"), (None, "active"),
                              ("pending clarification", "active"), ("superseded by D-3", "superseded"),
                              ("partially superseded by D-9", "partial"), ("done/expired", "done"),
                              ("Done (2026-10-01)", "done"), ("expired (one-off, D-278)", "done"),
                              ("Expired", "done"),
                              ("active; one-off part expired: \"go now\" (D-278)", "active"),
                              ("active until the next review", "active")):         # no date: plain active
            self.assertEqual(dec.state_of(status), state, status)

    def test_active_until(self):
        # D-278: a pending one-off is current until its date (Berlin time), then counts as done.
        st = "active until 2026-10-22 19:00 (one-off, D-278)"
        before = datetime(2026, 10, 22, 18, 59, tzinfo=dec.BERLIN)
        at = datetime(2026, 10, 22, 19, 0, tzinfo=dec.BERLIN)
        self.assertEqual(dec.state_of(st, now=before), "active")
        self.assertEqual(dec.state_of(st, now=at), "done")
        self.assertEqual(dec.state_of(st, now=datetime(2026, 10, 22, 17, 0, tzinfo=timezone.utc)), "done")  # = 19:00 CEST
        day = "Active until 2026-10-22"                                             # a date alone: to its end
        self.assertEqual(dec.state_of(day, now=datetime(2026, 10, 22, 23, 59, tzinfo=dec.BERLIN)), "active")
        self.assertEqual(dec.state_of(day, now=datetime(2026, 10, 23, 0, 0, tzinfo=dec.BERLIN)), "done")
        self.assertEqual(dec.state_of("active until 2999-01-01"), "active")         # default now: the clock
        self.assertEqual(dec.state_of("active until 2001-01-01"), "done")
        for bad in ("active until 2026-13-01", "active until 2026-10-22 25:00", "active until 22.10.2026"):
            with self.assertRaisesRegex(dec.SourceError, "needs a valid date"):
                dec.state_of(bad)

    def test_title_marker_only_when_no_status_line(self):
        # M-003-like: no Status line, but the title names its own fate (D-242 follow-up).
        self.assertEqual(dec.state_of(None, "Old default (SUPERSEDED by D-141: new default)"), "superseded")
        self.assertEqual(dec.state_of("", "default (superseded by D-9)"), "superseded")   # case-insensitive
        self.assertEqual(dec.state_of(None, "No marker here"), "active")                  # stays active
        self.assertEqual(dec.state_of("active", "Title says SUPERSEDED"), "active")       # a real Status wins

    def test_title_marker_through_parse_and_row(self):
        text = textwrap.dedent('''
            ### M-003 week_target default 90 (SUPERSEDED by D-141: one session left)
            **Type:** manager decision · **Keywords:** budget

            ### M-004 Still no Status line, no marker either
            **Type:** manager decision · **Keywords:** budget
            ''')
        rows = {e.id: e.row() for e in dec.parse(text)}
        self.assertEqual(rows["M-003"]["state"], "superseded")
        self.assertEqual(rows["M-004"]["state"], "active")

    def test_check_entries(self):
        with self.assertRaisesRegex(dec.SourceError, "no '### D-NNN'"):
            dec.check_entries(dec.parse("# nothing\n## a\ntext\n"), "x")
        with self.assertRaisesRegex(dec.SourceError, "D-001 appears twice"):
            dec.check_entries(dec.parse("### D-001 a\n### D-001 b\n"), "x")
        with self.assertRaisesRegex(dec.SourceError, "no title"):
            dec.check_entries(dec.parse("### D-001\n- **Type:** owner\n"), "x")


class Import(Env):
    def test_first_import_then_idempotent(self):
        r = self.imp()
        self.assertEqual((r["entries"], r["added"], r["changed"], r["removed"]),
                         (5, ["D-001", "D-002", "D-003", "M-001", "D-004"], [], []))
        self.assertEqual(r["no_status"], ["M-001"])   # the only SOURCE entry with no Status line
        c = self.conn()
        try:
            d = store.get_decision_entry(c, "d-001")                                # ids case-insensitive
            self.assertEqual((d["state"], d["words"], d["keywords"]),
                             ("active", '"fill it at night,\n  with some wiggle room"',
                              ["budget", "night-window", "Pacing"]))
            self.assertEqual([x["id"] for x in store.list_decision_entries(c, dec.CURRENT)],
                             ["D-001", "D-003", "M-001"])
        finally:
            c.close()
        self.assertEqual(len(self.audit("decision.import")), 1)
        r = self.imp()                                                              # unchanged: a no-op
        self.assertEqual((r["added"], r["changed"], r["removed"], r["unchanged"]), ([], [], [], 5))
        self.assertEqual(len(self.audit("decision.import")), 1)                     # no audit row
        with open(self.src, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), SOURCE)                                     # the file is never written

    def test_restated_when_derivation_logic_changes(self):
        # Simulate a stored row left over from before a derivation-logic change (e.g. the
        # title-marker inference added in state_of): D-001's text is untouched, but its stored
        # state is stale. A reimport of the unchanged file must correct it (D-242 follow-up bug).
        self.imp()
        c = self.conn()
        try:
            with store.transaction(c):
                c.execute("UPDATE decisions SET state = 'superseded' WHERE id = 'D-001'")
            self.assertEqual(store.get_decision_entry(c, "D-001")["state"], "superseded")
        finally:
            c.close()
        r = self.imp()
        self.assertEqual(r["restated"], {"D-001": ["state"]})
        self.assertEqual((r["added"], r["changed"], r["removed"]), ([], [], []))
        self.assertEqual(r["unchanged"], 4)                                        # the other 4 entries
        c = self.conn()
        try:
            self.assertEqual(store.get_decision_entry(c, "D-001")["state"], "active")   # corrected
        finally:
            c.close()
        a = self.audit("decision.import")[0]                                       # newest first
        self.assertEqual(a["after"]["restated"], {"D-001": ["state"]})
        self.assertEqual(len(self.audit("decision.import")), 2)                    # a real audit row
        r = self.imp()                                                             # now truly unchanged
        self.assertEqual(r["restated"], {})
        self.assertEqual(r["unchanged"], 5)
        self.assertEqual(len(self.audit("decision.import")), 2)                    # still no new row

    def test_change_remove_move(self):
        self.imp()
        text = SOURCE.replace('"deploy it now"', '"deploy it today"')               # D-004 changed
        text = text.replace("### D-002 Old cap of 50%\n- **Type:** owner\n- **Keywords:** session cap\n"
                            "- **Owner's words:** \"50 for now\"\n- **Status:** superseded by D-003\n\n", "")
        self.write(text)
        r = self.imp()
        self.assertEqual((r["changed"], r["removed"]), (["D-004"], ["D-002"]))
        self.assertEqual(r["moved"], 2)                                             # D-003, M-001 moved up
        a = self.audit("decision.import")[0]                                        # newest first
        after, before = a["after"], a["before"]
        self.assertIn('+- **Owner\'s words:** "deploy it today"', after["changed"]["D-004"])
        self.assertIn('-- **Owner\'s words:** "deploy it now"', after["changed"]["D-004"])
        self.assertIn('"50 for now"', before["removed_text"]["D-002"])              # the removed text is kept
        c = self.conn()
        try:
            self.assertIsNone(store.get_decision_entry(c, "D-002"))
            self.assertEqual(store.get_decision_entry(c, "D-004")["words"], '"deploy it today"')
        finally:
            c.close()

    def test_refusals(self):
        self.imp()
        self.write("### D-001 Night runs fill the week\n- **Keywords:** budget\n")   # 4 of 5 would go
        with self.assertRaisesRegex(dec.SourceError, "would remove 4 of 5"):
            self.imp()
        c = self.conn()
        try:
            self.assertEqual(len(store.list_decision_entries(c)), 5)                # nothing changed
        finally:
            c.close()
        r = self.imp(force=True)
        self.assertEqual(len(r["removed"]), 4)
        self.write("# empty\n")
        with self.assertRaisesRegex(dec.SourceError, "no '### D-NNN'"):
            self.imp()
        os.remove(self.src)
        with self.assertRaisesRegex(dec.SourceError, "can't read"):
            self.imp()


class Keywords(Env):
    def setUp(self):
        super().setUp()
        self.imp()

    def kw(self, op, *words, **kw):
        c = self.conn()
        try:
            return actions.perform(c, "decision.keywords", dict({"decision": "D-001", "op": op,
                                                                  "keywords": list(words)}, **kw),
                                   actor="cli", via="cli")
        finally:
            c.close()

    def test_add_remove_and_reimport_keeps_them(self):
        r = self.kw("add", "fill-up", "BUDGET")                                     # BUDGET: has it already
        self.assertEqual(r["keywords"], ["budget", "night-window", "Pacing", "fill-up"])
        r = self.kw("remove", "pacing", "fill-up")
        self.assertEqual(r["keywords"], ["budget", "night-window"])
        self.write(SOURCE.replace("budget, night-window, Pacing", "budget, night-window, Pacing, weekly"))
        self.assertEqual(self.imp()["changed"], ["D-001"])
        c = self.conn()
        try:
            d = store.get_decision_entry(c, "D-001")
        finally:
            c.close()
        self.assertEqual(d["effective_keywords"], ["budget", "night-window", "weekly"])   # removal kept
        self.assertEqual(d["keywords_removed"], ["pacing"])
        r = self.kw("add", "Pacing")                                                # back: the removal undone
        self.assertEqual(r["keywords"], ["budget", "night-window", "Pacing", "weekly"])
        audit = self.audit("decision.keywords")
        self.assertEqual(len(audit), 3)
        self.assertEqual(audit[-1]["after"], {"keywords": ["budget", "night-window", "Pacing",
                                                                       "fill-up"]})

    def test_errors_and_noop(self):
        with self.assertRaisesRegex(ValueError, "has no keyword 'nope'"):
            self.kw("remove", "nope")
        with self.assertRaisesRegex(ValueError, "without commas"):
            self.kw("add", "a, b")
        with self.assertRaisesRegex(ValueError, "op must be"):
            self.kw("rename", "x")
        c = self.conn()
        try:
            for did, err in (("D-999", store.NotFound), ("X-1", ValueError)):
                with self.assertRaises(err):
                    actions.perform(c, "decision.keywords", {"decision": did, "op": "add", "keywords": ["x"]},
                                    actor="cli", via="cli")
        finally:
            c.close()
        with self.assertRaises(actions.Conflict):
            self.kw("add", "x", version=7)
        self.kw("add", "budget")                                                    # has it: a no-op
        self.assertEqual(self.audit("decision.keywords"), [])


class Find(Env):
    def setUp(self):
        super().setUp()
        self.imp()

    def find(self, *terms, states=dec.CURRENT):
        c = self.conn()
        try:
            return [(d["id"], d["match"]) for d in store.find_decision_entries(c, list(terms), states)]
        finally:
            c.close()

    def test_keywords_first_then_text(self):
        self.assertEqual(self.find("night window"), [("D-001", "keyword")])         # hyphen = space
        self.assertEqual(self.find("Night-Windows"), [("D-001", "keyword")])        # case, plural
        self.assertEqual(self.find("cap"), [("D-003", "keyword")])                  # a word of "session cap"
        self.assertEqual(self.find("cap", states=None), [("D-002", "keyword"), ("D-003", "keyword")])
        hits = self.find("budget", "night")
        self.assertEqual(hits[0], ("D-001", "keyword"))                            # both terms: first
        self.assertEqual(sorted(hits[1:]), [("D-003", "keyword"), ("M-001", "keyword")])
        self.assertEqual(self.find("wiggle"), [("D-001", "text")])                  # only in the owner's words
        self.assertEqual(self.find("forecast"), [("M-001", "text")])
        self.assertEqual(self.find("nothing-like-this"), [])
        self.assertEqual(self.find("deploy"), [])                                   # D-004 is done
        self.assertEqual(self.find("deploy", states=None), [("D-004", "keyword")])
        with self.assertRaisesRegex(ValueError, "at least one keyword"):
            self.find(" ")

    def test_fts_and_like(self):
        c = self.conn()
        try:
            self.assertTrue(store.has_decisions_fts(c))                             # this build has FTS5
        finally:
            c.close()
        fts = self.find("wiggle", "retired")
        old = store.has_decisions_fts
        store.has_decisions_fts = lambda conn: False                                # a build without FTS5
        try:
            self.assertEqual(sorted(self.find("wiggle", "retired")), sorted(fts))
            self.assertEqual(self.find("night window"), [("D-001", "keyword")])
            self.imp()                                                              # writes skip the index
        finally:
            store.has_decisions_fts = old

    def test_fts_rebuilt_when_out_of_step(self):
        c = self.conn()
        try:
            with store.transaction(c):
                c.execute("DELETE FROM decisions_fts")
        finally:
            c.close()
        raw = sqlite3.connect(self.db)
        try:
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM decisions_fts").fetchone()[0], 0)
        finally:
            raw.close()
        self.assertEqual(self.find("wiggle"), [("D-001", "text")])                  # connect() rebuilt it

    def test_keyword_changes_reach_the_index(self):
        c = self.conn()
        try:
            actions.perform(c, "decision.keywords", {"decision": "D-004", "op": "add", "keywords": ["zebra"]},
                            actor="cli", via="cli")
        finally:
            c.close()
        self.assertEqual(self.find("zebra", states=None), [("D-004", "keyword")])


class Tldr(Env):
    def test_expired_and_lapsed_one_offs_are_not_current(self):
        # D-278: an expired one-off and an "active until" whose date passed drop out of the TLDR;
        # one still before its date stays.
        self.write(SOURCE + textwrap.dedent('''
            ### D-005 Go now
            - **Type:** owner · **Status:** expired (one-off, D-278)

            ### D-006 Use up the week before the reset
            - **Type:** owner · **Status:** active until 2001-01-01 19:00 (one-off, D-278)

            ### D-007 Use up the next week too
            - **Type:** owner · **Status:** active until 2999-01-01 19:00 (one-off, D-278)
            '''))
        self.imp()
        c = self.conn()
        try:
            ids = [ln.split(" ", 1)[0] for ln in dec.tldr_lines(c)]
            self.assertEqual(store.get_decision_entry(c, "D-005")["state"], "done")
            self.assertEqual(store.get_decision_entry(c, "D-006")["state"], "done")
        finally:
            c.close()
        self.assertEqual(ids, ["D-001", "D-003", "M-001", "D-007"])

    def test_lapsed_until_is_restated_on_the_next_import(self):
        # The stored state follows the clock at the next import (cron every 10 min, session start).
        self.write(SOURCE + "\n### D-005 Until the reset\n- **Status:** active until 2026-10-22 19:00\n")
        clock = [datetime(2026, 10, 22, 18, 0, tzinfo=dec.BERLIN)]

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz)
        with mock.patch.object(dec, "datetime", Clock):
            self.imp()
            c = self.conn()
            try:
                self.assertEqual(store.get_decision_entry(c, "D-005")["state"], "active")
            finally:
                c.close()
            clock[0] = datetime(2026, 10, 22, 19, 5, tzinfo=dec.BERLIN)
            r = self.imp()
        self.assertEqual(r["restated"], {"D-005": ["state"]})
        c = self.conn()
        try:
            self.assertEqual(store.get_decision_entry(c, "D-005")["state"], "done")
        finally:
            c.close()

    def test_bad_until_date_fails_the_import(self):
        self.write(SOURCE + "\n### D-005 Typo\n- **Status:** active until 2026-10-32\n")
        with self.assertRaisesRegex(dec.SourceError, "D-005: Status .*needs a valid date"):
            self.imp()

    def test_lines_and_parts(self):
        self.imp()
        c = self.conn()
        try:
            lines = dec.tldr_lines(c)
        finally:
            c.close()
        self.assertEqual(lines, ["D-001 Night runs fill the week",
                                 "D-003 Session cap 95% at night [partly superseded]",
                                 "M-001 The exception is retired"])
        parts = dec.split_parts(lines, 3)
        self.assertEqual(parts, [lines, [], []])
        text = dec.tldr_text(parts[0], 1, 3)
        self.assertIn("part 1 of 3", text)
        self.assertIn("decision show D-NNN", text)

    def test_split_under_the_cap_in_order(self):
        lines = [f"D-{n:03d} " + "x" * 90 for n in range(1, 230)]
        parts = dec.split_parts(lines, 3)
        self.assertEqual(sum(parts, []), lines)                                     # all, in order
        self.assertTrue(all(parts))
        for i, p in enumerate(parts, 1):
            self.assertLess(len(dec.tldr_text(p, i, 3)), dec.HOOK_CONTEXT_CAP)

    def test_overflow_is_named(self):
        lines = [f"D-{n:03d} " + "x" * 90 for n in range(1, 400)]
        parts = dec.split_parts(lines, 2)
        last = parts[-1][-1]
        self.assertRegex(last, r"^TLDR CUT: \d+ more decisions \(D-\d{3} \.\. D-399\)")
        shown = sum(len(p) for p in parts) - 1
        self.assertIn(f"TLDR CUT: {399 - shown} more", last)
        for i, p in enumerate(parts, 1):
            self.assertLess(len(dec.tldr_text(p, i, 2)), dec.HOOK_CONTEXT_CAP)


class Hook(Env):
    def hook(self, part=1, of=3, **inp):
        inp.setdefault("cwd", dec.HERE)
        return dec.hook_output(part, of, inp, self.src, self.db)

    def test_sources_and_directory(self):
        for source in ("compact", "startup", "clear"):
            out = self.hook(source=source)
            self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
            text = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("D-001 Night runs fill the week", text)
            self.assertNotIn("D-002", text)                                         # superseded
        self.assertIsNone(self.hook(source="resume"))                               # D-242: not on resume
        self.assertIsNone(self.hook())                                              # no source: nothing
        self.assertIsNone(self.hook(source="compact", cwd=self.tmp))                # another directory
        self.assertIsNone(self.hook(part=2, source="compact"))                      # an empty part

    def test_imports_first(self):
        self.hook(source="compact")
        self.write(SOURCE.replace("### D-004 A one-off that was carried out", "### D-005 Brand new\n"
                                  "- **Keywords:** new\n\n### D-004 A one-off that was carried out"))
        text = self.hook(source="compact")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("D-005 Brand new", text)
        c = self.conn()
        try:
            self.assertEqual(store.get_decision_entry(c, "D-005")["updated_by"], dec.HOOK_ACTOR)
        finally:
            c.close()

    def test_failure_is_said_in_part_one(self):
        self.write("# no entries\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            text = self.hook(source="compact")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("decisions hook: SourceError", err.getvalue())
        self.assertIn("could NOT be built", text)
        self.assertIn("SourceError", text)
        self.assertIn(self.src, text)
        self.assertIsNone(self.hook(part=2, source="compact"))

    def test_cli(self):
        """The hook command itself: valid JSON in the repo, exit 0 even for unreadable input."""
        env = dict(os.environ, AFCLAUDE_DB=self.db)
        cmd = [sys.executable, os.path.join(HERE, "decisions.py"), "hook", "--part", "1", "--of", "3",
               "--file", self.src]
        r = subprocess.run(cmd, input=json.dumps({"cwd": dec.HERE, "source": "compact"}), capture_output=True,
                           text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("M-001 The exception is retired", json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"])
        r = subprocess.run(cmd, input="not json", capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_settings(self):
        """.claude/settings.json runs the parts on startup/compact/clear, never on resume, each part once."""
        with open(os.path.join(HERE, ".claude", "settings.json")) as fh:
            groups = json.load(fh)["hooks"]["SessionStart"]
        mine = [g for g in groups if any("decisions.py" in h["command"] for h in g["hooks"])]
        self.assertEqual(len(mine), 1)
        self.assertEqual(set(mine[0]["matcher"].split("|")), set(dec.HOOK_SOURCES))
        cmds = [h["command"] for h in mine[0]["hooks"]]
        of = len(cmds)
        self.assertEqual(cmds, [f'python3 "$CLAUDE_PROJECT_DIR/decisions.py" hook --part {i} --of {of}'
                                for i in range(1, of + 1)])


class Cron(Env):
    def setUp(self):
        super().setUp()
        self.alerts = []

    def cron(self):
        return dec.cron_import(self.db, self.tmp, alert=lambda s, b: self.alerts.append(s))

    def test_import_alert_once_and_recover(self):
        code, line = self.cron()
        self.assertEqual(code, 0)
        self.assertIn("decisions import: added D-001, D-002", line)
        self.assertEqual(self.cron(), (0, ""))                                      # unchanged: quiet
        self.write("# broken\n")
        code, line = self.cron()
        self.assertEqual(code, 1)
        self.assertIn("SourceError", line)
        self.assertEqual(self.cron()[0], 1)
        self.assertEqual(self.alerts, ["decisions import failed"])                  # once per episode
        self.write(SOURCE)
        self.assertEqual(self.cron(), (0, ""))
        self.write("# broken again\n")
        self.cron()
        self.assertEqual(len(self.alerts), 2)                                       # a new episode

    def test_no_file_is_nothing_to_do(self):
        os.remove(self.src)
        self.assertEqual(self.cron(), (0, ""))
        self.assertEqual(self.alerts, [])


class Cli(Env):
    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tasks.main(["--db", self.db, "decision", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_commands(self):
        code, out, _ = self.cli("import", "--file", self.src)
        self.assertEqual(code, 0)
        self.assertIn("5 decisions from", out)
        self.assertIn("no Status line (1, counted as active unless the title names a state): M-001", out)
        self.assertIn("unchanged", self.cli("import", "--file", self.src)[1])
        out = self.cli("tldr")[1]
        self.assertIn("D-003 Session cap 95% at night [partly superseded]", out)
        self.assertNotIn("D-004", out)
        self.assertIn("D-004 A one-off that was carried out [done]", self.cli("tldr", "--all")[1])
        out = self.cli("show", "d-001")[1]
        self.assertIn('"fill it at night,', out)
        self.assertIn("Addendum (owner, 07.10.)", out)
        out = self.cli("find", "night window", "forecast")[1]
        self.assertIn("=== D-001 (keywords: night window)", out)
        self.assertIn("=== M-001 (text match)", out)
        self.assertIn("**Why:** the forecast replaces it.", out)
        out = self.cli("find", "budget", "--limit", "1")[1]
        self.assertIn("more matches not shown", out)
        self.assertIn("no current decision matches deploy", self.cli("find", "deploy")[1])
        code, out, _ = self.cli("keywords", "D-001", "add", "fill-up")
        self.assertEqual((code, out.strip()), (0, "D-001 keywords: budget, night-window, Pacing, fill-up"))
        self.assertIn("(keywords in the DB: budget, night-window, Pacing, fill-up)", self.cli("show", "D-001")[1])
        code, _, err = self.cli("keywords", "D-001", "remove", "zzz")
        self.assertEqual(code, 1)
        self.assertIn("has no keyword 'zzz'", err)
        code, _, err = self.cli("show", "D-999")
        self.assertEqual(code, 1)
        self.assertIn("no decision D-999", err)
        r = json.loads(self.cli("find", "cap", "--all", "--json")[1])
        self.assertEqual([h["id"] for h in r["hits"]], ["D-002", "D-003"])


if __name__ == "__main__":
    unittest.main()
