"""Phase 5b K6: the host checks (hostcheck.py), against a stub host kit: nothing here reaches a bridge, a tmux or a
Claude. Every check ok / warn / fail, a timeout is a fail, a blocking fail pauses and alerts once per episode, a
warn does not alert, the transitional no-kit mode, the e2e check against a stub tmux, the runner job, the
pre-start hook, the CLI and the quickview block."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from datetime import timedelta
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

import testenv  # noqa: F401  (before any AFClaude module)
import actions
import export_quickview as qv
import failstop
import host
import hostcheck
import keepalive as ka
import notify
import runner
import runner_jobs
import store

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = {"kit_version": host.HOST_KIT_VERSION, "commands": ["kit-version", "tmux-check", "df", "clock"],
       "files": None, "config_format": 2}


def done(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


AUTH_OK = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                      "subscriptionType": "team", "email": "someone@example.org"})
DOCTOR_CLEAN = ("Claude Code doctor\n\nRunning: native (2.1.325)\nPlatform: linux-arm64\n\nRemote Control\n"
                "Control this session from claude.ai/code\n\nNo installation issues found.\n")


class StubKit:
    """The host as the checks see it: `host.run_on_host` answers by the command's head from `answers`
    (a CompletedProcess, or a callable(argv, input), or an exception to raise)."""

    def __init__(self, **answers):
        self.calls = []
        self.multiplex = []      # per call: False = not over the ssh master socket (host.ssh_argv)
        self.answers = {"tmux has-session": done(1), "tmux-check": done(0, "ok\n"), "claude auth status": done(0, AUTH_OK),
                        "claude doctor": done(0, DOCTOR_CLEAN),
                        "df": done(0, json.dumps({"claude_dir": {"path": "/c", "total_bytes": 100 * 10**9, "free_bytes": 50 * 10**9},
                                                  "repo": {"path": "/r", "total_bytes": 100 * 10**9, "free_bytes": 50 * 10**9}})),
                        "clock": None}
        alias = {"tmux_has": "tmux has-session", "tmux_check": "tmux-check", "claude_auth_status": "claude auth status",
                 "claude_doctor": "claude doctor", "df": "df", "clock": "clock"}
        self.answers.update({alias[k]: v for k, v in answers.items()})

    def key(self, argv):
        head = argv[0]
        if head == "claude":
            return "claude " + " ".join(argv[1:3])
        if head == "tmux":
            return "tmux " + argv[1]
        return head

    def run(self, argv, input=None, cwd=None, env=None, timeout=None, multiplex=True):
        self.calls.append((list(argv), timeout))
        self.multiplex.append(multiplex)
        a = self.answers.get(self.key(argv))
        if a is None and argv[0] == "clock":
            import time
            return done(0, json.dumps({"epoch_ms": int(time.time() * 1000)}))
        if isinstance(a, BaseException):
            raise a
        return a(argv, input) if callable(a) else a


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        self.alerts = []
        p = mock.patch.object(notify, "notify", lambda subject, body="": self.alerts.append(subject) or "sent")
        p.start()
        self.addCleanup(p.stop)
        self.clear_pause()
        with store.transaction(self.conn):
            self.conn.execute("DELETE FROM host_checks")
        testenv.clear_settings()
        self.addCleanup(testenv.clear_settings)

    def clear_pause(self):
        actions.perform(self.conn, "automation.set", {"paused": False, "reason": ""}, actor="cli", via="cli")

    def paused(self):
        return failstop.paused()

    def stub(self, **answers):
        s = StubKit(**answers)
        for name, val in (("run_on_host", s.run),
                          ("check_host", lambda timeout=30: dict(KIT, files=host.kit_source_hashes())),
                          ("usage_cache", lambda: {"fetchedAtMs": int(__import__("time").time() * 1000) - 600000})):
            p = mock.patch.object(host, name, val)
            p.start()
            self.addCleanup(p.stop)
        return s

    def run_one(self, name, ctx=None):
        ctx = ctx or hostcheck.Ctx(conn=self.conn)
        return hostcheck.execute(hostcheck.BY_NAME[name], ctx)

    def rows(self):
        return {r["name"]: r for r in store.host_check_rows(self.conn)}


class EachCheck(Base):
    def test_bridge(self):
        for rc, status in ((1, "ok"), (0, "ok"), (255, "fail"), (3, "fail")):
            with self.subTest(rc=rc):
                self.stub(tmux_has=done(rc, "", "Permission denied (publickey)"))
                o = self.run_one("bridge")
                self.assertEqual(o.status, status)
        self.assertIn("from=", o.fix if o.status == "fail" and rc == 255 else "from=")
        self.stub(tmux_has=done(255))
        self.assertIn("from=", self.run_one("bridge").fix)

    def test_kit(self):
        self.stub()
        o = self.run_one("kit")
        self.assertEqual((o.status, o.kit_version), ("ok", host.HOST_KIT_VERSION))
        err = host.HostKitError("host kit version '1', this code needs '6'")
        with mock.patch.object(host, "check_host", side_effect=err):
            o = self.run_one("kit")
        self.assertEqual(o.status, "fail")
        self.assertIn("install_host_bridge.sh", o.fix)
        self.assertIn("version '1'", o.detail)

    def test_kit_newer_than_the_code_says_deploy_not_install(self):
        self.stub()
        newer = host.HostKitTooNew("host kit version '99' is newer than this code's '7': deploy the matching code")
        with mock.patch.object(host, "check_host", side_effect=newer):
            o = self.run_one("kit")
        self.assertEqual(o.status, "fail")
        self.assertIn("tools/deploy.py", o.fix)
        self.assertNotIn("re-run the host kit install", o.fix)

    def test_tmux(self):
        for ans, status in ((done(0, "ok\n"), "ok"), (done(1, "", "host_exec: tmux-check failed"), "fail"),
                            (done(124, "", "stopped after 60 s"), "fail"), (done(0, "weird\n"), "fail")):
            with self.subTest(rc=ans.returncode, out=ans.stdout):
                self.stub(tmux_check=ans)
                self.assertEqual(self.run_one("tmux").status, status)

    def test_claude_and_logged_in(self):
        cases = [(done(0, AUTH_OK), "ok", "ok"),
                 (done(0, json.dumps({"loggedIn": True, "authMethod": "console"})), "ok", "warn"),
                 (done(1, json.dumps({"loggedIn": False})), "ok", "fail"),
                 (done(1, "Not logged in. Run claude auth login\n"), "ok", "fail"),
                 (done(0, "garbage"), "fail", "skip"),
                 (done(124, "", "claude stopped after 60 s"), "fail", "skip"),
                 (done(127, "", "claude: not found"), "fail", "skip")]
        for ans, want_claude, want_login in cases:
            with self.subTest(rc=ans.returncode, out=ans.stdout[:20]):
                self.stub(claude_auth_status=ans)
                ctx = hostcheck.Ctx(conn=self.conn)
                self.assertEqual(self.run_one("claude", ctx).status, want_claude)
                o = self.run_one("logged_in", ctx)
                self.assertEqual(o.status, want_login)
                self.assertNotIn("someone@example.org", o.detail)     # no personal data from the answer is kept

    def test_claude_version_against_the_tested_prefix(self):
        self.stub()
        o = self.run_one("claude_version")
        self.assertEqual(o.status, "ok", o.detail)
        testenv.set_setting("host_claude_tested_version", "2.0")
        o = self.run_one("claude_version")
        self.assertEqual(o.status, "warn")
        self.assertIn("newer than the tested 2.0.x", o.detail)
        self.stub(claude_doctor=done(0, "no version here"))
        self.assertEqual(self.run_one("claude_version").status, "warn")

    def test_doctor(self):
        rc_text = "Running: native (2.1.325)\nRemote Control\nRemote Control is not available for this account.\n"
        cases = [(done(0, DOCTOR_CLEAN), "ok"),
                 (done(0, "Running: native (2.1.325)\nInvalid settings file: ~/.claude/settings.json error at hooks\n"), "fail"),
                 (done(0, rc_text), "warn"),
                 (done(0, "Running: native (2.1.325)\nSomething new and odd\n"), "warn"),
                 (done(1, "", "crashed"), "fail"), (done(124, "", "stopped"), "fail")]
        for cat in ("task_manager", "review", "continue", "test"):
            testenv.set_setting(f"remote_control_{cat}", False)       # every RC category off
        for ans, status in cases:
            with self.subTest(out=ans.stdout[-30:], rc=ans.returncode):
                self.stub(claude_doctor=ans)
                self.assertEqual(self.run_one("doctor").status, status)
        self.stub(claude_doctor=done(0, rc_text))
        for cat in ("task_manager", "review", "continue", "test"):
            testenv.set_setting(f"remote_control_{cat}", cat == "task_manager")
        o = self.run_one("doctor")
        self.assertEqual(o.status, "fail")      # an RC category is on while RC is not available
        self.assertIn("task_manager", o.detail)

    def test_usage(self):
        now = int(__import__("time").time() * 1000)
        for age_min, status in ((10, "ok"), (120, "warn"), (500, "fail")):
            with self.subTest(age=age_min):
                self.stub()
                with mock.patch.object(host, "usage_cache", lambda: {"fetchedAtMs": now - age_min * 60000}):
                    self.assertEqual(self.run_one("usage").status, status)
        self.stub()
        with mock.patch.object(host, "usage_cache", lambda: {}):
            self.assertEqual(self.run_one("usage").status, "fail")
        with mock.patch.object(host, "usage_cache", side_effect=OSError("usage-cache via host bridge failed rc=1")):
            o = self.run_one("usage")
        self.assertEqual(o.status, "fail")
        self.assertIn("OSError", o.detail)

    def test_disk(self):
        self.stub()
        self.assertEqual(self.run_one("disk").status, "ok")
        testenv.set_setting("host_disk_warn_gb", 60.0)      # the stub host has 50 GB free
        o = self.run_one("disk")
        self.assertEqual(o.status, "warn", o.detail)
        testenv.set_setting("host_disk_fail_gb", 55.0)
        self.assertEqual(self.run_one("disk").status, "fail")
        testenv.set_setting("host_disk_warn_gb", 5.0)
        testenv.set_setting("host_disk_fail_gb", 1.0)
        self.stub(df=done(1, "", "df: boom"))
        self.assertEqual(self.run_one("disk").status, "fail")

    def test_clock(self):
        import time
        for drift, status in ((0.2, "ok"), (-8, "warn"), (120, "fail")):
            with self.subTest(drift=drift):
                self.stub(clock=lambda argv, input, d=drift: done(0, json.dumps({"epoch_ms": int((time.time() + d) * 1000)})))
                self.assertEqual(self.run_one("clock").status, status)

    def test_mcp_against_stub_servers(self):
        good = textwrap.dedent('''
            import json, sys
            for line in sys.stdin:
                m = json.loads(line)
                if m.get("method") == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": "2025-06-18",
                          "capabilities": {}, "serverInfo": {"name": "stub", "version": "1"}}}), flush=True)
                elif m.get("method") == "tools/list":
                    print("a log line that is not JSON-RPC", flush=True)
                    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": "t1"}, {"name": "t2"}]}}), flush=True)
        ''')
        empty = good.replace('[{"name": "t1"}, {"name": "t2"}]', '[]')
        dies = "import sys; sys.stderr.write('boom: no database'); sys.exit(3)"
        hangs = "import time; time.sleep(60)"
        errs = good.replace('"result": {"tools"', '"error": {"code": -1, "message": "no"}, "x": {"tools"')
        with tempfile.TemporaryDirectory() as d:
            def server(name, code):
                p = os.path.join(d, name + ".py")
                with open(p, "w") as fh:
                    fh.write(code)
                return [sys.executable, p]
            self.assertEqual(hostcheck.mcp_tools(server("good", good), 20), ["t1", "t2"])
            self.assertEqual(hostcheck.mcp_tools(server("empty", empty), 20), [])
            with self.assertRaisesRegex(RuntimeError, "exited.*boom: no database"):
                hostcheck.mcp_tools(server("dies", dies), 20)
            with self.assertRaises(subprocess.TimeoutExpired):
                hostcheck.mcp_tools(server("hangs", hangs), 1.5)
            with self.assertRaisesRegex(RuntimeError, "tools/list failed"):
                hostcheck.mcp_tools(server("errs", errs), 20)
        for tools, status in ((["t1"], "ok"), ([], "fail")):
            with self.subTest(tools=tools), mock.patch.object(hostcheck, "mcp_tools", lambda argv, timeout, t=tools: t):
                self.assertEqual(self.run_one("mcp").status, status)
        with mock.patch.object(hostcheck, "mcp_tools", side_effect=RuntimeError("the MCP server exited (rc 3): boom")):
            o = self.run_one("mcp")
        self.assertEqual(o.status, "fail")
        self.assertIn("boom", o.detail)
        with mock.patch.object(hostcheck, "mcp_tools", side_effect=subprocess.TimeoutExpired("mcp", 60)):
            self.assertIn("no answer within 60 s", self.run_one("mcp").detail)

    @unittest.skipUnless(__import__("importlib").util.find_spec("mcp"), "needs the MCP SDK (the venv python)")
    def test_mcp_against_the_real_stdio_server(self):
        o = self.run_one("mcp")
        self.assertEqual(o.status, "ok", o.detail)
        self.assertRegex(o.detail, r"lists \d+ tools")


class BridgeRetry(Base):
    """One slow or refused bridge answer is retried once before the check fails (a blip must not lose a night)."""

    def test_rc_255_then_an_answer_is_ok(self):
        answers = iter([done(255, "", "mux_client_request_session: read from master failed"), done(1)])
        s = self.stub(tmux_has=lambda argv, input: next(answers))
        o = self.run_one("bridge")
        self.assertEqual(o.status, "ok")
        self.assertIn("retried once after ssh rc 255", o.detail)
        self.assertEqual(len(s.calls), 2)
        self.assertEqual(s.multiplex, [True, False])      # review L2: the retry bypasses the master socket

    def test_no_answer_then_an_answer_is_ok_and_the_first_call_leaves_room(self):
        answers = iter([subprocess.TimeoutExpired("ssh", 30), done(0)])

        def answer(argv, input):
            a = next(answers)
            if isinstance(a, BaseException):
                raise a
            return a
        s = self.stub(tmux_has=answer)
        o = self.run_one("bridge")
        self.assertEqual(o.status, "ok")
        self.assertIn("no answer within 30 s", o.detail)
        self.assertLessEqual(s.calls[0][1], hostcheck.BRIDGE_FIRST_TRY)       # the first try is capped
        self.assertEqual(s.multiplex, [True, False])      # a hung master must not hang the retry too
        self.assertGreater(hostcheck.BY_NAME["bridge"].budget, hostcheck.BRIDGE_FIRST_TRY)

    def test_two_timeouts_fail(self):
        s = self.stub(tmux_has=subprocess.TimeoutExpired("ssh", 5))
        o = self.run_one("bridge")
        self.assertEqual(o.status, "fail")
        self.assertIn("no answer within 40 s", o.detail)
        self.assertEqual(len(s.calls), 2)

    def test_another_rc_is_not_retried(self):
        s = self.stub(tmux_has=done(2, "", "boom"))
        self.assertEqual(self.run_one("bridge").status, "fail")
        self.assertEqual(len(s.calls), 1)

    def test_the_other_checks_keep_multiplexing(self):
        s = self.stub()
        hostcheck.run_checks(self.conn)
        self.assertEqual(set(s.multiplex), {True})


class Timeouts(Base):
    def test_a_timed_out_host_call_is_a_fail(self):
        self.stub(tmux_check=subprocess.TimeoutExpired("ssh", 5))
        o = self.run_one("tmux")
        self.assertEqual(o.status, "fail")
        self.assertIn("no answer within 45 s", o.detail)

    def test_a_check_that_uses_its_budget_up_fails_before_its_next_call(self):
        s = self.stub()
        check = hostcheck.Check("slowpoke", True, 0.2, lambda ctx: (__import__("time").sleep(0.3), hostcheck.check_tmux(ctx))[1])
        o = hostcheck.execute(check, hostcheck.Ctx(conn=self.conn))
        self.assertEqual(o.status, "fail")
        self.assertIn("no answer within 0.2 s", o.detail)
        self.assertEqual(s.calls, [])        # never reached the host: the budget was gone

    def test_every_call_gets_the_remaining_budget_as_its_timeout(self):
        s = self.stub()
        self.run_one("tmux")
        (argv, timeout), = s.calls
        self.assertLessEqual(timeout, hostcheck.BY_NAME["tmux"].budget)
        self.assertGreater(timeout, hostcheck.BY_NAME["tmux"].budget - 5)

    def test_a_crashing_check_is_a_fail_naming_the_error(self):
        self.stub(tmux_check=lambda argv, input: (_ for _ in ()).throw(KeyError("boom")))
        o = self.run_one("tmux")
        self.assertEqual(o.status, "fail")
        self.assertIn("raised KeyError", o.detail)

    def test_the_runner_timeout_exceeds_the_checks_budgets(self):
        job = runner.JOBS["host_check"]
        self.assertGreaterEqual(job.timeout_min * 60, hostcheck.RUN_WORST_CASE + 60)
        self.assertGreaterEqual(timedelta_s(runner_jobs.tick_min_timeout("host_check")), hostcheck.RUN_WORST_CASE + 60)
        self.assertEqual(actions.SETTINGS["runner_timeout_host_check"].default(), job.timeout_min)
        self.assertGreaterEqual(actions.SETTINGS["runner_timeout_window_start"].default(),   # the start runs the checks first
                                (sum(runner_jobs.window_start_inner().values(), timedelta(0)).total_seconds()
                                 + hostcheck.PRE_START_WORST_CASE + 300) / 60)
        self.assertEqual(runner_jobs.window_start_job_min_timeout(), runner_jobs.window_start_min_timeout()
                         + timedelta(seconds=hostcheck.PRE_START_WORST_CASE))


def timedelta_s(td):
    return td.total_seconds()


class Episodes(Base):
    """Fail-fast (D-235) without noise: a blocking fail pauses and alerts once per episode, a warn never alerts."""

    def test_a_blocking_fail_pauses_and_alerts_once_per_episode(self):
        self.stub(tmux_check=done(1, "", "host_exec: tmux-check failed"))
        note = hostcheck.run_job(self.conn)
        self.assertIn("1 fail (tmux)", note)
        p = self.paused()
        self.assertTrue(p["paused"])
        self.assertIn("host check tmux failed", p["reason"])
        self.assertEqual(self.alerts, ["AFClaude STOPPED: host check tmux failed"])
        hostcheck.run_job(self.conn)                    # the next hour, still failing: no second alert
        hostcheck.run_job(self.conn)
        self.assertEqual(len(self.alerts), 1)
        self.assertEqual(self.paused()["reason"].count("host check tmux failed"), 1)
        row = self.rows()["tmux"]
        self.assertEqual((row["status"], row["blocking"]), ("fail", 1))
        self.assertEqual(row["since"] <= row["ran_at"], True)
        self.clear_pause()                              # the task-manager looked, cleared it (D-283): still broken
        hostcheck.run_job(self.conn)
        self.assertEqual(len(self.alerts), 2)           # a new episode of the pause: it alerts and holds again
        self.assertTrue(self.paused()["paused"])

    def test_two_failing_checks_name_both(self):
        self.stub(tmux_check=done(1), claude_auth_status=done(127, "", "claude: not found"))
        hostcheck.run_job(self.conn)
        reason = self.paused()["reason"]
        self.assertIn("host check tmux failed", reason)
        self.assertIn("host check claude failed", reason)
        self.assertEqual(sorted(self.alerts), ["AFClaude STOPPED: host check claude failed",
                                               "AFClaude STOPPED: host check tmux failed"])
        self.assertEqual(self.rows()["logged_in"]["status"], "skip")

    def test_a_non_blocking_fail_alerts_once_and_pauses_nothing(self):
        self.stub(clock=lambda argv, input: done(1, "", "x"))
        hostcheck.run_job(self.conn)
        hostcheck.run_job(self.conn)
        self.assertEqual(self.alerts, ["host check clock failed"])
        self.assertFalse(self.paused()["paused"])
        self.stub()                                      # it recovers, then fails again: a new episode
        hostcheck.run_job(self.conn)
        self.assertEqual(self.rows()["clock"]["status"], "ok")
        self.stub(clock=lambda argv, input: done(1, "", "x"))
        hostcheck.run_job(self.conn)
        self.assertEqual(len(self.alerts), 2)

    def test_a_skip_between_two_fails_keeps_the_episode(self):
        """K6 review L3: fail, skip (a run gated by a failed bridge), fail is one episode: alerted once."""
        self.stub(clock=lambda argv, input: done(1, "", "x"))
        hostcheck.run_job(self.conn)
        since = self.rows()["clock"]["since"]
        self.stub(clock=lambda argv, input: done(1, "", "x"), tmux_has=done(2, "", "boom"))
        hostcheck.run_job(self.conn)                     # the bridge fails (blocking): clock is skipped
        self.assertEqual(self.rows()["clock"]["status"], "skip")
        self.clear_pause()
        self.alerts.clear()
        self.stub(clock=lambda argv, input: done(1, "", "x"))
        hostcheck.run_job(self.conn)
        self.assertEqual(self.alerts, [])                # the same episode: not alerted again
        r = self.rows()["clock"]
        self.assertEqual((r["status"], r["since"]), ("fail", since))
        self.assertIsNotNone(r["alerted_at"])

    def test_a_fail_the_cli_recorded_first_is_still_alerted_once_by_the_job(self):
        self.stub(clock=lambda argv, input: done(1, "", "x"))
        with redirect_stdout(StringIO()):
            hostcheck.main(["check", "clock"])           # a manual run records the fail, alerts nothing
        self.assertEqual(self.alerts, [])
        hostcheck.run_job(self.conn)
        hostcheck.run_job(self.conn)
        self.assertEqual(self.alerts, ["host check clock failed"])
        self.assertIsNotNone(self.rows()["clock"]["alerted_at"])

    def test_a_warn_is_recorded_but_never_alerts_or_pauses(self):
        self.stub(claude_auth_status=done(0, json.dumps({"loggedIn": True, "authMethod": "console"})))
        testenv.set_setting("host_claude_tested_version", "2.0")
        note = hostcheck.run_job(self.conn)
        self.assertIn("2 warn (logged_in, claude_version)", note)
        self.assertEqual((self.alerts, self.paused()["paused"]), ([], False))
        self.assertEqual(self.rows()["logged_in"]["status"], "warn")

    def test_a_healthy_run_records_every_scheduled_check_and_one_log_line(self):
        self.stub()
        n_lines = self.conn.execute("SELECT count(*) FROM app_log WHERE component='hostcheck'").fetchone()[0]
        with mock.patch.object(hostcheck, "mcp_tools", lambda argv, timeout: ["a"]):
            note = hostcheck.run_job(self.conn)
        self.assertEqual(note, f"host checks (all): {len(self.rows())} ok")
        self.assertEqual(set(self.rows()), {c.name for c in hostcheck.CHECKS if c.scheduled})
        lines = [r for r in self.conn.execute("SELECT level, line FROM app_log WHERE component='hostcheck' ORDER BY id")][n_lines:]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["level"], "info")
        self.assertIn("host checks (all): 11 ok", lines[0]["line"])
        before = self.rows()["kit"]["since"]
        hostcheck.run_job(self.conn)
        self.assertEqual(self.rows()["kit"]["since"], before)        # an unchanged status keeps its episode start

    def test_a_failed_bridge_skips_the_rest_instead_of_failing_each(self):
        s = self.stub(tmux_has=done(255, "", "Permission denied (publickey)"))
        hostcheck.run_job(self.conn)
        rows = self.rows()
        self.assertEqual(rows["bridge"]["status"], "fail")
        self.assertIn("retried once after ssh rc 255", rows["bridge"]["detail"])
        self.assertTrue(all(rows[n]["status"] == "skip" for n in rows if n != "bridge"))
        self.assertEqual(len(s.calls), 2)                # the handshake and its one retry
        self.assertEqual(self.alerts, ["AFClaude STOPPED: host check bridge failed"])

    def test_a_kit_mismatch_fails_and_skips_the_rest(self):
        self.stub()
        with mock.patch.object(host, "check_host", side_effect=host.HostKitError("files differ")):
            hostcheck.run_job(self.conn)
        rows = self.rows()
        self.assertEqual((rows["kit"]["status"], rows["tmux"]["status"]), ("fail", "skip"))
        self.assertIn("host check kit failed", self.paused()["reason"])


class NoKitYet(Base):
    """TRANSITIONAL (until K7): the legacy bridge answers, host.kit_info() is None: one warn, no pause, no alert,
    no blocked start."""

    def stub_legacy(self):
        s = self.stub()
        p = mock.patch.object(host, "check_host", lambda timeout=30: None)
        p.start()
        self.addCleanup(p.stop)
        return s

    def test_the_job_records_one_kit_not_installed_warn_and_skips_the_rest(self):
        s = self.stub_legacy()
        note = hostcheck.run_job(self.conn)
        rows = self.rows()
        self.assertEqual(rows["kit"]["status"], "warn")
        self.assertTrue(rows["kit"]["detail"].startswith(hostcheck.NO_KIT))
        self.assertEqual({n for n, r in rows.items() if r["status"] != "skip"}, {"bridge", "kit"})
        self.assertIn("1 warn (kit)", note)
        self.assertEqual((self.alerts, self.paused()["paused"]), ([], False))
        self.assertEqual([c[0][0] for c in s.calls], ["tmux"])          # only the bridge handshake touched the host
        b = qv.host_checks_block()
        self.assertTrue(b["no_kit"])
        self.assertEqual([c["name"] for c in b["checks"]], ["kit"])      # the warn expanded, the skips folded
        self.assertEqual([(g["reason"], g["count"]) for g in b["skipped"]], [(hostcheck.NO_KIT, len(rows) - 2)])

    def test_a_named_check_passes_the_gate_and_is_skipped_not_failed(self):
        s = self.stub_legacy()
        s.answers["tmux-check"] = done(126, "", "host_exec: denied (not whitelisted)")
        with redirect_stdout(StringIO()):
            rc = hostcheck.main(["check", "tmux"])
        self.assertEqual(rc, 0)
        rows = self.rows()
        self.assertEqual({n: r["status"] for n, r in rows.items()}, {"bridge": "ok", "kit": "warn", "tmux": "skip"})
        self.assertEqual(qv.host_checks_block()["counts"]["fail"], 0)

    def test_the_pre_start_hook_does_not_block(self):
        self.stub_legacy()
        self.assertIsNone(hostcheck.run_pre_start(self.conn))
        self.assertEqual((self.alerts, self.paused()["paused"]), ([], False))
        self.assertEqual(self.rows()["kit"]["status"], "warn")

    def test_check_host_maps_the_legacy_bridge_to_none(self):
        """host.check_host() itself (not patched): kit_info() None -> None."""
        with mock.patch.object(host, "kit_info", lambda timeout=30: None):
            self.assertIsNone(host.check_host())

    def test_the_kit_appearing_ends_the_warn(self):
        self.stub_legacy()
        hostcheck.run_job(self.conn)
        self.stub()
        with mock.patch.object(hostcheck, "mcp_tools", lambda argv, timeout: ["a"]):
            hostcheck.run_job(self.conn)
        self.assertEqual(self.rows()["kit"]["status"], "ok")
        self.assertFalse(qv.host_checks_block()["no_kit"])


class PreStart(Base):
    def test_a_blocking_fail_holds_the_start_and_pauses(self):
        self.stub(claude_auth_status=done(0, json.dumps({"loggedIn": False})))
        with mock.patch.object(hostcheck, "mcp_tools", lambda argv, timeout: ["a"]):
            why = hostcheck.run_pre_start(self.conn)
        self.assertIn("logged_in: Claude Code on the host is not logged in", why)
        self.assertTrue(self.paused()["paused"])
        self.assertEqual(self.alerts, ["AFClaude STOPPED: host check logged_in failed"])
        self.assertEqual(set(self.rows()), {c.name for c in hostcheck.CHECKS if c.blocking})   # only the blocking subset ran

    def test_all_blocking_checks_passing_lets_the_start_go(self):
        self.stub()
        with mock.patch.object(hostcheck, "mcp_tools", lambda argv, timeout: ["a"]):
            self.assertIsNone(hostcheck.run_pre_start(self.conn))
        self.assertEqual(self.alerts, [])

    def test_a_non_blocking_fail_does_not_hold_a_start(self):
        self.stub(clock=lambda argv, input: done(1))
        with mock.patch.object(hostcheck, "mcp_tools", lambda argv, timeout: ["a"]):
            self.assertIsNone(hostcheck.run_pre_start(self.conn))

    def test_keepalive_holds_the_start_before_reading_usage(self):
        notes = []
        args = types.SimpleNamespace(arm=False, now=False)
        now = ka.datetime(2026, 11, 14, 22, 0, tzinfo=ka.timezone.utc)
        fresh = mock.Mock(side_effect=AssertionError("usage read: the start went on"))
        with mock.patch.object(ka, "progress_note", notes.append), mock.patch.object(ka, "fresh_usage", fresh), \
                mock.patch.object(hostcheck, "pre_start", lambda conn: "tmux: tmux-check failed"):
            d = ka.window_start_pass(testenv.SID, now, args)
        self.assertEqual((d["go"], d["postpone"]), (False, False))
        self.assertIn("a host check failed (tmux: tmux-check failed)", d["reason"])
        self.assertIn("not resumed, HOLD: a host check failed", notes[0])
        fresh.assert_not_called()

    def test_keepalive_goes_on_when_the_hook_passes(self):
        args = types.SimpleNamespace(arm=False, now=False)
        now = ka.datetime(2026, 11, 14, 22, 0, tzinfo=ka.timezone.utc)
        stop = RuntimeError("reached the usage read")
        with mock.patch.object(ka, "progress_note", lambda t: None), \
                mock.patch.object(ka, "fresh_usage", mock.Mock(side_effect=stop)), \
                mock.patch.object(hostcheck, "pre_start", lambda conn: None):
            with self.assertRaises(RuntimeError) as cm:
                ka.window_start_pass(testenv.SID, now, args)
        self.assertIs(cm.exception, stop)

    def test_a_db_error_postpones_and_the_recheck_runs_the_checks_first(self):
        notes, saved = [], {}
        args = types.SimpleNamespace(arm=False, now=False)
        now = ka.datetime(2026, 11, 14, 22, 0, tzinfo=ka.timezone.utc)
        broken = mock.Mock(side_effect=store.DBError("database is locked", "locked"))
        with mock.patch.object(ka, "progress_note", notes.append), \
                mock.patch.object(ka, "fresh_usage", mock.Mock(side_effect=AssertionError("no"))), \
                mock.patch.object(ka, "save_deferred", lambda d: saved.update(d)), \
                mock.patch.object(ka, "load_deferred", lambda: {}), \
                mock.patch.object(hostcheck, "pre_start", broken):
            d = ka.window_start_pass(testenv.SID, now, args)
        self.assertEqual((d["go"], d["postpone"], d["recheck_at"]), (False, True, now + ka.DB_PAUSE_RECHECK))
        self.assertIn("postponed to", notes[0])
        self.assertFalse(self.paused()["paused"])        # a DB pause, not a fail_stop (D-202)
        (key, ent), = saved.items()
        self.assertTrue(ent["host_checks"])
        later = now + ka.DB_PAUSE_RECHECK
        # the recheck: the database is still broken, postponed again
        defs = {key: dict(ent)}
        with mock.patch.object(ka, "save_deferred", lambda d: None), mock.patch.object(hostcheck, "pre_start", broken), \
                mock.patch.object(ka, "fresh_usage", mock.Mock(side_effect=AssertionError("no"))):
            self.assertEqual(ka.deferred_act(testenv.SID, later, {}, args, key, defs[key], defs),
                             later + ka.DB_PAUSE_RECHECK)
        # then a failing check holds it, before the usage read
        with mock.patch.object(ka, "save_deferred", lambda d: None), mock.patch.object(ka, "progress_note", notes.append), \
                mock.patch.object(hostcheck, "pre_start", lambda conn: "tmux: gone"), \
                mock.patch.object(ka, "fresh_usage", mock.Mock(side_effect=AssertionError("no"))):
            self.assertIsNone(ka.deferred_act(testenv.SID, later, {}, args, key, defs[key], defs))
        self.assertNotIn(key, defs)
        self.assertIn("a host check failed (tmux: gone)", notes[-1])
        # a passing one goes on to the usage read, the flag cleared
        defs = {key: dict(ent)}
        stop = RuntimeError("reached the usage read")
        with mock.patch.object(ka, "save_deferred", lambda d: None), mock.patch.object(hostcheck, "pre_start", lambda conn: None), \
                mock.patch.object(ka, "fresh_usage", mock.Mock(side_effect=stop)):
            with self.assertRaises(RuntimeError):
                ka.deferred_act(testenv.SID, later, {}, args, key, defs[key], defs)
        self.assertNotIn("host_checks", defs[key])

    def db_down(self):
        return mock.patch.object(actions, "hostcheck_put", mock.Mock(side_effect=store.DBError("disk I/O error", "io")))

    def test_a_db_error_with_a_blocking_fail_still_holds_and_alerts(self):
        """K6 review L5: the database failing at the same time must not turn the HOLD into a postpone."""
        self.stub(claude_auth_status=done(0, json.dumps({"loggedIn": False})))
        with mock.patch.object(hostcheck, "mcp_tools", lambda argv, timeout: ["a"]), self.db_down(), \
                mock.patch.object(failstop, "_set_pause", lambda component, reason, project=None: "DBError: disk I/O error"):
            why = hostcheck.run_pre_start(self.conn)
        self.assertIn("logged_in: Claude Code on the host is not logged in", why)
        self.assertTrue(why.endswith("[the results could not be recorded: DBError: disk I/O error]"), why)
        self.assertEqual(self.alerts, ["AFClaude STOPPED: host check logged_in failed"])   # the pause could not be set

    def test_a_db_error_with_every_check_passing_is_raised(self):
        self.stub()
        with mock.patch.object(hostcheck, "mcp_tools", lambda argv, timeout: ["a"]), self.db_down():
            with self.assertRaises(store.DBError):
                hostcheck.run_pre_start(self.conn)
        self.assertEqual(self.alerts, [])

    def test_keepalive_holds_not_postpones_a_blocking_fail_during_a_db_error(self):
        notes, saved = [], {}
        args = types.SimpleNamespace(arm=False, now=False)
        now = ka.datetime(2026, 11, 14, 22, 0, tzinfo=ka.timezone.utc)
        self.stub(tmux_check=done(1, "", "no server"))
        with mock.patch.object(ka, "progress_note", notes.append), \
                mock.patch.object(ka, "fresh_usage", mock.Mock(side_effect=AssertionError("no"))), \
                mock.patch.object(ka, "save_deferred", lambda d: saved.update(d)), \
                mock.patch.object(hostcheck, "mcp_tools", lambda argv, timeout: ["a"]), \
                mock.patch.object(hostcheck, "pre_start", hostcheck.run_pre_start), self.db_down():
            d = ka.window_start_pass(testenv.SID, now, args)
        self.assertEqual((d["go"], d["postpone"]), (False, False))
        self.assertIn("HOLD: a host check failed (tmux:", d["reason"])
        self.assertIn("the database failed too, so automation may not be paused", d["reason"])   # not "paused"
        self.assertNotIn("automation paused", d["reason"])
        self.assertEqual(saved, {})                      # no postponed recheck
        self.assertTrue(self.paused()["paused"])         # the pause itself could be written here

    def test_a_crashing_hook_is_an_error_that_pauses_and_holds(self):
        args = types.SimpleNamespace(arm=False, now=False)
        now = ka.datetime(2026, 11, 14, 22, 0, tzinfo=ka.timezone.utc)
        with mock.patch.object(ka, "progress_note", lambda t: None), \
                mock.patch.object(ka, "fresh_usage", mock.Mock(side_effect=AssertionError("no"))), \
                mock.patch.object(hostcheck, "pre_start", mock.Mock(side_effect=ValueError("bad row"))):
            d = ka.window_start_pass(testenv.SID, now, args)
        self.assertFalse(d["go"])
        self.assertIn("host check could not run (ValueError)", d["reason"])
        self.assertIn("host check failed to run", self.paused()["reason"])


class E2E(Base):
    """The e2e check against a stub tmux: ka_resume.sh starts the session, the pane shows the prompt and then the
    reply, the check kills it."""

    class Tmux:
        def __init__(self, reply=True, start_rc=0, survives_kill=False, ends_early=False):
            self.sessions, self.log, self.captures = set(), [], 0
            self.reply, self.start_rc, self.survives_kill, self.ends_early = reply, start_rc, survives_kill, ends_early
            self.msg = ""

        def run(self, argv, input=None, cwd=None, env=None, timeout=None):
            self.log.append(list(argv))
            if os.path.basename(argv[0]) == "ka_resume.sh":
                a = argv[1:]
                self.msg = a[a.index("--message") + 1]
                assert "--new" in a and a[a.index("--model") + 1] == "haiku" and "--no-remote-control" in a
                if self.start_rc == 0:
                    self.sessions.add(a[a.index("--tmux") + 1])
                return done(self.start_rc, "started\n" if self.start_rc == 0 else "", "" if self.start_rc == 0 else "no trust")
            if argv[:2] == ["tmux", "has-session"]:
                return done(0 if argv[3][1:] in self.sessions else 1)
            if argv[:2] == ["tmux", "kill-session"]:
                if not self.survives_kill:
                    self.sessions.discard(argv[3][1:])
                return done(0)
            if argv[:2] == ["tmux", "capture-pane"]:
                self.captures += 1
                if self.ends_early:
                    self.sessions.clear()
                    return done(1, "", "can't find pane")
                nums = [int(x) for x in __import__("re").findall(r"\d+", self.msg)]
                text = "> " + self.msg + "\n"
                if self.reply and self.captures >= 2:
                    text += f"AFC-{sum(nums)}\n"
                return done(0, text)
            raise AssertionError(argv)

    def run_e2e(self, **kw):
        t = self.Tmux(**kw)
        p = mock.patch.object(host, "run_on_host", t.run)
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(hostcheck.time, "sleep", lambda s: None):
            return t, self.run_one("e2e")

    def test_it_starts_waits_for_the_reply_and_kills_the_session(self):
        t, o = self.run_e2e()
        self.assertEqual((o.status, t.sessions), ("ok", set()), o.detail)
        self.assertEqual(t.captures, 2)
        names = {a[a.index("--tmux") + 1] for a in t.log if os.path.basename(a[0]) == "ka_resume.sh"}
        self.assertEqual(names, {"afc-check-e2e"})
        self.assertTrue(t.log[-2][:2] == ["tmux", "kill-session"])

    def test_a_leftover_session_is_killed_first(self):
        t = self.Tmux()
        t.sessions.add("afc-check-e2e")
        p = mock.patch.object(host, "run_on_host", t.run)
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(hostcheck.time, "sleep", lambda s: None):
            o = self.run_one("e2e")
        self.assertEqual(o.status, "ok")
        self.assertEqual([a[:2] for a in t.log[:2]], [["tmux", "has-session"], ["tmux", "kill-session"]])

    def test_no_reply_is_a_fail_and_the_session_is_still_killed(self):
        with mock.patch.object(hostcheck, "E2E_WAIT", 0.0):
            t, o = self.run_e2e(reply=False)
        self.assertEqual(o.status, "fail")
        self.assertIn("no reply within", o.detail)
        self.assertEqual(t.sessions, set())

    def test_a_failed_start_and_a_session_that_ends_early_fail(self):
        t, o = self.run_e2e(start_rc=1)
        self.assertEqual(o.status, "fail")
        self.assertIn("ka_resume.sh did not start the session", o.detail)
        t, o = self.run_e2e(ends_early=True)
        self.assertEqual(o.status, "fail")
        self.assertIn("ended before it replied", o.detail)

    def test_a_session_that_survives_the_kill_is_a_fail(self):
        t, o = self.run_e2e(survives_kill=True)
        self.assertEqual(o.status, "fail")
        self.assertIn("still there after the kill", o.detail)

    def test_the_name_is_reserved_and_never_scheduled(self):
        import names
        self.assertIn(hostcheck.E2E_TMUX, names.RESERVED)
        self.assertIsNotNone(names.TMUX_RE.match(hostcheck.E2E_TMUX))
        self.assertNotIn("e2e", {c.name for c in hostcheck.CHECKS if c.scheduled})
        self.stub()
        with mock.patch.object(hostcheck, "mcp_tools", lambda argv, timeout: ["a"]):
            self.assertNotIn("e2e", {o.name for o in hostcheck.run_checks(self.conn)})


class Storage(Base):
    def put(self, status, hour):
        return actions.hostcheck_put(self.conn, [{"name": "disk", "status": status, "detail": "", "fix": "", "duration_ms": 1,
                                                  "kit_version": "6", "blocking": False}],
                                     actor="runner:host_check", via="runner", ran_at=f"2026-11-14T{hour:02d}:00:00.000Z")["disk"]

    def test_the_episode_rule_a_skip_neither_starts_nor_ends_an_episode(self):
        """K6 review L3. An episode = results of one status; a skip (not run) carries the episode it interrupts."""
        self.assertIsNone(self.put("fail", 1))
        actions.hostcheck_alerted(self.conn, ["disk"], actor="runner:host_check", via="runner")
        self.assertIsNotNone(self.put("skip", 2))                    # the skip carries the alerted fail episode
        self.assertIsNotNone(self.put("skip", 3))                    # two skips too
        self.assertIsNotNone(self.put("fail", 4))                    # still the same episode: no new alert
        r = self.rows()["disk"]
        self.assertEqual((r["status"], r["since"], r["episode_status"]), ("fail", "2026-11-14T01:00:00.000Z", "fail"))
        self.put("skip", 5)
        self.assertIsNone(self.put("ok", 6))                         # another status after the skip: a new episode
        self.assertEqual(self.rows()["disk"]["since"], "2026-11-14T06:00:00.000Z")
        self.put("skip", 7)
        self.assertIsNone(self.put("fail", 8))                       # ok, skip, fail: a new fail episode, alerted
        self.assertEqual(self.rows()["disk"]["since"], "2026-11-14T08:00:00.000Z")
        self.put("warn", 9)
        self.put("skip", 10)
        self.put("warn", 11)
        self.assertEqual(self.rows()["disk"]["since"], "2026-11-14T09:00:00.000Z")   # warn, skip, warn: one warn episode

    def test_a_first_skip_starts_a_skip_episode_and_an_old_row_reads_its_status(self):
        self.put("skip", 1)
        self.assertEqual(self.rows()["disk"]["episode_status"], "skip")
        self.assertIsNone(self.put("fail", 2))
        self.assertEqual(self.rows()["disk"]["since"], "2026-11-14T02:00:00.000Z")
        with store.transaction(self.conn):                           # a row written before the column existed
            self.conn.execute("UPDATE host_checks SET episode_status=NULL, alerted_at='x' WHERE name='disk'")
        self.assertEqual(self.put("fail", 3), "x")
        self.assertEqual(self.rows()["disk"]["since"], "2026-11-14T02:00:00.000Z")

    def test_put_keeps_since_while_the_status_holds_and_validates(self):
        row = {"name": "tmux", "status": "ok", "detail": "d", "fix": "", "duration_ms": 5, "kit_version": "6", "blocking": True}
        first = actions.hostcheck_put(self.conn, [row], actor="runner:host_check", via="runner", ran_at="2026-11-14T10:00:00.000Z")
        actions.hostcheck_alerted(self.conn, ["tmux"], actor="runner:host_check", via="runner")
        again = actions.hostcheck_put(self.conn, [row], actor="runner:host_check", via="runner", ran_at="2026-11-14T11:00:00.000Z")
        self.assertEqual(first, {"tmux": None})          # not alerted yet
        self.assertIsNotNone(again["tmux"])              # the same episode keeps its alert mark
        r = self.rows()["tmux"]
        self.assertEqual((r["since"], r["ran_at"], r["version"]), ("2026-11-14T10:00:00.000Z", "2026-11-14T11:00:00.000Z", 2))   # the alert mark and the put
        actions.hostcheck_put(self.conn, [dict(row, status="fail")], actor="runner:host_check", via="runner", ran_at="2026-11-14T12:00:00.000Z")
        self.assertEqual(self.rows()["tmux"]["since"], "2026-11-14T12:00:00.000Z")
        self.assertIsNone(self.rows()["tmux"]["alerted_at"])    # a new status is a new episode, not alerted
        with self.assertRaises(PermissionError):
            actions.hostcheck_alerted(self.conn, ["tmux"], actor="cli", via="cli")
        for bad in (dict(row, status="great"), dict(row, name="Bad Name"), dict(row, duration_ms=-1), dict(row, kit_version=6)):
            with self.assertRaises(ValueError):
                actions.hostcheck_put(self.conn, [bad], actor="runner:host_check", via="runner")
        with self.assertRaises(PermissionError):
            actions.hostcheck_put(self.conn, [row], actor="cli", via="mcp")
        with self.assertRaises(sqlite3.IntegrityError), store.transaction(self.conn):   # the CHECK constraint too
            self.conn.execute("UPDATE host_checks SET status='great'")

    def test_the_text_is_capped(self):
        actions.hostcheck_put(self.conn, [{"name": "x", "status": "warn", "detail": "d" * 5000, "fix": "f"}],
                              actor="runner:host_check", via="runner")
        self.assertLessEqual(len(self.rows()["x"]["detail"]), actions.HOST_CHECK_TEXT_MAX)


class Surfaces(Base):
    def test_the_job_is_the_runners_and_a_function_handler(self):
        job = runner.JOBS["host_check"]
        self.assertEqual((job.every.total_seconds(), job.paused, job.pause_on_failure), (3600, "runs", True))
        self.assertIn("host_check", runner.RUNNER_OWNS)
        self.assertEqual(runner_jobs.FUNCTIONS["host_check"][0], "host_check.log")

    def test_the_tick_runs_the_checks_and_returns_the_note(self):
        self.stub()
        with mock.patch.object(hostcheck, "mcp_tools", lambda argv, timeout: ["a"]), redirect_stdout(StringIO()) as out:
            note = runner_jobs.host_check_tick()
        self.assertTrue(note.startswith("host checks (all):"))
        self.assertIn(note, out.getvalue())

    def test_cli_check_prints_records_and_exits_by_the_result(self):
        self.stub(tmux_check=done(1, "", "no tmux"))
        out = StringIO()
        with redirect_stdout(out):
            rc = hostcheck.main(["check", "tmux", "clock"])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  tmux", out.getvalue())
        self.assertIn("OK    clock", out.getvalue())
        self.assertIn("fix:", out.getvalue())
        self.assertEqual({n for n in self.rows()}, {"bridge", "kit", "tmux", "clock"})   # named checks pass the gates
        self.assertEqual((self.alerts, self.paused()["paused"]), ([], False))      # a manual run pauses nothing
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(hostcheck.main(["status"]), 0)
        self.assertIn("FAIL  tmux", out.getvalue())
        with redirect_stdout(StringIO()):
            self.assertEqual(hostcheck.main(["check", "nope"]), 2)
            self.assertEqual(hostcheck.main([]), 2)

    def test_cli_through_the_afclaude_command(self):
        import afclaude
        py = sys.executable
        self.assertEqual(afclaude.resolve(["host", "check", "e2e"]), [py, os.path.join(HERE, "hostcheck.py"), "check", "e2e"])
        self.assertNotIn("host", afclaude.PLANNED)

    def test_the_quickview_block_counts_and_expands_the_not_ok_rows(self):
        self.assertEqual(qv.host_checks_block()["total"], 0)
        actions.hostcheck_put(self.conn, [
            {"name": "bridge", "status": "ok", "detail": "fine"},
            {"name": "tmux", "status": "fail", "detail": "no tmux", "fix": "install it", "blocking": True},
            {"name": "clock", "status": "warn", "detail": "drift 7 s", "fix": "sync"},
            {"name": "mcp", "status": "skip", "detail": "not run"}], actor="runner:host_check", via="runner")
        b = qv.host_checks_block()
        self.assertEqual(b["counts"], {"ok": 1, "warn": 1, "fail": 1, "skip": 1})
        self.assertEqual(sorted(c["name"] for c in b["checks"]), ["clock", "tmux"])     # ok is only counted
        self.assertEqual(b["skipped"], [{"reason": "not run", "count": 1, "names": ["mcp"]}])  # skip: one line per reason
        self.assertFalse(b["no_kit"])
        with open(os.path.join(HERE, "quickview", "AFClaude.html")) as fh:
            page = fh.read()
        self.assertIn("s.host_checks", page)
        self.assertIn("function hostTile", page)

    def test_the_status_file_carries_the_block(self):
        with open(os.path.join(HERE, "export_quickview.py")) as fh:
            self.assertIn('"host_checks": host_checks_block()', fh.read())


if __name__ == "__main__":
    unittest.main()
