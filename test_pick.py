#!/usr/bin/env python3
"""Offline tests for phase 3b S4, project selection at the start (docs/phase3b_plan.md §2, §5 S4):
store.pick_project's order (chained first, D-281; ready stages by priority then rank, D-064; the
task-managers without a ready stage after them; paused and session-less idle projects skipped),
keepalive.pick_start inside handle_fire (an owner-held project passed over while a later one
exists, at most PICK_MAX_PASS_OVER, D-239.1; one at a limit is not, D-204; none: HOLD), one run_log
row per start (not per owner-busy retry), a chained start and a last-stretch slot keeping the
running project, a project's first start creating its task-manager (D-281) and taking it back on
any failure up to the launch, the dedup of one start key, the per-project prompt (inline rules
without manager_afclaude.md, its length), and the single-task-manager case behaving as before S4
(no extra preflight or limit check)."""
import subprocess
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic DB / data dir)
import actions  # noqa: E402
import keepalive as ka  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
SID_A = "aaaaaaaa-0000-4000-8000-000000000001"
SID_B = "bbbbbbbb-0000-4000-8000-000000000002"
NOW = datetime(2026, 10, 29, 22, 0, tzinfo=UTC)
HELD = f"{ka.RC_HELD} thread (pid [7])"
RUNNING = ka.running_session   # the real one (Base patches it)
TMUX_LINE_MAX = 15000     # under tmux's 16 KiB command message (send-keys -l types the whole line)


class Args:
    arm = True
    now = False


class DryArgs(Args):
    arm = False


class Base(unittest.TestCase):
    """A temp DB: alpha (rank 1) and beta (rank 2) with task-manager sessions, gamma (rank 3) without."""

    def setUp(self):
        testenv.fresh_db(self)
        self.addCleanup(testenv.unpause)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        self.dir = tempfile.mkdtemp(prefix="afclaude-pick-")
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        for name, sid in (("alpha", SID_A), ("beta", SID_B), ("gamma", None)):
            path = os.path.join(self.dir, name)
            os.makedirs(path)
            store.add_project(self.conn, name, f"the {name} project", path)
            if sid:
                store.update_project(self.conn, name, manager_session=sid)
        self.conn.commit()
        self.logs, self.notes = [], []
        for n, v in (("STATE_FILE", os.path.join(self.dir, "state.json")),       # side files, never the checkout's
                     ("DEFER_FILE", os.path.join(self.dir, "deferred.json")),
                     ("FILLUP_FILE", os.path.join(self.dir, "fillup.json")), ("log", self.logs.append), ("progress_note", self.notes.append),
                     ("db_paused", lambda what: False), ("running_session", lambda now, st=None: None),
                     ("fail_stop", mock.Mock())):
            p = mock.patch.object(ka, n, v)
            p.start()
            self.addCleanup(p.stop)
        self.preflights, self.limited, self.held, self.stall_calls = [], set(), set(), []
        p = mock.patch.object(ka, "preflight", self.fake_preflight)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(ka, "stall_status", self.fake_stall_status)
        p.start()
        self.addCleanup(p.stop)
        self.fired = []
        p = mock.patch.object(ka, "_fire_start", lambda sid, stall, *a: self.fired.append((sid, dict(stall))))
        p.start()
        self.addCleanup(p.stop)

    def fake_preflight(self, sid):
        self.preflights.append(sid)
        return (False, [HELD], None) if sid in self.held else (True, [], "send-keys")

    def fake_stall_status(self, sid, now):
        self.stall_calls.append(sid)
        return ("WAIT_RESET", "session limit resets 23:00", {}) if sid in self.limited else ("NO_STALL", "", None)

    def task(self, project, priority="high", status="pending"):
        t = store.add_task(self.conn, "t", project=project, priority=priority)
        if status != "pending":
            self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, t["id"]))
        self.conn.commit()

    def pause(self, name):
        actions.perform(self.conn, "automation.set_project", {"project": name, "paused": True, "reason": "test"},
                        actor="cli", via="cli")

    def names(self, chained=None):
        return [c["name"] for c in store.pick_project(self.conn, chained)["candidates"]]

    def start(self, key="2026-10-29T22:00", stall=None, args=Args(), st=None):
        st = st if st is not None else {"handled": {}, "fires": {}}
        r = ka.handle_fire(SID_A, dict({"uuid": key, "timestamp": NOW, "budget": "b", "pick": True}, **(stall or {})),
                           "window start, test", st, args)
        return r, st

    def run_log(self):
        return [(r["decision"], r["session_id"], r["reason"]) for r in store.run_log(self.conn, "keepalive")]


class PickProject(Base):
    def test_rank_order_and_the_idle_task_managers_after_the_ready_ones(self):
        self.assertEqual(self.names(), ["alpha", "beta"])          # gamma: no session, no ready stage
        self.task("beta")
        self.assertEqual(self.names(), ["beta", "alpha"])          # no ready stage -> the next project
        self.task("alpha")
        self.assertEqual(self.names(), ["alpha", "beta"])

    def test_stage_priority_before_project_rank(self):
        """D-064: all high stages (by project rank), then medium, then low."""
        self.task("alpha", "medium")
        self.task("beta", "high")
        self.assertEqual(self.names(), ["beta", "alpha"])
        self.task("alpha", "high", "in_progress")                   # unfinished work counts as ready
        self.assertEqual(self.names(), ["alpha", "beta"])

    def test_blocked_stages_are_not_ready(self):
        self.task("beta", status="blocked")
        self.assertEqual(self.names(), ["alpha", "beta"])

    def test_a_paused_project_is_skipped(self):
        self.task("alpha")
        self.pause("alpha")
        sel = store.pick_project(self.conn)
        self.assertEqual([c["name"] for c in sel["candidates"]], ["beta"])
        self.assertEqual(sel["skipped"][0][0], "alpha")
        self.assertIn("paused (test)", sel["skipped"][0][1])

    def test_a_project_without_session_is_a_candidate_only_with_a_ready_stage(self):
        self.task("gamma")
        c = store.pick_project(self.conn)["candidates"]
        self.assertEqual([(x["name"], x["session"]) for x in c], [("gamma", None), ("alpha", SID_A), ("beta", SID_B)])

    def test_chained_keeps_its_project_over_rank(self):
        self.task("alpha")
        beta = store.get_project(self.conn, "beta")["id"]
        c = store.pick_project(self.conn, beta)["candidates"]
        self.assertEqual([x["name"] for x in c], ["beta", "alpha"])
        self.assertIn("chained", c[0]["why"])

    def test_none(self):
        for n in ("alpha", "beta"):
            self.pause(n)
        self.assertEqual(store.pick_project(self.conn)["candidates"], [])


class PickStart(Base):
    def test_the_first_candidate_is_continued_and_recorded(self):
        self.task("beta")
        r, st = self.start()
        self.assertIsNone(r)
        self.assertEqual(self.fired[0][0], SID_B)                  # anchored on alpha, fired at beta
        self.assertEqual(self.fired[0][1]["project"]["name"], "beta")
        self.assertEqual(self.preflights, [SID_B])                 # its preflight ran once, in the pick
        (dec, sid, why), = self.run_log()
        self.assertEqual((dec, sid), ("PICK", SID_B))
        self.assertIn("project beta (task-manager bbbbbbbb): 1 ready stage(s), best priority high", why)

    def test_an_owner_held_project_is_passed_over(self):
        self.task("alpha")
        self.held = {SID_A}
        self.start("k1")
        self.assertEqual(self.fired[-1][0], SID_B)
        self.assertIn("passed over: alpha: the owner holds its session", self.fired[-1][1]["project"]["why"])

    def test_a_project_at_its_limit_is_not_passed_over(self):
        """A 04:00 start fired to the second, its session limit resetting at 04:00 (still inside
        RESET_GRACE): it continues, as before S4 (limits are account-wide; the gate decided)."""
        self.task("alpha")
        self.limited = {SID_A}
        self.start("window-start-2026-10-29-s2")
        self.assertEqual(self.fired[-1][0], SID_A)
        self.assertNotIn("passed over", self.fired[-1][1]["project"]["why"])
        self.assertEqual(self.stall_calls, [])

    def test_at_most_pick_max_pass_over_projects_are_passed_over(self):
        self.task("alpha")
        self.task("beta")
        self.task("gamma")
        self.held = {SID_A, SID_B}
        with mock.patch.object(ka, "PICK_MAX_PASS_OVER", 1):
            r, st = self.start()
        self.assertEqual(self.preflights, [SID_A, SID_B])          # alpha's in the pick, beta's in handle_fire
        rec = st["handled"]["2026-10-29T22:00"]
        self.assertEqual((rec["result"], rec["project"]), ("preflight-failed", "beta"))

    def test_one_run_log_row_per_start_not_per_owner_busy_retry(self):
        self.held = {SID_A, SID_B}
        st = {"handled": {}, "fires": {}}
        for _ in range(2):
            r, _ = self.start(stall={"owner_check": True}, st=st)
            self.assertEqual(r, ka.OWNER_BUSY)
        self.assertEqual(self.run_log(), [])
        self.held = set()
        with mock.patch.object(ka, "owner_typing", lambda sid, now: None):
            self.start(stall={"owner_check": True}, st=st)
        self.assertEqual([d for d, _, _ in self.run_log()], ["PICK"])
        self.assertEqual(self.fired[0][0], SID_A)

    def test_the_last_candidate_goes_through_the_starts_own_checks(self):
        """Owner-held with no other candidate: handle_fire's preflight skip as before S4."""
        self.held = {SID_A, SID_B}
        r, st = self.start()
        self.assertIsNone(r)
        self.assertEqual(self.fired, [])
        rec = st["handled"]["2026-10-29T22:00"]
        self.assertEqual((rec["result"], rec["held"], rec["project"]), ("preflight-failed", True, "beta"))
        ka.fail_stop.assert_not_called()

    def test_no_candidate_is_a_hold_not_an_error(self):
        self.pause("alpha")
        self.pause("beta")
        with mock.patch.object(ka, "automation_paused", lambda what, sid=None: False):   # the pick decides
            r, st = self.start()
        self.assertIsNone(r)
        self.assertEqual(self.fired, [])
        rec = st["handled"]["2026-10-29T22:00"]
        self.assertEqual(rec["result"], "no-project")
        self.assertIn("no project to run (alpha: paused (test); beta: paused (test)", rec["why"])
        self.assertEqual(self.run_log()[0][0], "HOLD")
        ka.fail_stop.assert_not_called()

    def test_a_chained_start_keeps_its_project_over_a_ready_higher_rank(self):
        self.task("alpha")
        self.start(stall={"chained": SID_B})
        self.assertEqual(self.fired[0][0], SID_B)
        with mock.patch.object(ka, "running_session", lambda now, st=None: SID_B):   # e.g. a last-stretch slot
            self.start("k2")
        self.assertEqual(self.fired[1][0], SID_B)
        self.assertIn("chained", self.fired[1][1]["project"]["why"])

    def test_one_start_key_fires_one_project(self):
        st = {"handled": {}, "fires": {}}
        self.task("beta")
        with mock.patch.object(ka, "_fire_start", side_effect=lambda sid, stall, reason, st, args, key, *a:
                               st["handled"].__setitem__(key, {"result": "continued-in-place", "session": sid})
                               or self.fired.append(sid)):
            self.start(st=st)
            self.task("alpha")
            self.start(st=st)                                       # the runner reruns the plan step
        self.assertEqual(self.fired, [SID_B])

    def test_a_pick_failure_aborts_the_start(self):
        with mock.patch.object(store, "pick_project", side_effect=RuntimeError("db gone")):
            r, st = self.start()
        self.assertEqual(r, "failed")
        self.assertEqual(st["handled"]["2026-10-29T22:00"]["result"], "start-failed")
        self.assertIn("project pick failed", ka.fail_stop.call_args[0][0])

    def test_a_fill_up_does_not_pick(self):
        self.task("beta")
        st = {"handled": {}, "fires": {}}
        ka.handle_fire(SID_A, {"uuid": "fillup-k", "timestamp": NOW, "prompt": "fillup"}, "fill-up", st, Args())
        self.assertEqual(self.fired[0][0], SID_A)
        self.assertNotIn("project", self.fired[0][1])
        self.assertEqual([d for d, _, _ in self.run_log()], ["FILLUP"])    # its own row (S9b), not a PICK


class OneTaskManager(Base):
    """Today's live shape: one project with a session (rank 1, with ready stages), one without
    (rank 2, no stages): every start continues the one, with no extra host call."""

    def setUp(self):
        super().setUp()
        store.update_project(self.conn, "beta", manager_session="")
        self.conn.commit()
        self.task("alpha")

    def test_the_start_continues_it_as_before(self):
        self.start()
        self.assertEqual([f[0] for f in self.fired], [SID_A])
        self.assertEqual((self.preflights, self.stall_calls), ([SID_A], []))   # handle_fire's own preflight only

    def test_owner_held_is_skipped_as_before(self):
        self.held = {SID_A}
        r, st = self.start()
        self.assertEqual(st["handled"]["2026-10-29T22:00"]["result"], "preflight-failed")
        r, st = self.start(stall={"owner_check": True})
        self.assertEqual(r, ka.OWNER_BUSY)                          # D-259: the last stretch waits, as before

    def test_without_a_ready_stage_it_still_continues(self):
        self.conn.execute("UPDATE tasks SET status='done'")
        self.conn.commit()
        self.start()
        self.assertEqual([f[0] for f in self.fired], [SID_A])

    def test_the_session_less_project_gets_none_while_paused_alone(self):
        self.pause("alpha")
        with mock.patch.object(ka, "automation_paused", lambda what, sid=None: False):
            r, st = self.start()
        self.assertEqual(st["handled"]["2026-10-29T22:00"]["result"], "no-project")   # beta: no stage, no session


class FirstStart(Base):
    def setUp(self):
        super().setUp()
        self.task("gamma")
        self.launches = []
        self.rc = 0
        for n, v in (("fire", self.fake_fire), ("transcript_path", lambda sid: "/t"),
                     ("verify_reply", lambda path, since: {"message": {"model": "m"}}), ("_fire_usage", lambda: None),
                     ("tmux_has", lambda name: False), ("run_pace_text", lambda sid, stall, now, u: "PACE"),
                     ("usage_cache_checked", lambda: {})):
            p = mock.patch.object(ka, n, v)
            p.start()
            self.addCleanup(p.stop)

    def fake_fire(self, sid, cwd, message, plan, new=False, **kw):
        self.launches.append((sid, cwd, message, plan, new, ka.tmux_name(sid)))
        return self.rc, "", "boom" if self.rc else ""

    def gamma(self):
        return store.get_project(self.conn, "gamma")

    def test_its_task_manager_is_created_at_its_first_start(self):
        r, st = self.start()
        self.assertIsNone(r)
        (sid, cwd, msg, plan, new, tmux), = self.launches
        self.assertEqual((cwd, plan, new, tmux), (os.path.join(self.dir, "gamma"), "resume", True,
                                                  "afc-gamma-task-manager"))
        self.assertEqual(self.gamma()["manager_session"], sid)
        self.assertIn("task-manager of the project gamma", msg)
        self.assertIn("the gamma project", msg)                    # the description: the kickstart (D-301)
        self.assertIn("PACE", msg)
        self.assertNotIn("AFClaude-repo rules", msg)
        rec = st["handled"]["2026-10-29T22:00"]
        self.assertEqual((rec["result"], rec["session"], rec["project"], rec["created"]),
                         ("continued-in-place", sid, "gamma", True))
        self.assertEqual(self.run_log()[0][0], "PICK")
        row = store.get_driven_session(self.conn, sid)
        self.assertEqual((row["kind"], row["tmux"]), ("task-manager", "afc-gamma-task-manager"))
        self.assertIn(sid, [t["session"] for t in ka.registry_sessions()])

    def test_a_launcher_failure_takes_it_back_and_aborts(self):
        self.rc = 1
        r, st = self.start()
        self.assertEqual(r, "failed")
        self.assertIsNone(self.gamma()["manager_session"])
        sid = self.launches[0][0]
        self.assertIsNotNone(store.get_driven_session(self.conn, sid)["ended_at"])
        self.assertIn("task-manager creation failed", ka.fail_stop.call_args[0][0])

    def assert_taken_back(self, r):
        self.assertEqual(r, "failed")
        self.assertIsNone(self.gamma()["manager_session"])
        rows = [x for x in store.driven_sessions(self.conn, include_ended=True) if x["kind"] == "task-manager"
                and x["project_id"] == self.gamma()["id"]]
        self.assertTrue(rows and all(x["ended_at"] for x in rows))
        self.assertIn("task-manager creation failed", ka.fail_stop.call_args[0][0])

    def test_a_failure_storing_it_takes_it_back(self):
        real = actions.perform

        def perform(conn, action, *a, **kw):
            if action == "driven.set" and "kind" in a[0]:
                real(conn, action, *a, **kw)
                raise RuntimeError("db hiccup")
            return real(conn, action, *a, **kw)
        with mock.patch.object(actions, "perform", perform):
            r, st = self.start()
        self.assertEqual(self.launches, [])
        self.assert_taken_back(r)
        self.assertIn("db hiccup", st["handled"]["2026-10-29T22:00"]["error"])

    def test_a_launch_exception_takes_it_back(self):
        with mock.patch.object(ka, "fire", side_effect=RuntimeError("no launcher")):
            r, st = self.start()
        self.assert_taken_back(r)

    def test_a_timed_out_launch_without_its_tmux_session_is_taken_back(self):
        with mock.patch.object(ka, "fire", side_effect=subprocess.TimeoutExpired("ka_resume.sh", 120)):
            r, st = self.start()
        self.assert_taken_back(r)

    def test_a_timed_out_launch_with_its_tmux_session_is_kept_and_fails(self):
        launched = []

        def fire(*a, **kw):
            launched.append(1)
            raise subprocess.TimeoutExpired("ka_resume.sh", 120)
        with mock.patch.object(ka, "fire", fire), \
                mock.patch.object(ka, "tmux_has", lambda name: bool(launched)):
            r, st = self.start()
        self.assertEqual(r, "failed")
        sid = self.gamma()["manager_session"]
        self.assertTrue(sid)
        self.assertIsNone(store.get_driven_session(self.conn, sid)["ended_at"])
        self.assertIn("kept on the project", st["handled"]["2026-10-29T22:00"]["error"])
        ka.fail_stop.assert_called_once()

    def test_no_reply_is_an_error_but_the_session_stays(self):
        with mock.patch.object(ka, "verify_reply", lambda path, since: None):
            r, st = self.start()
        self.assertEqual(st["handled"]["2026-10-29T22:00"]["result"], "no-reply-within-timeout")
        self.assertIsNotNone(self.gamma()["manager_session"])
        self.assertIn("did not reply", ka.fail_stop.call_args[0][0])

    def test_a_dry_run_creates_nothing(self):
        r, st = self.start(args=DryArgs())
        self.assertEqual(self.launches, [])
        self.assertIsNone(self.gamma()["manager_session"])
        self.assertEqual(st["handled"]["2026-10-29T22:00"]["result"], "dry-run")
        self.assertTrue(any(ln.startswith("WOULD CREATE (dry-run): project 'gamma'") for ln in self.logs))
        self.assertEqual(self.run_log(), [])

    def test_a_project_without_path_is_an_error(self):
        self.conn.execute("UPDATE projects SET path=NULL WHERE name='gamma'")
        self.conn.commit()
        r, st = self.start()
        self.assertEqual(r, "failed")
        self.assertIn("has no path", st["handled"]["2026-10-29T22:00"]["error"])


class DryRunSecondProject(unittest.TestCase):
    """The S4 'done when' check: in testenv (--arm off) a second test project ranked first with a ready
    stage is picked and its session would be continued; the per-project message has the rules inline."""

    def setUp(self):
        testenv.fresh_db(self)
        self.addCleanup(testenv.unpause)
        conn = store.connect()
        self.addCleanup(conn.close)
        self.dir = tempfile.mkdtemp(prefix="afclaude-pick-")
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        store.add_project(conn, "AFClaude", path=ka.HERE)
        store.update_project(conn, "AFClaude", manager_session=SID_A)
        other = os.path.join(self.dir, "second")
        os.makedirs(other)
        store.add_project(conn, "second", "a test project", other, rank=1)
        store.update_project(conn, "second", manager_session=SID_B)
        store.add_task(conn, "do it", project="second")
        conn.commit()
        for n in ("STATE_FILE", "DEFER_FILE"):
            p = mock.patch.object(ka, n, os.path.join(self.dir, n))
            p.start()
            self.addCleanup(p.stop)
        self.transcript = os.path.join(self.dir, "b.jsonl")
        with open(self.transcript, "w") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": "2026-10-29T20:00:00Z", "cwd": other}) + "\n")

    def test_it_picks_the_second_project_and_would_continue_it(self):
        logs = []
        with mock.patch.object(ka, "log", logs.append), mock.patch.object(ka, "progress_note"), \
                mock.patch.object(ka, "db_paused", lambda what: False), \
                mock.patch.object(ka, "preflight", lambda sid: (True, [], "send-keys")), \
                mock.patch.object(ka, "running_session", lambda now, st=None: None), \
                mock.patch.object(ka, "transcript_path", lambda sid: self.transcript if sid == SID_B else None), \
                mock.patch.object(ka, "usage_cache_checked", lambda: {}), \
                mock.patch.object(ka, "fire") as fire:
            st = {"handled": {}, "fires": {}}
            ka.handle_fire(ka.start_session(), {"uuid": "k", "timestamp": NOW, "budget": "b", "pick": True},
                           "window start, test", st, DryArgs())
        fire.assert_not_called()
        self.assertEqual(ka.start_session(), SID_B)                 # rank 1 is the anchor too
        rec = st["handled"]["k"]
        self.assertEqual((rec["result"], rec["session"], rec["project"]), ("dry-run", SID_B, "second"))
        would = next(ln for ln in logs if ln.startswith("WOULD FIRE"))
        self.assertIn(f"--session {SID_B}", would)
        self.assertIn("This run is for the project second.", would)
        self.assertIn(os.path.join(self.dir, "second", "PROGRESS.md"), would)
        self.assertNotIn(ka.load_prompt("rules_pointer")[:40], would)
        self.assertNotIn("AFClaude-repo", would.split("plan=send-keys", 1)[1].split("This run", 1)[0])


class Prompts(unittest.TestCase):
    def test_another_projects_inline_rules_fit_one_tmux_line(self):
        msg = ka.session_message("continue", reason="window start, " + "r" * 300, progress="/p/PROGRESS.md",
                                 insert="This run is for the project " + "x" * 64 + ". " + "PACE " * 120,
                                 hooked=False, afclaude=False)
        self.assertLess(len(msg.encode()), TMUX_LINE_MAX)
        self.assertNotIn(" ".join(ka.load_prompt("manager_afclaude").split())[:60], msg)
        self.assertIn(" ".join(ka.load_prompt("manager").split())[:60], msg)
        first = ka.session_message("manager_first_start", project="x" * 64, path="/p", desc="d" * 2000,
                                   progress="/p/PROGRESS.md", insert="PACE " * 120, hooked=False, afclaude=False)
        self.assertLess(len(first.encode()), TMUX_LINE_MAX)


class Plumbing(Base):
    def test_a_deferred_entry_of_another_session_is_left_alone(self):
        key = "2026-10-29T22:00"
        with mock.patch.object(ka.schedule, "in_window", lambda now: True), \
                mock.patch.object(ka.schedule, "start_key", lambda now: key):
            ka.defer_window_start(key, SID_A, NOW + timedelta(hours=1), "r")
            self.assertIsNone(ka.deferred_plan(SID_B, NOW, {"handled": {}}))
            self.assertIn(key, ka.load_deferred())                  # beta's plan step does not drop alpha's
            self.assertEqual(ka.deferred_plan(SID_A, NOW, {"handled": {}})[0], key)

    def test_run_active_counts_only_its_own_sessions_fires(self):
        st = {"handled": {"k": {"at": NOW.isoformat(), "result": "continued-in-place", "session": SID_A}}}
        with mock.patch.object(ka, "transcript_path", lambda sid: None):
            self.assertIsNone(ka.run_active(SID_B, NOW, st))
        path = os.path.join(self.dir, "a.jsonl")
        with open(path, "w") as fh:
            fh.write("{}\n")
        with mock.patch.object(ka, "transcript_path", lambda sid: path), \
                mock.patch.object(ka, "last_message", lambda p: {}):
            now = datetime.now(UTC)
            st["handled"]["k"]["at"] = now.isoformat()
            self.assertEqual(ka.run_active(SID_A, now, st), now)
            self.assertIsNone(ka.run_active(SID_B, now, st))

    def test_running_session_is_the_registrys_running_one(self):
        with mock.patch.object(ka, "run_active", lambda sid, now, st=None: NOW if sid == SID_B else None):
            self.assertEqual(RUNNING(NOW, {}), SID_B)
        with mock.patch.object(ka, "run_active", lambda sid, now, st=None: None):
            self.assertIsNone(RUNNING(NOW, {}))

    def test_project_edit_allows_several_task_managers(self):
        actions.perform(self.conn, "project.edit", {"project": "gamma",
                                                    "manager_session": "cccccccc-0000-4000-8000-000000000003"},
                        actor="cli", via="cli")
        self.assertEqual(len(store.manager_sessions(self.conn)), 3)

    def test_run_log_add_checks_its_decision(self):
        with self.assertRaisesRegex(ValueError, "unknown run_log decision"):
            actions.run_log_add(self.conn, "keepalive", "NOPE", "x", actor="keepalive", via="runner")
        actions.run_log_add(self.conn, "keepalive", "HOLD", "x", actor="keepalive", via="runner")
        self.assertEqual(self.run_log(), [("HOLD", None, "x")])


if __name__ == "__main__":
    unittest.main()
