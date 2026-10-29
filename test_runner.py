#!/usr/bin/env python3
"""Offline tests for the resident runner (phase 3a S1, docs/phase3a_plan.md §3, §5 S1): runner.py
and runner_jobs.py, the runner_jobs / runner_runs tables and their actions.py writes.

A fake clock and a fake spawner for the planning, catch-up and recovery; real children for the
timeouts and exit codes (a `sleep` past its timeout is ended and recorded, a crash is reported,
exit 3 is not reported twice); real `runner.py serve` processes for the single-instance lock and
SIGTERM; the wake socket's coalescing; the outer > inner timeout check of the window start; the
cut-over check (RUNNER_OWNS vs docker/crontab)."""
import ast
import asyncio
import contextlib
import importlib.util
import io
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import actions  # noqa: E402
import runner  # noqa: E402
import runner_jobs  # noqa: E402
import runner_state  # noqa: E402
import schedule  # noqa: E402
import store  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
UTC = timezone.utc
MIN = timedelta(minutes=1)
CFG = schedule.Config.every_day()           # 23:00 x 2 x 5 h Europe/Berlin: 21:00Z and 02:00Z in October
START = datetime(2026, 10, 19, 21, 0, tzinfo=UTC)
OK_LINE = json.dumps({"status": "ok", "next_due": None, "note": ""})
OTHER_BOOT = "a" * 32
PRELUDE = ("import sys, notify, store; notify.push = lambda t: 'test: not sent'; "
           "store.NOTIFY = lambda s, b='': 'test: not sent'; ")


def ok() -> runner.ChildOutcome:
    return runner.ChildOutcome(0, OK_LINE + "\n", "", False)


class FakeSpawner:
    """Records each child it is asked for; returns the job's outcome, or waits on its gate."""
    def __init__(self):
        self.calls = []
        self.outcomes = {}
        self.gates = {}

    async def run(self, argv, timeout_s, on_pid):
        name = argv[3] if len(argv) > 3 and argv[2] == runner.RUNNER_JOBS else argv[0]
        self.calls.append((name, argv[5] if "--key" in argv else None, timeout_s))
        on_pid(1)                      # pid 1: never reaped at a later boot
        if name in self.gates:
            await self.gates[name].wait()
        return self.outcomes.get(name, ok())


class Base(unittest.IsolatedAsyncioTestCase):
    real_signals = False       # a test class that spawns its own children (and only those) sets True

    def setUp(self):
        # no test may signal a real process group: the fakes' pid 1 would be kill(-1, ...), every process of the user
        self.signalled = []
        if not self.real_signals:
            patcher = mock.patch.object(runner, "_signal_group", lambda pid, sig: self.signalled.append((pid, sig)))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp(prefix="afc-runner-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = store.connect(os.path.join(self.tmp, "t.db"), create=True)
        self.addCleanup(self.conn.close)
        self.fails, self.logs = [], []
        self.now = START
        self.spawner = FakeSpawner()

    def record_fail(self, component, subject, detail="", **kw):
        self.fails.append((component, subject, detail, kw.get("pause", True)))

    def runner(self, owns=(), jobs=None, **kw):
        kw.setdefault("spawner", self.spawner)
        kw.setdefault("supervise", {})     # S7: no supervised children unless a test asks for one
        return runner.Runner(self.conn, jobs=jobs, owns=frozenset(owns), clock=lambda: self.now,
                             log_fn=self.logs.append, fail=self.record_fail, schedule_cfg=CFG,
                             sock=os.path.join(self.tmp, "r.sock"), **kw)

    def subjects(self):
        return [f[1] for f in self.fails]

    def runs(self, job=None):
        return list(reversed(store.recent_runs(self.conn, 100, job)))

    def prev_boot(self, beat, clean=True):
        """A previous runner: its heartbeat at `beat` and its exit (clean or not)."""
        actions.runner_job_set(self.conn, "runner", actor="runner", via="runner", heartbeat_at=runner._iso(beat))
        actions.runner_state_put(self.conn, {"boot": {"boot_id": OTHER_BOOT, "pid": 4, "started_at": "x"},
                                             "clean_exit": clean}, actor="runner", via="runner")

    def next_due(self, job):
        row = store.get_runner_job(self.conn, job)
        return row and store.parse_iso(row["next_due"])

    async def run_due(self, r, due):
        run_id = r.start(due, self.now)
        task = r.tasks.get(due.job.name)
        if task is not None:
            await task
        return run_id


# ---------------------------------------------------------------- tables and actions

class Tables(Base):
    def test_unique_key_and_rerun(self):
        kw = dict(boot_id=OTHER_BOOT, job="window_start", key="window-start-2026-10-20", timeout_s=60,
                  actor="runner:window_start", via="runner")
        a, why = actions.run_begin(self.conn, **kw)
        self.assertIsNone(why)
        self.assertEqual(actions.run_begin(self.conn, **kw)[0], None)            # a keyed run can't start twice
        with self.assertRaises(sqlite3.IntegrityError):                          # also not around actions.py
            store.insert_run(self.conn, OTHER_BOOT, "window_start", kw["key"], "running", 60, "runner", "runner")
        actions.run_end(self.conn, a, "interrupted", actor="runner", via="runner")
        self.assertIsNone(actions.run_begin(self.conn, **kw, rerun=False)[0])    # a "fail" job: never again
        b, why = actions.run_begin(self.conn, **kw)                              # a "rerun" job: a new row
        self.assertIsNotNone(b)
        self.assertEqual([r["status"] for r in self.runs()], ["interrupted", "running"])
        self.assertEqual(actions.run_begin(self.conn, **dict(kw, key=None))[1], None)   # ticks: no key, no limit

    def test_a_run_leaves_running_once_and_deletes_only_through_the_prune(self):
        a, _ = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="sampler", key=None, timeout_s=60,
                                 actor="runner:sampler", via="runner")
        self.assertTrue(actions.run_end(self.conn, a, "ok", actor="runner:sampler", via="runner", result=OK_LINE))
        self.assertFalse(actions.run_end(self.conn, a, "failed", actor="runner:sampler", via="runner"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE runner_runs SET status='failed' WHERE id=?", (a,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM runner_runs WHERE id=?", (a,))
        with self.assertRaises(ValueError):
            actions.run_end(self.conn, a, "running", actor="runner", via="runner")
        with self.assertRaises(ValueError):
            actions.run_begin(self.conn, boot_id="not-hex", job="sampler", key=None, timeout_s=1, actor="runner",
                              via="runner")

    def test_prune(self):
        ids = []
        for _ in range(3):
            i, _ = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="sampler", key=None, timeout_s=60,
                                     actor="runner:sampler", via="runner")
            ids.append(i)
        self.conn.execute("UPDATE runner_runs SET started_at='2020-01-01T00:00:00Z' WHERE id IN (?, ?)", ids[:2])
        self.conn.commit()
        actions.run_end(self.conn, ids[0], "ok", actor="runner", via="runner")
        actions.run_end(self.conn, ids[2], "ok", actor="runner", via="runner")
        r = actions.perform(self.conn, "runner.prune", {}, actor="runner:prune_runs", via="runner")
        self.assertEqual(r["deleted"], 1)                             # old and finished only
        self.assertEqual({x["id"] for x in self.runs()}, {ids[1], ids[2]})   # an old running row stays
        self.assertEqual(store.audit_log(self.conn, target_type="runner_runs")[0]["action"], "runner.prune")
        with self.assertRaises(actions.Forbidden):
            actions.perform(self.conn, "runner.prune", {}, actor="mcp", via="mcp")

    def test_no_audit_row_per_run(self):
        """D-256: a run row carries actor and via; no audit_log row of its own."""
        before = len(store.audit_log(self.conn))
        a, _ = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="sampler", key=None, timeout_s=60,
                                 actor="runner:sampler", via="runner")
        actions.run_end(self.conn, a, "ok", actor="runner:sampler", via="runner")
        self.assertEqual(len(store.audit_log(self.conn)), before)
        self.assertEqual((self.runs()[0]["actor"], self.runs()[0]["via"]), ("runner:sampler", "runner"))

    def test_settings_match_the_jobs_and_the_window_start_timeout_exceeds_its_inner_ones(self):
        for name, job in runner.JOBS.items():
            self.assertEqual(actions.SETTINGS[actions.RUNNER_TIMEOUT + name].default(), job.timeout_min, name)
        lo = actions.SETTINGS["runner_timeout_window_start"].bounds[0]
        inner = runner_jobs.window_start_min_timeout()
        self.assertGreaterEqual(timedelta(minutes=lo), inner)          # an inner timeout always fires first
        self.assertGreater(inner, timedelta(minutes=60))               # COMPACT_LOCK_WAIT + VERIFY_TIMEOUT, ...

    def test_the_declared_host_call_timeouts_are_keepalives(self):
        with open(os.path.join(HERE, "keepalive.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        found = {}
        for f in ast.walk(tree):
            if isinstance(f, ast.FunctionDef):
                for n in ast.walk(f):
                    if isinstance(n, ast.keyword) and n.arg == "timeout" and isinstance(n.value, ast.Constant):
                        found.setdefault(f.name, set()).add(n.value.value)
        declared = {}
        for fn, secs, _ in runner_jobs.WINDOW_START_HOST_CALLS:
            declared.setdefault(fn, set()).add(secs)
        for fn, secs in declared.items():
            self.assertEqual(found.get(fn), secs, fn)

    def test_cut_over_one_owner_per_job(self):
        with open(os.path.join(HERE, "docker", "crontab"), encoding="utf-8") as fh:
            cron = [ln for ln in fh.read().splitlines() if ln.strip() and not ln.startswith("#")]
        for name in runner.RUNNER_OWNS:
            tag = runner.JOBS[name].cron_tag
            self.assertFalse(tag and any(tag in ln for ln in cron), f"{name} is run by cron AND the runner")
        for name, job in runner.JOBS.items():
            if job.cron_tag and name not in runner.RUNNER_OWNS:
                self.assertTrue(any(job.cron_tag in ln for ln in cron), f"{name}: no cron line tagged {job.cron_tag!r}")
        self.assertEqual(set(runner.JOBS), set(runner_jobs.HANDLERS) | set(runner_jobs.FUNCTIONS)
                         | set(runner_jobs.PASSES) | {runner_jobs.AT_JOB})

    def test_the_legacy_crontab_runs_every_job_and_no_runner(self):
        def lines(name):
            with open(os.path.join(HERE, "docker", name), encoding="utf-8") as fh:
                return [ln for ln in fh.read().splitlines() if ln.strip() and not ln.startswith("#")]
        legacy, cron = lines("crontab.legacy"), lines("crontab")
        for name, job in runner.JOBS.items():
            if job.cron_tag:
                self.assertTrue(any(job.cron_tag in ln for ln in legacy), name)
        self.assertFalse(any("runner.py" in ln for ln in legacy))
        self.assertEqual(cron, [])                      # S8: the runner is the only schedule, no watchdog line either
        # S7: the inbox API watchdog has no Job of its own (the runner supervises it directly,
        # runner.SUPERVISED), so it is not matched by a cron_tag; it is the one extra line removed
        # from `on` mode that no owned job's tag accounts for.
        tagged = {ln for ln in legacy if any(
            runner.JOBS[n].cron_tag in ln for n in runner.RUNNER_OWNS if runner.JOBS[n].cron_tag)}
        untagged = {ln for ln in legacy if "AFClaude inbox API watchdog" in ln}
        self.assertEqual(set(legacy) - set(cron), tagged | untagged)


# ---------------------------------------------------------------- boot: recovery and catch-up

class Boot(Base):
    def test_first_boot_is_quiet(self):
        r = self.runner(owns={"sampler"})
        r.boot()
        self.assertEqual(self.fails, [])
        self.assertEqual(self.next_due("sampler"), datetime(2026, 10, 19, 21, 15, tzinfo=UTC))
        st = actions.runner_state_get(self.conn)
        self.assertEqual((st["boot"]["boot_id"], st["clean_exit"]), (r.boot_id, False))
        self.assertIsNotNone(store.get_runner_job(self.conn, "runner")["heartbeat_at"])

    def test_unclean_exit_alerts_and_pauses(self):
        self.prev_boot(START - MIN, clean=False)
        self.runner().boot()
        self.assertEqual(self.fails[0][1:2] + self.fails[0][3:], ("the previous runner did not exit cleanly", True))

    def test_a_crash_the_runner_reported_itself_is_not_alerted_again(self):
        self.prev_boot(START - MIN, clean=False)
        actions.runner_state_put(self.conn, {"crashed": {"boot_id": OTHER_BOOT, "at": "x"}}, actor="runner",
                                 via="runner")
        self.runner().boot()
        self.assertEqual(self.fails, [])
        self.assertNotIn("crashed", actions.runner_state_get(self.conn))

    def test_missed_ticks_run_once_each_staggered(self):
        self.prev_boot(START - 3 * 60 * MIN)
        for job in ("sampler", "quickview"):
            actions.runner_job_set(self.conn, job, actor="runner", via="runner",
                                   next_due=runner._iso(START - 2 * 60 * MIN))
        r = self.runner(owns={"sampler", "quickview"})
        r.boot()
        self.assertEqual(self.fails, [])
        self.assertEqual([d.job.name for d in r.plan(self.now)], ["quickview"])
        self.now += runner.STAGGER
        self.assertEqual([d.job.name for d in r.plan(self.now)], ["quickview", "sampler"])  # (not started yet)

    async def test_missed_ticks_run_once(self):
        self.prev_boot(START - 3 * 60 * MIN)
        actions.runner_job_set(self.conn, "sampler", actor="runner", via="runner",
                               next_due=runner._iso(START - 2 * 60 * MIN))
        r = self.runner(owns={"sampler"})
        r.boot()
        for due in r.plan(self.now):
            await self.run_due(r, due)
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(self.next_due("sampler"), START + 15 * MIN)
        self.assertEqual([c[0] for c in self.spawner.calls], ["sampler"])

    def test_a_start_within_the_grace_runs_after_it_is_missed(self):
        for minutes, due, missed in ((10, True, False), (29, True, False), (31, False, True)):
            with self.subTest(minutes=minutes):
                self.setUp()
                self.prev_boot(START - 10 * MIN)
                self.now = START + minutes * MIN
                r = self.runner(owns={"window_start"})
                r.boot()
                plan = r.plan(self.now)
                self.assertEqual([(d.job.name, d.key) for d in plan],
                                 [("window_start", "window-start-2026-10-20")] if due else [])
                self.assertEqual(self.subjects(), ["missed session-window start"] if missed else [])
                if missed:
                    run, = self.runs("window_start")
                    self.assertEqual((run["status"], run["key"]), ("skipped", "window-start-2026-10-20"))
                    self.assertEqual(r.plan(self.now), [])              # reported once
                    self.assertEqual(len(self.fails), 1)

    def test_a_night_down_misses_the_first_start_and_runs_the_second(self):
        self.prev_boot(START - 10 * MIN)
        self.now = START + 5 * 60 * MIN + 10 * MIN                       # 02:10Z: the 04:00 Berlin start
        r = self.runner(owns={"window_start"})
        r.boot()
        self.assertEqual([d.key for d in r.plan(self.now)], ["window-start-2026-10-20-s2"])
        self.assertEqual(self.subjects(), ["missed session-window start"])

    def test_a_start_the_keepalive_handled_is_not_run_again(self):
        actions.state_save(self.conn, "keepalive", {"handled": {"window-start-2026-10-20": {"at": "x"}}},
                           mode="file", actor="runner:keepalive", via="runner")
        self.prev_boot(START - 10 * MIN)
        self.now = START + 10 * MIN
        r = self.runner(owns={"window_start"})
        r.boot()
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(self.fails, [])

    def test_no_start_planned_without_owning_it(self):
        self.prev_boot(START - 10 * MIN)
        self.now = START + 40 * MIN
        r = self.runner(owns={"sampler"})
        r.boot()
        self.assertEqual([d.job.name for d in r.plan(self.now)], [])
        self.assertEqual(self.fails, [])

    async def test_an_interrupted_tick_is_rerun_and_an_interrupted_start_fails(self):
        self.prev_boot(START - MIN)
        tick, _ = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="sampler", key=None, timeout_s=600,
                                    actor="runner:sampler", via="runner")
        start, _ = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="window_start",
                                     key="window-start-2026-10-20", timeout_s=4200, actor="runner:window_start",
                                     via="runner", session=True)
        actions.runner_job_set(self.conn, "sampler", actor="runner", via="runner", next_due=runner._iso(START + MIN))
        self.now = START + 5 * MIN
        r = self.runner(owns={"sampler", "window_start"})
        r.boot()
        self.assertEqual({x["id"]: x["status"] for x in self.runs()}, {tick: "interrupted", start: "interrupted"})
        self.assertEqual(self.subjects(), ["window_start interrupted by a runner restart"])
        self.assertTrue(self.fails[0][3])                               # alert + pause (D-257)
        self.assertEqual([d.job.name for d in r.plan(self.now)], ["sampler"])   # the start is never rerun

    async def test_an_interrupted_keyed_run_of_a_rerun_job_runs_again(self):
        job = runner.Job("t_keyed", 1, window_start=True, on_interrupt="rerun")
        self.now = START - 30 * MIN                                      # no window start due
        self.prev_boot(self.now - MIN)
        actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="t_keyed", key="k1", timeout_s=60,
                          actor="runner:t_keyed", via="runner")
        r = self.runner(owns={"t_keyed"}, jobs={"t_keyed": job})
        r.boot()
        due, = [d for d in r.plan(self.now) if d.key == "k1"]
        await self.run_due(r, due)
        self.assertEqual([x["status"] for x in self.runs("t_keyed")], ["interrupted", "ok"])


# ---------------------------------------------------------------- runs (fake spawner)

class Runs(Base):
    async def test_ok_failed_reported_timeout(self):
        r = self.runner(owns={"sampler"})
        r.boot()
        job = runner.JOBS["sampler"]
        cases = [(ok(), "ok", []),
                 (runner.ChildOutcome(1, "", "Traceback ...\nValueError: x", False), "failed", ["sampler failed"]),
                 (runner.ChildOutcome(3, "", "handled", False), "failed", []),
                 (runner.ChildOutcome(-9, "", "", True), "timeout", ["sampler timed out"]),
                 (runner.ChildOutcome(0, "no json here\n", "", False), "failed", ["sampler failed"])]
        for outcome, status, subjects in cases:
            with self.subTest(status=status, rc=outcome.returncode):
                self.fails.clear()
                self.spawner.outcomes["sampler"] = outcome
                run_id = await self.run_due(r, runner.Due(job))
                row = store.get_run(self.conn, run_id)
                self.assertEqual((row["status"], row["pid"], row["timeout_s"]), (status, 1, 1200.0))
                self.assertEqual(self.subjects(), subjects)
        jr = store.get_runner_job(self.conn, "sampler")
        self.assertEqual((jr["last_status"], jr["consecutive_failures"]), ("failed", 4))
        self.assertIsNotNone(jr["last_ok_at"])

    async def test_quickview_failure_alerts_without_a_pause(self):
        r = self.runner(owns={"quickview"})
        self.spawner.outcomes["quickview"] = runner.ChildOutcome(1, "", "boom", False)
        await self.run_due(r, runner.Due(runner.JOBS["quickview"]))
        self.assertEqual([(f[1], f[3]) for f in self.fails], [("quickview failed", False)])

    async def test_timeout_comes_from_the_setting(self):
        testenv.set_setting("runner_timeout_sampler", 3, db=os.path.join(self.tmp, "t.db"))
        r = self.runner(owns={"sampler"})
        await self.run_due(r, runner.Due(runner.JOBS["sampler"]))
        self.assertEqual(self.spawner.calls[-1][2], 180.0)

    async def test_overrun_skips_the_tick_and_logs_once(self):
        r = self.runner(owns={"stall_scan"})
        self.spawner.gates["stall_scan"] = asyncio.Event()
        actions.runner_job_set(self.conn, "stall_scan", actor="runner", via="runner", next_due=runner._iso(self.now))
        due, = r.plan(self.now)
        r.start(due, self.now)
        self.now += 4 * MIN
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(sum("still running at its next tick" in ln for ln in self.logs), 1)
        self.spawner.gates["stall_scan"].set()
        await r.tasks["stall_scan"]
        self.assertEqual([c[0] for c in self.spawner.calls], ["stall_scan"])

    async def test_a_skip_job_is_recorded_skipped_while_paused(self):
        job = runner.Job("t_skip", 1, every=MIN, paused="skip")
        r = self.runner(owns={"t_skip"}, jobs={"t_skip": job}, paused=lambda: {"paused": True, "reason": "an error"})
        self.assertIsNone(r.start(runner.Due(job), self.now))
        run, = self.runs("t_skip")
        self.assertEqual((run["status"], run["error"]), ("skipped", "automation is paused: an error"))
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(self.next_due("t_skip"), START + MIN)

    async def test_a_drain_holds_a_session_run_until_it_is_missed(self):
        self.prev_boot(START - 10 * MIN)
        until = datetime.now(UTC) + timedelta(days=1)
        self.now = START + 5 * MIN
        r = self.runner(owns={"window_start"})
        r.boot()                                                         # (a boot lifts a drain: set it after)
        actions.runner_state_put(self.conn, {"drain": {"until": runner._iso(until)}}, actor="cli", via="cli")
        due, = r.plan(self.now)
        self.assertIsNone(r.start(due, self.now))                        # drained: not started, not settled
        self.now = START + 20 * MIN
        due, = r.plan(self.now)
        self.assertIsNone(r.start(due, self.now))
        self.now = START + 31 * MIN
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(self.subjects(), ["missed session-window start"])
        self.assertEqual(self.spawner.calls, [])

    async def test_a_window_start_runs_once_with_its_key(self):
        self.prev_boot(START - 10 * MIN)
        self.now = START + 2 * MIN
        r = self.runner(owns={"window_start"})
        r.boot()
        due, = r.plan(self.now)
        await self.run_due(r, due)
        self.assertEqual(self.spawner.calls, [("window_start", "window-start-2026-10-20", 4800.0)])
        self.now += 5 * MIN
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(self.next_due("window_start"), START + 5 * 60 * MIN)
        self.now = START + 5 * 60 * MIN
        self.assertEqual([d.key for d in r.plan(self.now)], ["window-start-2026-10-20-s2"])


# ---------------------------------------------------------------- the loop: wake, coalescing, stop

class Loop(Base):
    async def wait_for(self, cond, timeout=5.0):
        t = time.monotonic()
        while not cond():
            if time.monotonic() - t > timeout:
                self.fail_test("condition not reached")
            await asyncio.sleep(0.01)

    def fail_test(self, msg):
        raise AssertionError(msg)

    async def test_100_datagrams_during_a_run_cause_one_rescan(self):
        r = self.runner(owns={"stall_scan"})
        r.boot()
        actions.runner_job_set(self.conn, "stall_scan", actor="runner", via="runner", next_due=runner._iso(self.now))
        self.spawner.gates["stall_scan"] = asyncio.Event()
        serve = asyncio.ensure_future(r.serve(install_signals=False))
        await self.wait_for(lambda: "stall_scan" in r.tasks)
        await asyncio.sleep(0.05)
        scans = r.scans
        # the senders never block: a full queue (the container's net.unix.max_dgram_qlen is 10)
        # counts as sent, the runner has wakes waiting already
        self.assertTrue(all([runner.wake(r.sock) for _ in range(100)]))
        await self.wait_for(lambda: r.wakes >= 1)
        await asyncio.sleep(0.1)
        self.assertLessEqual(r.wakes, 100)
        self.assertEqual(r.scans, scans + 1)
        self.assertTrue(runner.wake(r.sock))                           # the sender's side
        await self.wait_for(lambda: r.scans == scans + 2)
        self.spawner.gates["stall_scan"].set()
        r.request_stop()
        self.assertEqual(await serve, 0)
        self.assertTrue(actions.runner_state_get(self.conn)["clean_exit"])
        self.assertFalse(os.path.exists(r.sock))

    async def test_sigterm_lets_a_run_finish_within_the_grace(self):
        r = self.runner(owns={"stall_scan"})
        r.boot()
        actions.runner_job_set(self.conn, "stall_scan", actor="runner", via="runner", next_due=runner._iso(self.now))
        self.spawner.gates["stall_scan"] = asyncio.Event()
        serve = asyncio.ensure_future(r.serve(install_signals=False))
        await self.wait_for(lambda: "stall_scan" in r.tasks)
        r.request_stop()
        await asyncio.sleep(0.05)
        self.spawner.gates["stall_scan"].set()
        self.assertEqual(await serve, 0)
        self.assertEqual([x["status"] for x in self.runs("stall_scan")], ["ok"])
        self.assertEqual(len(self.spawner.calls), 1)                    # no new run after the stop

    async def test_a_run_past_the_stop_grace_is_left_for_the_next_boot(self):
        r = self.runner(owns={"stall_scan"}, stop_grace=0.2)
        r.boot()
        actions.runner_job_set(self.conn, "stall_scan", actor="runner", via="runner", next_due=runner._iso(self.now))
        self.spawner.gates["stall_scan"] = asyncio.Event()
        serve = asyncio.ensure_future(r.serve(install_signals=False))
        await self.wait_for(lambda: "stall_scan" in r.tasks)
        r.request_stop()
        self.assertEqual(await serve, 0)
        self.assertEqual([x["status"] for x in self.runs("stall_scan")], ["running"])
        self.assertTrue(actions.runner_state_get(self.conn)["clean_exit"])
        r2 = self.runner(owns={"stall_scan"})
        r2.boot()                                                       # interrupted, rerun (a tick)
        self.assertEqual([x["status"] for x in self.runs("stall_scan")], ["interrupted"])
        self.assertEqual(self.fails, [])
        self.assertEqual([d.job.name for d in r2.plan(self.now)], ["stall_scan"])

    async def test_a_blocking_job_trips_the_watchdog_while_it_blocks(self):
        """S8, in the loop itself: a synchronous sleep in a job's spawner blocks the event loop; the
        watchdog thread (exit injected: no process is ended here) fires during the block, not after."""
        marks = {}

        class Blocking:
            async def run(self, argv, timeout_s, on_pid):
                time.sleep(0.8)                                    # blocks the loop
                marks["unblocked"] = time.monotonic()
                return ok()

        r = self.runner(owns={"stall_scan"}, spawner=Blocking())
        r.boot()
        actions.runner_job_set(self.conn, "stall_scan", actor="runner", via="runner", next_due=runner._iso(self.now))
        exits = []
        wd = runner.Watchdog(lambda: r.beat_mono, stale=0.3, every=0.05,
                             exit_fn=lambda code: exits.append((code, time.monotonic())), log_fn=self.logs.append)
        wd.start()
        serve = asyncio.ensure_future(r.serve(install_signals=False))
        await self.wait_for(lambda: "unblocked" in marks)
        wd.join(5)
        self.assertEqual([c for c, _ in exits], [runner.WATCHDOG_EXIT])
        self.assertLess(exits[0][1], marks["unblocked"])           # it did not wait for the loop to come back
        self.assertTrue(any("has not passed" in m for m in self.logs), self.logs)
        r.request_stop()
        self.assertEqual(await serve, 0)

    async def test_a_runner_write_failure_crashes_the_loop(self):
        r = self.runner(owns={"stall_scan"})
        r.boot()
        actions.runner_job_set(self.conn, "stall_scan", actor="runner", via="runner", next_due=runner._iso(self.now))
        with mock.patch.object(actions, "run_end", side_effect=store.DBUnavailable("database locked", "locked")):
            with self.assertRaises(store.DBError):
                await asyncio.wait_for(r.serve(install_signals=False), 5)


# ---------------------------------------------------------------- real children

class Children(Base):
    real_signals = True        # real children of this test (SubprocessSpawner), each in its own group

    def job(self, name, code, timeout_s=10.0):
        return runner.Job(name, timeout_s / 60, every=MIN, argv=(sys.executable, "-c", code))

    async def run_real(self, job):
        r = self.runner(owns={job.name}, jobs={job.name: job}, spawner=runner.SubprocessSpawner(term_grace=1.0))
        run_id = await self.run_due(r, runner.Due(job))
        return store.get_run(self.conn, run_id)

    async def test_a_sleep_past_its_timeout_is_ended_and_recorded(self):
        t = time.monotonic()
        row = await self.run_real(self.job("t_sleep", "import time; time.sleep(60)", timeout_s=1.0))
        self.assertLess(time.monotonic() - t, 10)
        self.assertEqual(row["status"], "timeout")
        self.assertIn("timed out after 1 s", row["error"])
        self.assertEqual(self.subjects(), ["t_sleep timed out"])
        with self.assertRaises(ProcessLookupError):
            os.killpg(row["pid"], 0)                                     # its group is gone

    async def test_a_child_ignoring_sigterm_is_killed(self):
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        row = await self.run_real(self.job("t_stubborn", code, timeout_s=0.5))
        self.assertEqual(row["status"], "timeout")

    async def test_a_crash_is_reported(self):
        row = await self.run_real(self.job("t_crash", "raise ValueError('boom')"))
        self.assertEqual(row["status"], "failed")
        self.assertIn("ValueError: boom", row["error"])
        self.assertEqual(self.subjects(), ["t_crash failed"])

    async def test_exit_3_is_not_reported_twice(self):
        row = await self.run_real(self.job("t_handled", "import sys; sys.stderr.write('reported'); sys.exit(3)"))
        self.assertEqual((row["status"], self.fails), ("failed", []))
        self.assertIn("reported", row["error"])

    async def test_ok_with_its_result_line(self):
        nd = runner._iso(START + 30 * 60 * MIN)
        code = f"print('{{\"status\": \"ok\", \"next_due\": \"{runner._iso(START + 90 * 60 * MIN)}\", \"note\": \"hi\"}}')"
        actions.runner_job_set(self.conn, "t_ok", actor="runner", via="runner", next_due=nd)
        row = await self.run_real(self.job("t_ok", code))
        self.assertEqual((row["status"], json.loads(row["result"])["note"]), ("ok", "hi"))
        self.assertEqual(self.next_due("t_ok"), START + MIN)             # its own tick is earlier


class SignalGuard(unittest.TestCase):
    def test_pids_that_would_signal_everything_raise_without_signalling(self):
        for pid in (1, 0, -1, True):
            with self.subTest(pid=pid), mock.patch.object(os, "killpg") as killpg, mock.patch.object(os, "kill") as kill:
                with self.assertRaises(ValueError):
                    runner._signal_group(pid, signal.SIGTERM)
                with self.assertRaises(ValueError):
                    runner.stop(pids=lambda pid=pid: [pid], out=lambda m: None)
                killpg.assert_not_called()
                kill.assert_not_called()

    def test_a_real_child_pid_is_signalled(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(p.wait)
        runner._signal_group(p.pid, signal.SIGKILL)
        self.assertEqual(p.wait(10), -signal.SIGKILL)


class ChildSide(unittest.TestCase):
    """runner_jobs.py itself: the result line, the exit codes, the lock, the log file."""
    def run_job(self, script, lock=""):
        path = os.path.join(testenv.DIR, "t_script.py")
        with open(path, "w") as fh:
            fh.write(script)
        code = PRELUDE + ("import runner_jobs as r; r.HANDLERS['t'] = r.Handler(sys.argv[1], (), 't.log', "
                          "sys.argv[2] or None); sys.exit(r.main(['t']))")
        return subprocess.run([sys.executable, "-c", code, path, lock], cwd=HERE, capture_output=True, text=True,
                              timeout=60)

    def test_ok_prints_one_result_line_and_logs_to_the_file(self):
        p = self.run_job("print('hello from the handler')\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "ok")
        with open(os.path.join(testenv.DIR, "t.log")) as fh:
            self.assertIn("hello from the handler", fh.read())

    def test_a_failure_the_handler_reported_exits_3(self):
        p = self.run_job("import failstop\nfailstop.fail('test', 'boom', 'x', pause=False, alert=lambda s, b='': None)\n")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(p.stdout, "")
        self.assertIn("reported its failure itself: boom", p.stderr)

    def test_a_crash_and_a_nonzero_exit(self):
        p = self.run_job("raise RuntimeError('kaputt')\n")
        self.assertEqual(p.returncode, 1)
        self.assertIn("RuntimeError: kaputt", p.stderr)
        self.assertEqual(self.run_job("import sys\nsys.exit(5)\n").returncode, 5)
        self.assertEqual(self.run_job("import sys\nsys.exit(3)\n").returncode, 1)   # 3 only when reported

    def test_a_held_lock_skips(self):
        fd = os.open(os.path.join(testenv.DIR, ".t.lock"), os.O_WRONLY | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            p = self.run_job("raise SystemExit('must not run')\n", lock=".t.lock")
        finally:
            os.close(fd)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "skipped")

    def test_unknown_job(self):
        self.assertEqual(runner_jobs.main(["nope"]), runner_jobs.EXIT_USAGE)


# ---------------------------------------------------------------- real runner processes

class Process(unittest.TestCase):
    def setUp(self):
        testenv.unpause()
        self.addCleanup(testenv.unpause)
        conn = store.connect()
        try:
            actions.runner_state_put(conn, {"boot": None, "clean_exit": None, "crashed": None, "drain": None},
                                     actor="runner", via="runner")
        finally:
            conn.close()

    def popen(self, extra=""):
        """A real `.../runner.py serve` (a shim of that name, so runner_pids() sees it as /proc
        does a live one) that owns nothing: no stall scan of the real transcripts mid-test
        (`extra`: code that runs before main, e.g. to give it a job)."""
        d = tempfile.mkdtemp(prefix="afc-runner-shim-", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        shim = os.path.join(d, "runner.py")
        with open(shim, "w", encoding="utf-8") as fh:
            fh.write(f"import sys\nsys.path.insert(0, {HERE!r})\n" + PRELUDE
                     + "import runner; runner.RUNNER_OWNS = frozenset()\n" + extra
                     + "sys.exit(runner.main(sys.argv[1:]))\n")
        p = subprocess.Popen([sys.executable, "-u", shim, "serve"], cwd=HERE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=dict(os.environ, KEEPALIVE_PROJECTS_DIR=d))
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.stderr.close)
        return p

    def booted(self, p):
        """Wait until the runner `p` has booted (its boot, heartbeat and socket)."""
        t = time.monotonic()
        while True:
            conn = store.connect()
            try:
                st = actions.runner_state_get(conn)
                beat = store.get_runner_job(conn, "runner")
            finally:
                conn.close()
            if (st.get("boot") or {}).get("pid") == p.pid and beat and os.path.exists(runner.sock_path()):
                return
            self.assertIsNone(p.poll(), p.stderr.read() if p.poll() is not None else "")
            self.assertLess(time.monotonic() - t, 30, "the runner did not boot")
            time.sleep(0.1)

    def test_two_runners_one_exits(self):
        fd = runner.acquire_lock()
        self.assertIsNotNone(fd)
        try:
            p = self.popen()
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, runner.EXIT_LOCKED, err)
            self.assertIn("another runner holds", out)
        finally:
            os.close(fd)

    def test_sigterm_exits_cleanly(self):
        p = self.popen()
        try:
            self.booted(p)
            self.assertIsNone(runner.acquire_lock())                    # it holds the lock
            p.send_signal(signal.SIGTERM)
            out, err = p.communicate(timeout=30)
        finally:
            if p.poll() is None:
                p.terminate()
                p.wait(10)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("clean exit", out)
        conn = store.connect()
        try:
            self.assertTrue(actions.runner_state_get(conn)["clean_exit"])
        finally:
            conn.close()

    def test_a_blocked_loop_makes_the_watchdog_exit_the_process(self):
        """S8: the only watchdog. A job whose child call blocks the asyncio loop synchronously (here: a
        time.sleep in the spawner's coroutine) stops the loop's heartbeat; the watchdog thread then ends
        the whole process with WATCHDOG_EXIT (compose's `restart: unless-stopped` starts the container
        again) without a clean exit, and a stuck run is left `running` for the next boot to record."""
        extra = ("import time\n"
                 "runner.STALE_LOOP, runner.WATCHDOG_EVERY = 1.0, 0.2\n"
                 "runner.RUNNER_OWNS = frozenset({'stall_scan'})\n"
                 "async def blocked(self, argv, timeout_s, on_pid):\n"
                 "    time.sleep(3600)\n"
                 "runner.SubprocessSpawner.run = blocked\n")
        conn = store.connect()
        try:
            actions.runner_job_set(conn, "stall_scan", actor="runner", via="runner",
                                   next_due=runner._iso(runner._utcnow() - timedelta(minutes=1)))
        finally:
            conn.close()
        p = self.popen(extra)
        try:
            out, err = p.communicate(timeout=60)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(10)
        self.assertEqual(p.returncode, runner.WATCHDOG_EXIT, err)
        self.assertIn("the loop has not passed for more than 1 s", out)
        conn = store.connect()
        try:
            self.assertIsNot(actions.runner_state_get(conn).get("clean_exit"), True)
            self.assertEqual([r["status"] for r in store.running_runs(conn) if r["job"] == "stall_scan"], ["running"])
            fails = []                                        # the restarted container's boot: D-257, alert + pause
            runner.Runner(conn, owns=frozenset(), log_fn=lambda m: None, shadow=lambda m: None,
                          fail=lambda component, subject, detail="", **kw: fails.append(subject)).boot()
            self.assertEqual(fails, ["the previous runner did not exit cleanly"])
            self.assertEqual(store.running_runs(conn), [])    # and the cut-off run is recorded interrupted
        finally:
            conn.close()
        fd = runner.acquire_lock()
        self.assertIsNotNone(fd)                              # the lock went with the process
        os.close(fd)

    def test_stop_ends_the_runner_cleanly(self):
        p = self.popen()
        out = []
        try:
            self.booted(p)
            mine = lambda: [x for x in runner.runner_pids() if x == p.pid]   # noqa: E731 - never another runner
            self.assertEqual(mine(), [p.pid])
            self.assertEqual(runner.stop(30, pids=mine, out=out.append), 0)
            p.wait(10)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(10)
        self.assertEqual(p.returncode, 0)
        self.assertIn("stopped", out[-1])
        conn = store.connect()
        try:
            self.assertTrue(actions.runner_state_get(conn)["clean_exit"])
        finally:
            conn.close()
        self.assertEqual(runner.stop(pids=lambda: [], out=out.append), 0)
        self.assertIn("none running", out[-1])


# ---------------------------------------------------------------- drain, status, watchdog, watcher check

class Misc(Base):
    def test_drain_waits_for_a_running_session_run(self):
        run_id, _ = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="window_start", key="k", timeout_s=60,
                                      actor="runner:window_start", via="runner", session=True)
        t = [datetime.now(UTC)]
        out = []

        def sleep(s):
            t[0] += timedelta(seconds=s)
        with mock.patch.object(runner, "wake", return_value=False):
            self.assertEqual(runner.drain(self.conn, timedelta(seconds=20), clock=lambda: t[0], sleep=sleep,
                                          out=out.append), 1)
            self.assertIn("NOT drained", out[-1])
            d = actions.runner_drain(self.conn)
            self.assertIsNotNone(d)                                      # held until the longest timeout + 5 min
            self.assertIsNone(actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="window_start", key="k2",
                                                timeout_s=60, actor="runner", via="runner", session=True)[0])
            self.assertIsNotNone(actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="sampler", key=None,
                                                   timeout_s=60, actor="runner", via="runner")[0])
            actions.run_end(self.conn, run_id, "ok", actor="runner", via="runner")
            self.assertEqual(runner.drain(self.conn, clock=lambda: t[0], sleep=sleep, out=out.append), 0)
            runner.undrain(self.conn)
        self.assertIsNone(actions.runner_drain(self.conn))

    def test_a_boot_lifts_the_drain(self):
        until = runner._iso(datetime.now(UTC) + timedelta(hours=1))
        actions.runner_state_put(self.conn, {"drain": {"until": until}}, actor="cli", via="cli")
        self.runner().boot()
        self.assertIsNone(actions.runner_drain(self.conn))

    def test_status(self):
        r = self.runner(owns={"sampler"})
        r.boot()
        s = runner.status(self.conn, self.now)
        self.assertEqual(s["heartbeat_age_s"], 0)
        self.assertEqual({j["job"] for j in s["jobs"]}, set(runner.JOBS))
        with mock.patch("builtins.print") as p:
            runner.print_status(s)
        self.assertIn("heartbeat:", p.call_args_list[0][0][0])

    def test_watchdog(self):
        beat = [100.0]
        exits = []
        w = runner.Watchdog(lambda: beat[0], stale=300, every=0.01, exit_fn=exits.append, log_fn=self.logs.append)
        self.assertFalse(w.stuck(now=399))
        self.assertTrue(w.stuck(now=401))
        beat[0] = time.monotonic() - 301
        w.start()
        w.join(5)
        self.assertEqual(exits, [runner.WATCHDOG_EXIT])
        self.assertIn("has not passed", self.logs[-1])

    def test_health(self):
        r = self.runner(owns={"sampler"})
        r.boot()
        on, off = {"AFCLAUDE_SCHEDULER": "runner"}, {"AFCLAUDE_SCHEDULER": "legacy"}
        live = lambda: [7]   # noqa: E731
        self.assertIsNone(runner.health(self.conn, on, self.now, pids=live))
        self.assertIsNone(runner.health(self.conn, {"AFCLAUDE_SCHEDULER": "on"}, self.now, pids=live))
        self.assertIn("no runner process", runner.health(self.conn, on, self.now, pids=lambda: []))
        late = self.now + runner.HEALTH_STALE + timedelta(seconds=1)
        self.assertIn("heartbeat is", runner.health(self.conn, on, late, pids=live))
        self.assertIsNone(runner.health(self.conn, on, late - timedelta(seconds=2), pids=live))
        # the other modes have no runner to check
        self.assertIsNone(runner.health(self.conn, off, late, pids=lambda: []))
        self.assertIsNone(runner.health(self.conn, {}, late, pids=lambda: []))
        conn2 = store.connect(os.path.join(self.tmp, "empty.db"), create=True)
        self.addCleanup(conn2.close)
        self.assertEqual(runner.health(conn2, on, self.now, pids=live), "the runner has no heartbeat")

    def test_health_cli_reports_and_fails(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AFCLAUDE_SCHEDULER": "legacy"}), contextlib.redirect_stdout(out):
            self.assertEqual(runner.main(["health"]), 0)
        self.assertEqual(out.getvalue().strip(), "healthy")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AFCLAUDE_SCHEDULER": "runner"}), \
                mock.patch.object(runner, "runner_pids", return_value=[]), contextlib.redirect_stdout(out):
            self.assertEqual(runner.main(["health"]), 1)
        self.assertTrue(out.getvalue().startswith("unhealthy:"), out.getvalue())
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"AFCLAUDE_SCHEDULER": "runner"}), contextlib.redirect_stdout(out), \
                mock.patch.object(store, "connect", side_effect=store.DBUnavailable("database locked", "locked")):
            self.assertEqual(runner.main(["health"]), 1)      # an unreadable DB is never "healthy" (D-235)
        self.assertIn("health check failed", out.getvalue())

    def test_watchdog_and_health_bounds(self):
        """The watchdog must outlast the loop's longest idle sleep and the health check must fail first;
        the stop grace must fit compose's stop_grace_period."""
        self.assertGreater(runner.STALE_LOOP, 2 * runner.MAX_SLEEP)
        self.assertGreater(runner.HEALTH_STALE.total_seconds(), runner.MAX_SLEEP + runner.HEARTBEAT_EVERY.total_seconds())
        self.assertLess(runner.HEALTH_STALE.total_seconds(), runner.STALE_LOOP)
        self.assertNotEqual(runner.WATCHDOG_EXIT, runner.EXIT_REPORTED)
        self.assertNotIn(runner.WATCHDOG_EXIT, (0, runner.EXIT_LOCKED, runner.EXIT_CRASH))
        with open(os.path.join(HERE, "docker", "compose.yml"), encoding="utf-8") as fh:
            compose = fh.read()
        self.assertIn("restart: unless-stopped", compose)
        self.assertRegex(compose, r"(?m)^    stop_grace_period: 90s$")
        self.assertLess(runner.STOP_GRACE + 5, 90)
        self.assertRegex(compose, r'(?m)^      test: \["CMD", "python3", "runner.py", "health"\]$')
        interval = int(re.search(r"(?m)^      interval: (\d+)s$", compose).group(1))
        start = int(re.search(r"(?m)^      start_period: (\d+)s$", compose).group(1))
        self.assertGreaterEqual(start, 2 * runner.MAX_SLEEP)
        self.assertLessEqual(interval, runner.HEALTH_STALE.total_seconds())

    def test_the_runner_modes_run_no_cron(self):
        """S8: `runner` and `on` exec the runner alone; only `legacy` runs supercronic."""
        with open(os.path.join(HERE, "docker", "entrypoint.sh"), encoding="utf-8") as fh:
            sh = fh.read()
        self.assertEqual(runner.RUNNER_MODES, ("on", "runner"))
        self.assertIn("on|runner|legacy)", sh)
        self.assertEqual(sh.count("exec python3 -u \"$D/runner.py\" serve"), 1)
        self.assertNotIn("runner.py\" stop", sh)
        self.assertNotIn("&\n    CRON=", sh)

    def test_watcher_conflict_only_once_it_owns_a_watcher_pass(self):
        def boom():
            raise OSError("no bridge")
        self.assertIsNone(runner.watcher_conflict(frozenset({"sampler"}), local=lambda: [5], on_host=boom))
        own = frozenset({"watch"})
        self.assertIn("runs here", runner.watcher_conflict(own, local=lambda: [5], on_host=lambda: []))
        self.assertIn("unavailable", runner.watcher_conflict(own, local=lambda: [], on_host=boom))
        self.assertIn("HOST", runner.watcher_conflict(own, local=lambda: [], on_host=lambda: [7]))
        self.assertIsNone(runner.watcher_conflict(own, local=lambda: [], on_host=lambda: []))
        self.assertTrue(runner._is_watcher(["python3", "-u", "/x/keepalive.py", "--session", "u", "--arm"]))
        self.assertFalse(runner._is_watcher(["python3", "keepalive.py", "--session", "u", "--window-start"]))

    def test_next_tick(self):
        t = datetime(2026, 10, 19, 23, 59, 30, tzinfo=UTC)
        self.assertEqual(runner.next_tick(runner.JOBS["sampler"], t), datetime(2026, 10, 20, 0, 0, tzinfo=UTC))
        self.assertEqual(runner.next_tick(runner.JOBS["prune_logs"], t), datetime(2026, 10, 20, 3, 17, tzinfo=UTC))
        self.assertEqual(runner.next_tick(runner.JOBS["usage_review"], START), START + 60 * MIN)

    def test_job_validation(self):
        with self.assertRaises(ValueError):
            runner.Job("runner", 1, every=MIN)
        with self.assertRaises(ValueError):
            runner.Job("x", 1, every=MIN, daily=(1, 1))
        with self.assertRaises(ValueError):
            runner.Runner(self.conn, owns=frozenset({"nope"}))



# ---------------------------------------------------------------- S2: offset, ownership, shadow, ensure, stop

SEC = timedelta(seconds=1)
BERLIN_PREFIX = "[2026-10-19 23:00:00 CEST] "


def cron_line(t, tag):
    return (f'time="{t:%Y-%m-%dT%H:%M:%SZ}" level=info msg=starting iteration=1 job.command="x >> y 2>&1 # {tag}" '
            'job.position=1 job.schedule="* * * * *"')


class Shadow(Base):
    def test_the_stall_scan_runs_90_s_off_the_minute(self):
        j = runner.JOBS["stall_scan"]
        self.assertEqual(runner.next_tick(j, datetime(2026, 10, 19, 23, 59, tzinfo=UTC)),
                         datetime(2026, 10, 20, 0, 1, 30, tzinfo=UTC))
        self.assertEqual(runner.next_tick(j, datetime(2026, 10, 20, 0, 1, 30, tzinfo=UTC)),
                         datetime(2026, 10, 20, 0, 4, 30, tzinfo=UTC))
        self.assertEqual(runner.next_tick(j, datetime(2026, 10, 20, 0, 0, 10, tzinfo=UTC)),
                         datetime(2026, 10, 20, 0, 1, 30, tzinfo=UTC))
        with self.assertRaises(ValueError):
            runner.Job("x", 1, every=MIN, offset=MIN)
        with self.assertRaises(ValueError):
            runner.Job("x", 1, daily=(1, 1), offset=SEC)


    def test_shadow_lines_for_the_jobs_cron_runs(self):
        lines = []
        self.now = START - 2 * MIN
        r = self.runner(owns={"stall_scan", "watch"}, shadow=lines.append)
        self.assertEqual([j.name for j in r.shadow_jobs],
                         ["dispatcher", "docs_import", "prune_logs", "quickview", "sampler", "usage_review",
                          "window_start"])            # cron's jobs; not the owned stall scan / watch, not prune_runs
        r.shadow_pass(self.now)
        self.assertEqual(lines, [])
        self.assertEqual(r.shadow_next["quickview"], START)
        self.assertEqual(r.shadow_next["window_start"], START)
        self.assertEqual(r.shadow_next["prune_logs"], datetime(2026, 10, 20, 3, 17, tzinfo=UTC))
        self.assertEqual(r.sleep_for(START - 30 * SEC), 30.0)          # it wakes for a shadow time too
        self.now = START + timedelta(milliseconds=12)
        r.shadow_pass(self.now)
        key = schedule.start_key(START, CFG)
        self.assertEqual(lines, [f"{n} due 2026-10-19T21:00:00.000Z at 2026-10-19T21:00:00.012Z" for n in
                                 ("dispatcher", "docs_import", "quickview", "sampler", "usage_review")]
                         + [f"window_start {key} due 2026-10-19T21:00:00.000Z at 2026-10-19T21:00:00.012Z"])
        r.shadow_pass(self.now)
        self.assertEqual(len(lines), 6)                                 # once per time
        self.assertEqual(r.shadow_next["quickview"], START + 3 * MIN)
        self.assertEqual(r.shadow_next["window_start"], START + 5 * 60 * MIN)
        self.assertEqual(self.runner(owns={"stall_scan"}).shadow_jobs, [])   # no writer: no shadow

    def entries(self, quickview_until=30, window_start=True):
        lines = [BERLIN_PREFIX + f"quickview due {runner._iso_ms(START + k * MIN)} at "
                 f"{runner._iso_ms(START + k * MIN + timedelta(milliseconds=9))}" for k in range(0, quickview_until + 1, 3)]
        if window_start:
            lines.append(BERLIN_PREFIX + f"window_start {schedule.start_key(START, CFG)} due {runner._iso_ms(START)} "
                         f"at {runner._iso_ms(START + timedelta(milliseconds=20))}")
        return runner.parse_shadow(lines)

    def report(self, shadow, cron_lines, decision_lines):
        cron = runner.parse_cron(cron_lines, runner.JOBS)
        return runner.shadow_report(shadow, cron, runner.parse_decisions(decision_lines),
                                    since=START - 60 * MIN, until=START + 60 * MIN)

    def test_shadow_report_matches(self):
        qv, ws = runner.JOBS["quickview"].cron_tag, runner.JOBS["window_start"].cron_tag
        cron = [cron_line(START + k * MIN, qv) for k in range(0, 31, 3)]
        cron += [cron_line(START + k * MIN, ws) for k in (-30, 0, 30)]      # */30: acts only at a start
        cron.append(cron_line(START, "AFClaude at-shim (the jobs in the DB)"))  # no runner job: ignored
        cron.append(cron_line(START - 9 * MIN, qv))                          # the runner was not up yet
        dec = ["[2026-10-19 23:00:05 CEST] window-start: HOLD: no run", "[2026-10-19 23:15:07 CEST] window-start "
               "(postponed): HOLD: user active"]
        lines, bad = self.report(self.entries(), cron, dec)
        self.assertEqual(bad, 0, "\n".join(lines))
        self.assertIn("OK", lines[-1])
        self.assertTrue(any("window_start" in ln and "HOLD" in ln for ln in lines))
        self.assertTrue(any("1 cron start(s) while the runner was not running" in ln for ln in lines))

    def test_shadow_report_mismatches(self):
        qv, ws = runner.JOBS["quickview"].cron_tag, runner.JOBS["window_start"].cron_tag
        cron = [cron_line(START + k * MIN, qv) for k in range(0, 31, 3) if k != 9]   # cron missed 21:09
        cron.append(cron_line(START + 13 * MIN, qv))                                  # an off-grid cron start
        cron.append(cron_line(START + 2 * SEC + 1 * SEC, ws))                         # 3 s late: no match
        dec = ["[2026-10-19 23:20:05 CEST] window-start: FIRE: run"]                  # no start then
        lines, bad = self.report(self.entries(), cron, dec)
        text = "\n".join(lines)
        self.assertIn("21:09:00.000Z has no cron start", text)
        self.assertIn("cron started at 2026-10-19T21:13:00Z; the runner had no time", text)
        self.assertIn("MISMATCH window_start", text)
        self.assertIn("decided FIRE at 2026-10-19T21:20:05Z", text)
        self.assertEqual(bad, 4, text)

    def test_the_parsers(self):
        self.assertEqual(runner.parse_shadow(["no", BERLIN_PREFIX + "quickview due x at y"]), [])
        self.assertEqual(runner.parse_cron(['time="2026-10-19T21:00:00Z" level=info msg="job succeeded" '
                                            'job.command="x # AFClaude quickview export"'], runner.JOBS), [])
        self.assertEqual(runner.parse_decisions(["[2026-10-19 23:00:05 CEST] window-start: FIRE: x"]),
                         [(START + 5 * SEC, "FIRE")])


class FakeProc:
    def __init__(self, rc):
        self.pid, self.rc = 4242, rc

    def poll(self):
        return self.rc


class Ensure(Base):
    def ensure(self, running=(), rc=None):
        spawned = []

        def spawn():
            spawned.append(1)
            return FakeProc(rc)
        n = runner.ensure(connect=lambda: store.connect(os.path.join(self.tmp, "t.db")), pids=lambda: list(running),
                          spawn=spawn, settle=0.05, clock=lambda: self.now, sleep=lambda s: None, fail=self.record_fail,
                          log_fn=self.logs.append)
        self.assertEqual(n, 0)
        return spawned

    def test_a_running_runner_with_a_fresh_heartbeat(self):
        self.prev_boot(self.now - MIN, clean=False)
        self.assertEqual(self.ensure(running=[7]), [])
        self.assertEqual(self.fails, [])

    def test_a_stale_heartbeat_alerts(self):
        self.prev_boot(self.now - 20 * MIN, clean=False)
        self.assertEqual(self.ensure(running=[7]), [])
        self.assertEqual(self.subjects(), ["the runner's heartbeat is stale"])
        self.assertTrue(self.fails[0][3])                                         # and pauses (D-235)

    def test_a_cleanly_stopped_runner_is_started_and_alerted(self):
        self.prev_boot(self.now - 20 * MIN, clean=True)
        self.assertEqual(self.ensure(), [1])
        self.assertEqual(self.subjects(), ["the runner was not running"])
        self.assertIn("exited cleanly", self.fails[0][2])

    def test_never_booted(self):
        self.assertEqual(self.ensure(), [1])
        self.assertEqual(self.subjects(), ["the runner was not running"])
        self.assertIn("never booted", self.fails[0][2])

    def test_an_unclean_exit_or_a_reported_crash_is_reported_by_others(self):
        self.prev_boot(self.now - 20 * MIN, clean=False)
        self.assertEqual(self.ensure(), [1])
        self.assertIn("unclean exit", self.logs[-1])
        actions.runner_state_put(self.conn, {"crashed": {"boot_id": OTHER_BOOT, "at": "x"}}, actor="runner",
                                 via="runner")
        self.ensure()
        self.assertIn("its crash", self.logs[-1])
        self.assertEqual(self.fails, [])

    def test_a_start_that_fails(self):
        self.prev_boot(self.now - 20 * MIN, clean=False)
        self.ensure(rc=1)
        self.assertEqual(self.subjects(), ["the runner did not start"])
        self.ensure(rc=runner.EXIT_REPORTED)                                      # it said why itself
        self.ensure(rc=runner.EXIT_LOCKED)                                        # another one runs
        self.assertEqual(len(self.fails), 1)

    def test_one_ensure_at_a_time(self):
        fd = runner._ensure_lock()
        try:
            self.assertEqual(self.ensure(), [])
        finally:
            os.close(fd)

    def test_runner_pids(self):
        proc = os.path.join(self.tmp, "proc")
        for pid, argv in ((11, [sys.executable, "-u", "/x/runner.py", "serve"]), (12, ["python3", "runner.py", "status"]),
                          (13, []), (14, ["python3", "/x/runner_jobs.py", "serve"])):
            os.makedirs(os.path.join(proc, str(pid)))
            with open(os.path.join(proc, str(pid), "cmdline"), "wb") as fh:
                fh.write(b"".join(a.encode() + b"\0" for a in argv))
        self.assertEqual(runner.runner_pids(proc), [11])

    def test_wake_with_a_full_queue_counts_as_sent(self):
        path = os.path.join(self.tmp, "full.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as rx:
            rx.bind(path)
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as tx:
                tx.setblocking(False)
                with self.assertRaises(BlockingIOError):
                    for _ in range(100000):
                        tx.sendto(b"w", path)
            self.assertTrue(runner.wake(path))
        self.assertFalse(runner.wake(os.path.join(self.tmp, "none.sock")))


# ---------------------------------------------------------------- S3: the simple ticks

S3_TICKS = ("sampler", "dispatcher", "quickview", "docs_import", "prune_logs", "usage_review")
TINY = timedelta(microseconds=1)


def crontab_lines(name):
    with open(os.path.join(HERE, "docker", name), encoding="utf-8") as fh:
        return [ln for ln in fh.read().splitlines() if ln.strip() and not ln.startswith("#")]


def cron_field(field, lo, hi):
    """The values a crontab field matches (*, */N, N, a,b)."""
    out = set()
    for part in field.split(","):
        if part == "*":
            out |= set(range(lo, hi + 1))
        elif part.startswith("*/"):
            out |= set(range(lo, hi + 1, int(part[2:])))
        else:
            out.add(int(part))
    return out


def cron_times(line, a, b):
    """The minutes in [a, b) at which a crontab line runs (UTC: the container's clock)."""
    f = line.split()[:5]
    assert f[2:] == ["*", "*", "*"], line
    mins, hours = cron_field(f[0], 0, 59), cron_field(f[1], 0, 23)
    out, t = [], a
    while t < b:
        if t.minute in mins and t.hour in hours:
            out.append(t)
        t += MIN
    return out


class SimpleTicks(Base):
    """S3: sampler, dispatcher, quickview, docs_import, prune_logs, usage_review move from cron to
    the runner; the legacy crontab keeps them; a timed-out tick is its run's failure, not D-257."""

    def test_s3_owns_the_simple_ticks(self):
        self.assertEqual(runner.RUNNER_OWNS, frozenset({"stall_scan", "prune_runs", "at", "window_start",
                                                         "inbox", "inbox_verify", "status_snapshot"}
                                                       | set(S3_TICKS) | runner.WATCHER_PASSES))   # + S4, S5, S6, S7, S9b
        self.assertEqual(set(runner.JOBS) - runner.RUNNER_OWNS, set())             # every job is the runner's

    def test_crontab_per_mode(self):
        cron, legacy = crontab_lines("crontab"), crontab_lines("crontab.legacy")
        for name in S3_TICKS:
            tag = runner.JOBS[name].cron_tag
            self.assertEqual([ln for ln in cron if tag in ln], [], f"{name}: still a cron line in `on` mode")
            self.assertEqual(len([ln for ln in legacy if tag in ln]), 1, f"{name}: legacy mode lost its line")
        # legacy = the ticks + the at-shim (S4), the window start (S5), the watcher watchdog (S6), the inbox API watchdog
        # (the runner supervises it directly since S7); S8: the S2-S7 runner watchdog line is gone too,
        # so `on`/`runner` has no cron line at all
        self.assertEqual(cron, [])
        self.assertEqual(len(legacy), len(S3_TICKS) + 4)

    def test_each_tick_runs_what_its_cron_line_ran(self):
        """Registration: the handler has the legacy line's script, arguments, log file and flock."""
        legacy = crontab_lines("crontab.legacy")
        for name in S3_TICKS:
            with self.subTest(name):
                h = runner_jobs.HANDLERS[name]
                line, = [ln for ln in legacy if runner.JOBS[name].cron_tag in ln]
                cmd = line.split(" # ")[0]
                self.assertIn(f"{h.script} {' '.join(h.args)}".strip(), cmd)
                log = ("$KEEPALIVE_STATE_DIR/" + h.log[len("keepalive/"):] if h.log.startswith("keepalive/")
                       else f"$AFCLAUDE_DIR/data/{h.log}")
                self.assertIn(f">> {log} 2>&1", cmd)
                self.assertEqual(f"flock -n $AFCLAUDE_DIR/data/{h.lock} " in cmd, h.lock is not None)

    def test_each_cadence_is_its_cron_lines(self):
        legacy = crontab_lines("crontab.legacy")
        a = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)                    # over the DST night (UTC: no change)
        b = a + timedelta(days=2)
        for name in S3_TICKS:
            with self.subTest(name):
                job = runner.JOBS[name]
                line, = [ln for ln in legacy if job.cron_tag in ln]
                ticks, t = [], a - TINY
                while True:
                    t = runner.next_tick(job, t)
                    if t >= b:
                        break
                    ticks.append(t)
                self.assertEqual(ticks, cron_times(line, a, b))

    def test_tick_timeouts_exceed_their_inner_timeouts_and_the_measured_p99(self):
        for name in runner.RUNNER_OWNS:
            if runner.JOBS[name].poll is not None:
                continue   # S7: not a fixed-grid tick (no TICK_HOST_CALLS/TICK_MARGIN comparison fits it)
            with self.subTest(name):
                self.assertGreaterEqual(timedelta(minutes=runner.JOBS[name].timeout_min),
                                        runner_jobs.tick_min_timeout(name))
        # the measured p99 before S3 (runner.JOBS): sampler 10.4 s, dispatcher 2 s
        self.assertGreater(runner.JOBS["sampler"].timeout_min * 60, 100 * 10.4)
        self.assertGreater(runner.JOBS["dispatcher"].timeout_min * 60, 100 * 2)
        self.assertGreater(runner_jobs.tick_min_timeout("usage_review"), timedelta(minutes=10))   # its LOCK_WAIT

    def test_the_declared_tick_call_timeouts_are_in_the_sources(self):
        found = {}
        for mod in {c[0] for calls in runner_jobs.TICK_HOST_CALLS.values() for c in calls}:
            with open(os.path.join(HERE, mod + ".py"), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for f in ast.walk(tree):
                if isinstance(f, ast.FunctionDef):
                    for n in ast.walk(f):
                        if isinstance(n, ast.keyword) and n.arg == "timeout" and isinstance(n.value, ast.Constant):
                            found.setdefault((mod, f.name), set()).add(n.value.value)
        for job, calls in runner_jobs.TICK_HOST_CALLS.items():
            for mod, fn, secs, _ in calls:
                with self.subTest(job=job, call=f"{mod}.{fn}"):
                    self.assertIn(secs, found.get((mod, fn), set()))

    async def test_a_timed_out_tick_is_its_runs_failure_not_runner_downtime(self):
        """D-234: an alert (+ the pause unless pause_on_failure=False, D-239.3); not D-257: the next
        tick is on time, and the next boot neither reruns it nor alerts for it again."""
        r = self.runner(owns={"sampler", "quickview"})
        r.boot()
        for name in ("sampler", "quickview"):
            self.spawner.outcomes[name] = runner.ChildOutcome(-15, "", "", True)
            await self.run_due(r, runner.Due(runner.JOBS[name]))
        self.assertEqual([(f[1], f[3]) for f in self.fails],
                         [("sampler timed out", True), ("quickview timed out", False)])
        self.assertEqual((self.next_due("sampler"), self.next_due("quickview")), (START + 15 * MIN, START + 3 * MIN))
        self.assertEqual([x["status"] for x in self.runs()], ["timeout", "timeout"])
        actions.runner_state_put(self.conn, {"clean_exit": True}, actor="runner", via="runner")
        self.fails.clear()
        self.now = START + MIN
        r2 = self.runner(owns={"sampler", "quickview"})
        r2.boot()
        self.assertEqual((self.fails, r2.reruns), ([], []))             # no unclean exit, nothing interrupted
        self.assertEqual(r2.plan(self.now), [])                           # due at their next tick, not now
        self.now = START + 15 * MIN
        self.assertEqual([d.job.name for d in r2.plan(self.now)], ["quickview", "sampler"])

    async def test_a_sampler_slower_than_the_old_timeout_is_not_killed(self):
        r = self.runner(owns={"sampler"})
        await self.run_due(r, runner.Due(runner.JOBS["sampler"]))
        self.assertGreater(self.spawner.calls[-1][2], 15 * 60)            # was 10 min (S1)

    def test_a_job_runs_in_the_repo_directory(self):
        path = os.path.join(testenv.DIR, "t_cwd.py")
        with open(path, "w") as fh:
            fh.write("import os; print('cwd=' + os.getcwd())\n")
        code = PRELUDE + ("import runner_jobs as r; r.HANDLERS['t'] = r.Handler(sys.argv[1], (), 't_cwd.log'); "
                          "sys.exit(r.main(['t']))")
        p = subprocess.run([sys.executable, "-c", code, path], cwd="/", capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, PYTHONPATH=HERE))
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(testenv.DIR, "t_cwd.log")) as fh:
            self.assertIn(f"cwd={HERE}", fh.read())


# ---------------------------------------------------------------- S4: the at-jobs

AT_SHIM_LINE = ("* * * * * python3 $AFCLAUDE_DIR/docker/at_shim.py --run-due >> $AFCLAUDE_DIR/data/at_shim.log 2>&1 "
                "# AFClaude at-shim (the jobs in the DB: the sampler's pre/post weekly-reset samples, one-off reviews)")


def load_shim():
    spec = importlib.util.spec_from_file_location("at_shim_s4", os.path.join(HERE, "docker", "at_shim.py"))
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    return shim


class AtJobs(Base):
    """S4: the runner runs scheduled_jobs (claimed in the run's begin, a tracked child with a
    timeout); on_interrupt per job; `at -t` wakes it; the legacy crontab keeps `at --run-due`."""
    AT = runner.JOBS["at"]

    def add(self, jid, at, cmd="true\n", **fields):
        actions.job_put(self.conn, dict({"id": jid, "at": runner._iso(at), "cmd": cmd, "cwd": "/"}, **fields),
                        actor="runner:at_shim", via="runner")
        return jid

    async def run_at(self, r, due):
        run_id = r.start(due, self.now)
        task = r.tasks.get(self.AT.slot(due.key))
        if task is not None:
            await task
        return run_id

    def test_s4_owns_the_at_jobs_and_the_legacy_crontab_keeps_run_due(self):
        self.assertIn("at", runner.RUNNER_OWNS)
        self.assertEqual([ln for ln in crontab_lines("crontab") if "at_shim" in ln or "AFClaude at-shim" in ln], [])
        self.assertEqual([ln for ln in crontab_lines("crontab.legacy") if "at_shim" in ln], [AT_SHIM_LINE])
        self.assertEqual(runner.JOBS["at"].cron_tag, "AFClaude at-shim")
        self.assertEqual(runner_jobs.log_path("at"), os.path.join(testenv.DIR, "at_shim.log"))   # the line's log

    async def test_an_at_job_runs_at_its_minute_and_is_claimed_once(self):
        jid = self.add("job1", START + MIN)
        r = self.runner(owns={"at"})
        r.boot()
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(self.next_due("at"), START + MIN)               # the loop wakes for it
        self.assertEqual(r.sleep_for(self.now), 60.0)
        self.now = START + MIN
        due, = r.plan(self.now)
        self.assertEqual((due.job.name, due.key), ("at", jid))
        run_id = await self.run_at(r, due)
        self.assertEqual(store.get_run(self.conn, run_id)["status"], "ok")
        self.assertEqual(store.get_job(self.conn, jid)["status"], "started")
        self.assertEqual(self.spawner.calls, [("at", jid, 1800.0)])      # the default: runner_timeout_at
        self.assertEqual([a["action"] for a in store.audit_log(self.conn, target_type="scheduled_job")],
                         ["job.started", "job.put"])
        self.assertEqual(r.plan(self.now + MIN), [])                      # never twice
        self.assertIsNone(self.next_due("at"))
        self.assertEqual(self.fails, [])

    async def test_claim_exclusivity_against_at_run_due(self):
        jid = self.add("job2", START)
        r = self.runner(owns={"at"})
        r.boot()
        due, = r.plan(self.now)
        self.assertTrue(actions.job_started(self.conn, jid, actor="runner:at_shim", via="runner"))   # --run-due won
        self.assertIsNone(r.start(due, self.now))
        self.assertEqual((self.runs("at"), self.spawner.calls), ([], []))
        self.assertTrue(any("was claimed already" in ln for ln in self.logs))
        a, _ = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="at", key="job3", timeout_s=60,
                                 actor="runner:at", via="runner", claim=self.add("job3", START))
        b, why = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="at", key="job3", timeout_s=60,
                                   actor="runner:at", via="runner", claim="job3")
        self.assertIsNotNone(a)
        self.assertEqual((b, why), (None, f"at job3 ran before (run {a}, running)"))

    async def test_the_payload_timeout_and_several_at_once(self):
        self.add("j_a", START, timeout=5)
        self.add("j_b", START)
        r = self.runner(owns={"at"})
        r.boot()
        self.spawner.gates["at"] = asyncio.Event()
        dues = r.plan(self.now)
        for d in dues:
            r.start(d, self.now)
        self.assertEqual(sorted(r.tasks), ["at j_a", "at j_b"])           # one slot per job
        self.assertEqual(r.plan(self.now), [])                            # running: not again
        self.spawner.gates["at"].set()
        await asyncio.gather(*r.tasks.values())
        self.assertEqual(sorted((k, t) for _, k, t in self.spawner.calls), [("j_a", 300.0), ("j_b", 1800.0)])

    async def test_an_interrupted_at_job_is_rerun_or_fails_per_its_flag(self):
        self.prev_boot(START - MIN)
        ids = {}
        for jid, flag in (("j_rerun", "rerun"), ("j_fail", "fail"), ("j_old", None)):
            self.add(jid, START - MIN, **({"on_interrupt": flag} if flag else {}))
            ids[jid], _ = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="at", key=jid, timeout_s=60,
                                            actor="runner:at", via="runner", claim=jid)
        r = self.runner(owns={"at"})
        r.boot()
        self.assertEqual({x["id"]: x["status"] for x in self.runs("at")}, {i: "interrupted" for i in ids.values()})
        self.assertEqual(self.subjects(), ["at interrupted by a runner restart"] * 2)   # j_fail and j_old (default)
        self.assertTrue(all(f[3] for f in self.fails))                    # alert + pause (D-257)
        self.assertTrue(any("j_fail" in f[2] for f in self.fails) and any("j_old" in f[2] for f in self.fails))
        due, = r.plan(self.now)
        self.assertEqual((due.key, due.rerun), ("j_rerun", True))
        run_id = await self.run_at(r, due)
        self.assertEqual(store.get_run(self.conn, run_id)["status"], "ok")
        self.assertEqual([x["status"] for x in self.runs("at") if x["key"] == "j_rerun"], ["interrupted", "ok"])
        self.assertEqual(r.plan(self.now), [])                            # the failed ones never run again

    async def test_a_drain_holds_a_fail_job_not_a_rerun_job(self):
        self.add("j_review", START, on_interrupt="fail")
        self.add("j_sample", START, on_interrupt="rerun")
        r = self.runner(owns={"at"})
        r.boot()
        actions.runner_state_put(self.conn, {"drain": {"until": runner._iso(datetime.now(UTC) + timedelta(days=1))}},
                                 actor="cli", via="cli")
        started = {d.key: await self.run_at(r, d) for d in r.plan(self.now)}
        self.assertIsNone(started["j_review"])
        self.assertIsNotNone(started["j_sample"])
        self.assertEqual(store.get_job(self.conn, "j_review")["status"], "pending")   # not claimed: runs later
        actions.runner_state_put(self.conn, {"drain": None}, actor="cli", via="cli")
        due, = r.plan(self.now + MIN)
        self.assertEqual(due.key, "j_review")
        self.assertIsNotNone(await self.run_at(r, due))

    def test_the_drain_waits_for_a_running_fail_at_job_only(self):
        for jid, flag in (("j_f", "fail"), ("j_r", "rerun")):
            self.add(jid, START, on_interrupt=flag)
        rr, _ = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="at", key="j_r", timeout_s=60, actor="runner:at",
                                  via="runner", claim="j_r")
        out = []
        with mock.patch.object(runner, "wake"):
            self.assertEqual(runner.drain(self.conn, timedelta(0), out=out.append), 0)    # a rerun job: not waited for
            rf, _ = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="at", key="j_f", timeout_s=60,
                                      actor="runner:at", via="runner", claim="j_f")
            self.assertEqual(runner.drain(self.conn, timedelta(0), sleep=lambda s: None, out=out.append), 1)
        self.assertIn(f"run {rf} at j_f", out[-1])

    def test_the_at_timeout_covers_every_at_t_caller(self):
        """The at -t callers (usage_sampler's samples, usage_review's one-off reviews and chain
        steps) run the same code paths as the sampler and usage-review ticks (S3's inner timeouts):
        the default at-job timeout is above each one's worst case + margin."""
        at = timedelta(minutes=actions.SETTINGS["runner_timeout_at"].default())
        self.assertEqual(at, timedelta(minutes=runner.JOBS["at"].timeout_min))
        for tick in ("sampler", "usage_review"):
            self.assertGreaterEqual(at, runner_jobs.tick_min_timeout(tick), tick)
        callers = subprocess.run(["grep", "-rlE", r'\["at",', "--include=*.py", "--exclude-dir=.claude", "."], cwd=HERE,
                                 capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(c for c in callers if not os.path.basename(c).startswith("test_")),
                         ["./usage_review.py", "./usage_sampler.py"])     # a new caller: check its worst case

    def test_job_put_checks_the_runner_fields(self):
        for bad in ({"on_interrupt": "maybe"}, {"timeout": 0}, {"timeout": "5"}, {"timeout": 721}, {"timeout": True}):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.add("jbad", START, **bad)
        self.add("jok", START, on_interrupt="rerun", timeout=2.5)
        self.assertEqual(runner.at_policy(store.get_job(self.conn, "jok"), self.AT), ("rerun", 2.5))
        self.assertEqual(runner.at_policy(None, self.AT), ("fail", None))


class AtShim(unittest.TestCase):
    """`at -t` with the runner's fields, and its wake (only when a runner listens)."""
    def setUp(self):
        self.shim = load_shim()
        self.conn = store.connect()
        self.addCleanup(self.conn.close)

    def at(self, *argv, cmd="true\n"):
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(cmd)), contextlib.redirect_stderr(err):
            rc = self.shim.main(list(argv))
        return rc, err.getvalue()

    def test_the_runner_fields_are_stored(self):
        rc, err = self.at("--on-interrupt", "rerun", "--timeout", "5", "-t", "209901010000")
        self.assertEqual(rc, 0, err)
        jid = err.split()[1]
        p = json.loads(store.get_job(self.conn, jid)["payload"])
        self.assertEqual((p["on_interrupt"], p["timeout"]), ("rerun", 5.0))
        rc, err = self.at("-t", "209901010001")                          # as before: no fields (default fail)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("on_interrupt", json.loads(store.get_job(self.conn, err.split()[1])["payload"]))
        for bad in (("--on-interrupt", "x", "-t", "209901010000"), ("--timeout", "0", "-t", "209901010000"),
                    ("-t",)):
            with self.subTest(bad):
                self.assertEqual(self.at(*bad)[0], 1)
        self.assertEqual(self.at("--on-interrupt", "rerun")[0], 2)      # no -t: the usage

    def test_at_t_wakes_a_listening_runner_and_is_quiet_without_one(self):
        path = runner.sock_path()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        rc, err = self.at("-t", "209901010000")
        self.assertEqual(rc, 0)
        self.assertTrue(err.startswith("job "), err)                      # no runner (legacy): no warning
        os.makedirs(os.path.dirname(path), exist_ok=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(path)
        self.addCleanup(os.unlink, path)
        self.addCleanup(s.close)
        s.setblocking(False)
        rc, err = self.at("-t", "209901010000")
        self.assertEqual(rc, 0)
        self.assertEqual(s.recv(64), b"w")                                # the wake
        self.assertTrue(err.strip().splitlines()[-1].startswith("job "))  # the callers keep the tail


class AtChild(unittest.TestCase):
    """runner_jobs.py at --key: the claimed job's command as a tracked child; real runners and
    children against the test DB (the children read it through the environment)."""
    def setUp(self):
        testenv.unpause()
        self.addCleanup(testenv.unpause)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.mkdtemp(prefix="afc-at-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fails, self.logs = [], []
        self.n = 0

    def add(self, cmd, claim=True, **fields):
        self.n += 1
        jid = f"t{os.getpid()}x{self.n}x{int(time.time() * 1000) % 10**8}"
        actions.job_put(self.conn, dict({"id": jid, "at": runner._iso(datetime.now(UTC)), "cmd": cmd,
                                         "cwd": self.tmp}, **fields), actor="runner:at_shim", via="runner")
        if claim:
            actions.job_started(self.conn, jid, actor="runner:at_shim", via="runner")
        return jid

    def child(self, jid):
        code = PRELUDE + "import runner_jobs as r; sys.exit(r.main(['at', '--key', sys.argv[1]]))"
        return subprocess.run([sys.executable, "-c", code, jid], cwd=HERE, capture_output=True, text=True, timeout=60)

    def log_text(self):
        try:
            with open(runner_jobs.log_path("at"), encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return ""

    def test_ok_runs_in_its_cwd_and_logs_to_at_shim_log(self):
        p = self.child(self.add("echo hello-$(basename $(pwd))\n"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "ok")
        self.assertIn(f"hello-{os.path.basename(self.tmp)}", self.log_text())

    def test_a_failure_the_command_reported_exits_3(self):
        cmd = (f"{sys.executable} -c \"import sys; sys.path.insert(0, '{HERE}'); import failstop; "
               "failstop.fail('t', 'sample broke', pause=False, alert=lambda s, b='': None); sys.exit(1)\"\n")
        p = self.child(self.add(cmd))
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("reported its failure itself: t: sample broke", p.stderr)
        self.assertEqual([f for f in os.listdir(os.path.join(testenv.DIR, "run")) if f.endswith(".reports")], [])

    def test_a_nonzero_exit_and_an_unclaimed_job(self):
        p = self.child(self.add("exit 5\n"))
        self.assertEqual(p.returncode, 5)
        self.assertIn("exited 5", p.stderr)
        p = self.child(self.add("true\n", claim=False))
        self.assertEqual(p.returncode, 1)
        self.assertIn("pending, not claimed by this run", p.stderr)
        self.assertEqual(runner_jobs.main(["at"]), runner_jobs.EXIT_USAGE)

    def runner(self, clock=None, boot_id=None):
        return runner.Runner(self.conn, owns=frozenset({"at"}), spawner=runner.SubprocessSpawner(term_grace=1.0),
                             log_fn=self.logs.append, boot_id=boot_id,
                             fail=lambda c, s, d="", **kw: self.fails.append((s, d)),
                             sock=os.path.join(self.tmp, "r.sock"), clock=clock)

    async def _start(self, r, jid):
        due = runner.Due(runner.JOBS["at"], jid, datetime.now(UTC))
        run_id = r.start(due, datetime.now(UTC))
        self.assertIsNotNone(run_id)
        return run_id, r.tasks[runner.JOBS["at"].slot(jid)]

    def test_a_real_at_job_past_its_timeout_is_ended(self):
        async def go():
            r = self.runner()
            jid = self.add(f"sleep 60 & echo $! > {self.tmp}/pid; wait\n", claim=False, timeout=0.02)
            run_id, task = await self._start(r, jid)
            await task
            return run_id
        run_id = asyncio.run(go())
        row = store.get_run(self.conn, run_id)
        self.assertEqual(row["status"], "timeout")
        self.assertEqual([f[0] for f in self.fails], ["at timed out"])
        with open(os.path.join(self.tmp, "pid")) as fh:
            pid = int(fh.read())
        time.sleep(0.2)
        self.assertFalse(os.path.exists(f"/proc/{pid}") and "sleep" in open(f"/proc/{pid}/cmdline").read())

    def test_a_restart_reruns_or_fails_a_real_at_job_per_its_flag(self):
        """The done-when of S4: a test job interrupted by a restart is rerun or fails by its flag."""
        marker = os.path.join(self.tmp, "ran-once")
        cmd = f"if [ -e {marker}.$FLAG ]; then echo second; exit 0; fi; touch {marker}.$FLAG; sleep 60\n"
        jobs = {flag: self.add(cmd.replace("$FLAG", flag), claim=False, on_interrupt=flag) for flag in ("rerun", "fail")}

        async def first():
            r = self.runner()
            tasks = [(await self._start(r, jid))[1] for jid in jobs.values()]
            t = time.monotonic()
            while not all(os.path.exists(f"{marker}.{f}") for f in jobs) and time.monotonic() - t < 20:
                await asyncio.sleep(0.05)
            await r._stop_children()                                     # the stop grace is over: left running
            for t_ in tasks:
                self.assertTrue(t_.done())
            await asyncio.sleep(0.5)                                     # (their groups got SIGTERM: let them go)
        asyncio.run(first())
        self.assertEqual({x["key"]: x["status"] for x in store.running_runs(self.conn)},
                         {jid: "running" for jid in jobs.values()})
        actions.runner_state_put(self.conn, {"clean_exit": True}, actor="runner", via="runner")

        async def second():
            r = self.runner()
            r.boot()
            dues = [d for d in r.plan(datetime.now(UTC)) if d.key in jobs.values()]
            self.assertEqual([(d.key, d.rerun) for d in dues], [(jobs["rerun"], True)])
            await (await self._start_due(r, dues[0]))
        asyncio.run(second())
        self.assertEqual([x["status"] for x in store.key_runs(self.conn, "at", jobs["rerun"])], ["interrupted", "ok"])
        self.assertEqual([x["status"] for x in store.key_runs(self.conn, "at", jobs["fail"])], ["interrupted"])
        self.assertEqual([f[0] for f in self.fails], ["at interrupted by a runner restart"])
        self.assertIn(jobs["fail"], self.fails[0][1])
        self.assertIn("second", self.log_text())

    async def _start_due(self, r, due):
        self.assertIsNotNone(r.start(due, datetime.now(UTC)))
        return r.tasks[runner.JOBS["at"].slot(due.key)]


# ---------------------------------------------------------------- S5: the session-window starts

def _ws_run(conn, key, started, status="ok", error=None):
    """A finished window_start run row started at `started` (the report reads started_at)."""
    run_id = store.insert_run(conn, OTHER_BOOT, "window_start", key, "running", 4800, "runner:window_start", "runner")
    conn.execute("UPDATE runner_runs SET started_at=? WHERE id=?", (store.iso(started), run_id))
    conn.commit()
    if status == "skipped":
        conn.execute("UPDATE runner_runs SET status='skipped', finished_at=started_at, error=? WHERE id=?",
                     (error, run_id))
        conn.commit()
    else:
        actions.run_end(conn, run_id, status, actor="runner:window_start", via="runner", error=error)
    return store.get_run(conn, run_id)


class WindowStarts(Base):
    """S5: the runner starts each session-window start at its second, keyed by schedule.start_key,
    with the 30-min catch-up; cron's */30 line is gone (legacy keeps it); one claim per start shared
    with that legacy line (keepalive "window_starts"); the DST nights; window-report."""

    def test_s5_crontab_per_mode_and_the_handler_is_the_legacy_line_plus_its_key(self):
        cron, legacy = crontab_lines("crontab"), crontab_lines("crontab.legacy")
        tag = runner.JOBS["window_start"].cron_tag
        self.assertEqual([ln for ln in cron if tag in ln], [])
        line, = [ln for ln in legacy if tag in ln]
        self.assertTrue(line.startswith("*/30 * * * * "), line)
        h = runner_jobs.HANDLERS["window_start"]
        cmd = line.split(" && ", 1)[1].split(" >> ")[0].split()
        self.assertEqual(cmd[1], h.script)
        legacy_args = [a.replace("$AFCLAUDE_MANAGER_SESSION", "{session}") for a in cmd[2:]]
        self.assertEqual([a for a in h.args if a not in ("--start-key", "{key}")], legacy_args)
        self.assertEqual(h.args[h.args.index("--start-key") + 1], "{key}")
        self.assertIn("$KEEPALIVE_STATE_DIR/keepalive.log", line)
        self.assertEqual(h.log, "keepalive/keepalive.log")

    def test_the_child_gets_the_runs_key(self):
        h = runner_jobs.HANDLERS["window_start"]
        with mock.patch.dict(os.environ, {"AFCLAUDE_MANAGER_SESSION": "not-read"}):    # 3b S3: the registry's
            argv = runner_jobs._argv(h, "window-start-2026-10-20-s2")
            self.assertEqual(argv[1:], ["--session", testenv.SID, "--window-start", "--start-key",
                                        "window-start-2026-10-20-s2", "--arm", "--take-over-idle"])
            with self.assertRaises(RuntimeError):
                runner_jobs._argv(h, None)

    async def test_it_wakes_at_the_start_and_starts_it_at_its_second(self):
        self.prev_boot(START - 10 * MIN)
        self.now = START - 90 * SEC
        r = self.runner(owns={"window_start"})
        r.boot()
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(self.next_due("window_start"), START)
        self.assertEqual(r.sleep_for(self.now), 60.0)                      # MAX_SLEEP first ...
        self.now = START - 7 * SEC
        self.assertEqual(r.sleep_for(self.now), 7.0)                       # ... then to the start
        self.now = START
        due, = r.plan(self.now)
        self.assertEqual((due.key, due.at), ("window-start-2026-10-20", START))
        await self.run_due(r, due)
        self.assertEqual(self.spawner.calls, [("window_start", "window-start-2026-10-20", 4800.0)])
        self.assertEqual(self.fails, [])

    async def test_dst_nights_give_the_next_start(self):
        """Fall back (25.10.2026) and spring forward (29.03.2026): a window is n whole sessions
        (D-148), so the second start is 03:00 CET / 05:00 CEST on the wall clock."""
        for first, second, nxt in (
                (datetime(2026, 10, 24, 21, 0, tzinfo=UTC), datetime(2026, 10, 25, 2, 0, tzinfo=UTC),    # 23:00 CEST, 03:00 CET
                 datetime(2026, 10, 25, 22, 0, tzinfo=UTC)),                                             # 23:00 CET
                (datetime(2026, 3, 28, 22, 0, tzinfo=UTC), datetime(2026, 3, 29, 3, 0, tzinfo=UTC),      # 23:00 CET, 05:00 CEST
                 datetime(2026, 3, 29, 21, 0, tzinfo=UTC))):                                             # 23:00 CEST
            with self.subTest(first=first):
                self.setUp()
                self.prev_boot(first - 10 * MIN)
                self.now = first - MIN
                r = self.runner(owns={"window_start"})
                r.boot()
                r.plan(self.now)
                self.assertEqual(self.next_due("window_start"), first)
                got = []
                for t in (first, first + 10 * MIN, second - SEC, second, second + 31 * MIN, nxt - MIN, nxt):
                    self.now = t
                    for due in r.plan(self.now):
                        got.append((t, due.key))
                        await self.run_due(r, due)
                    if t == first:
                        self.assertEqual(self.next_due("window_start"), second)
                key1, key2 = schedule.start_key(first, CFG), schedule.start_key(second, CFG)
                self.assertEqual(got, [(first, key1), (second, key2), (nxt, schedule.start_key(nxt, CFG))])
                self.assertTrue(key2.endswith("-s2") and key2.startswith(key1))
                self.assertEqual(self.fails, [])

    def test_a_start_cron_decided_in_legacy_mode_is_neither_run_nor_missed(self):
        """A rollback to legacy and back: the starts cron's */30 line decided meanwhile (its claim in
        the keep-alive's "window_starts", whatever the outcome) are settled, not missed starts; a
        start nobody decided is a missed start (alert + pause, D-257)."""
        claims = {"window-start-2026-10-20": {"at": "2026-10-19T21:00:03+00:00", "by": "cron"}}
        actions.state_save(self.conn, "keepalive", {"window_starts": claims}, mode="file",
                           actor="runner:keepalive", via="runner")
        self.prev_boot(START - 60 * MIN)                                  # the runner stopped before the night
        self.now = START + 5 * 60 * MIN + 10 * MIN                        # back at 02:10Z: s2 is 10 min old
        r = self.runner(owns={"window_start"})
        r.boot()
        self.assertEqual([d.key for d in r.plan(self.now)], ["window-start-2026-10-20-s2"])
        self.assertEqual(self.fails, [])
        self.setUp()
        actions.state_save(self.conn, "keepalive", {"window_starts": {}}, mode="file", actor="runner:keepalive",
                           via="runner")
        self.prev_boot(START - 60 * MIN)
        self.now = START + 5 * 60 * MIN + 10 * MIN
        r = self.runner(owns={"window_start"})
        r.boot()
        self.assertEqual([d.key for d in r.plan(self.now)], ["window-start-2026-10-20-s2"])
        self.assertEqual(self.subjects(), ["missed session-window start"])

    def test_the_catch_up_is_at_most_30_min(self):
        self.assertEqual(actions.SETTINGS["runner_start_catchup"].bounds[1], 30)
        self.assertEqual(actions.SETTINGS["runner_start_catchup"].default(), 30)

    def report(self, runs, decisions, until=START + 6 * 60 * MIN):
        starts = [(t, schedule.start_key(t, CFG)) for t, _ in schedule.session_slots(START - TINY, until, CFG)]
        return runner.window_report(starts, runs, runner.parse_decisions(decisions), since=START - 60 * MIN,
                                    until=until)

    def test_window_report_ok(self):
        runs = [_ws_run(self.conn, "window-start-2026-10-20", START + timedelta(milliseconds=40)),
                _ws_run(self.conn, "window-start-2026-10-20-s2", START + 5 * 60 * MIN + timedelta(milliseconds=900))]
        dec = ["[2026-10-19 23:00:04 CEST] window-start: HOLD: no run",
               "[2026-10-20 04:00:06 CEST] window-start: POSTPONE: user active",
               "[2026-10-20 05:10:00 CEST] window-start (postponed): FIRE: x"]          # the watcher's: not a start
        lines, bad = self.report(runs, dec)
        self.assertEqual(bad, 0, "\n".join(lines))
        self.assertIn("decision HOLD", "\n".join(lines))
        self.assertIn("decision POSTPONE", "\n".join(lines))

    def test_window_report_mismatches(self):
        runs = [_ws_run(self.conn, "window-start-2026-10-20", START + 3 * SEC),                 # 3 s late
                _ws_run(self.conn, "window-start-2026-10-20-s2", START + 5 * 60 * MIN, status="skipped",
                        error="missed start")]
        dec = ["[2026-10-20 01:00:00 CEST] window-start: FIRE: x"]                              # at no start
        lines, bad = self.report(runs, dec)
        text = "\n".join(lines)
        self.assertIn("started +3.000 s", text)
        self.assertIn("logged no window-start decision", text)
        self.assertIn("not started (missed start)", text)
        self.assertIn("decided FIRE at 2026-10-19T23:00:00Z, at no session-window start", text)
        self.assertEqual(bad, 4, text)
        lines, bad = self.report([], [])
        self.assertEqual(bad, 2)                                                               # no run for either


class WindowStartClaim(unittest.TestCase):
    """keepalive.py --window-start (window_start_cli): the runner's --start-key must be the current
    start; each start is decided once, by whichever of the runner and cron's legacy line comes
    first (the keepalive state's "window_starts"); window_start_pass itself is unchanged."""

    def setUp(self):
        import keepalive as ka
        import runner_state
        self.ka = ka
        old = {n: getattr(ka, n) for n in ("STATE_FILE", "window_start_pass")}
        self.addCleanup(lambda: [setattr(ka, n, v) for n, v in old.items()])
        ka.STATE_FILE = runner_state.live_path("keepalive")
        st = ka.load_state()
        st["window_starts"] = {}
        ka.save_state(st)
        self.passes = []
        ka.window_start_pass = lambda sid, now, args: self.passes.append((now, args.now)) or {"go": False}
        self.cfg = mock.patch.object(schedule, "load", lambda: CFG)
        self.cfg.start()
        self.addCleanup(self.cfg.stop)

    def call(self, now, start_key=None, manual=False):
        args = mock.Mock(now=manual, start_key=start_key, arm=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            res = self.ka.window_start_cli("sid", now, args)
        return res, out.getvalue()

    def claims(self):
        return self.ka.load_state().get("window_starts") or {}

    def test_the_runner_first_then_cron_in_the_same_slot(self):
        res, _ = self.call(START + SEC, start_key="window-start-2026-10-20")
        self.assertEqual(res, {"go": False})
        self.assertEqual(self.claims()["window-start-2026-10-20"]["by"], "runner")
        res, out = self.call(START + 2 * SEC)                                   # cron's */30 at 23:00 (legacy)
        self.assertIsNone(res)
        self.assertIn("decided already", out)
        self.assertEqual(len(self.passes), 1)

    def test_cron_first_then_the_runner(self):
        self.call(START + SEC)
        self.assertEqual(self.claims()["window-start-2026-10-20"]["by"], "cron")
        res, out = self.call(START + 5 * SEC, start_key="window-start-2026-10-20")
        self.assertIsNone(res)
        self.assertIn("by the cron", out)
        self.assertEqual(len(self.passes), 1)

    def test_a_key_that_is_not_the_current_start_is_an_error(self):
        with self.assertRaises(SystemExit) as cm:
            self.call(START + 5 * 60 * MIN + SEC, start_key="window-start-2026-10-20")   # it is s2 now
        self.assertIn("window-start-2026-10-20-s2", str(cm.exception.code))
        with self.assertRaises(SystemExit) as cm:
            self.call(START - 60 * MIN, start_key="window-start-2026-10-20")              # outside a window
        self.assertIn("outside a window", str(cm.exception.code))
        self.assertEqual((self.passes, self.claims()), ([], {}))

    def test_the_runners_catch_up_is_not_cut_by_the_cron_step(self):
        res, _ = self.call(START + 29 * MIN + 59 * SEC, start_key="window-start-2026-10-20")
        self.assertIsNotNone(res)                                       # the runner decided it was due
        self.assertEqual(len(self.passes), 1)

    def test_cron_acts_only_in_its_slot_and_now_never_claims(self):
        self.assertEqual(self.call(START + 30 * MIN)[0], None)          # cron's 23:30: no start due
        self.assertEqual((self.passes, self.claims()), ([], {}))
        self.call(START + 30 * MIN, manual=True)
        self.call(START + 31 * MIN, manual=True)
        self.assertEqual(len(self.passes), 2)
        self.assertEqual(self.claims(), {})

    def test_the_claims_are_kept_a_month(self):
        st = self.ka.load_state()
        st["window_starts"] = {f"k{i:03d}": {"at": f"2026-09-{1 + i // 3:02d}T21:00:{i % 60:02d}+00:00"}
                               for i in range(self.ka.WINDOW_STARTS_KEPT)}
        self.ka.save_state(st)
        self.call(START + SEC, start_key="window-start-2026-10-20")
        c = self.claims()
        self.assertEqual(len(c), self.ka.WINDOW_STARTS_KEPT)
        self.assertIn("window-start-2026-10-20", c)
        self.assertNotIn("k000", c)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- S6: the keep-alive watcher's passes

class WatcherPasses(Base):
    """S6: the watcher's passes as runner jobs: the ticks (watch, run_readings, compaction_check)
    and the planned acts (last_stretch, deferred_start, fillup, compaction) at the time their plan
    step stored (runner state 'keepalive.watch', acts.<job>), each under its run key once; a cut-off
    act fails (D-257) and is never rerun; a drain holds it; the legacy mode keeps the watcher."""
    AT = START + 3 * 60 * MIN + 17 * SEC
    SID = "00000000-0000-4000-8000-000000000001"

    def setUp(self):
        super().setUp()
        store.add_project(self.conn, "alpha")           # the registry (3b S3): one task-manager
        store.update_project(self.conn, "alpha", manager_session=self.SID)
        self.conn.commit()

    def store_act(self, job, at, key="fillup-2026-10-20T02:00:00+00:00", run=None, session=SID):
        ent = {"key": key, "at": runner._iso(at), "run": run or f"{session[:8]}.{key}.{at:%Y%m%dT%H%M%S}",
               "session": session}
        actions.state_save(self.conn, runner.PLANS_COMPONENT, {"acts": {runner_state.act_name(job, session): ent}},
                           mode="file", actor="runner:keepalive", via="runner")
        return ent["run"]

    def test_s6_owns_the_passes_the_crontabs_and_the_watcher_check(self):
        self.assertLessEqual(runner.WATCHER_PASSES, runner.RUNNER_OWNS)
        self.assertEqual(set(runner_jobs.PASSES), set(runner.WATCHER_PASSES))
        self.assertIsNotNone(runner.watcher_conflict(runner.RUNNER_OWNS, local=lambda: [5], on_host=lambda: []))
        cron, legacy = crontab_lines("crontab"), crontab_lines("crontab.legacy")
        self.assertFalse(any("start_watcher.sh" in ln for ln in cron))
        self.assertEqual(len([ln for ln in legacy if "start_watcher.sh" in ln]), 1)
        for name in runner.WATCHER_PASSES:
            job = runner.JOBS[name]
            self.assertEqual(runner_jobs.log_path(name), os.path.join(runner_state.data_dir(), "keepalive",
                                                                     "keepalive.log"))
            if job.planned:
                self.assertEqual((job.on_interrupt, job.session, job.paused), ("fail", True, "handler"), name)
            else:
                self.assertEqual(job.on_interrupt, "rerun", name)
        self.assertEqual(runner.JOBS["watch"].paused, "runs")           # detection runs while paused

    def test_runner_conflict_keeps_a_watcher_from_starting_beside_the_runner(self):
        """The reverse of watcher_conflict: keepalive.run, start_watcher.sh and start_keepalive.sh
        (runner.py watcher-check) refuse under AFCLAUDE_SCHEDULER=on or a fresh heartbeat of a
        runner that did not exit cleanly; legacy after the runner's clean stop does not."""
        legacy = {"AFCLAUDE_SCHEDULER": "legacy"}
        self.assertIsNone(runner.runner_conflict(self.conn, legacy, self.now))           # never ran
        self.assertIn("AFCLAUDE_SCHEDULER=on", runner.runner_conflict(self.conn, {"AFCLAUDE_SCHEDULER": "on"}, self.now))
        self.prev_boot(self.now - 30 * SEC, clean=False)
        self.assertIn("heartbeat is 30 s old", runner.runner_conflict(self.conn, legacy, self.now))
        self.assertIn("heartbeat", runner.runner_conflict(self.conn, {}, self.now + runner.STALE_BEAT - 31 * SEC))
        self.assertIsNone(runner.runner_conflict(self.conn, legacy, self.now + runner.STALE_BEAT - 29 * SEC))
        self.prev_boot(self.now - 30 * SEC, clean=True)
        self.assertIsNone(runner.runner_conflict(self.conn, legacy, self.now))

    def test_the_act_timeouts_exceed_their_inner_timeouts(self):
        for name in runner_jobs.START_ACTS + ("compaction",):
            with self.subTest(name):
                least = runner_jobs.act_min_timeout(name)
                self.assertGreaterEqual(timedelta(minutes=runner.JOBS[name].timeout_min), least)
                self.assertGreaterEqual(timedelta(minutes=actions.SETTINGS[actions.RUNNER_TIMEOUT + name].bounds[0]),
                                        least)
        self.assertGreater(runner_jobs.act_min_timeout("compaction"), timedelta(minutes=45))   # 4 waits of 10 min, ...
        with open(os.path.join(HERE, "keepalive.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fire = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "fire")
        found = {n.value.value for n in ast.walk(fire) if isinstance(n, ast.keyword) and n.arg == "timeout"
                 and isinstance(n.value, ast.Constant)}
        for fn, secs, _ in runner_jobs.COMPACTION_HOST_CALLS:
            self.assertIn(secs, found, fn)

    async def test_a_planned_act_runs_at_its_time_under_its_run_key_once(self):
        r = self.runner(owns={"fillup"})
        r.boot()
        run = self.store_act("fillup", self.AT)
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(self.next_due("fillup"), self.AT.replace(microsecond=0))
        self.assertEqual(r.sleep_for(self.AT - 30 * SEC), 30.0)          # it wakes at the act's second
        self.now = self.AT
        due, = r.plan(self.now)
        self.assertEqual((due.job.name, due.key), ("fillup", run))
        await self.run_due(r, due)
        self.assertEqual(self.spawner.calls, [("fillup", run, 4800.0)])
        self.assertEqual(r.plan(self.now + MIN), [])                    # its run key ran: never twice
        self.assertIsNone(self.next_due("fillup"))
        self.assertEqual(self.runs("fillup")[0]["key"], run)

    async def test_an_interrupted_act_fails_and_is_not_rerun(self):
        self.prev_boot(self.AT - MIN)
        run = self.store_act("compaction", self.AT, key="compact-2026-10-19T23:00:00+00:00")
        rid, _ = actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="compaction", key=run, timeout_s=3600,
                                   actor="runner:compaction", via="runner", session=True, rerun=False)
        self.now = self.AT + MIN
        r = self.runner(owns={"compaction"})
        r.boot()
        self.assertEqual(self.subjects(), ["compaction interrupted by a runner restart"])
        self.assertTrue(self.fails[0][3])                                # alert + pause (D-257)
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(self.spawner.calls, [])

    async def test_a_drain_holds_an_act_until_it_is_lifted(self):
        r = self.runner(owns={"deferred_start"})
        r.boot()
        actions.runner_state_put(self.conn, {"drain": {"until": runner._iso(datetime.now(UTC) + timedelta(days=1))}},
                                 actor="cli", via="cli")
        self.store_act("deferred_start", self.AT, key="window-start-2026-10-20")
        self.now = self.AT
        due, = r.plan(self.now)
        self.assertIsNone(r.start(due, self.now))
        self.assertTrue(any("drained" in ln for ln in self.logs))
        runner.undrain(self.conn)
        due, = r.plan(self.now + MIN)
        await self.run_due(r, due)
        self.assertEqual([c[0] for c in self.spawner.calls], ["deferred_start"])

    def test_a_stored_act_that_is_not_valid_alerts_once(self):
        actions.state_save(self.conn, runner.PLANS_COMPONENT, {"acts": {runner_state.act_name("fillup", self.SID): {"key": "k", "at": "soon"}}},
                           mode="file", actor="runner:keepalive", via="runner")
        r = self.runner(owns={"fillup"})
        r.boot()
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(r.plan(self.now + MIN), [])
        self.assertEqual(self.subjects(), ["a planned act is not valid"])

    SID2 = "22222222-0000-4000-8000-000000000002"

    def two_managers(self):
        store.add_project(self.conn, "beta")
        store.update_project(self.conn, "beta", manager_session=self.SID2)
        store.add_project(self.conn, "gamma")            # no task-manager session yet (D-281): skipped
        self.conn.commit()

    async def test_each_task_manager_has_its_own_act_and_one_runs_at_a_time(self):
        """3b S3: acts.<job>:<sid8> per registry session; the earliest due runs, a tie goes to the better rank,
        the next one when it ends; a run key that ran never runs again."""
        self.two_managers()
        r = self.runner(owns={"fillup"})
        r.boot()
        run_a = self.store_act("fillup", self.AT + 10 * SEC)
        run_b = self.store_act("fillup", self.AT, session=self.SID2)
        self.assertNotEqual(run_a, run_b)                                  # one dedup key, two run keys
        self.assertEqual(r.plan(self.now), [])
        self.assertEqual(self.next_due("fillup"), self.AT.replace(microsecond=0))   # the earliest of the two
        self.now = self.AT
        due, = r.plan(self.now)
        self.assertEqual(due.key, run_b)                                    # beta's is earlier
        await self.run_due(r, due)
        self.now = self.AT + 10 * SEC
        self.assertEqual(self.next_due("fillup"), (self.AT + 10 * SEC).replace(microsecond=0))   # beta's: ahead
        due, = r.plan(self.now)
        self.assertEqual(due.key, run_a)
        await self.run_due(r, due)
        self.assertEqual(r.plan(self.now + MIN), [])
        self.assertEqual([c[1] for c in self.spawner.calls], [run_b, run_a])

    async def test_a_tie_goes_to_the_better_ranked_project(self):
        self.two_managers()
        r = self.runner(owns={"fillup"})
        r.boot()
        run_b = self.store_act("fillup", self.AT, session=self.SID2)
        run_a = self.store_act("fillup", self.AT)
        self.now = self.AT + MIN
        due, = r.plan(self.now)
        self.assertEqual(due.key, run_a)
        r.tasks["fillup"] = asyncio.get_running_loop().create_future()      # one is running: the other waits
        self.assertEqual(r.plan(self.now), [])
        del r.tasks["fillup"]
        await self.run_due(r, due)
        due, = r.plan(self.now)
        self.assertEqual(due.key, run_b)

    def test_an_act_of_a_session_outside_the_registry_is_not_planned(self):
        r = self.runner(owns={"fillup"})
        r.boot()
        self.store_act("fillup", self.AT, session=self.SID2)               # beta is no project here
        self.now = self.AT + MIN
        self.assertEqual(r.plan(self.now), [])
        self.assertIsNone(self.next_due("fillup"))

    def test_status_lists_the_task_managers_and_their_plans(self):
        self.two_managers()
        run = self.store_act("fillup", self.AT)
        st = runner.status(self.conn, self.now)
        self.assertEqual([(t["project"], t["session"]) for t in st["task_managers"]],
                         [("alpha", self.SID[:8]), ("beta", self.SID2[:8])])
        self.assertEqual([(p["act"], p["run"]) for p in st["plans"]], [(f"fillup:{self.SID[:8]}", run)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.print_status(st)
        self.assertIn(f"plan fillup:{self.SID[:8]}", buf.getvalue())

    def test_the_ticks_result_next_due_moves_the_tick_earlier(self):
        r = self.runner(owns={"watch"})
        r.boot()
        due, = r.plan(self.next_due("watch"))
        self.now = self.next_due("watch")
        run_id = actions.run_begin(self.conn, boot_id=r.boot_id, job="watch", key=None, timeout_s=720,
                                   actor="runner:watch", via="runner")[0]
        soon = self.now + 20 * SEC
        line = json.dumps({"status": "ok", "next_due": runner._iso(soon), "note": ""})
        actions.runner_job_set(self.conn, "watch", actor="runner", via="runner",
                               next_due=runner._iso(self.now + MIN))
        r.finish(due.job, None, run_id, runner.ChildOutcome(0, line + "\n", "", False), 720, 1.0)
        self.assertEqual(self.next_due("watch"), soon)


class Inbox(Base):
    """S7: the inbox jobs (docs/phase3a_plan.md §5 S7, §3.2). A `poll` job only runs while
    POLL_PENDING[name] says there is work, at most once every `poll`; dormant (no due time, only a
    wake restarts the plan loop's attention to it) once it reports none."""

    def set_pending(self, name, value):
        old = runner.POLL_PENDING[name]
        runner.POLL_PENDING[name] = lambda conn, now: value
        self.addCleanup(runner.POLL_PENDING.__setitem__, name, old)

    def test_s7_owns_the_inbox_jobs(self):
        self.assertLessEqual({"inbox", "inbox_verify"}, runner.RUNNER_OWNS)
        self.assertIsNone(runner.JOBS["inbox"].cron_tag)            # no cron line ever ran it (S7 is new)
        self.assertIsNone(runner.JOBS["inbox_verify"].cron_tag)
        self.assertLessEqual({"inbox", "inbox_verify"}, set(runner_jobs.FUNCTIONS))
        self.assertEqual(runner.JOBS["inbox"].poll, timedelta(seconds=30))
        self.assertEqual(runner.JOBS["inbox_verify"].poll, timedelta(seconds=5))
        for name in ("inbox", "inbox_verify"):
            job = runner.JOBS[name]
            self.assertEqual((job.paused, job.on_interrupt, job.session), ("runs", "rerun", False), name)

    async def test_dormant_until_pending_then_polls_at_its_cooldown(self):
        self.set_pending("inbox", False)
        r = self.runner(owns={"inbox"})
        r.boot()
        self.assertIsNone(self.next_due("inbox"))        # no catch-up tick: a poll job starts dormant
        self.assertEqual(r.plan(self.now), [])
        self.set_pending("inbox", True)
        due, = r.plan(self.now)
        self.assertEqual(due.job.name, "inbox")
        await self.run_due(r, due)
        self.assertEqual(self.next_due("inbox"), self.now + timedelta(seconds=30))   # the cooldown
        self.assertEqual(r.plan(self.now + timedelta(seconds=10)), [])               # still cooling down
        due, = r.plan(self.now + timedelta(seconds=30))
        self.assertEqual(due.job.name, "inbox")
        await self.run_due(r, due)
        self.set_pending("inbox", False)
        self.assertEqual(r.plan(self.now + timedelta(seconds=60)), [])
        self.assertIsNone(self.next_due("inbox"))                                    # dormant again

    async def test_a_running_job_is_not_started_twice_while_pending(self):
        self.set_pending("inbox", True)
        r = self.runner(owns={"inbox"})
        r.boot()
        self.spawner.gates["inbox"] = asyncio.Event()
        due, = r.plan(self.now)
        r.start(due, self.now)
        self.assertEqual(r.plan(self.now), [])                 # already running: not started again
        self.spawner.gates["inbox"].set()
        await r.tasks["inbox"]

    def test_an_interrupted_poll_job_is_rerun(self):
        """on_interrupt=rerun (not keyed): it is simply due now, no alert (D-257 is for "fail" jobs)."""
        self.prev_boot(self.now - MIN)
        actions.run_begin(self.conn, boot_id=OTHER_BOOT, job="inbox_verify", key=None, timeout_s=30,
                          actor="runner:inbox_verify", via="runner")
        r = self.runner(owns={"inbox_verify"})
        r.boot()
        self.assertEqual(self.runs("inbox_verify")[0]["status"], "interrupted")
        self.assertEqual(self.fails, [])
        self.assertEqual(self.next_due("inbox_verify"), self.now)


class FakeSupervisor:
    """Records each start (full argv); the run blocks on a fresh gate until it is told to exit."""
    def __init__(self):
        self.calls = []
        self.outcome = runner.ChildOutcome(0, "", "", False)
        self.gate = asyncio.Event()

    async def run(self, argv, timeout_s, on_pid):
        self.calls.append(argv)
        self.gate = asyncio.Event()
        on_pid(1)                      # pid 1: never really signalled (as FakeSpawner's on_pid(1))
        await self.gate.wait()
        return self.outcome


class Supervised(Base):
    """S7: the runner keeps `inbox_api.py serve --no-worker` running as a long-lived child
    (SUPERVISED): started at boot without an alert, restarted with one on an unexpected exit, ended
    (not restarted) once a stop was requested."""

    def setUp(self):
        super().setUp()
        self.sup = FakeSupervisor()
        patch = mock.patch.object(runner, "SUPERVISE_DELAY", 0.05)
        patch.start()
        self.addCleanup(patch.stop)

    def start(self):
        r = self.runner(supervise={"inbox_api": ("inbox_api.py", "serve", "--no-worker")}, spawner=self.sup)
        r.boot()
        r.start_supervised("inbox_api")
        return r

    async def test_starts_at_boot_without_an_alert(self):
        r = self.start()
        await asyncio.sleep(0)
        self.assertEqual(len(self.sup.calls), 1)
        self.assertEqual(self.sup.calls[0][-2:], ["serve", "--no-worker"])
        self.assertEqual(self.sup.calls[0][0], sys.executable)
        self.assertEqual(self.fails, [])
        self.assertIn("inbox_api", r.sup_tasks)
        self.assertEqual(r.sup_pids["inbox_api"], 1)

    async def test_never_started_twice(self):
        r = self.start()
        await asyncio.sleep(0)
        r.start_supervised("inbox_api")
        self.assertEqual(len(self.sup.calls), 1)

    async def test_an_unexpected_exit_is_restarted_and_alerted(self):
        r = self.start()
        await asyncio.sleep(0)
        self.sup.outcome = runner.ChildOutcome(1, "", "boom", False)
        self.sup.gate.set()
        for _ in range(100):
            if len(self.sup.calls) >= 2:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.sup.calls), 2)
        self.assertEqual(self.subjects(), ["inbox_api was not running"])
        self.assertTrue(self.fails[0][3])                      # alert + pause, like any other runner failure
        self.assertIn("inbox_api", r.sup_tasks)                 # the restart is running again

    async def test_stop_ends_it_without_restarting(self):
        r = self.start()
        await asyncio.sleep(0)
        signalled = []
        with mock.patch.object(runner, "_signal_group", lambda pid, sig: signalled.append((pid, sig))):
            r.request_stop()             # SIGTERMs its pid right away: it does not wait for a tick
        self.assertEqual(signalled, [(1, signal.SIGTERM)])     # (patched: the fake pid 1 is never signalled)
        self.sup.gate.set()
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.sup.calls), 1)
        self.assertEqual(self.fails, [])
        self.assertEqual(r.sup_tasks, {})


class PassChild(unittest.TestCase):
    """runner_jobs.py <pass>: keepalive.runner_pass in the child, its status and next_due in the
    result line, its output in the keep-alive's log."""
    def run_pass(self, job, ret, key=None):
        code = PRELUDE + ("import json, keepalive, runner_jobs as r; "
                          f"keepalive.runner_pass = lambda job, key: print('pass', job, key) or {ret!r}; "
                          f"sys.exit(r.main([{job!r}] + ([\"--key\", {key!r}] if {key!r} else [])))")
        return subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, timeout=60)

    def test_the_result_line_carries_status_and_next_due(self):
        p = self.run_pass("watch", {"status": "ok", "note": "n", "next_due": "2026-10-20T01:00:00Z"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"status": "ok", "next_due": "2026-10-20T01:00:00Z", "note": "n"})
        with open(runner_jobs.log_path("watch")) as fh:
            self.assertIn("pass watch None", fh.read())
        p = self.run_pass("fillup", {"status": "skipped", "note": "STOP"}, key="fillup-x.20261020T010000")
        self.assertEqual(json.loads(p.stdout)["status"], "skipped")
        with open(runner_jobs.log_path("fillup")) as fh:
            self.assertIn("pass fillup fillup-x.20261020T010000", fh.read())


class StatusSnapshotJob(Base):
    """S9(b): the status snapshot's job (status_snapshot.py): a tick at most once a minute, written
    while automation is paused too, and a failed write is the run's failure (alert + pause, D-234/D-235)."""

    async def test_at_most_once_a_minute(self):
        r = self.runner(owns={"status_snapshot"})
        r.boot()
        starts = []
        for _ in range(60 * 10):                          # a scan every 7 s for 10 minutes (wakes make extra scans)
            for due in r.plan(self.now):
                self.assertIsNotNone(await self.run_due(r, due))
                starts.append(self.now)
            self.now += timedelta(seconds=7)
        self.assertGreaterEqual(len(starts), 9)           # it did run, each minute
        minutes = [t.replace(second=0) for t in starts]       # a grid tick each minute: never two in one minute
        self.assertEqual(len(minutes), len(set(minutes)))
        self.assertTrue(all(30 <= t.second < 37 for t in starts))              # on its offset (+ the scan step)

    async def test_it_is_written_while_paused_too(self):
        r = self.runner(owns={"status_snapshot"}, paused=lambda: {"paused": True, "reason": "an error"})
        r.boot()
        actions.runner_job_set(self.conn, "status_snapshot", actor="runner", via="runner",
                               next_due=runner._iso(self.now))
        due, = r.plan(self.now)
        await self.run_due(r, due)
        run, = self.runs("status_snapshot")
        self.assertEqual(run["status"], "ok")             # not skipped for the pause
        self.assertEqual([c[0] for c in self.spawner.calls], ["status_snapshot"])

    async def test_a_failed_write_is_an_alert_and_a_pause_and_the_next_tick_still_runs(self):
        self.spawner.outcomes["status_snapshot"] = runner.ChildOutcome(
            1, "", "Traceback ...\nstore.DBUnavailable: database locked", False)
        r = self.runner(owns={"status_snapshot"})
        r.boot()
        actions.runner_job_set(self.conn, "status_snapshot", actor="runner", via="runner",
                               next_due=runner._iso(self.now))
        due, = r.plan(self.now)
        await self.run_due(r, due)
        self.assertEqual(self.subjects(), ["status_snapshot failed"])
        self.assertTrue(self.fails[0][3])                 # pause=True
        self.now += MIN
        self.assertEqual([d.job.name for d in r.plan(self.now)], ["status_snapshot"])
