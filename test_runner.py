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
import fcntl
import json
import os
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
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afc-runner-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = store.connect(os.path.join(self.tmp, "t.db"), create=True)
        self.addCleanup(self.conn.close)
        self.fails, self.logs = [], []
        self.now = START
        self.spawner = FakeSpawner()

    def fail(self, component, subject, detail="", **kw):
        self.fails.append((component, subject, detail, kw.get("pause", True)))

    def runner(self, owns=(), jobs=None, **kw):
        kw.setdefault("spawner", self.spawner)
        return runner.Runner(self.conn, jobs=jobs, owns=frozenset(owns), clock=lambda: self.now,
                             log_fn=self.logs.append, fail=self.fail, schedule_cfg=CFG,
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
        self.assertEqual(set(runner.JOBS), set(runner_jobs.HANDLERS) | set(runner_jobs.FUNCTIONS))

    def test_the_legacy_crontab_runs_every_job_and_no_runner(self):
        def lines(name):
            with open(os.path.join(HERE, "docker", name), encoding="utf-8") as fh:
                return [ln for ln in fh.read().splitlines() if ln.strip() and not ln.startswith("#")]
        legacy, cron = lines("crontab.legacy"), lines("crontab")
        for name, job in runner.JOBS.items():
            if job.cron_tag:
                self.assertTrue(any(job.cron_tag in ln for ln in legacy), name)
        self.assertFalse(any("runner.py" in ln for ln in legacy))
        self.assertEqual(len([ln for ln in cron if "runner.py ensure" in ln]), 1)      # the S2-S7 watchdog
        self.assertEqual(set(legacy) - set(cron), {ln for ln in legacy if any(
            runner.JOBS[n].cron_tag in ln for n in runner.RUNNER_OWNS if runner.JOBS[n].cron_tag)})


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
                self.assertEqual((row["status"], row["pid"], row["timeout_s"]), (status, 1, 600.0))
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
        self.assertEqual(self.spawner.calls, [("window_start", "window-start-2026-10-20", 4200.0)])
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

    async def test_a_runner_write_failure_crashes_the_loop(self):
        r = self.runner(owns={"stall_scan"})
        r.boot()
        actions.runner_job_set(self.conn, "stall_scan", actor="runner", via="runner", next_due=runner._iso(self.now))
        with mock.patch.object(actions, "run_end", side_effect=store.DBUnavailable("database locked", "locked")):
            with self.assertRaises(store.DBError):
                await asyncio.wait_for(r.serve(install_signals=False), 5)


# ---------------------------------------------------------------- real children

class Children(Base):
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

    def popen(self):
        """A real `.../runner.py serve` (a shim of that name, so runner_pids() sees it as /proc
        does a live one) that owns nothing: no stall scan of the real transcripts mid-test."""
        d = tempfile.mkdtemp(prefix="afc-runner-shim-", dir="/tmp")
        self.addCleanup(shutil.rmtree, d, True)
        shim = os.path.join(d, "runner.py")
        with open(shim, "w", encoding="utf-8") as fh:
            fh.write(f"import sys\nsys.path.insert(0, {HERE!r})\n" + PRELUDE
                     + "import runner; runner.RUNNER_OWNS = frozenset(); sys.exit(runner.main(sys.argv[1:]))\n")
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

    def test_s2_owns_the_new_jobs_only(self):
        self.assertEqual(runner.RUNNER_OWNS, frozenset({"stall_scan", "prune_runs"}))
        self.assertEqual([n for n in runner.RUNNER_OWNS if runner.JOBS[n].cron_tag], [])   # none cron ran

    def test_shadow_lines_for_the_jobs_cron_runs(self):
        lines = []
        self.now = START - 2 * MIN
        r = self.runner(owns={"stall_scan"}, shadow=lines.append)
        self.assertEqual([j.name for j in r.shadow_jobs],
                         ["dispatcher", "docs_import", "prune_logs", "quickview", "sampler", "usage_review",
                          "window_start"])            # cron's jobs; not the owned stall scan, not prune_runs
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
                          spawn=spawn, settle=0.05, clock=lambda: self.now, sleep=lambda s: None, fail=self.fail,
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


if __name__ == "__main__":
    unittest.main()
