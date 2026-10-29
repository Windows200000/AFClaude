#!/usr/bin/env python3
"""Offline tests for phase 3b S5, run-now (docs/phase3b_plan.md §2 "Run-now", §5 S5; run_now.py):
each request kind started (tmux name in the result), refused (running, at its limit, the owner
holds it, unknown target) or failed (no reply: alert AND pause, D-282 -- plain assertions today;
re-run once the pre-release step of design §13 lands to confirm it flips to alert-only); no window
or budget gate but the preflight (D-153); it runs while paused (D-282); a work_on_now against a
project without a task-manager creates it through the shared helper (D-281); one open request per
(kind, target); the runner's job: one keyed run per open request, a restart during one fails it
(D-257), a run that ends without deciding it fails it; the timeout covers the inner waits (D-234);
the retired keepalive flags; the CLI's usage line."""
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic DB / data dir)
import actions  # noqa: E402
import dispatcher  # noqa: E402
import failstop  # noqa: E402
import keepalive as ka  # noqa: E402
import run_now  # noqa: E402
import runner  # noqa: E402
import runner_jobs  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402
import usage_review  # noqa: E402

UTC = timezone.utc
SID_A = "aaaaaaaa-0000-4000-8000-000000000001"
SID_NEW = "cccccccc-0000-4000-8000-000000000003"
STALLED = "dddddddd-0000-4000-8000-000000000004"
NOW = datetime(2026, 10, 29, 11, 0, tzinfo=UTC)          # midday: outside the night window
HELD = f"{ka.RC_HELD} thread (pid [7])"


class Base(unittest.TestCase):
    """A temp DB: alpha (rank 1) with a task-manager session, gamma (rank 2) without one."""

    def setUp(self):
        testenv.fresh_db(self)
        self.addCleanup(testenv.unpause)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        self.dir = tempfile.mkdtemp(prefix="afclaude-runnow-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, sid in (("alpha", SID_A), ("gamma", None)):
            path = os.path.join(self.dir, name)
            os.makedirs(path)
            store.add_project(self.conn, name, f"the {name} project", path)
            if sid:
                store.update_project(self.conn, name, manager_session=sid)
        self.conn.commit()
        actions.perform(self.conn, "driven.set", {"session_id": SID_A, "kind": "task-manager",
                                                  "tmux": "afc-alpha-task-manager", "project": "alpha"},
                        actor="cli", via="cli")
        self.alerts, self.preflights, self.fired = [], [], []
        self.held, self.limited, self.running, self.reply = False, False, False, "continued-in-place"
        for n, v in (("STATE_FILE", os.path.join(self.dir, "state.json")), ("log", lambda m: None),
                     ("progress_note", lambda line: None), ("alert", lambda s, b="": self.alerts.append(s)),
                     ("preflight", self.fake_preflight), ("stall_status", self.fake_stall_status),
                     ("run_active", lambda sid, now, st=None: NOW - timedelta(minutes=5) if self.running else None),
                     ("_fire_start", self.fake_fire_start), ("release_lock", lambda fh: None),
                     ("budget_headroom", mock.Mock(side_effect=AssertionError("no budget gate (D-153)"))),
                     ("budget_eval", mock.Mock(side_effect=AssertionError("no budget gate (D-153)")))):
            p = mock.patch.object(ka, n, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(ka, "session_lock", lambda sid, wait=None: object())
        p.start()
        self.addCleanup(p.stop)

    def fake_preflight(self, sid, tmux=None):
        self.preflights.append(sid)
        return (False, [HELD], None) if self.held else (True, [], "send-keys")

    def fake_stall_status(self, sid, now):
        if self.limited:
            return "WAIT_RESET", "session limit resets 13:00", {"reset": NOW + timedelta(hours=2)}
        return "NO_STALL", "", None

    def fake_fire_start(self, sid, stall, reason, st, args, key, night, plan):
        self.fired.append((sid, dict(stall), reason))
        st["handled"][key] = {"at": NOW.isoformat(), "result": self.reply, "session": sid, "plan": plan}
        ka.save_state(st)
        if self.reply != "continued-in-place":
            ka.fail_stop(f"keep-alive continue of {sid[:8]} failed: {self.reply}", "test")

    def request(self, kind, target=None):
        return actions.perform(self.conn, "request.add", {"kind": kind, "target": target}, actor="cli", via="cli")

    def decide(self, r):
        note = run_now.run(str(r["id"]), now=NOW)
        return store.get_request(self.conn, r["id"]), note

    def result(self, row):
        return __import__("json").loads(row["result"])


class WorkOnNow(Base):
    def test_started_with_its_tmux_name_outside_the_window_and_without_the_budget(self):
        r = self.request("work_on_now", "alpha")
        row, note = self.decide(r)
        self.assertEqual(row["status"], "started")
        self.assertEqual(self.result(row), {"tmux": "afc-alpha-task-manager", "session_id": SID_A})
        self.assertEqual(self.preflights, [SID_A])                 # the preflight still applies (D-153)
        sid, stall, reason = self.fired[0]
        self.assertTrue(stall["run_now"])
        self.assertNotIn("pick", stall)                            # no window / timing / pick
        self.assertIn("D-153", stall["budget"])
        self.assertEqual(stall["project"]["name"], "alpha")
        st = ka.load_state()
        self.assertIn("request:1", st["handled"])                  # an ordinary run's fire record (D-224, D-212)
        self.assertEqual(st["fires"], {})                          # not one of the night's capped starts
        audit = [(a["action"], a["actor"]) for a in store.audit_log(self.conn) if a["action"].startswith("request.")]
        self.assertIn(("request.start", "runner:request"), audit)

    def test_runs_while_paused(self):
        """D-282: run-now runs while automation is paused, install-wide or the project's."""
        actions.perform(self.conn, "automation.set", {"paused": True, "reason": "test"}, actor="cli", via="cli")
        actions.perform(self.conn, "automation.set_project", {"project": "alpha", "paused": True, "reason": "t"},
                        actor="cli", via="cli")
        row, _ = self.decide(self.request("work_on_now", "alpha"))
        self.assertEqual(row["status"], "started")

    def test_refused_running(self):
        self.running = True
        row, _ = self.decide(self.request("work_on_now", "alpha"))
        self.assertEqual(row["status"], "refused")
        self.assertIn("running", self.result(row)["reason"])
        self.assertEqual(self.fired, [])

    def test_refused_at_its_limit(self):
        self.limited = True
        row, _ = self.decide(self.request("work_on_now", "alpha"))
        self.assertEqual(row["status"], "refused")
        self.assertIn("at its limit until", self.result(row)["reason"])
        self.assertFalse(failstop.paused()["paused"])              # refused is no failure

    def test_refused_when_the_owner_holds_it(self):
        self.held = True
        row, _ = self.decide(self.request("work_on_now", "alpha"))
        self.assertEqual(row["status"], "refused")
        self.assertIn("the owner holds the session", self.result(row)["reason"])
        self.assertEqual(self.fired, [])
        self.assertFalse(failstop.paused()["paused"])

    def test_refused_unknown_target(self):
        r = store.add_request(self.conn, "cli", "work_on_now", "999")
        self.conn.commit()
        row, _ = self.decide(r)
        self.assertEqual(row["status"], "refused")
        self.assertIn("unknown target", self.result(row)["reason"])

    def test_failed_no_reply_alerts_and_pauses(self):
        """D-282 (for now; the pre-release step of design §13 makes it alert-only)."""
        self.reply = "no-reply-within-timeout"
        row, _ = self.decide(self.request("work_on_now", "alpha"))
        self.assertEqual(row["status"], "failed")
        self.assertIn("no-reply-within-timeout", self.result(row)["reason"])
        self.assertTrue(failstop.paused()["paused"])
        self.assertEqual(len(self.alerts), 1)                      # reported once (by the continue's own path)

    def test_a_failure_the_handler_did_not_report_is_reported(self):
        def fire(sid, stall, reason, st, args, key, night, plan):
            st["handled"][key] = {"result": "launcher-failed", "session": sid}
        with mock.patch.object(ka, "_fire_start", fire):
            row, _ = self.decide(self.request("work_on_now", "alpha"))
        self.assertEqual(row["status"], "failed")
        self.assertTrue(failstop.paused()["paused"])
        self.assertEqual(len(self.alerts), 1)

    def test_a_crash_records_failed_and_raises(self):
        with mock.patch.object(ka, "_start_session", side_effect=RuntimeError("boom")):
            r = self.request("work_on_now", "alpha")
            with self.assertRaises(RuntimeError):
                run_now.run(str(r["id"]), now=NOW)
        row = store.get_request(self.conn, r["id"])
        self.assertEqual(row["status"], "failed")
        self.assertIn("boom", self.result(row)["reason"])
        with self.assertRaises(RuntimeError):                       # never decided twice
            run_now.run(str(r["id"]), now=NOW)

    def test_a_project_without_a_task_manager_gets_one_through_the_shared_helper(self):
        """D-281: work_on_now is the project's first start (keepalive.create_task_manager), no special path."""
        made = []

        def create(project, arm=True, insert=None, actor="keepalive"):
            made.append(project["name"])
            actions.perform(self.conn, "project.edit", {"project": project["project_id"], "manager_session": SID_NEW},
                            actor=actor, via="runner")
            actions.perform(self.conn, "driven.set", {"session_id": SID_NEW, "kind": "task-manager",
                                                      "tmux": "afc-gamma-task-manager",
                                                      "project": project["project_id"]}, actor=actor, via="runner")
            return {"session": SID_NEW, "tmux": "afc-gamma-task-manager", "rc": 0, "out": "", "err": "",
                    "sent_at": NOW, "reply": {"timestamp": "x"}, "message": "m"}
        with mock.patch.object(ka, "create_task_manager", create), \
                mock.patch.object(ka, "run_pace_text", lambda *a: "pace"), \
                mock.patch.object(ka, "usage_cache_checked", lambda: {}), \
                mock.patch.object(ka, "_fire_usage", lambda: {}):
            row, _ = self.decide(self.request("work_on_now", "gamma"))
        self.assertEqual(made, ["gamma"])
        self.assertEqual(store.get_project(self.conn, "gamma")["manager_session"], SID_NEW)
        self.assertEqual(row["status"], "started")
        self.assertEqual(self.result(row)["tmux"], "afc-gamma-task-manager")
        self.assertEqual(ka.load_state()["fires"], {})


class Requests(Base):
    def test_one_open_request_per_kind_and_target(self):
        a = self.request("work_on_now", "alpha")
        self.assertEqual(self.request("work_on_now", "alpha")["id"], a["id"])
        self.assertNotEqual(self.request("work_on_now", "gamma")["id"], a["id"])
        self.decide(a)
        self.assertNotEqual(self.request("work_on_now", "alpha")["id"], a["id"])    # decided: a new one

    def test_owner_only(self):
        with self.assertRaises(actions.Forbidden):
            actions.perform(self.conn, "request.add", {"kind": "review_now"}, actor="mcp:x", via="mcp",
                            autonomous=True)


class ContinueAndReview(Base):
    def test_continue_now_refused_when_not_stalled_and_started_through_the_dispatcher(self):
        with mock.patch.object(dispatcher, "LOCK_FILE", os.path.join(self.dir, "d.lock")), \
                mock.patch.object(dispatcher.stalled, "scan", lambda conn: None), \
                mock.patch.object(dispatcher, "load_state", lambda: {"handled": {}, "sessions": {}, "alerted": {},
                                                                     "rc_held": {}, "starts": {}}), \
                mock.patch.object(dispatcher, "save_state", lambda st: True):
            r = store.add_request(self.conn, "cli", "continue_now", STALLED)
            self.conn.commit()
            with mock.patch.object(store, "stalled_decisions", lambda conn: []):
                row, _ = self.decide(r)
            self.assertEqual(row["status"], "refused")
            self.assertIn("not stalled", self.result(row)["reason"])
            r = store.add_request(self.conn, "cli", "continue_now", STALLED)
            self.conn.commit()
            stall = {"session_id": STALLED, "stall_uuid": "u1", "title": "x"}
            with mock.patch.object(store, "stalled_decisions", lambda conn: [stall]), \
                    mock.patch.object(dispatcher.Pass, "continue_stalled", lambda p, rr, key: ("started", "afc-stall-x-dddd")):
                row, _ = self.decide(r)
        self.assertEqual(row["status"], "started")
        self.assertEqual(self.result(row)["tmux"], "afc-stall-x-dddd")

    def _refused_preflight_stall(self, quiet):
        """Pass.continue_stalled on a stall whose preflight refuses (a "failed" outcome that alerts)."""
        st = {"handled": {}, "sessions": {}, "alerted": {}, "rc_held": {}, "starts": {}}
        p = dispatcher.Pass(self.conn, {}, st, NOW, True, quiet=quiet)
        stall = {"session_id": STALLED, "stall_uuid": "u1", "title": "x"}
        with mock.patch.object(ka, "evaluate", lambda sid, now, getter: ("FIRE", "d", None)), \
                mock.patch.object(dispatcher, "session_cwd", lambda conn, sid: self.dir), \
                mock.patch.object(ka, "claim_tmux", lambda *a, **kw: "afc-stall-x"), \
                mock.patch.object(ka, "preflight", lambda sid, tmux: (False, ["busy"], None)):
            return p.continue_stalled(stall, "u1"), st

    def test_a_failed_continue_now_alerts_once(self):
        # the pass itself alerts (alert_once) ...
        (outcome, _), st = self._refused_preflight_stall(quiet=False)
        self.assertEqual(outcome, "failed")
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("preflight:u1", st["alerted"])
        # ... a run-now continue leaves the alert to its request (failstop.fail, alert + pause, D-282)
        self.alerts.clear()
        with mock.patch.object(dispatcher, "LOCK_FILE", os.path.join(self.dir, "d.lock")), \
                mock.patch.object(dispatcher.stalled, "scan", lambda conn: None), \
                mock.patch.object(dispatcher, "save_state", lambda st: True), \
                mock.patch.object(dispatcher, "load_state", lambda: {"handled": {}, "sessions": {}, "alerted": {},
                                                                     "rc_held": {}, "starts": {}}), \
                mock.patch.object(store, "stalled_decisions",
                                  lambda conn: [{"session_id": STALLED, "stall_uuid": "u1", "title": "x"}]), \
                mock.patch.object(ka, "evaluate", lambda sid, now, getter: ("FIRE", "d", None)), \
                mock.patch.object(dispatcher, "session_cwd", lambda conn, sid: self.dir), \
                mock.patch.object(ka, "claim_tmux", lambda *a, **kw: "afc-stall-x"), \
                mock.patch.object(ka, "preflight", lambda sid, tmux: (False, ["busy"], None)):
            r = store.add_request(self.conn, "cli", "continue_now", STALLED)
            self.conn.commit()
            row, _ = self.decide(r)
        self.assertEqual(row["status"], "failed")
        self.assertIn("preflight refused", self.result(row)["reason"])
        self.assertEqual(len(self.alerts), 1)
        self.assertTrue(failstop.paused()["paused"])

    def test_continue_now_refuses_a_task_manager(self):
        with mock.patch.object(dispatcher, "LOCK_FILE", os.path.join(self.dir, "d.lock")), \
                mock.patch.object(dispatcher.stalled, "scan", lambda conn: None), \
                mock.patch.object(dispatcher, "load_state", lambda: {"handled": {}, "sessions": {}}), \
                mock.patch.object(store, "stalled_decisions", lambda conn: [{"session_id": SID_A}]):
            r = store.add_request(self.conn, "cli", "continue_now", SID_A)
            self.conn.commit()
            row, _ = self.decide(r)
        self.assertEqual(row["status"], "refused")
        self.assertIn("run-now work", self.result(row)["reason"])

    def test_review_now_without_the_quota_gate(self):
        def run_once(st, now, which, extra=None, record=None, sid=None):
            st.setdefault("one_off_runs", []).append({"session": SID_NEW, "only": which})
            return True
        with mock.patch.object(usage_review, "run_once", run_once), \
                mock.patch.object(usage_review, "quota_check", side_effect=AssertionError("no quota gate")), \
                mock.patch.object(usage_review, "lock", lambda: None), \
                mock.patch.object(usage_review, "save_checked", lambda st, what: None), \
                mock.patch.object(ka, "release_lock", lambda fh: None), \
                mock.patch.object(ka, "tmux_name", lambda sid: "afc-review"):
            row, _ = self.decide(self.request("review_now"))
        self.assertEqual(row["status"], "started")
        self.assertEqual(self.result(row), {"tmux": "afc-review", "session_id": SID_NEW})


class RunnerJob(unittest.IsolatedAsyncioTestCase):
    """The runner's `request` job: one keyed run per open request, at once; D-257; undecided = failed."""

    def setUp(self):
        self.signalled = []
        p = mock.patch.object(runner, "_signal_group", lambda pid, sig: self.signalled.append((pid, sig)))
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp(prefix="afc-runnow-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = store.connect(os.path.join(self.tmp, "t.db"), create=True)
        self.addCleanup(self.conn.close)
        self.fails = []
        self.now = NOW

    def runner(self, outcome=None):
        class Spawner:
            calls = []

            async def run(self, argv, timeout_s, on_pid):
                self.calls.append((argv[argv.index("--key") + 1], timeout_s))
                on_pid(1)
                return outcome() if outcome else runner.ChildOutcome(0, '{"status": "ok"}\n', "", False)
        self.spawner = Spawner()
        return runner.Runner(self.conn, owns=frozenset({"request"}), spawner=self.spawner, clock=lambda: self.now,
                             log_fn=lambda m: None, fail=lambda c, s, d="", **kw: self.fails.append(s),
                             sock=os.path.join(self.tmp, "r.sock"), supervise={})

    def add(self, kind="review_now"):
        r = store.add_request(self.conn, "cli", kind, None)
        self.conn.commit()
        return r

    async def test_open_requests_are_due_at_once_and_run_once(self):
        r = self.runner()
        a = self.add()
        due = r.plan(self.now)
        self.assertEqual([(d.job.name, d.key) for d in due], [("request", str(a["id"]))])
        r.start(due[0], self.now)
        self.assertIn(f"request {a['id']}", r.tasks)                 # one slot per request
        await r.tasks[f"request {a['id']}"]
        self.assertEqual(r.plan(self.now), [])                       # a run row: never again
        # its child exited 0 without deciding it: the run fails, the request is failed (alert + pause)
        self.assertEqual(store.get_request(self.conn, a["id"])["status"], "failed")
        self.assertEqual(store.key_runs(self.conn, "request", str(a["id"]))[0]["status"], "failed")
        self.assertEqual(self.fails, ["request failed"])
        self.assertEqual(self.spawner.calls[0][1], 90 * 60)

    async def test_paused_does_not_hold_it(self):
        r = self.runner()
        r._paused = lambda: {"paused": True, "reason": "x"}
        self.add()
        self.assertEqual(len(r.plan(self.now)), 1)

    async def test_a_restart_during_a_request_fails_it(self):
        a = self.add()
        actions.run_begin(self.conn, boot_id="a" * 32, job="request", key=str(a["id"]), timeout_s=60,
                          actor="runner:request", via="runner", session=True, rerun=False)
        actions.runner_state_put(self.conn, {"boot": {"boot_id": "a" * 32, "pid": 4, "started_at": "x"},
                                             "clean_exit": True}, actor="runner", via="runner")
        r = self.runner()
        r.boot()
        req = store.get_request(self.conn, a["id"])
        self.assertEqual(req["status"], "failed")
        self.assertIn("runner restart", req["result"])
        self.assertIn("request interrupted by a runner restart", self.fails)
        self.assertEqual(r.plan(self.now), [])

    async def test_a_finished_run_that_left_its_request_open_is_decided_failed(self):
        # finish() died between run_end and the request's write (a DB error): nothing else decides it
        a = self.add()
        rid, _ = actions.run_begin(self.conn, boot_id="a" * 32, job="request", key=str(a["id"]), timeout_s=60,
                                actor="runner:request", via="runner", session=True, rerun=False)
        actions.run_end(self.conn, rid, "failed", actor="runner:request", via="runner", error="x")
        r = self.runner()
        self.assertEqual(r.plan(self.now), [])
        req = store.get_request(self.conn, a["id"])
        self.assertEqual(req["status"], "failed")
        self.assertIn("ended without deciding the request", req["result"])
        self.assertEqual(store.list_requests(self.conn, "open"), [])  # request.add can take a new one
        self.assertEqual(r.plan(self.now), [])

    async def test_a_running_run_is_not_decided_by_the_plan(self):
        a = self.add()
        actions.run_begin(self.conn, boot_id="a" * 32, job="request", key=str(a["id"]), timeout_s=60,
                          actor="runner:request", via="runner", session=True, rerun=False)
        r = self.runner()
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(store.get_request(self.conn, a["id"])["status"], "open")

    async def test_a_db_error_while_deciding_it_is_not_swallowed(self):
        a = self.add()
        rid, _ = actions.run_begin(self.conn, boot_id="a" * 32, job="request", key=str(a["id"]), timeout_s=60,
                                actor="runner:request", via="runner", session=True, rerun=False)
        actions.run_end(self.conn, rid, "failed", actor="runner:request", via="runner", error="x")
        r = self.runner()
        with mock.patch.object(actions, "perform", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                r.plan(self.now)
        self.assertEqual(store.get_request(self.conn, a["id"])["status"], "open")

    def test_the_timeout_covers_the_inner_waits(self):
        least = runner_jobs.request_min_timeout()
        self.assertGreaterEqual(timedelta(minutes=runner.JOBS["request"].timeout_min), least)
        self.assertGreaterEqual(timedelta(minutes=actions.SETTINGS["runner_timeout_request"].bounds[0]), least)
        self.assertGreaterEqual(least, runner_jobs.window_start_min_timeout())     # work_on_now continues as a start
        self.assertIn("dispatcher.CONTINUE_LOCK_WAIT", runner_jobs.request_inner("continue_now"))
        self.assertTrue(runner.JOBS["request"].session)              # a drain holds it
        self.assertEqual((runner.JOBS["request"].on_interrupt, runner.JOBS["request"].paused), ("fail", "runs"))


class RetiredAndCli(Base):
    def test_the_retired_keepalive_flags_point_at_run_now(self):
        for argv in (["--work-on", "1"], ["--window-start", "--now"]):
            with self.subTest(argv), mock.patch.object(sys, "argv", ["keepalive.py"] + argv):
                with self.assertRaises(SystemExit) as cm:
                    ka.main()
                self.assertIn("tasks.py run-now", str(cm.exception.code))

    def test_cli_prints_the_usage_then_queues_and_wakes(self):
        woke = []
        buf = io.StringIO()
        with mock.patch.object(tasks, "run_now_usage", lambda: "usage now: session 40 %; weekly 60 %"), \
                mock.patch.object(runner, "wake", lambda: woke.append(1)), redirect_stdout(buf):
            self.assertEqual(tasks.main(["run-now", "work", "alpha"]), 0)
        out = buf.getvalue().splitlines()
        self.assertTrue(out[0].startswith("usage now: session 40 %"))
        self.assertIn("work_on_now", out[1])
        self.assertEqual(woke, [1])
        req = store.list_requests(self.conn, "open")
        self.assertEqual((req[0]["kind"], req[0]["target"], req[0]["actor"]), ("work_on_now", "1", "cli"))

    def test_mcp_request_queues_through_request_add(self):
        try:
            import mcp_server as ms
        except ImportError:
            self.skipTest("the MCP SDK is not installed (.venv)")
        import asyncio
        import json
        woke = []
        with mock.patch.object(tasks, "run_now_usage", lambda: "usage now: x"), \
                mock.patch.object(runner, "wake", lambda: woke.append(1)), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SID_NEW, "CLAUDE_GUARD_DISABLE": ""}):
            r = json.loads(asyncio.run(ms.afclaude_request("work", "alpha")))
        self.assertEqual((r["kind"], r["target"], r["status"], r["by"], r["usage"]),
                         ("work_on_now", "1", "open", f"mcp:{SID_NEW}", "usage now: x"))
        self.assertEqual(woke, [1])

    def test_run_now_usage_reads_the_cache(self):
        u = {"session": {"percent": 41.0, "resets_at": NOW}, "weekly": {"percent": 62.0, "resets_at": NOW}}
        with mock.patch.object(ka, "usage_cache_checked", lambda: u), \
                mock.patch.object(ka, "budget_headroom", lambda u, now: (5.0, "budget for this run: about +5 weekly %")):
            line = tasks.run_now_usage()
        self.assertIn("session 41 %", line)
        self.assertIn("weekly 62 %", line)
        self.assertIn("budget for this run", line)
        with mock.patch.object(ka, "usage_cache_checked", side_effect=OSError("gone")):
            self.assertIn("NOT READ", tasks.run_now_usage())


if __name__ == "__main__":
    unittest.main()
