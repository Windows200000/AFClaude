#!/usr/bin/env python3
"""Offline tests for docker/start_watcher.sh and the entrypoint's watcher start (the container
watcher comes back right after a rebuild, not only at the next :07/:22/:37/:52 watchdog).

Hermetic: the scripts run from a temp AFCLAUDE_DIR with a fake host.py (the bridge's process
list), a fake keepalive.py (records its argv) and a stub pgrep (a marker file = "running")."""
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SID = "f" * 8 + "-0000-0000-0000-" + "0" * 12

FAKE_HOST = '''
import json, os, time
D = os.path.dirname(os.path.abspath(__file__))
def proc_snapshot():
    time.sleep(float(os.environ.get("FAKE_HOST_DELAY", "0")))
    fails = os.path.join(D, "fails")
    if os.path.exists(fails):
        n = int(open(fails).read() or 0)
        if n > 0:
            open(fails, "w").write(str(n - 1))
            raise OSError("proc-snapshot via host bridge failed rc=255: connection refused")
    raw = json.loads(os.environ.get("FAKE_SNAP", "{}"))      # host_exec's shape, parsed as host.py does
    return {int(k): {"ppid": v[0], "start": str(v[1]), "argv": v[2]} for k, v in raw.items()}
'''
FAKE_KEEPALIVE = '''
import os, sys
D = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(D, "started.log"), "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
open(os.path.join(D, "running"), "w").close()
'''
STUB_PGREP = '''#!/bin/sh
[ -e "$AFCLAUDE_DIR/running" ]
'''


class StartWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = self.tmp.name
        os.makedirs(os.path.join(d, "docker"))
        os.makedirs(os.path.join(d, "bin"))
        for name in ("start_watcher.sh", "entrypoint.sh"):
            dst = os.path.join(d, "docker", name)
            shutil.copy(os.path.join(HERE, "docker", name), dst)
            os.chmod(dst, 0o755)
        with open(os.path.join(d, "host.py"), "w") as fh:
            fh.write(FAKE_HOST)
        with open(os.path.join(d, "keepalive.py"), "w") as fh:
            fh.write(FAKE_KEEPALIVE)
        pg = os.path.join(d, "bin", "pgrep")
        with open(pg, "w") as fh:
            fh.write(STUB_PGREP)
        os.chmod(pg, os.stat(pg).st_mode | stat.S_IEXEC)
        self.state = os.path.join(d, "data", "keepalive")
        self.env = {"PATH": os.path.join(d, "bin") + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": d, "LANG": "C.UTF-8", "AFCLAUDE_DIR": d, "KEEPALIVE_STATE_DIR": self.state,
                    "AFCLAUDE_MANAGER_SESSION": SID, "AFCLAUDE_BOOT_RETRY": "0", "AFCLAUDE_BOOT_SETTLE": "0.3",
                    "AFCLAUDE_BOOT_TRIES": "5", "AFCLAUDE_BRIDGE_KNOWN_HOSTS": os.path.join(d, "known_hosts")}

    def tearDown(self):
        self.tmp.cleanup()

    def sh(self, *args, env=None, timeout=30):
        return subprocess.run([os.path.join(self.d, "docker", "start_watcher.sh"), *args], env=dict(self.env, **(env or {})),
                              capture_output=True, text=True, timeout=timeout)

    def started(self, wait=3.0):
        """The fake watcher's argv lines (waits a moment: it is started in the background)."""
        p = os.path.join(self.d, "started.log")
        end = time.monotonic() + wait
        while time.monotonic() < end and not os.path.exists(p):
            time.sleep(0.05)
        time.sleep(0.2)                       # a second start would show up by now
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return fh.read().splitlines()

    def test_starts_the_watcher(self):
        os.makedirs(self.state)
        open(os.path.join(self.state, "STOP"), "w").close()
        r = self.sh()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.started(), [f"--session {SID} --arm"])
        self.assertFalse(os.path.exists(os.path.join(self.state, "STOP")))   # a start clears STOP

    def test_noop_while_one_runs_or_paused(self):
        open(os.path.join(self.d, "running"), "w").close()
        self.assertEqual(self.sh().returncode, 0)
        os.remove(os.path.join(self.d, "running"))
        open(os.path.join(self.d, "PAUSED"), "w").close()
        self.assertEqual(self.sh().returncode, 0)
        self.assertEqual(self.started(wait=0.5), [])

    def test_host_watcher_blocks_but_one_shot_runs_do_not(self):
        watcher = {"901": [1, "1", ["python3", "-u", "/x/keepalive.py", "--session", SID, "--arm"]]}
        r = self.sh(env={"FAKE_SNAP": json.dumps(watcher)})
        self.assertIn("runs on the HOST", r.stderr)
        self.assertEqual(self.started(wait=0.5), [])
        # the */30 window-start run (or --plan-fillup-test) is no watcher: it must not block the start
        one_shot = {"902": [1, "1", ["python3", "keepalive.py", "--session", SID, "--window-start", "--arm"]],
                    "903": [1, "1", ["python3", "keepalive.py", "--session", SID, "--plan-fillup-test", "--arm"]]}
        self.sh(env={"FAKE_SNAP": json.dumps(one_shot)})
        self.assertEqual(len(self.started()), 1)

    def test_bridge_down_no_start(self):
        with open(os.path.join(self.d, "fails"), "w") as fh:
            fh.write("1")
        r = self.sh()
        self.assertIn("host process list unavailable", r.stderr)
        self.assertEqual(self.started(wait=0.5), [])

    def test_boot_retries_until_the_bridge_answers(self):
        with open(os.path.join(self.d, "fails"), "w") as fh:
            fh.write("2")
        r = self.sh("--boot")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.count("host process list unavailable"), 2)
        self.assertEqual(self.started(), [f"--session {SID} --arm"])

    def test_boot_gives_up_after_its_tries(self):
        with open(os.path.join(self.d, "fails"), "w") as fh:
            fh.write("99")
        r = self.sh("--boot", env={"AFCLAUDE_BOOT_TRIES": "3", "AFCLAUDE_BOOT_SETTLE": "0"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("no watcher after 3 tries", r.stderr)
        self.assertEqual(self.started(wait=0.3), [])

    def test_boot_respects_paused(self):
        open(os.path.join(self.d, "PAUSED"), "w").close()
        t = time.monotonic()
        self.assertEqual(self.sh("--boot", env={"AFCLAUDE_BOOT_RETRY": "5"}).returncode, 0)
        self.assertLess(time.monotonic() - t, 3)
        self.assertEqual(self.started(wait=0.3), [])

    def test_concurrent_starts_start_one(self):
        """The entrypoint's start and the watchdog at the same moment: the lock lets one through."""
        env = dict(self.env, FAKE_HOST_DELAY="0.7")
        script = os.path.join(self.d, "docker", "start_watcher.sh")
        ps = [subprocess.Popen([script], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
              for _ in range(3)]
        for p in ps:
            p.wait(timeout=30)
        self.assertEqual(self.started(), [f"--session {SID} --arm"])

    def entrypoint(self, scheduler, wait):
        p = subprocess.Popen([os.path.join(self.d, "docker", "entrypoint.sh")],
                             env=dict(self.env, AFCLAUDE_SCHEDULER=scheduler, AFCLAUDE_CRONTAB="/nonexistent"),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            out, err = p.communicate(timeout=wait)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
        return out, err

    def test_entrypoint_starts_the_watcher_at_once_when_the_scheduler_is_on(self):
        # supercronic is not on this machine: the exec fails, the backgrounded start still runs
        out, _ = self.entrypoint("on", wait=10)
        self.assertIn("scheduler ON", out)
        self.assertEqual(self.started(wait=5), [f"--session {SID} --arm"])

    def test_entrypoint_off_starts_nothing(self):
        out, _ = self.entrypoint("off", wait=1.5)
        self.assertIn("scheduler OFF", out)
        self.assertEqual(self.started(wait=0.5), [])

    def test_entrypoint_on_but_paused_starts_nothing(self):
        open(os.path.join(self.d, "PAUSED"), "w").close()
        self.entrypoint("on", wait=10)
        self.assertEqual(self.started(wait=1.5), [])


if __name__ == "__main__":
    unittest.main()
