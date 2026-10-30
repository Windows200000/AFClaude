#!/usr/bin/env python3
"""Offline tests for rc_continue.py (D-288, docs/rc_server_mode.md §4.1): the registry read, the
Haiku sender's stream-json check and the transcript verification. Fake registry, pids and sender
only: no process is started, messaged or signalled. The dispatcher's use: test_dispatcher.RcThreads."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: a temp DB, config and data dir; before the AFClaude imports)
import host  # noqa: E402
import keepalive as ka  # noqa: E402
import rc_continue as rc  # noqa: E402
from test_dispatcher import sender_out  # noqa: E402

SID = "abcdef01-0000-4000-8000-000000000001"
CHILD = ["/x/versions/2.1.314", "--print", "--sdk-url", "https://x", "--session-id", "cse_1"]
PROCS = {100: (50, CHILD), 50: (1, ["claude", "rc"]), 200: (10, ["claude", "--resume", SID]), 10: (1, ["-bash"]),
         300: (1, ["node", "agent-sdk-host"])}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions = os.path.join(self.tmp.name, "sessions")
        self.projects = os.path.join(self.tmp.name, "projects")
        os.makedirs(self.sessions)
        os.makedirs(os.path.join(self.projects, "-work"))
        self.path = os.path.join(self.projects, "-work", f"{SID}.jsonl")
        with open(self.path, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "limit"}]}}) + "\n")
        for target, value in ((ka, {"SESSIONS_DIR": self.sessions, "PROJECTS_DIR": self.projects}),):
            for k, v in value.items():
                p = mock.patch.object(target, k, v)
                p.start()
                self.addCleanup(p.stop)
        for k, f in (("pid_alive", lambda pid: pid in PROCS), ("proc_argv", lambda pid: PROCS.get(pid, (0, []))[1]),
                     ("proc_ppid", lambda pid: PROCS.get(pid, (None, []))[0]), ("proc_starttime", lambda pid: "77")):
            p = mock.patch.object(ka, k, f)
            p.start()
            self.addCleanup(p.stop)

    def register(self, pid=100, **kw):
        with open(os.path.join(self.sessions, f"{pid}.json"), "w") as fh:
            json.dump(dict({"pid": pid, "sessionId": SID, "entrypoint": "sdk-cli", "name": "work-a1",
                            "status": "idle"}, **kw), fh)


class ThreadEntry(Base):
    def test_live_rc_child(self):
        self.register(procStart="77")
        e = rc.thread_entry(SID)
        self.assertEqual((e["pid"], e["name"], e["argv"]), (100, "work-a1", CHILD))

    def test_errors(self):
        with self.assertRaisesRegex(rc.RcError, "no ~/.claude/sessions registry entry"):
            rc.thread_entry(SID)
        self.register(pid=400)
        with self.assertRaisesRegex(rc.RcError, "no live child"):
            rc.thread_entry(SID)
        self.register(pid=400, procStart="1")
        self.register(pid=100, procStart="1")                       # a reused pid: a stale file
        with self.assertRaisesRegex(rc.RcError, "no live child"):
            rc.thread_entry(SID)
        os.remove(os.path.join(self.sessions, "400.json"))
        for pid, kw in ((200, {"entrypoint": "cli"}), (300, {})):   # a terminal; an SDK host that is no rc thread
            os.remove(os.path.join(self.sessions, os.listdir(self.sessions)[0]))
            self.register(pid=pid, **kw)
            with self.assertRaisesRegex(rc.RcError, "no rc-server thread child"):
                rc.thread_entry(SID)
        self.register(pid=100)
        self.register(pid=200, entrypoint="sdk-cli")
        with self.assertRaisesRegex(rc.RcError, "several live processes"):
            rc.thread_entry(SID)


class CheckSender(unittest.TestCase):
    def test_delivered(self):
        self.assertIn("inbox", rc.check_sender(sender_out("work-a1"), "work-a1"))
        nested = sender_out("work-a1").stdout.replace('"to": "work-a1"', '"to": {"name": "work-a1"}')
        self.assertIn("inbox", rc.check_sender(subprocess.CompletedProcess([], 0, nested, ""), "work-a1"))

    def test_failures(self):
        for out, why in ((sender_out("a", rc=1), "exited rc=1"),
                         (sender_out("a", rc=126), "denied the rc-send shape"),
                         (sender_out("a", uses=0), "made 0 SendMessage calls"),
                         (sender_out("a", uses=2), "made 2 SendMessage calls"),
                         (sender_out("a", target="b"), "did not address 'a'"),
                         (sender_out("a", result="No agent named a is reachable", is_error=True), "is reachable"),
                         (sender_out("a", result="Message held: the session bypasses permissions"), "held or refused"),
                         (sender_out("a", result="Delivery refused by the receiver"), "held or refused"),
                         (sender_out("a", result="something broke", is_error=True), "held or refused"),
                         (subprocess.CompletedProcess([], 0, "", ""), "made 0 SendMessage")):
            with self.subTest(why=why), self.assertRaisesRegex(rc.RcError, why):
                rc.check_sender(out, "a")
        no_result = "\n".join(ln for ln in sender_out("a").stdout.splitlines() if '"tool_result"' not in ln)
        with self.assertRaisesRegex(rc.RcError, "has no result"):
            rc.check_sender(subprocess.CompletedProcess([], 0, no_result, ""), "a")
        bad_end = sender_out("a").stdout.replace('"subtype": "success", "is_error": false', '"is_error": true')
        with self.assertRaisesRegex(rc.RcError, "ended in an error"):
            rc.check_sender(subprocess.CompletedProcess([], 0, bad_end, ""), "a")


class Send(Base):
    def answer(self, user=True, answer=True):
        with open(self.path, "a") as fh:
            if user:
                fh.write(json.dumps({"type": "user", "message": {"content": [
                    {"type": "text", "text": '<cross-session-message from="x">go</cross-session-message>'}]}}) + "\n")
            if answer:
                fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}})
                         + "\n")

    def test_verified_after_polling(self):
        self.register()
        sleeps = []

        def sleep(s):                                                 # the thread answers on the 2nd look
            sleeps.append(s)
            self.answer()
        res = rc.send(SID, "go", runner=lambda n, t, timeout: sender_out(n), sleep=sleep, verify_seconds=60)
        self.assertEqual((res["name"], res["pid"], sleeps), ("work-a1", 100, [rc.POLL_SECONDS]))

    def test_old_entries_do_not_count(self):
        self.answer()                                                 # before the send: not this message's
        self.register()
        with self.assertRaisesRegex(rc.RcError, "no <cross-session-message> entry within 0 s"):
            rc.send(SID, "go", runner=lambda n, t, timeout: sender_out(n), verify_seconds=0)

    def test_sender_errors(self):
        self.register()

        def timeout(n, t, timeout):
            raise subprocess.TimeoutExpired("claude", timeout)
        with self.assertRaisesRegex(rc.RcError, "timed out after"):
            rc.send(SID, "go", runner=timeout, verify_seconds=0)

        def oserror(n, t, timeout):
            raise OSError("ssh: connect refused")
        with self.assertRaisesRegex(rc.RcError, "OSError: ssh"):
            rc.send(SID, "go", runner=oserror, verify_seconds=0)

    def test_refused_before_sending(self):
        never = mock.Mock(side_effect=AssertionError("no send"))
        for kw, exc, why in (({"status": "busy"}, rc.Busy, "is busy"),
                             ({"status": "waiting"}, rc.RcError, "not idle/busy"),
                             ({"name": "a b;c"}, rc.RcError, "no usable registry name"),
                             ({"name": ""}, rc.RcError, "no usable registry name")):
            self.register(**kw)
            with self.subTest(kw=kw), self.assertRaisesRegex(exc, why):
                rc.send(SID, "go", runner=never, verify_seconds=0)
        os.remove(self.path)
        self.register()
        with self.assertRaisesRegex(rc.RcError, "no transcript"):
            rc.send(SID, "go", runner=never, verify_seconds=0)


class HostRcSend(unittest.TestCase):
    """host.rc_send: the local `claude -p` on the host, the rc-send bridge shape in the container."""

    def test_host_mode(self):
        with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": ""}), \
                mock.patch.object(host.subprocess, "run", return_value="r") as run:
            self.assertEqual(host.rc_send("work-a1", "go on", 30), "r")
        argv, kw = run.call_args
        self.assertEqual(argv[0], ["claude"] + host.RC_SEND_FLAGS)
        self.assertEqual(kw["input"], host.rc_send_prompt("work-a1", "go on"))
        self.assertEqual((kw["timeout"], os.path.basename(kw["cwd"])), (30, "data"))

    def test_container_mode(self):
        with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": "1"}), \
                mock.patch.object(host, "run_on_host", return_value="r") as run:
            host.rc_send("work-a1", "go on", 30)
        run.assert_called_once_with(["rc-send", "work-a1"], input="go on", timeout=30)

    def test_bad_input(self):
        for name, text in (("a b", "x"), ("-x", "x"), ("", "x"), ("a", ""), ("a", "x\0y"),
                           ("a", "x" * (host.RC_TEXT_MAX + 1))):
            with self.subTest(name=name, n=len(text)), self.assertRaises(ValueError):
                host.rc_send(name, text, 30)


if __name__ == "__main__":
    unittest.main()
