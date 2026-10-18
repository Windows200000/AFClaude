#!/usr/bin/env python3
"""Offline tests for nightly_tasks.py (D-254/D-261): a task is added (checked: name, prompts,
fields, report doc), due from its time on, named in a night start's message (only the
task-manager's window and postponed starts, never a fill-up or a last-stretch slot, never a
session elsewhere) with its name in the start's handled entry, done once its report doc was
written after it became due, missed (an event for the caller, a fresh count, still pending) after
max_runs nightly runs without it; cancel and the CLI, incl. the ETA review re-run's command."""
import io
import json
import os
import sys
import time
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import keepalive as ka  # noqa: E402
import nightly_tasks as nt  # noqa: E402
import failstop  # noqa: E402
import runner_state  # noqa: E402
import test_compaction as tc  # noqa: E402  (its Env: a fake tmux session and transcript, for the night starts)
from test_usage_review import clear_nightly_tasks, set_doc, this_project  # noqa: E402

UTC = timezone.utc
# the ETA review re-run (D-261), as the owner schedules it against the live DB after the merge
ETA_CMD = ["add", "eta-review-2026-10-23", "--due", "2026-10-23 22:00",
           "--title", "the stage-ETA review re-run (D-261)", "--report", "review:eta-2026-10-23",
           "--prompt", "stage_eta_review_once", "--prompt", "stage_eta_review_rerun", "--field", "date=2026-10-23"]


clear_tasks = clear_nightly_tasks


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = nt.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        testenv.unpause()
        self.addCleanup(testenv.unpause)
        this_project()
        clear_tasks()
        self.addCleanup(clear_tasks)
        self.now = datetime.now(UTC)
        self.report = f"review:t-{uuid.uuid4().hex[:8]}"   # the docs stay in the shared test DB
        self.saved_ka_state = ka.STATE_FILE
        ka.STATE_FILE = os.path.join(testenv.DIR, f"ka_state_{self.id()}.json")   # the night starts' handled entries
        self.addCleanup(setattr, ka, "STATE_FILE", self.saved_ka_state)

    def add(self, name="t1", due=None, report=None, prompts=("stage_eta_review_once",), **kw):
        return nt.add(name, due or self.now - timedelta(minutes=1), "a test task", list(prompts), report or self.report,
                      kw.pop("fields", {"date": "2026-10-23"}), **kw)

    def track(self, now):
        return nt.track(now, ka.load_state(), ka.RUN_RESULTS)

    def carried(self, name, at, result="continued-in-place"):
        """A night start at `at` that took the task along (keepalive._fire_start's handled entry)."""
        st = ka.load_state()
        st["handled"][f"window-start-{name}-{at.isoformat()}"] = {
            "at": at.isoformat(), "result": result, "plan": "send-keys", "nightly_tasks": [name]}
        ka.save_state(st)


class Tasks(Base):
    def test_add_checks_and_stores_in_the_db(self):
        t = self.add()
        self.assertEqual((t["status"], t["report"], t["max_runs"], t["by"]), ("pending", self.report, nt.MAX_RUNS, "cli"))
        self.assertEqual(nt.get("t1"), t)
        self.assertEqual(runner_state.read_json(nt.STATE)["tasks"]["t1"]["name"], "t1")    # the DB, one entry
        for bad, why in ((lambda: self.add("t1"), "exists already"),
                         (lambda: self.add("Bad Name"), "bad task name"),
                         (lambda: self.add("t2", prompts=("no_such_prompt",)), "no prompt"),
                         (lambda: self.add("t3", fields={}), "needs the field"),
                         (lambda: self.add("t4", report="notes"), "doc name"),
                         (lambda: self.add("t5", max_runs=0), "max_runs"),
                         (lambda: nt.add("t6", datetime(2026, 10, 23, 22), "x", ["nightly_task"], "review:x"),
                          "time zone")):
            with self.assertRaisesRegex(ValueError, why):
                bad()
        self.assertEqual(sorted(n for n, t in nt.load()["tasks"].items() if t["status"] == "pending"), ["t1"])

    def test_due_and_start_insert(self):
        self.add("later", due=self.now + timedelta(days=1), report="review:later")
        self.assertEqual(nt.start_insert(self.now), (None, []))
        self.add("t1")
        text, names = nt.start_insert(self.now)
        self.assertEqual(names, ["t1"])
        self.assertIn(f"Scheduled nightly tasks are due (D-254): t1 (a test task; report {self.report})", text)
        self.assertIn("python3 nightly_tasks.py brief <name>", text)
        self.assertEqual([t["name"] for t in nt.due(self.now + timedelta(days=2))], ["t1", "later"])

    def test_brief_fills_in_the_wrapper_and_the_prompts(self):
        self.add()
        b = nt.brief("t1")
        self.assertTrue(b.startswith("[AFClaude nightly task t1: a test task] You are a worker subagent"))
        self.assertIn(f"Write the report doc {self.report} only once the WHOLE task", b)
        self.assertIn("[AFClaude stage-ETA review (one-off, D-220/D-221) 2026-10-23]", b)
        self.assertIn(f"section to the review doc {self.report} in the AFClaude DB", b)
        self.assertNotIn("{", b.replace("{}", ""))
        with self.assertRaises(ValueError):
            nt.brief("nope")

    def test_done_once_the_report_is_written_after_it_became_due(self):
        set_doc(self.report, "an older doc of the same name\n")
        self.add(due=datetime.now(UTC))
        time.sleep(0.01)                                          # the docs' times are on the millisecond
        self.carried("t1", self.now + timedelta(hours=1))
        later = self.now + timedelta(hours=2)
        self.assertEqual(self.track(later), [])                     # the older doc does not count
        set_doc(self.report, "# done\n")
        [(task, event, why)] = self.track(later)
        self.assertEqual((task["name"], event, task["status"], task["runs"]), ("t1", "done", "done", 1))
        self.assertEqual(nt.get("t1")["status"], "done")
        self.assertEqual(nt.start_insert(later), (None, []))       # no longer carried
        self.assertEqual(self.track(later), [])

    def test_missed_after_max_runs_then_a_fresh_count(self):
        self.add(max_runs=2)
        night = self.now + timedelta(hours=1)
        self.carried("t1", night)
        self.carried("t1", night + timedelta(days=1, minutes=1), result="start-failed")   # started no run
        self.assertEqual(self.track(night + timedelta(days=2)), [])
        last = night + timedelta(days=2)
        self.carried("t1", last)
        self.assertEqual(self.track(last + timedelta(hours=1)), [])  # its run may still be going
        [(task, event, why)] = self.track(last + nt.RUN_SPAN)
        self.assertEqual((event, task["status"]), ("missed", "pending"))
        self.assertIn("2 nightly runs took it along", why)
        t = nt.get("t1")
        self.assertEqual((t["count_from"], len(t["misses"])), ((last + nt.RUN_SPAN).isoformat(), 1))
        self.assertEqual(self.track(last + nt.RUN_SPAN + timedelta(days=3)), [])   # a fresh count
        self.assertEqual(nt.start_insert(self.now + timedelta(days=9))[1], ["t1"])   # still due

    def test_cancel(self):
        self.add()
        self.assertEqual(nt.cancel("t1", "not needed")["status"], "cancelled")
        self.assertEqual(nt.start_insert(self.now), (None, []))
        with self.assertRaises(ValueError):
            nt.cancel("t1", "")

    def test_unreadable_state_is_an_error(self):
        broken = runner_state.StateDoc({"tasks": {}})
        broken.unavailable = True
        old = runner_state.load
        runner_state.load = lambda *a, **k: broken
        try:
            with self.assertRaises(failstop.Failed):
                nt.start_insert(self.now)
        finally:
            runner_state.load = old

    def test_a_failed_save_is_an_error(self):
        old = runner_state.save
        runner_state.save = lambda *a, **k: False
        try:
            with self.assertRaisesRegex(failstop.Failed, "could not be saved"):
                self.add()
        finally:
            runner_state.save = old


class Cli(Base):
    def test_the_eta_review_command(self):
        rc, out, err = cli(*ETA_CMD)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("due from Fri 23.10. 22:00 CEST", out)
        t = nt.get("eta-review-2026-10-23")
        self.assertEqual(t["due_from"], "2026-10-23T20:00:00+00:00")
        self.assertEqual((t["prompts"], t["fields"]), (["stage_eta_review_once", "stage_eta_review_rerun"],
                                                       {"date": "2026-10-23"}))
        due = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
        self.assertEqual(nt.start_insert(due - timedelta(minutes=1)), (None, []))
        self.assertEqual(nt.start_insert(due)[1], ["eta-review-2026-10-23"])
        rc, out, err = cli("brief", "eta-review-2026-10-23")
        self.assertEqual(rc, 0)
        self.assertIn("review doc review:eta-2026-10-23", out)
        self.assertIn("AFClaude-wt-eta-review-once-2026-10-23", out)
        self.assertIn("[AFClaude stage-ETA review re-run, D-261]", out)
        self.assertIn("python3 stage_eta.py --split", out)
        self.assertEqual(cli(*ETA_CMD)[0], 2)                      # once only
        rc, out, _ = cli("list")
        self.assertEqual([t["name"] for t in json.loads(out)], ["eta-review-2026-10-23"])
        self.assertEqual(cli("cancel", "eta-review-2026-10-23", "--why", "test")[0], 0)
        self.assertEqual(json.loads(cli("list")[1]), [])
        self.assertIn("eta-review-2026-10-23", cli("list", "--all")[1])

    def test_bad_input(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                nt.main(["add", "x", "--due", "23.10. 22:00", "--title", "t", "--report", "review:x",
                         "--prompt", "nightly_task"])
        self.assertEqual(cli("brief", "nope")[0], 2)


class InStarts(tc.Env):
    """keepalive._fire_start: a night start's continue names the due tasks (and its handled entry
    lists them); a last-stretch slot, a fill-up or a session outside this checkout never does."""
    def setUp(self):
        super().setUp()
        this_project()
        clear_tasks()
        self.addCleanup(clear_tasks)
        nt.add("t1", datetime.now(UTC) - timedelta(minutes=1), "a test task", ["stage_eta_review_once"],
               "review:t1", {"date": "2026-10-23"})

    def start(self, key, prompt=None):
        at = tc.NIGHT + timedelta(days=1)
        stall = {"uuid": key, "timestamp": at, "budget": "b", "headroom": 14.0, "start_at": at}
        if prompt:
            stall["prompt"] = prompt
        if prompt == "fillup":
            stall["fields"] = {"remaining": "30", "reset": "04:00 CEST"}
        ka.handle_fire(tc.SID, stall, "test start", ka.load_state(), tc.Armed())
        return self.sent[-1][1]

    def test_a_night_start_names_the_due_tasks(self):
        msg = self.start("window-start-n1")
        self.assertTrue(msg.startswith("[keep-alive] A new run starts"))
        self.assertIn("Scheduled nightly tasks are due (D-254): t1 (a test task; report review:t1)", msg)
        rec = ka.load_state()["handled"]["window-start-n1"]
        self.assertEqual((rec["nightly_tasks"], rec["result"]), (["t1"], "continued-in-place"))
        self.assertEqual(nt.carried(nt.get("t1"), ka.load_state(), ka.RUN_RESULTS), [ka.parse_ts(rec["at"])])
        nt.cancel("t1", "test")
        self.assertNotIn("nightly tasks are due", self.start("window-start-n2"))
        self.assertNotIn("nightly_tasks", ka.load_state()["handled"]["window-start-n2"])

    def test_no_line_in_fill_ups_or_last_stretch_slots(self):
        for key, prompt in (("fillup-n1", "fillup"), ("last-mile-n1", "last_mile")):
            self.assertNotIn("nightly tasks are due", self.start(key, prompt))
            self.assertNotIn("nightly_tasks", ka.load_state()["handled"][key])

    def test_no_line_for_a_session_elsewhere(self):
        old = ka.session_cwd
        ka.session_cwd = lambda path: self.tmp.name
        try:
            self.assertNotIn("nightly tasks are due", self.start("window-start-n3"))
        finally:
            ka.session_cwd = old

    def test_an_unreadable_state_aborts_the_start(self):
        old = nt.load
        nt.load = lambda: (_ for _ in ()).throw(failstop.Failed("the nightly tasks could not be read", "test"))
        try:
            key = "window-start-n4"
            at = tc.NIGHT + timedelta(days=1)
            ka.handle_fire(tc.SID, {"uuid": key, "timestamp": at, "budget": "b", "headroom": 14.0, "start_at": at},
                           "test start", ka.load_state(), tc.Armed())
        finally:
            nt.load = old
        rec = ka.load_state()["handled"]["window-start-n4"]
        self.assertEqual(rec["result"], "start-failed")
        self.assertIn("the nightly tasks could not be read", rec["error"])


if __name__ == "__main__":
    unittest.main()
