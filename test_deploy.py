#!/usr/bin/env python3
"""Tests for tools/deploy.py (D-257, the 21.10. incident): the container-quiet wait logic,
with fake `docker top` output and a fake clock/sleep (no docker, no real waiting)."""
import os
import sys
import unittest
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
import deploy  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
