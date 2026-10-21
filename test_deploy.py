#!/usr/bin/env python3
"""Tests for tools/deploy.py (D-257, the 21.10. incident): the container-quiet wait logic,
with fake `docker top` output and a fake clock/sleep (no docker, no real waiting)."""
import os
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
import deploy  # noqa: E402
import schedule  # noqa: E402

HEADER = "PID                 COMMAND"
IDLE = [HEADER,
       "1       /usr/bin/tini -- /usr/local/bin/afclaude-entrypoint",
       "2       /usr/local/bin/supercronic -passthrough-logs /mnt/BlockVolume/Claude/work/AFClaude/docker/crontab",
       "3       python3 -u /mnt/BlockVolume/Claude/work/AFClaude/inbox_api.py serve",
       "4       ssh: /tmp/afclaude-ssh-deadbeef [mux]"]
SAMPLER = IDLE + ["5       flock -n data/.sampler.lock python3 /mnt/BlockVolume/Claude/work/AFClaude/usage_sampler.py"]
BRIDGE_CALL = IDLE + ["6       ssh -T -i /run/secrets/host_bridge_ed25519 -o ControlMaster=auto -- "
                     "claude -p --no-session-persistence --permission-mode dontAsk /usage"]
UNITTEST = IDLE + ["7       python3 -m unittest test_keepalive"]
REVIEW = IDLE + ["8       python3 /mnt/BlockVolume/Claude/work/AFClaude/usage_review.py"]


class FakeCompleted:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class BusyTest(unittest.TestCase):
    def test_idle_container_not_busy(self):
        # tini, supercronic, inbox_api and an idle ControlMaster mux socket: nothing to wait for.
        self.assertEqual(deploy.busy(IDLE[1:]), [])

    def test_sampler_is_busy(self):
        hits = deploy.busy(SAMPLER[1:])
        self.assertEqual(len(hits), 1)
        self.assertIn("usage_sampler.py", hits[0])

    def test_bridge_usage_call_is_busy(self):
        # the ssh-bridge line names "claude" as the remote command's own argv, not the local one.
        hits = deploy.busy(BRIDGE_CALL[1:])
        self.assertEqual(len(hits), 1)
        self.assertIn("claude", hits[0])

    def test_unittest_is_busy(self):
        self.assertEqual(len(deploy.busy(UNITTEST[1:])), 1)

    def test_usage_review_is_busy(self):
        self.assertEqual(len(deploy.busy(REVIEW[1:])), 1)

    def test_afclaude_itself_is_not_mistaken_for_claude(self):
        # "afclaude"/"AFCLAUDE_DIR" contain the letters "claude" but never as a whole word.
        lines = ["1    /usr/local/bin/afclaude-entrypoint",
                "2    AFCLAUDE_DIR=/mnt/BlockVolume/Claude/work/AFClaude python3 keepalive.py --arm"]
        self.assertEqual(deploy.busy(lines), [])


class ContainerProcessesTest(unittest.TestCase):
    def test_strips_header(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return FakeCompleted("\n".join(SAMPLER) + "\n")

        lines = deploy.container_processes(run=fake_run)
        self.assertEqual(lines, SAMPLER[1:])
        self.assertEqual(calls[0][:2], ["docker", "top"])

    def test_nonzero_rc_is_no_processes(self):
        self.assertEqual(deploy.container_processes(run=lambda *a, **k: FakeCompleted("", 1)), [])

    def test_docker_missing_is_no_processes(self):
        def fake_run(*a, **k):
            raise OSError("no such file")
        self.assertEqual(deploy.container_processes(run=fake_run), [])


class WaitClearTest(unittest.TestCase):
    def test_already_clear_returns_true_at_once(self):
        calls = {"sleep": 0}
        ok = deploy.wait_clear(timedelta(minutes=5), lister=lambda: IDLE[1:],
                              sleep=lambda s: calls.__setitem__("sleep", calls["sleep"] + 1),
                              clock=iter([0.0]).__next__, out=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(calls["sleep"], 0)

    def test_clears_after_a_few_polls(self):
        # busy, busy, then idle: two sleeps, then True.
        snapshots = iter([SAMPLER[1:], SAMPLER[1:], IDLE[1:]])
        ticks = iter([0.0, 0.0, 1.0, 1.0, 2.0, 2.0])
        sleeps = []
        ok = deploy.wait_clear(timedelta(minutes=5), lister=lambda: next(snapshots),
                              sleep=lambda s: sleeps.append(s), clock=lambda: next(ticks),
                              out=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(len(sleeps), 2)

    def test_times_out_while_still_busy(self):
        ticks = iter([0.0, 0.0, 10.0])   # deadline at 5 s; still busy once, then past it
        msgs = []
        ok = deploy.wait_clear(timedelta(seconds=5), lister=lambda: SAMPLER[1:],
                              sleep=lambda s: None, clock=lambda: next(ticks), out=msgs.append)
        self.assertFalse(ok)
        self.assertIn("NOT clear", msgs[-1])
        self.assertIn("usage_sampler.py", msgs[-1])



CEST = timezone(timedelta(hours=2))
CFG = schedule.Config.every_day("23:00", 2)       # starts 23:00 and 04:00 CEST


def at(day, hh, mm=0):
    return datetime(2026, 10, day, hh, mm, tzinfo=CEST)


class StartGuardTest(unittest.TestCase):
    """No rebuild within 30 min before or after a session-window start (D-257: from S5 on a
    restart during a start alerts + pauses)."""

    def test_far_from_a_start_is_safe_now(self):
        now = at(20, 12)
        self.assertEqual(deploy.start_guard(now, CFG), (None, now.astimezone(timezone.utc)))

    def test_before_a_start(self):
        near, safe = deploy.start_guard(at(20, 22, 40), CFG)
        self.assertEqual(near, at(20, 23))
        self.assertEqual(safe, at(20, 23, 30))

    def test_after_a_start(self):
        near, safe = deploy.start_guard(at(21, 4, 10), CFG)
        self.assertEqual(near, at(21, 4))
        self.assertEqual(safe, at(21, 4, 30))

    def test_exactly_the_guard_away_is_safe(self):
        self.assertIsNone(deploy.start_guard(at(20, 22, 30), CFG)[0])
        self.assertIsNone(deploy.start_guard(at(20, 23, 30), CFG)[0])
        self.assertIsNotNone(deploy.start_guard(at(20, 22, 31), CFG)[0])

    def test_close_starts_chain(self):
        # a 30-min session length puts the starts 30 min apart: one guard after another.
        cfg = schedule.Config.every_day("23:00", 3, session_hours=0.5)
        near, safe = deploy.start_guard(at(20, 22, 45), cfg)
        self.assertEqual(near, at(20, 23))
        self.assertEqual(safe, at(21, 0, 30))

    def test_no_windows_is_always_safe(self):
        cfg = schedule.Config({d: None for d in schedule.DAYS})
        self.assertIsNone(deploy.start_guard(at(20, 23), cfg)[0])

    def test_check_refuses_and_prints_the_next_safe_time(self):
        msgs = []
        self.assertFalse(deploy.check_start_guard(False, at(20, 22, 50), CFG, out=msgs.append))
        self.assertIn("deploy refused", msgs[-1])
        self.assertIn("next safe time 2026-10-20 23:30", msgs[-1])

    def test_force_deploys_anyway_with_the_warning(self):
        msgs = []
        self.assertTrue(deploy.check_start_guard(True, at(20, 22, 50), CFG, out=msgs.append))
        self.assertIn("--force", msgs[-1])

    def test_check_passes_quietly_far_from_a_start(self):
        msgs = []
        self.assertTrue(deploy.check_start_guard(False, at(20, 12), CFG, out=msgs.append))
        self.assertEqual(msgs, [])


class MainGuardTest(unittest.TestCase):
    """main(): a refusal before the drain touches nothing; one after it lifts the drain."""

    def _main(self, guards, drained=0):
        calls = []
        with mock.patch.object(deploy, "check_start_guard", side_effect=guards), \
             mock.patch.object(deploy.store, "connect", return_value=mock.Mock()), \
             mock.patch.object(deploy.runner, "drain", side_effect=lambda *a, **k: calls.append("drain") or drained), \
             mock.patch.object(deploy.runner, "undrain", side_effect=lambda *a: calls.append("undrain")), \
             mock.patch.object(deploy, "wait_clear", return_value=True), \
             mock.patch.object(deploy.subprocess, "run", side_effect=AssertionError("no rebuild")), \
             mock.patch("builtins.print"):
            rc = deploy.main([])
        return rc, calls

    def test_refused_before_the_drain(self):
        self.assertEqual(self._main([False]), (1, []))

    def test_refused_after_the_drain_lifts_it(self):
        self.assertEqual(self._main([True, False]), (1, ["drain", "undrain"]))

    def test_not_drained_lifts_it(self):
        self.assertEqual(self._main([True, True], drained=1), (1, ["drain", "undrain"]))


if __name__ == "__main__":
    unittest.main()
