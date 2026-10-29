#!/usr/bin/env python3
"""Offline tests for phase 3b S3, the task-manager registry replacing the session setting
(docs/phase3b_plan.md §5 S3): two task-managers in a temp DB, per-session plans keyed <job>:<sid8>, a pause
of one project holding only its jobs (D-244), a limit hit of one leaving the other alone (D-204), the legacy
`--session` failing when it disagrees with the registry. The keep-alive's own passes are exercised by
test_keepalive / test_compaction / test_fillup (their testenv helper registers the one session)."""
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic DB / data dir)
import actions  # noqa: E402
import keepalive as ka  # noqa: E402
import runner  # noqa: E402
import runner_jobs  # noqa: E402
import runner_state  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
SID_A = "aaaaaaaa-0000-4000-8000-000000000001"
SID_B = "bbbbbbbb-0000-4000-8000-000000000002"
NOW = datetime(2026, 10, 29, 22, 0, tzinfo=UTC)


class Base(unittest.TestCase):
    """A temp DB with two task-managers (alpha, beta; rank order) and a project without one (gamma)."""
    def setUp(self):
        testenv.fresh_db(self)
        self.addCleanup(testenv.unpause)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        for name, sid in (("alpha", SID_A), ("beta", SID_B), ("gamma", None)):
            store.add_project(self.conn, name)
            if sid:
                store.update_project(self.conn, name, manager_session=sid)
        self.conn.commit()
        self.logs = []
        self.addCleanup(setattr, ka, "log", ka.log)
        ka.log = self.logs.append
        for n in ("_WATCH_LINE", "_FILLUP_LINE", "_COMPACT_LINE", "_STOP_LINE"):
            self.addCleanup(setattr, ka, n, getattr(ka, n))
        self.addCleanup(setattr, ka, "_LINES", {})
        self.addCleanup(setattr, ka, "_PAUSE_LOGGED", {})
        ka._LINES, ka._PAUSE_LOGGED, ka._STOP_LINE = {}, {}, None

    def pause_project(self, name, paused=True):
        actions.perform(self.conn, "automation.set_project", {"project": name, "paused": paused,
                                                              "reason": "test"}, actor="cli", via="cli")


class Registry(Base):
    def test_manager_sessions_are_the_projects_with_a_session_in_rank_order(self):
        self.assertEqual([t["name"] for t in store.manager_sessions(self.conn)], ["alpha", "beta"])
        self.assertEqual([t["name"] for t in store.task_managers(self.conn)], ["alpha", "beta", "gamma"])

    def test_the_start_continues_the_first_unpaused_task_manager(self):
        self.assertEqual(store.start_manager(self.conn)["session"], SID_A)
        self.pause_project("alpha")
        self.assertEqual(store.start_manager(self.conn)["session"], SID_B)
        self.pause_project("beta")
        self.assertEqual(store.start_manager(self.conn)["session"], SID_A)   # all paused: its own pause holds it
        h = runner_jobs.HANDLERS["window_start"]
        self.assertEqual(runner_jobs._argv(h, "k")[1:3], ["--session", SID_A])
        with mock.patch.dict(os.environ, {"AFCLAUDE_MANAGER_SESSION": "not-read"}):
            self.pause_project("alpha", False)
            self.assertEqual(runner_jobs._argv(h, "k")[1:3], ["--session", SID_A])
            self.pause_project("alpha")
            self.pause_project("beta", False)
            self.assertEqual(runner_jobs._argv(h, "k")[1:3], ["--session", SID_B])

    def test_no_task_manager_at_all_is_an_error(self):
        for n in ("alpha", "beta"):
            store.update_project(self.conn, n, manager_session="")
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "no project has a task-manager session"):
            store.start_manager(self.conn)
        with self.assertRaisesRegex(ValueError, "no project has a task-manager session"):
            ka.start_session()
        self.assertEqual(ka.runner_pass("watch"), {"status": "ok", "note": "", "next_due": None})   # nothing to watch

    def test_a_registry_session_must_be_a_full_uuid(self):
        store.update_project(self.conn, "gamma", manager_session="00000000")
        self.conn.commit()
        with self.assertRaisesRegex(RuntimeError, "FULL session UUID"):
            ka.registry_sessions()

    def test_autonomous_sessions_are_the_registry_not_the_setting(self):
        ids = actions.autonomous_sessions(self.conn)
        self.assertLessEqual({SID_A, SID_B}, ids)
        self.assertNotIn(testenv.SID, ids)         # the machine setting alone makes nothing autonomous


class Ticks(Base):
    def test_a_tick_runs_over_every_session_with_its_own_lines_and_the_earliest_next_due(self):
        seen = []
        due = {SID_A: NOW + timedelta(minutes=9), SID_B: NOW + timedelta(minutes=4)}

        def watch(sid, now, args):
            seen.append((sid, args.session, args.arm))
            ka._WATCH_LINE = f"line of {sid[:8]}"            # what watch_pass leaves for its log-once
            return {"status": "ok", "note": f"n-{sid[:4]}", "next_due": due[sid].isoformat()}
        with mock.patch.dict(ka.TICKS, {"watch": watch}), mock.patch.object(ka, "_now", lambda: NOW):
            res = ka.runner_pass("watch")
        self.assertEqual(seen, [(SID_A, SID_A, True), (SID_B, SID_B, True)])   # rank order; gamma (no session) skipped
        self.assertEqual(res["next_due"], due[SID_B].isoformat())
        self.assertEqual(res["note"], "alpha aaaaaaaa: n-aaaa; beta bbbbbbbb: n-bbbb")
        self.assertEqual(ka._LINES[SID_A]["_WATCH_LINE"], "line of aaaaaaaa")   # kept per session
        self.assertEqual(ka._LINES[SID_B]["_WATCH_LINE"], "line of bbbbbbbb")

    def test_the_lines_are_logged_once_per_session_not_alternating(self):
        """watch_pass logs a line when it changes: one global would log both sessions' every minute."""
        def stall(sid, now):
            return "WAIT", f"quiet {sid[:4]}", None
        tick = lambda sid, now, args: (ka.watch_pass(sid, now, {}), {"status": "ok"})[1]   # noqa: E731
        with mock.patch.dict(ka.TICKS, {"watch": tick}), mock.patch.object(ka, "stall_status", stall), \
                mock.patch.object(ka, "_now", lambda: NOW):
            for _ in range(3):
                ka.runner_pass("watch")
        self.assertEqual(self.logs, ["WAIT: quiet aaaa", "WAIT: quiet bbbb"])

    def test_a_limit_hit_of_one_task_manager_does_not_touch_the_other(self):
        """D-204: the detection notes the hit of the session that has it only."""
        hits = []

        def stall(sid, now):
            return ("WAIT", "x", {"since": "s"}) if sid == SID_A else ("WAIT", "x", None)
        tick = lambda sid, now, args: (ka.watch_pass(sid, now, {}), {"status": "ok"})[1]   # noqa: E731
        with mock.patch.dict(ka.TICKS, {"watch": tick}), mock.patch.object(ka, "stall_status", stall), \
                mock.patch.object(ka, "note_limit_hit", lambda sid, stall, st, now: hits.append(sid)), \
                mock.patch.object(ka, "_now", lambda: NOW):
            ka.runner_pass("watch")
        self.assertEqual(hits, [SID_A])
        self.assertTrue(any("LIMIT_HIT" in ln for ln in self.logs))
        self.assertEqual(sum("LIMIT_HIT" in ln for ln in self.logs), 1)

    def test_the_compaction_check_stores_one_plan_per_session(self):
        """Each task-manager's due compaction is its own act <job>:<sid8>, under its own run key."""
        def plan(sid, now, args):
            return types.SimpleNamespace(key="compact-2026-10-29", reason="due"), ("path", {}, "head")
        with mock.patch.object(ka, "compaction_plan", plan), mock.patch.object(ka, "_now", lambda: NOW):
            ka.runner_pass("compaction_check")
        acts = ka.load_watch()["acts"]
        self.assertEqual(sorted(acts), [f"compaction:{SID_A[:8]}", f"compaction:{SID_B[:8]}"])
        a, b = acts[f"compaction:{SID_A[:8]}"], acts[f"compaction:{SID_B[:8]}"]
        self.assertEqual((a["key"], b["key"]), ("compact-2026-10-29", "compact-2026-10-29"))   # one dedup key ...
        self.assertNotEqual(a["run"], b["run"])                                                 # ... two run keys
        self.assertEqual((a["session"], b["session"]), (SID_A, SID_B))
        self.assertIn(SID_A[:8], a["run"])
        self.assertIsNotNone(testenv.act_of(ka, "compaction", SID_B))

    def test_a_pause_of_one_project_holds_only_its_jobs(self):
        """D-244: alpha's compaction check is held (and says so), beta's goes on to its own reading."""
        self.pause_project("alpha")
        with mock.patch.object(ka, "_now", lambda: NOW):
            ka.runner_pass("compaction_check")
        text = "\n".join(self.logs)
        self.assertIn("PAUSED (the project alpha), not starting the compaction of aaaaaaaa", text)
        self.assertNotIn("PAUSED (the project beta)", text)
        self.assertIn("compaction of bbbbbbbb: no transcript", text)       # beta's check ran
        self.assertNotIn("compaction of aaaaaaaa: no transcript", text)    # alpha's did not
        self.assertTrue(ka.automation_paused("x", SID_A))
        self.assertFalse(ka.automation_paused("x", SID_B))
        self.assertFalse(failstop_install_paused())


def failstop_install_paused():
    import failstop
    return failstop.paused()["paused"]


class Acts(Base):
    def store(self, job, sid, key, at):
        return ka.store_act(job, sid, key, at, NOW)

    def test_plans_are_keyed_per_session_and_dropped_per_session(self):
        key = "fillup-2026-10-30T02:00:00+00:00"
        at = NOW + timedelta(minutes=30)
        self.assertEqual(self.store("fillup", SID_A, key, at), at)
        self.assertEqual(self.store("fillup", SID_B, key, at + timedelta(minutes=1)), at + timedelta(minutes=1))
        acts = ka.load_watch()["acts"]
        self.assertEqual(sorted(acts), [f"fillup:{SID_A[:8]}", f"fillup:{SID_B[:8]}"])
        self.assertNotEqual(acts[f"fillup:{SID_A[:8]}"]["run"], acts[f"fillup:{SID_B[:8]}"]["run"])
        self.assertEqual(self.store("fillup", SID_A, None, None), None)           # A's plan goes away ...
        self.assertEqual(sorted(ka.load_watch()["acts"]), [f"fillup:{SID_B[:8]}"])   # ... B's stays
        self.assertEqual(runner_state.act_name("fillup", SID_B), f"fillup:{SID_B[:8]}")

    def test_an_act_runs_on_the_session_whose_plan_it_is(self):
        key = "compact-2026-10-29"
        self.store("compaction", SID_A, key, NOW)
        self.store("compaction", SID_B, key, NOW)
        ran = []

        def act(sid, now, args, k):
            ran.append((sid, args.session, k))
            return None, "done"
        run_b = ka.load_watch()["acts"][f"compaction:{SID_B[:8]}"]["run"]
        with mock.patch.dict(ka.ACTS, {"compaction": act}), mock.patch.object(ka, "_now", lambda: NOW):
            res = ka.runner_pass("compaction", run_b)
            self.assertEqual(res["note"], "done")
            self.assertEqual(ran, [(SID_B, SID_B, key)])
            self.assertEqual(sorted(ka.load_watch()["acts"]), [f"compaction:{SID_A[:8]}"])   # B's entry ended
            res = ka.runner_pass("compaction", run_b)                                         # its key again
            self.assertIn("its plan changed", res["note"])
            self.assertEqual(len(ran), 1)

    def test_a_session_that_left_the_registry_has_no_act(self):
        self.store("compaction", SID_A, "k", NOW)
        run = ka.load_watch()["acts"][f"compaction:{SID_A[:8]}"]["run"]
        store.update_project(self.conn, "alpha", manager_session="")
        self.conn.commit()
        with mock.patch.dict(ka.ACTS, {"compaction": lambda *a: (_ for _ in ()).throw(AssertionError("ran"))}):
            res = ka.runner_pass("compaction", run)
        self.assertIn("no task-manager has it planned", res["note"])


class Legacy(Base):
    """3a's legacy mode keeps `--session`; it must be a task-manager of the registry (§3.3, §3.4)."""
    def run_main(self, *argv):
        with mock.patch.object(sys, "argv", ["keepalive.py", *argv]), \
                mock.patch.object(ka, "fail_stop") as fs, \
                mock.patch.object(ka, "window_start_cli") as ws:
            try:
                ka.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, fs, ws

    def test_a_session_of_the_registry_runs(self):
        code, fs, ws = self.run_main("--session", SID_B, "--window-start")
        self.assertEqual(code, 0)
        ws.assert_called_once()
        fs.assert_not_called()

    def test_a_session_that_disagrees_fails_and_starts_nothing(self):
        code, fs, ws = self.run_main("--session", "cccccccc-0000-4000-8000-000000000003", "--window-start")
        self.assertEqual(code, 1)
        ws.assert_not_called()
        self.assertEqual(fs.call_args[0][0], "the keep-alive's --session disagrees with the task-manager registry")
        self.assertIn("cccccccc", fs.call_args[0][1])

    def test_run_stopped_needs_its_own_session(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            code, _, _ = self.run_main("--run-stopped")
        self.assertIn("--run-stopped needs the session", str(code))

    def test_the_runner_checks_the_setting_at_boot(self):
        r = runner.Runner(self.conn, owns=frozenset(), fail=lambda *a, **kw: fails.append((a, kw)),
                          log_fn=lambda s: None, sock=os.path.join(testenv.DIR, "r.sock"))
        fails = []
        self.assertTrue(r.check_registry(SID_A))
        self.assertTrue(r.check_registry(""))              # no setting any more: nothing to check
        self.assertEqual(fails, [])
        self.assertFalse(r.check_registry("cccccccc-0000-4000-8000-000000000003"))
        self.assertEqual(fails[0][0][1], "the manager_session setting disagrees with the task-manager registry")


if __name__ == "__main__":
    unittest.main()
