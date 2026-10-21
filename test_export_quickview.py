#!/usr/bin/env python3
"""Offline tests for export_quickview.py's phase strip (build_stages / stages)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import export_quickview as qv  # noqa: E402
import store  # noqa: E402


def T(seq, title, status="pending"):
    return {"id": seq, "stage_seq": seq, "title": title, "status": status, "priority": "high"}


class BuildStages(unittest.TestCase):
    def test_grouping_status_and_current(self):
        out = qv.build_stages([
            T(1, "Dispatcher", "done"),
            T(2, "Dashboard 1: config", "done"),
            T(3, "Dashboard 2: runners", "done"),
            T(4, "Dashboard 3: prompts", "in_progress"),
            T(5, "Dashboard 8b: deploy"),
            T(6, "Dashboard 8a: review", "done"),
            T(7, "Dashboard 8: deploy", "cancelled"),     # superseded: dropped
            T(8, "Dashboard 9: retire", "cancelled"),     # only cancelled: phase disappears
            T(9, "Dashboard 10: later", "blocked"),
            T(10, "Dashboard", "cancelled"),              # no number: an "other" stage
        ])
        ph = {p["n"]: p for p in out["phases"]}
        self.assertEqual(list(ph), [1, 2, 3, 8, 10])       # numeric order, 9 gone
        self.assertEqual(ph[1]["status"], "done")
        self.assertEqual(ph[3]["status"], "in_progress")
        self.assertEqual(ph[8]["status"], "in_progress")   # partly done
        self.assertEqual((ph[8]["done"], ph[8]["total"]), (1, 2))
        self.assertEqual([s["key"] for s in ph[8]["steps"]], ["8a", "8b"])
        self.assertEqual(ph[8]["steps"][0]["title"], "review")
        self.assertEqual(ph[10]["status"], "blocked")
        self.assertEqual([p["n"] for p in out["phases"] if p["current"]], [3])
        self.assertEqual([o["title"] for o in out["others"]], ["Dispatcher", "Dashboard"])

    def test_all_done_has_no_current(self):
        out = qv.build_stages([T(1, "Dashboard 1: a", "done")])
        self.assertFalse(any(p["current"] for p in out["phases"]))

    def test_stages_from_db_and_unknown_project(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            old_path, old_proj = store.DB_PATH, qv.STAGES_PROJECT
            try:
                store.DB_PATH = db
                conn = store.connect(db, create=True)
                store.add_project(conn, "P")
                store.add_task(conn, "Dashboard 1: a", project="P")
                store.add_task(conn, "Other", project="P")
                conn.close()
                qv.STAGES_PROJECT = "P"
                out = qv.stages()
                self.assertIsNone(out["error"])
                self.assertEqual([p["n"] for p in out["phases"]], [1])
                self.assertEqual(out["project"], "P")
                qv.STAGES_PROJECT = "nope"
                self.assertTrue(qv.stages()["error"])      # reported, not raised
            finally:
                store.DB_PATH, qv.STAGES_PROJECT = old_path, old_proj


class OpenQuestions(unittest.TestCase):
    """D-279: an open question must reach the UI whole, not cut to its first line, and an item
    that is mainly a pointer to a .md file gets a visible warning instead of passing silently."""

    def test_split_keeps_continuation_and_nested_items_with_one_item(self):
        md = ("# Open questions\n\n"
              "- **3b open questions:** see docs/phase3b_plan.md for details.\n"
              "  1. question one?\n"
              "  2. question two?\n"
              "- A plain single-line question?\n")
        items = qv._split_open_questions(md)
        self.assertEqual(len(items), 2)
        text0 = qv._open_question_text(items[0])
        self.assertIn("question one?", text0)
        self.assertIn("question two?", text0)
        self.assertEqual(qv._open_question_text(items[1]), "A plain single-line question?")

    def test_nested_item_with_real_content_gets_no_warning(self):
        md = ("- **3b:** see docs/phase3b_plan.md for details.\n"
              "  1. question one?\n"
              "  2. question two?\n")
        items = qv._split_open_questions(md)
        self.assertIsNone(qv._open_question_warning(items[0]))

    def test_md_pointer_only_item_gets_a_warning(self):
        md = "- See docs/phase3b_plan.md for details.\n"
        items = qv._split_open_questions(md)
        self.assertEqual(len(items), 1)
        self.assertIsNotNone(qv._open_question_warning(items[0]))

    def test_md_mentioned_alongside_real_question_gets_no_warning(self):
        md = ("- Should the design doc at docs/dashboard_design.md move into phase 4? "
              "Recommended: yes, once phase 3b ships.\n")
        items = qv._split_open_questions(md)
        self.assertIsNone(qv._open_question_warning(items[0]))

    def test_needs_input_reads_full_item_through_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            old_here = qv.HERE
            qv.HERE = d
            try:
                with open(os.path.join(d, "OPEN_QUESTIONS.md"), "w") as fh:
                    fh.write("- **3b open questions:** see docs/phase3b_plan.md for details.\n"
                              "  1. question one?\n  2. question two?\n")
                out = qv.needs_input()
                self.assertEqual(len(out["open_questions"]), 1)
                q = out["open_questions"][0]
                self.assertIn("question one?", q["text"])
                self.assertIn("question two?", q["text"])
                self.assertNotIn("warning", q)
                self.assertEqual(out["count"], 1)
            finally:
                qv.HERE = old_here

    def test_needs_input_flags_a_pointer_only_item(self):
        with tempfile.TemporaryDirectory() as d:
            old_here = qv.HERE
            qv.HERE = d
            try:
                with open(os.path.join(d, "OPEN_QUESTIONS.md"), "w") as fh:
                    fh.write("- See docs/phase3b_plan.md for details.\n")
                out = qv.needs_input()
                self.assertEqual(len(out["open_questions"]), 1)
                self.assertIn("warning", out["open_questions"][0])
            finally:
                qv.HERE = old_here


class DocsFromTheDb(unittest.TestCase):
    """PROGRESS/GOALS/ALERTS/EXCEPTIONS come from the project docs in the DB (design §7.7: the
    runners' notes and the alerts are only there); the file only when the DB has no such doc."""
    def test_db_text_wins_file_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            old_here = qv.HERE
            qv.HERE = d
            try:
                testenv.notes_project(d)
                src = os.path.join(d, "PROGRESS.md")
                with open(src, "w") as fh:
                    fh.write("- from the file\n")
                text, at = qv.doc_source("PROGRESS.md", src)
                self.assertEqual(text, "- from the file\n")                 # no doc yet: the file
                conn = store.connect()
                try:
                    actions.perform(conn, "doc.append", {"project": "notes-" + os.path.basename(d), "name": "alerts",
                                                         "text": "- **x** alert\n"}, actor="runner:notify", via="runner")
                finally:
                    conn.close()
                text, at = qv.doc_source("ALERTS.md", os.path.join(d, "ALERTS.md"))
                self.assertEqual(text, "- **x** alert\n")                    # no file at all: the DB
                self.assertIsNotNone(at)
                self.assertEqual(qv.doc_source("README.md", os.path.join(d, "README.md")), (None, None))
            finally:
                qv.HERE = old_here


class PreviewMarker(unittest.TestCase):
    """D-211: status.json carries preview_from (the step key from which a non-blocking owner
    preview build is possible); the page outlines the chip of the phase containing that step."""

    def test_preview_from_key_matches_a_real_phase_step(self):
        out = qv.build_stages([
            T(1, "Dashboard 5e: migration", "done"),
            T(2, "Dashboard 6a: skeleton + local auth"),
            T(3, "Dashboard 6b: passkeys + oidc"),
            T(4, "Dashboard 6c: read views"),
            T(5, "Dashboard 7a: work writes"),
        ])
        ph = {p["n"]: p for p in out["phases"]}
        keys = [s["key"] for s in ph[6]["steps"]]
        self.assertIn(qv.PREVIEW_FROM, keys)   # the configured marker is an actual 6-step

    def test_env_override(self):
        old = os.environ.get("QUICKVIEW_PREVIEW_FROM")
        try:
            os.environ["QUICKVIEW_PREVIEW_FROM"] = "7a"
            import importlib
            reloaded = importlib.reload(qv)
            self.assertEqual(reloaded.PREVIEW_FROM, "7a")
        finally:
            if old is None:
                os.environ.pop("QUICKVIEW_PREVIEW_FROM", None)
            else:
                os.environ["QUICKVIEW_PREVIEW_FROM"] = old
            importlib.reload(qv)   # restore the module-level default for later tests

    def test_page_has_the_preview_outline_and_marker(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        self.assertIn("s.preview_from", page)
        self.assertIn("chip.preview", page)
        self.assertIn("owner preview possible from here (D-211)", page)
        self.assertIn("#ec4899", page)   # the pink outline color, distinct from the purple "cur" ring


class AutomationPaused(unittest.TestCase):
    """D-234/D-235: the page shows a paused automation with its reason up front, and how to clear it."""
    def test_block(self):
        self.addCleanup(testenv.unpause)
        testenv.unpause()
        self.assertFalse(qv.automation_block()["paused"])
        conn = store.connect()
        try:
            actions.perform(conn, "automation.set", {"paused": True, "reason": "keepalive: start aborted"},
                            actor="runner:keepalive", via="runner")
        finally:
            conn.close()
        b = qv.automation_block()
        self.assertEqual((b["paused"], b["reason"], b["by"], b["resume"]),
                         (True, "keepalive: start aborted", "runner:keepalive", "python3 tasks.py automation resume"))
        self.assertTrue(b["since_berlin"])

    def test_block_names_the_paused_project(self):
        """D-244: a project's pause (an inbox error) is shown by the project's name, with its reason
        and its own resume command; the install-wide part stays unpaused."""
        self.addCleanup(testenv.unpause)
        testenv.unpause()
        self.addCleanup(testenv.managed_project("PQ", "dddddddd-1111-4222-8333-444444444444"))
        conn = store.connect()
        try:
            actions.perform(conn, "automation.set_project", {"project": "PQ", "paused": True,
                                                             "reason": "inbox: triage failed"},
                            actor="runner:inbox", via="runner")
        finally:
            conn.close()
        b = qv.automation_block()
        self.assertEqual((b["paused"], b["install"]), (True, False))
        (p,) = b["projects"]
        self.assertEqual((p["name"], p["reason"], p["by"], p["resume"]),
                         ("PQ", "inbox: triage failed", "runner:inbox", "python3 tasks.py automation resume --project PQ"))
        self.assertTrue(p["since_berlin"])
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        self.assertIn('id="paused-projects"', page)
        self.assertIn("Automation PAUSED for ", page)

    def test_budget_model_error_is_shown_never_paused(self):
        """Task-manager decision on D-234/D-235: the quickview is a display path; a budget-model
        failure there is shown on the page, it never pauses automation."""
        from unittest import mock
        import keepalive as ka
        import pacing
        self.addCleanup(testenv.unpause)
        testenv.unpause()
        testenv.setcfg(usage_model="pacing")
        self.addCleanup(testenv.clear_settings)
        now = datetime.now(qv.UTC)
        cache = {"fetched_at": now, "session": {"percent": 5.0, "resets_at": None},
                 "weekly": {"percent": 20.0, "resets_at": None}}
        alerts = []
        with mock.patch.object(ka, "read_usage_cache", return_value=cache), \
                mock.patch.object(pacing, "decide", side_effect=RuntimeError("broken model")), \
                mock.patch.object(ka, "alert", lambda s, b="": alerts.append(s)):
            out = qv.keepalive_and_usage(now)
        self.assertIn("budget model failed (RuntimeError: broken model)", out["budget_error"])
        self.assertEqual(alerts, [])
        self.assertFalse(qv.automation_block()["paused"])
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        self.assertIn('id="budget-error"', page)
        self.assertIn("budget_error", page)

    def test_page_renders_the_banner_first(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        for part in ('id="paused"', "Automation is PAUSED", "s.automation", "renderPaused(s); renderUsage(s)",
                     'id="paused-reason"', 'id="paused-cmd"'):
            self.assertIn(part, page)
        self.assertLess(page.index('id="paused"'), page.index('id="s-input"'))


class WindowInfo(unittest.TestCase):
    """The 23:00-09:00 window spans midnight: inside it, the next window is the following
    evening's; outside, this evening's."""

    def Z(self, s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    def test_window_info(self):
        out = qv.window_info(self.Z("2026-09-29T22:30:00Z"))        # Wed 00:30 CEST: inside
        self.assertEqual(out["window_berlin"], "23:00–09:00")
        self.assertTrue(out["in_window"])
        self.assertEqual(out["window_end_berlin"], "Wed 30.09. 09:00 CEST")
        self.assertEqual(out["next_window_berlin"], "Wed 30.09. 23:00 CEST")
        out = qv.window_info(self.Z("2026-09-29T21:30:00Z"))        # Tue 23:30 CEST: inside
        self.assertEqual(out["window_end_berlin"], "Wed 30.09. 09:00 CEST")
        self.assertEqual(out["next_window_berlin"], "Wed 30.09. 23:00 CEST")
        out = qv.window_info(self.Z("2026-09-30T10:00:00Z"))        # Wed 12:00 CEST: outside
        self.assertFalse(out["in_window"])
        self.assertIsNone(out["window_end_berlin"])
        self.assertEqual(out["next_window_berlin"], "Wed 30.09. 23:00 CEST")
        out = qv.window_info(self.Z("2026-10-25T12:00:00Z"))        # first CET evening
        self.assertEqual(out["next_window_berlin"], "Sun 25.10. 23:00 CET")
        out = qv.window_info(self.Z("2026-10-24T23:00:00Z"))        # the DST-end night: 10 h absolute (D-148)
        self.assertEqual(out["window_berlin"], "23:00–08:00")
        self.assertEqual(out["window_end_berlin"], "Sun 25.10. 08:00 CET")



class LimitsBlock(unittest.TestCase):
    OLD = {"generated_at": "x", "ratio": {"status": "ok", "median": 0.13333, "trimmed_mean": 0.1, "n": 55},
           "windows_per_week": 7.5, "windows_left_this_week": 6.2,
           "attribution": {"week_share": {"status": "ok", "own_share": 1.0, "other_share": 0.0}}}

    def test_old_snapshot_still_renders(self):
        b = qv.limits_from_snapshot(self.OLD)
        self.assertEqual((b["ratio_status"], b["ratio_median"], b["ratio_n"]), ("ok", 0.1333, 55))
        self.assertIsNone(b["ratio_pref"])
        self.assertIsNone(qv.limits_from_snapshot(None))

    def test_per_window_fields(self):
        snap = dict(self.OLD,
                    ratio_windows={"status": "ok", "n": 11, "n_total": 17, "weighted": 0.15368, "weighted_stdev": 0.01834,
                                   "p25": 0.1125, "p75": 0.16273, "se": 0.0045, "vs_15min": {"median_15min": 0.1381}},
                    preferred_ratio={"value": 0.15368, "source": "windows", "flagged": False,
                                     "windows_per_week": 6.507, "windows_left_this_week": 5.21})
        b = qv.limits_from_snapshot(snap)
        self.assertEqual((b["ratio_pref"], b["ratio_spread"], b["ratio_windows_n"]), (0.1537, 0.0183, 11))
        self.assertEqual(b["ratio_iqr"], [0.1125, 0.1627])
        self.assertEqual((b["ratio_pref_source"], b["ratio_15min_median"]), ("windows", 0.1381))
        self.assertEqual((b["windows_per_week_pref"], b["windows_left_pref"]), (6.5, 5.2))
        self.assertEqual(b["ratio_median"], 0.1333)   # old field unchanged

    def test_share_method(self):
        b = qv.limits_from_snapshot(self.OLD)                # older snapshot: the token split by session
        self.assertEqual((b["share_method"], b["own_share_week"]), ("tokens", 1.0))
        self.assertIsNone(b["own_pct_week"])
        snap = dict(self.OLD, attribution={
            "week_share": {"status": "ok", "method": "time", "own_share": 0.12, "other_share": 0.88,
                           "own_pct": 9.0, "user_pct": 66.04, "user_unseen_pct": 12.0,
                           "runs_in_cycle": 1, "run_hours": 2.5},
            "week_share_tokens": {"status": "ok", "own_share": 0.94376, "other_share": 0.05624}})
        b = qv.limits_from_snapshot(snap)
        self.assertEqual((b["share_method"], b["own_share_week"], b["user_share_week"]), ("time", 0.12, 0.88))
        self.assertEqual((b["own_pct_week"], b["user_pct_week"], b["user_unseen_pct_week"]), (9.0, 66.0, 12.0))
        self.assertEqual((b["runs_week"], b["run_hours_week"]), (1, 2.5))
        self.assertEqual(b["afc_session_token_share_week"], 0.944)


class ThresholdBlock(unittest.TestCase):
    """The keep-alive + usage section carries pacing.threshold_info() (the reserve threshold,
    the ratio's variance and the model's inaccuracy); a failure is shown, not raised."""

    def test_threshold_block(self):
        import pacing
        old = pacing.threshold_info
        seen = {}
        try:
            pacing.threshold_info = lambda now=None, decision=None: seen.update(d=decision) or {"threshold": {"active": 84.6}}
            self.assertEqual(qv.threshold_block(datetime(2026, 10, 4), {"mode": "night"})["threshold"]["active"], 84.6)
            self.assertEqual(seen["d"], {"mode": "night"})
            qv.threshold_block(datetime(2026, 10, 4), {"mode": "linear"})       # not a pacing decision
            self.assertIsNone(seen["d"])

            def boom(**kw):
                raise RuntimeError("x")
            pacing.threshold_info = boom
            self.assertIn("RuntimeError", qv.threshold_block(datetime(2026, 10, 4))["error"])
        finally:
            pacing.threshold_info = old

    def test_page_renders_the_tile(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        for needle in ("k.threshold", "Reserve threshold", "model error", "dynamic default"):
            self.assertIn(needle, page)



class NextRunBlock(unittest.TestCase):
    """D-166: the keep-alive + usage section shows when the next run takes place
    (pacing.next_run), not the budget rule; a failure is shown, not raised."""

    def test_block(self):
        import pacing
        from datetime import timezone
        old = pacing.next_run
        at = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        try:
            seen = {}
            pacing.next_run = lambda u, now, decision=None, **kw: seen.update(kw) or {
                "at": at, "kind": "last_stretch", "label": "last stretch", "reason": "predicted 98.6% > 84.6%",
                "predicted_end": 98.6123, "threshold": 84.58, "last_stretch_at": at,
                "expected": {"at": at, "kind": "last_stretch", "label": "last stretch", "reason": "r",
                             "predicted_end": 101.04, "last_stretch_at": at}}
            b = qv.next_run_block({}, datetime(2026, 10, 4, tzinfo=timezone.utc))
            self.assertEqual((b["kind"], b["label"], b["predicted_end"], b["threshold"]),
                             ("last_stretch", "last stretch", 98.6, 84.6))
            self.assertTrue(b["at_berlin"].startswith("Thu 08.10. 14:00"))
            self.assertEqual((b["expected"]["kind"], b["expected"]["predicted_end"]), ("last_stretch", 101.0))
            self.assertTrue(b["expected"]["at_berlin"].startswith("Thu 08.10. 14:00"))
            self.assertEqual(set(seen), {"active", "deferred", "slot_next"})   # D-202/D-204: the runner's state

            def boom(*a, **kw):
                raise RuntimeError("x")
            pacing.next_run = boom
            self.assertIn("RuntimeError", qv.next_run_block({}, datetime(2026, 10, 4))["error"])
        finally:
            pacing.next_run = old

    def test_runner_state(self):
        from datetime import timezone
        now = datetime(2026, 10, 5, 6, 18, tzinfo=timezone.utc)
        olds = (qv.ka.run_active, qv.ka.pending_deferral, qv.ka.last_mile_next_slot)
        try:
            qv.ka.run_active, qv.ka.pending_deferral = (lambda sid, n: None), (lambda sid, n: None)
            qv.ka.last_mile_next_slot = lambda n: None
            self.assertEqual(qv.runner_state(now), {"active": None, "deferred": False, "slot_next": None})
            t = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)
            qv.ka.run_active, qv.ka.pending_deferral = (lambda sid, n: t), (lambda sid, n: t)
            qv.ka.last_mile_next_slot = lambda n: t
            self.assertEqual(qv.runner_state(now), {"active": t, "deferred": t, "slot_next": t})

            def boom(*a):
                raise OSError("x")
            qv.ka.run_active, qv.ka.pending_deferral, qv.ka.last_mile_next_slot = boom, boom, boom
            self.assertEqual(qv.runner_state(now), {"active": None, "deferred": None, "slot_next": None})   # unknown
        finally:
            qv.ka.run_active, qv.ka.pending_deferral, qv.ka.last_mile_next_slot = olds

    def test_no_running_between_session_window_starts(self):
        """D-202: Mon 08:18 Berlin with a passing gate and nothing running -> 'Holding until' the
        next session-window start (Mon 23:00), not 'Running now'; at 04:05 (a due start) -> now."""
        import pacing
        import schedule
        from datetime import timedelta, timezone
        Z = timezone.utc
        reset = datetime(2026, 10, 8, 17, 0, tzinfo=Z)
        win = schedule.Config.every_day()
        tmp = tempfile.TemporaryDirectory()
        olds = (pacing.SAMPLES_FILE, pacing.FIRE_FILES, qv.runner_state)
        pacing.SAMPLES_FILE = os.path.join(tmp.name, "samples.jsonl")
        pacing.FIRE_FILES = []
        qv.runner_state = lambda now: {"active": None, "deferred": False}
        try:
            u = {"weekly": {"percent": 10.0, "resets_at": reset}, "session": {"percent": 0.0, "resets_at": None}}
            for now, kind, at in ((datetime(2026, 10, 5, 6, 18, tzinfo=Z), "night", "Mon 05.10. 23:00"),
                                  (datetime(2026, 10, 5, 2, 5, tzinfo=Z), "now", None)):
                d = pacing.decide_core(10.0, reset, now, 600.0, None, None, None, True, 0.2, "auto", win)
                self.assertTrue(d["go"], d["reason"])                      # the gate passes at that moment
                b = qv.next_run_block(u, now, d)
                self.assertEqual(b["kind"], kind, b)
                if at:
                    self.assertTrue(b["at_berlin"].startswith(at), b)
            qv.runner_state = lambda now: {"active": now - timedelta(hours=1), "deferred": False}
            now = datetime(2026, 10, 5, 3, 0, tzinfo=Z)
            d = pacing.decide_core(10.0, reset, now, 600.0, None, None, None, True, 0.2, "auto", win)
            self.assertEqual(qv.next_run_block(u, now, d)["kind"], "now")   # a run is going
        finally:
            pacing.SAMPLES_FILE, pacing.FIRE_FILES, qv.runner_state = olds
            tmp.cleanup()

    def test_page_shows_the_next_run_not_the_rule(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        self.assertIn("k.next_run", page)
        self.assertIn("Holding until", page)   # D-200, D-201
        self.assertIn("With your forecast usage", page)
        self.assertIn("nr.expected", page)
        self.assertNotIn("budget_rule_now", page)
        self.assertNotIn("Budget rule", page)


class FillupBlock(unittest.TestCase):
    """D-212: the page shows the next planned fill-up (Berlin time, remaining %, weekly cost) or
    "none: <reason>", from the watcher's plan in keepalive_fillup.json."""

    def setUp(self):
        import json
        from datetime import timezone
        self.json, self.UTC = json, timezone.utc
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (qv.ka.FILLUP_FILE, qv.SELF_SESSION)
        qv.ka.FILLUP_FILE = os.path.join(self.tmp.name, "fillup.json")
        qv.SELF_SESSION = "s" * 36
        self.now = datetime(2026, 10, 9, 23, 0, tzinfo=self.UTC)

    def tearDown(self):
        qv.ka.FILLUP_FILE, qv.SELF_SESSION = self.old
        self.tmp.cleanup()

    def write(self, d):
        with open(qv.ka.FILLUP_FILE, "w") as fh:
            self.json.dump(d, fh)

    def plan(self, **kw):
        p = {"key": "fillup-2026-10-10T02:00:00+00:00", "reset": "2026-10-10T02:00:00.341664+00:00",
             "remaining": 5.0, "rate": 66.3, "factor": 1.1, "start": "2026-10-10T01:55:01.400000+00:00",
             "cost": 0.7704, "status": "wait", "reason": "planned", "recheck_at": "2026-10-10T01:55:01.400000+00:00"}
        p.update(kw)
        return p

    def test_planned(self):
        self.write({"plan": self.plan()})
        b = qv.fillup_block(self.now)
        self.assertTrue(b["planned"])
        self.assertEqual(b["at_berlin"], "Sat 10.10. 03:55 CEST")
        self.assertEqual((b["remaining"], b["cost"], b["test"]), (5.0, 0.8, False))
        self.assertEqual(b["reset_berlin"], "Sat 10.10. 04:00 CEST")
        self.json.dumps(b)                                         # status.json-safe

    def test_none_with_reason(self):
        self.assertEqual(qv.fillup_block(self.now)["planned"], False)            # no file yet
        self.write({"plan": self.plan(status="skip", reason="the session-window start at 04:00 continues "
                                                            "this window anyway")})
        b = qv.fillup_block(self.now)
        self.assertEqual(b, {"planned": False, "reason": "the session-window start at 04:00 continues this "
                                                         "window anyway"})
        self.write({"plan": {"status": "none", "reason": "no live session window"}})
        self.assertEqual(qv.fillup_block(self.now)["reason"], "no live session window")
        self.write({"plan": self.plan()})                                        # a stale plan
        self.assertIn("ended", qv.fillup_block(datetime(2026, 10, 10, 3, 0, tzinfo=self.UTC))["reason"])

    def test_disabled(self):
        self.write({"plan": self.plan()})
        testenv.set_setting("fillup_enabled", False)
        try:
            self.assertEqual(qv.fillup_block(self.now), {"planned": False, "reason": "fillup_enabled is off"})
        finally:
            testenv.set_setting("fillup_enabled", None)

    def test_test_entry_of_this_session(self):
        t = {"session": "s" * 36, "key": "fillup-test-x", "reset": "2026-10-10T02:00:00+00:00",
             "start": "2026-10-10T01:57:01+00:00", "remaining": 3.0, "cost": 0.46}
        self.write({"plan": self.plan(status="none", reason="no AFClaude run in this session window"), "test": t})
        b = qv.fillup_block(self.now)
        self.assertTrue(b["planned"] and b["test"])
        self.assertEqual(b["at_berlin"], "Sat 10.10. 03:57 CEST")
        self.write({"test": dict(t, session="o" * 36)})                         # another session's: ignored
        self.assertFalse(qv.fillup_block(self.now)["planned"])

    def test_broken_file(self):
        with open(qv.ka.FILLUP_FILE, "w") as fh:
            fh.write("{not json")
        self.assertFalse(qv.fillup_block(self.now)["planned"])
        self.write({"plan": self.plan(start="garbage")})
        self.assertFalse(qv.fillup_block(self.now)["planned"])

    def test_page_renders_the_tile(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        for needle in ("fillupTile(k.fillup", "Next fill-up", "weekly cost", "f.reason"):
            self.assertIn(needle, page)


class NextUsageReview(unittest.TestCase):
    """Owner bug report: the dashboard's "next usage review" tile showed only the monthly
    schedule (usage_review_state.json's next_run_at), ignoring a one-off review (owner's
    `usage_review.py --only ... --at ...`) spooled as a pending at-shim job that lands earlier."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (qv.USAGE_REVIEW_STATE_FILE, qv.AT_SPOOL_DIR)
        qv.USAGE_REVIEW_STATE_FILE = os.path.join(self.tmp.name, "usage_review_state.json")
        qv.AT_SPOOL_DIR = os.path.join(self.tmp.name, "at_spool")

    def tearDown(self):
        qv.USAGE_REVIEW_STATE_FILE, qv.AT_SPOOL_DIR = self.old
        self.tmp.cleanup()
        # scheduled_jobs rows are marked started, never deleted (store.py's no-delete trigger);
        # mark every pending row started so the next test starts with none pending.
        conn = store.connect()
        try:
            for row in store.list_jobs(conn, "pending"):
                actions.job_started(conn, row["id"], actor="runner:at", via="runner")
        finally:
            conn.close()

    def write_state(self, next_run_at):
        with open(qv.USAGE_REVIEW_STATE_FILE, "w") as fh:
            json.dump({"next_run_at": next_run_at, "runs": []}, fh)

    def put_job(self, job_id, at, which):
        cmd = f"cd x && python3 usage_review.py --only {which} --now >> keepalive.log 2>&1\n"
        conn = store.connect()
        try:
            actions.job_put(conn, {"id": job_id, "at": at, "cmd": cmd, "cwd": "/"},
                            actor="runner:at", via="runner")
        finally:
            conn.close()

    def spool_job(self, job_id, at, which):
        os.makedirs(qv.AT_SPOOL_DIR, exist_ok=True)
        cmd = f"cd x && python3 usage_review.py --only {which} --now >> keepalive.log 2>&1\n"
        with open(os.path.join(qv.AT_SPOOL_DIR, f"{job_id}.json"), "w") as fh:
            json.dump({"id": job_id, "at": at, "cmd": cmd, "cwd": "/"}, fh)

    def test_monthly_only(self):
        self.write_state("2026-10-29T08:00:00Z")
        out = qv.next_review_block()
        self.assertEqual(out["which"], "monthly")
        self.assertTrue(out["at_berlin"].startswith("Thu 29.10."))
        self.assertIsNone(out["label"])
        self.assertIsNone(out["secondary"])

    def test_one_off_before_monthly_wins_and_shows_monthly_as_secondary(self):
        """The owner's example: a one-off usage+ETA review on 15.10., monthly review on 29.10."""
        self.write_state("2026-10-29T08:00:00Z")
        self.put_job("44dc54fd2635", "2026-10-15T14:00:00Z", "usage,stage-eta")
        out = qv.next_review_block()
        self.assertEqual(out["which"], "one-off")
        self.assertTrue(out["at_berlin"].startswith("Thu 15.10."))
        self.assertEqual(out["label"], "usage + ETA")
        self.assertEqual(out["secondary"]["which"], "monthly")
        self.assertTrue(out["secondary"]["at_berlin"].startswith("Thu 29.10."))

    def test_one_off_after_monthly_shows_monthly_as_primary(self):
        self.write_state("2026-10-15T08:00:00Z")
        self.put_job("abc123", "2026-10-29T14:00:00Z", "stage-eta")
        out = qv.next_review_block()
        self.assertEqual(out["which"], "monthly")
        self.assertEqual(out["secondary"], {"at_berlin": out["secondary"]["at_berlin"], "which": "one-off",
                                            "label": "ETA"})
        self.assertTrue(out["secondary"]["at_berlin"].startswith("Thu 29.10."))

    def test_solo_one_off_label(self):
        self.put_job("x1", "2026-10-15T14:00:00Z", "usage")
        out = qv.one_off_reviews()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["label"], "usage")
        self.assertEqual(out[0]["which"], "usage")

    def test_earliest_of_several_one_offs(self):
        self.put_job("later", "2026-10-20T10:00:00Z", "usage")
        self.put_job("earlier", "2026-10-16T10:00:00Z", "stage-eta")
        out = qv.one_off_reviews()
        self.assertEqual([o["label"] for o in out], ["ETA", "usage"])

    def test_non_review_jobs_ignored(self):
        conn = store.connect()
        try:
            actions.job_put(conn, {"id": "weekly-reset", "at": "2026-10-14T00:00:00Z",
                                   "cmd": "cd x && python3 usage_sampler.py --now\n", "cwd": "/"},
                            actor="runner:at", via="runner")
        finally:
            conn.close()
        self.assertEqual(qv.one_off_reviews(), [])

    def test_legacy_spool_job_the_db_doesnt_know_is_shown(self):
        """A legacy spool file (frozen, D-161) whose job the DB doesn't know: the at-shim adopts
        it as pending on its next run, so it is shown already."""
        self.spool_job("spooled1", "2026-10-15T14:00:00Z", "usage,stage-eta")
        out = qv.one_off_reviews()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["label"], "usage + ETA")

    def test_legacy_spool_job_known_to_the_db_follows_the_db(self):
        """The spool dir is never written or cleaned any more: a job the DB knows (here: already
        started) is the DB's, its frozen spool file never shows it again."""
        self.put_job("db-job", "2026-10-16T10:00:00Z", "usage")
        self.put_job("ran-job", "2026-10-14T10:00:00Z", "stage-eta")
        conn = store.connect()
        try:
            actions.job_started(conn, "ran-job", actor="runner:at", via="runner")
        finally:
            conn.close()
        self.spool_job("ran-job", "2026-10-14T10:00:00Z", "stage-eta")
        self.spool_job("db-job", "2026-10-13T10:00:00Z", "stage-eta")       # the DB's row wins
        out = qv.one_off_reviews()
        self.assertEqual([(o["which"], o["at"].day) for o in out], [("usage", 16)])

    def test_a_pending_nightly_task_is_shown(self):
        """D-254/D-261: a nightly task (here the ETA review's re-run) shows with its due time."""
        import nightly_tasks
        conn = store.connect()
        try:
            with store.transaction(conn):
                conn.execute("DELETE FROM runner_state WHERE component=?", (nightly_tasks.COMPONENT,))
        finally:
            conn.close()
        self.write_state("2026-10-29T08:00:00Z")
        nightly_tasks.add("eta-rerun", datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc), "the ETA review re-run",
                          ["stage_eta_review_once"], "review:eta-rerun", {"date": "2026-10-23"})
        nightly_tasks.add("gone", datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc), "cancelled one",
                          ["stage_eta_review_once"], "review:gone", {"date": "2026-10-20"})
        nightly_tasks.cancel("gone", "test")
        out = qv.next_review_block()
        self.assertEqual((out["which"], out["label"]), ("nightly", "the ETA review re-run (nightly run)"))
        self.assertTrue(out["at_berlin"].startswith("Fri 23.10."))
        self.assertEqual(out["secondary"]["which"], "monthly")
        nightly_tasks.cancel("eta-rerun", "test")
        self.assertEqual(qv.next_review_block()["which"], "monthly")

    def test_nothing_scheduled(self):
        self.assertEqual(qv.next_review_block(),
                         {"at_berlin": None, "which": None, "label": None, "secondary": None})

    def test_page_renders_the_one_off_tagging(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        for needle in ("next_usage_review_label", "next_usage_review_secondary", "Next usage review"):
            self.assertIn(needle, page)


PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quickview", "AFClaude.html")


class UsageTiles(unittest.TestCase):
    """The Session / Week tiles: the fields the page reads are the ones the export writes (the
    browser check is tools/quickview_render_check.py)."""
    def usage(self, cache):
        from unittest import mock
        import keepalive as ka
        now = datetime.now(qv.UTC)
        cache = dict(cache, fetched_at=now)
        with mock.patch.object(ka, "read_usage_cache", return_value=cache):
            return qv.keepalive_and_usage(now)

    def test_export_and_page_agree(self):
        out = self.usage({"session": {"percent": 40.0, "resets_at": datetime(2026, 10, 17, 3, 39, tzinfo=qv.UTC)},
                          "weekly": {"percent": 7.0, "resets_at": datetime(2026, 10, 22, 17, 0, tzinfo=qv.UTC)}})
        self.assertEqual(out["session"], {"percent": 40.0, "resets_at_berlin": "Sat 17.10. 05:39 CEST"})
        self.assertEqual(out["weekly"]["percent"], 7.0)
        self.assertNotIn("usage_error", out)
        self.assertEqual(out["age_minutes"], 0.0)
        self.assertTrue(out["fetched_at_berlin"])
        with open(PAGE_FILE, encoding="utf-8") as fh:
            page = fh.read()
        for read in ('pctTile("Session", k.session', 'pctTile("Week", k.weekly', "u.percent", "u.resets_at_berlin",
                     "k.usage_error", "k.fetched_at_berlin", "k.age_minutes"):
            self.assertIn(read, page)

    def test_no_window_open(self):
        """After a reset with nothing used, /usage has no reset time: 0%, shown as "no window open"."""
        out = self.usage({"session": {"percent": 0.0, "resets_at": None},
                          "weekly": {"percent": 0.0, "resets_at": None}})
        self.assertEqual(out["session"], {"percent": 0.0, "resets_at_berlin": None})
        with open(PAGE_FILE, encoding="utf-8") as fh:
            self.assertIn("no window open", fh.read())

    def test_no_cache(self):
        from unittest import mock
        import keepalive as ka
        with mock.patch.object(ka, "read_usage_cache", return_value=None):
            out = qv.keepalive_and_usage(datetime.now(qv.UTC))
        self.assertNotIn("session", out)
        self.assertIn("usage_error", out)


class PublishPage(unittest.TestCase):
    """D-253: every export publishes the page next to status.json; nginx serves it as HTML."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = (qv.OUT, qv.PAGE)
        self.addCleanup(lambda: setattr(qv, "OUT", self.old[0]) or setattr(qv, "PAGE", self.old[1]))
        qv.OUT = self.tmp.name

    def test_publish_copies_the_page_atomically(self):
        qv.PAGE = PAGE_FILE
        with open(os.path.join(self.tmp.name, "AFClaude.html"), "w") as fh:
            fh.write("old page")
        qv.publish_page()
        out = os.path.join(self.tmp.name, "AFClaude.html")
        with open(PAGE_FILE, encoding="utf-8") as a, open(out, encoding="utf-8") as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o644)
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.startswith(".tmp-")], [])

    def test_unreadable_or_wrong_page_raises_and_keeps_the_old_one(self):
        out = os.path.join(self.tmp.name, "AFClaude.html")
        with open(out, "w") as fh:
            fh.write("old page")
        qv.PAGE = os.path.join(self.tmp.name, "missing.html")
        with self.assertRaises(OSError):
            qv.publish_page()
        qv.PAGE = os.path.join(self.tmp.name, "empty.html")
        open(qv.PAGE, "w").close()
        with self.assertRaises(ValueError):
            qv.publish_page()
        with open(out) as fh:
            self.assertEqual(fh.read(), "old page")

    def test_main_publishes_the_page_after_the_data(self):
        from unittest import mock
        qv.PAGE = PAGE_FILE
        written = []
        real = qv.write_atomic
        blocks = {n: mock.DEFAULT for n in ("automation_block", "keepalive_and_usage", "context_block", "parse_goals",
                                             "stages", "latest_decision", "needs_input", "inbox_block", "cron_entries")}
        with mock.patch.multiple(qv, all_docs=lambda: [], doc_source=lambda n, s: ("", None), **blocks), \
                mock.patch.object(qv.ka, "read_usage_cache", return_value=None), \
                mock.patch.object(qv.glob, "glob", return_value=[]), \
                mock.patch.object(qv, "write_atomic", lambda n, d: written.append(n) or real(n, d)):
            for n in blocks:
                getattr(qv, n).return_value = {}
            qv.main()
        self.assertEqual(written, ["status.json", "AFClaude.html"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "AFClaude.html")))

    def test_nginx_serves_html_as_html(self):
        with open(os.path.join(os.path.dirname(PAGE_FILE), "nginx.conf"), encoding="utf-8") as fh:
            conf = fh.read()
        loc = conf[conf.index("location /afclaude/ {"):conf.index("location /afclaude/api/")]
        self.assertRegex(loc, r"types \{[^}]*text/html html;")
        self.assertRegex(loc, r"charset_types [^;]*text/html")


if __name__ == "__main__":
    unittest.main()
