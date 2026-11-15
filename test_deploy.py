#!/usr/bin/env python3
"""Tests for tools/deploy.py (D-257, the 21.10. incident; three services since phase 5b K4): the
container-quiet wait logic, the three-service flow and the health wait, with fake `docker` output, stub
subprocess calls and a fake clock/sleep (no docker, no real waiting)."""
import os
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
import deploy  # noqa: E402
import schedule  # noqa: E402

CHECK_HOST_KIT = deploy.check_host_kit      # KitGuardTest's; every other test runs main() without asking a kit


def setUpModule():
    p = mock.patch.object(deploy, "check_host_kit", return_value=True)
    p.start()
    unittest.addModuleCleanup(p.stop)

HEADER = "PID                 COMMAND"
IDLE = [HEADER,
       "1       /usr/bin/tini -- /usr/local/bin/afclaude-entrypoint",
       "2       /usr/local/bin/supercronic -passthrough-logs /mnt/BlockVolume/Claude/work/AFClaude/docker/crontab",
       "3       python3 -u /mnt/BlockVolume/Claude/work/AFClaude/inbox_api.py serve",
       "4       ssh: /tmp/afclaude-ssh-deadbeef [mux]"]
SAMPLER = IDLE + ["5       flock -n data/.sampler.lock python3 /mnt/BlockVolume/Claude/work/AFClaude/usage_sampler.py"]
BRIDGE_CALL = IDLE + ["6       ssh -T -i /app/data/bridge/host_bridge_ed25519 -o ControlMaster=auto -- "
                     "claude -p --no-session-persistence --permission-mode dontAsk /usage"]
UNITTEST = IDLE + ["7       python3 -m unittest test_keepalive"]
REVIEW = IDLE + ["8       python3 /mnt/BlockVolume/Claude/work/AFClaude/usage_review.py"]


class FakeCompleted:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
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
             mock.patch.object(deploy, "scheduler_mode", return_value="runner"), \
             mock.patch.object(deploy, "check_data", return_value="/data/afclaude.db"), \
             mock.patch.object(deploy, "check_old_container", return_value=True), \
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

    def test_a_bad_scheduler_mode_is_refused_before_anything_even_forced(self):
        for argv in ([], ["--force"]):
            with mock.patch.object(deploy, "scheduler_mode", return_value="on"), \
                 mock.patch.object(deploy, "check_start_guard", side_effect=AssertionError("no guard")), \
                 mock.patch.object(deploy.store, "connect", side_effect=AssertionError("no drain")), \
                 mock.patch("builtins.print") as out:
                self.assertEqual(deploy.main(argv), 1)
            self.assertIn("AFCLAUDE_SCHEDULER=on", out.call_args[0][0])


class SchedulerModeTest(unittest.TestCase):
    """The mode compose gives the new container: the environment, else docker/.env, else `off`."""

    def test_resolution(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, ".env")
            self.assertEqual(deploy.scheduler_mode(env, {}), "off")                  # no file
            with open(env, "w") as fh:
                fh.write("# c\nAFCLAUDE_SCHEDULER=on\nOTHER=x\nAFCLAUDE_SCHEDULER='runner'\n")
            self.assertEqual(deploy.scheduler_mode(env, {}), "runner")              # the last line, unquoted
            self.assertEqual(deploy.scheduler_mode(env, {"AFCLAUDE_SCHEDULER": "legacy"}), "legacy")
            with open(env, "w") as fh:
                fh.write("AFCLAUDE_SCHEDULER=on\n")
            self.assertEqual(deploy.scheduler_mode(env, {}), "on")


class EnvValueTest(unittest.TestCase):
    def test_environment_first_then_the_last_env_file_line(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, ".env")
            self.assertEqual(deploy.env_value("X", env, {}), "")
            with open(env, "w") as fh:
                fh.write("X=1\nX='2'\n")
            self.assertEqual(deploy.env_value("X", env, {}), "2")
            self.assertEqual(deploy.env_value("X", env, {"X": "3"}), "3")


class ThreeServicesTest(unittest.TestCase):
    """The pieces of the three-service deploy: data check, old container, compose files, health wait."""

    def test_container_names(self):
        self.assertEqual(deploy.container_names("afclaude", with_mcp=True),
                         ["afclaude-runner", "afclaude-mcp", "afclaude-web"])
        self.assertEqual(deploy.container_names("x", with_mcp=True), ["x-runner", "x-mcp", "x-web"])
        self.assertEqual(deploy.container_names("x", with_mcp=False), ["x-runner", "x-web"])

    def test_the_mcp_service_is_waited_for_only_with_its_profile(self):
        """K5 part 2a: afclaude-mcp is in the compose profile `mcp` (a plain up does not start it), so the health wait
        names it only when COMPOSE_PROFILES does."""
        for value, want in (("", False), ("other", False), ("mcp", True), ("a, mcp", True), ("*", True), ("mcp2", False)):
            self.assertEqual(deploy.mcp_enabled(environ={"COMPOSE_PROFILES": value}, env_file="/nonexistent"), want, value)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            env = os.path.join(d, ".env")
            with open(env, "w") as fh:
                fh.write("COMPOSE_PROFILES=mcp\n")
            self.assertTrue(deploy.mcp_enabled(env, {}))
        with mock.patch.object(deploy, "mcp_enabled", return_value=False):
            self.assertEqual(deploy.container_names("x"), ["x-runner", "x-web"])
        with mock.patch.object(deploy, "mcp_enabled", return_value=True):
            self.assertEqual(deploy.container_names("x"), ["x-runner", "x-mcp", "x-web"])

    def test_the_mcp_profile_is_refused_while_roles_are_not_on(self):
        with mock.patch.object(deploy, "mcp_enabled", return_value=False):
            self.assertTrue(deploy.check_mcp_profile("services", mode=lambda: self.fail("no read without the profile")))
        with mock.patch.object(deploy, "mcp_enabled", return_value=True):
            self.assertTrue(deploy.check_mcp_profile("single", mode=lambda: self.fail("single layout: no mcp service")))
            self.assertTrue(deploy.check_mcp_profile("services", mode=lambda: "on"))
            with mock.patch("builtins.print") as out:
                self.assertFalse(deploy.check_mcp_profile("services", mode=lambda: "shadow"))
            self.assertIn("roles_enforce is 'shadow'", out.call_args[0][0])
            self.assertIn("Nothing was drained", out.call_args[0][0])
            import actions
            def broken():
                raise actions.RolesFailure("the saved roles_enforce setting can't be used", "bad", "fix")
            with mock.patch("builtins.print") as out:
                self.assertFalse(deploy.check_mcp_profile("services", mode=broken))
            self.assertIn("unusable", out.call_args[0][0])

    def test_check_data_refuses_unset_and_a_directory_without_a_database(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch("builtins.print") as out:
            with mock.patch.object(deploy, "env_value", return_value=""):
                self.assertIsNone(deploy.check_data())
            self.assertIn("AFCLAUDE_DATA is not set", out.call_args[0][0])
            with mock.patch.object(deploy, "env_value", return_value=d):
                self.assertIsNone(deploy.check_data())
            self.assertIn("no database", out.call_args[0][0])
            open(os.path.join(d, "afclaude.db"), "w").close()
            old = deploy.store.DB_PATH
            try:
                with mock.patch.object(deploy, "env_value", return_value=d):
                    self.assertEqual(deploy.check_data(), os.path.join(d, "afclaude.db"))
                self.assertEqual(deploy.store.DB_PATH, os.path.join(d, "afclaude.db"))   # the drain uses it
            finally:
                deploy.store.DB_PATH = old

    def test_the_old_single_container_blocks_the_deploy(self):
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return FakeCompleted("afclaude\n")
        self.assertTrue(deploy.old_container_running(run, "afclaude"))
        self.assertIn("name=^afclaude$", seen[0])
        self.assertFalse(deploy.old_container_running(lambda c, **k: FakeCompleted("afclaude-runner\n"), "afclaude"))
        self.assertFalse(deploy.old_container_running(lambda c, **k: FakeCompleted(""), "afclaude"))
        with self.assertRaises(RuntimeError):
            deploy.old_container_running(lambda c, **k: FakeCompleted("", 1), "afclaude")
        with mock.patch.object(deploy, "old_container_running", return_value=True), mock.patch("builtins.print") as out:
            self.assertFalse(deploy.check_old_container())
        self.assertIn("old layout", out.call_args[0][0])

    def test_compose_command_with_overrides(self):
        with mock.patch.object(deploy, "env_value", return_value=""):
            self.assertEqual(deploy.compose_command("up", "-d", lay="services"),
                             ["docker", "compose", "-f", deploy.COMPOSE_SERVICES, "up", "-d"])
        with mock.patch.object(deploy, "env_value", return_value="compose.host-mounts.yml, compose.subnet.yml"):
            cmd = deploy.compose_command("up", lay="services")
            single = deploy.compose_command("up")     # the single layout never takes overrides (main's behaviour)
        self.assertEqual([os.path.basename(x) for x in cmd if x.endswith(".yml")],
                         ["compose.services.yml", "compose.host-mounts.yml", "compose.subnet.yml"])
        self.assertEqual(single, ["docker", "compose", "-f", deploy.COMPOSE_FILE, "up"])
        for bad in ("../x.yml", "/etc/x.yml", "x.sh"):
            with mock.patch.object(deploy, "env_value", return_value=bad), self.assertRaises(ValueError):
                deploy.compose_command("up", lay="services")

    def test_layout_is_single_unless_chosen(self):
        with mock.patch.object(deploy, "env_value", return_value=""):
            self.assertEqual(deploy.layout(), "single")
        with mock.patch.object(deploy, "env_value", return_value="services"):
            self.assertEqual(deploy.layout(), "services")
        with mock.patch.object(deploy, "env_value", return_value="both"), self.assertRaises(ValueError):
            deploy.layout()

    def test_the_old_container_is_found_whatever_the_name_prefix(self):
        """compose.yml fixes the single container's name, so a services prefix must not hide it (review k4a L5)."""
        seen = []
        with mock.patch.object(deploy, "env_value", return_value="other"):
            deploy.old_container_running(lambda c, **k: seen.append(c) or FakeCompleted("afclaude\n"))
        self.assertIn("name=^afclaude$", seen[0])

    def test_health_state_from_inspect(self):
        import json

        def inspect(state):
            return lambda c, **k: FakeCompleted(json.dumps([{"State": state}]))
        self.assertEqual(deploy.health_state("c", inspect({"Status": "running", "Health": {
            "Status": "unhealthy", "Log": [{"Output": "a"}, {"Output": "no answer\n"}]}})), ("unhealthy", "no answer"))
        self.assertEqual(deploy.health_state("c", inspect({"Status": "running", "Health": {"Status": "starting", "Log": []}})),
                         ("starting", ""))
        self.assertEqual(deploy.health_state("c", inspect({"Status": "running"})), ("none", ""))
        self.assertEqual(deploy.health_state("c", inspect({"Status": "restarting"})), ("restarting", ""))
        self.assertEqual(deploy.health_state("c", lambda c, **k: FakeCompleted("", 1))[0], "missing")

    def _wait(self, script, max_wait=60):
        """script: [{container: (state, output)}, ...] per poll; the last repeats. -> (ok, messages)."""
        polls = iter(script)
        current = {}
        msgs, clock = [], [0.0]

        def state(c):
            return current[c]

        def sleep(s):
            clock[0] += s
        # advance the script once per round of three states
        cs = ["a", "b", "c"]

        def lister(c):
            if c == cs[0]:
                nonlocal current
                try:
                    current = next(polls)
                except StopIteration:
                    pass
            return current[c]
        ok = deploy.wait_healthy(cs, timedelta(seconds=max_wait), poll=5, state=lister, sleep=sleep,
                                 clock=lambda: clock[0], out=msgs.append)
        return ok, msgs

    def test_waits_for_all_three_then_reports_healthy(self):
        H, S = ("healthy", ""), ("starting", "")
        ok, msgs = self._wait([{"a": H, "b": S, "c": S}, {"a": H, "b": H, "c": S}, {"a": H, "b": H, "c": H}])
        self.assertTrue(ok)
        self.assertIn("healthy: a, b, c", msgs[-1])

    def test_one_unhealthy_fails_at_once_with_its_output(self):
        ok, msgs = self._wait([{"a": ("healthy", ""), "b": ("unhealthy", "no answer on 127.0.0.1:8000"), "c": ("starting", "")}])
        self.assertFalse(ok)
        self.assertIn("b is unhealthy (no answer on 127.0.0.1:8000)", msgs[-1])

    def test_an_exited_or_missing_service_fails(self):
        for bad in ("exited", "missing", "none", "restarting"):
            ok, msgs = self._wait([{"a": ("healthy", ""), "b": ("healthy", ""), "c": (bad, "")}])
            self.assertFalse(ok, bad)
            self.assertIn(f"c is {bad}", msgs[-1])

    def test_a_wait_that_runs_out_fails(self):
        S = ("starting", "")
        ok, msgs = self._wait([{"a": S, "b": S, "c": S}], max_wait=12)
        self.assertFalse(ok)
        self.assertIn("NOT healthy after", msgs[-1])
        self.assertIn("a, b, c", msgs[-1])


class MainFlowTest(unittest.TestCase):
    """main() end to end with stubs: drain the runner, recreate the three services with one compose call,
    wait for three health checks; an unhealthy service is exit 1."""

    layout = "services"

    def _main(self, argv=(), compose_rc=0, healthy=True, profiles="", roles="on"):
        events = []
        runs = []

        def run(cmd, **kw):
            runs.append((cmd, kw.get("env", {}).get("AFCLAUDE_CODE_VERSION")))
            events.append("compose")
            return FakeCompleted("", compose_rc)
        with mock.patch.object(deploy, "scheduler_mode", return_value="runner"), \
             mock.patch.object(deploy, "layout", return_value=self.layout), \
             mock.patch.object(deploy, "check_data", return_value="/d/afclaude.db"), \
             mock.patch.object(deploy, "check_old_container", return_value=True), \
             mock.patch.object(deploy, "check_start_guard", return_value=True), \
             mock.patch.object(deploy, "env_value", side_effect=lambda name, *a, **k: profiles if name == "COMPOSE_PROFILES" else ""), \
             mock.patch("actions.roles_mode", return_value=roles), \
             mock.patch.object(deploy, "alert", side_effect=lambda s, b: events.append(("alert", s))), \
             mock.patch.object(deploy, "code_version", return_value="abc1234"), \
             mock.patch.object(deploy.store, "connect", return_value=mock.Mock()), \
             mock.patch.object(deploy.runner, "drain", side_effect=lambda *a, **k: events.append("drain") or 0), \
             mock.patch.object(deploy, "container_processes", side_effect=lambda c: events.append(f"top {c}") or []), \
             mock.patch.object(deploy, "wait_healthy", side_effect=lambda cs, t, **k: events.append(("health", tuple(cs))) or healthy), \
             mock.patch.object(deploy.subprocess, "run", side_effect=run), \
             mock.patch("builtins.print"):
            rc = deploy.main(list(argv))
        return rc, events, runs

    def test_order_drain_quiet_recreate_health(self):
        rc, events, runs = self._main()
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["drain", "top afclaude-runner", "compose",
                                  ("health", ("afclaude-runner", "afclaude-web"))])      # the mcp profile is not enabled
        cmd, version = runs[0]
        self.assertEqual(cmd[:4], ["docker", "compose", "-f", deploy.COMPOSE_SERVICES])
        self.assertEqual(cmd[-3:], ["up", "-d", "--build"])
        self.assertEqual(version, "abc1234")

    def test_with_the_mcp_profile_and_roles_on_the_health_wait_names_the_mcp_service_too(self):
        if self.layout != "services":
            self.skipTest("the single layout has no mcp service")
        rc, events, _ = self._main(profiles="mcp")
        self.assertEqual(rc, 0)
        self.assertIn(("health", ("afclaude-runner", "afclaude-mcp", "afclaude-web")), events)

    def test_the_mcp_profile_with_roles_in_shadow_refuses_before_the_drain(self):
        if self.layout != "services":
            self.skipTest("the single layout has no mcp service")
        rc, events, runs = self._main(profiles="mcp", roles="shadow")
        self.assertEqual((rc, events, runs), (1, [], []))

    def test_unhealthy_after_the_recreate_is_exit_1_and_alerted(self):
        rc, events, _ = self._main(healthy=False)
        self.assertEqual(rc, 1)
        self.assertIn(("alert", "deploy: the services are not healthy"), events)

    def test_a_failed_compose_is_its_exit_code_alerts_lifts_the_drain_and_waits_for_nothing(self):
        with mock.patch.object(deploy.runner, "undrain") as undrain:
            rc, events, _ = self._main(compose_rc=17)
        self.assertEqual(rc, 17)
        self.assertNotIn("health", [e[0] for e in events if isinstance(e, tuple)])
        self.assertIn(("alert", "deploy: docker compose up failed"), events)
        undrain.assert_called_once()

    def test_data_or_old_container_refusal_touches_nothing(self):
        for which in ("check_data", "check_old_container"):
            with mock.patch.object(deploy, "scheduler_mode", return_value="runner"), \
                 mock.patch.object(deploy, "layout", return_value="services"), \
                 mock.patch.object(deploy, "check_data", return_value="/d/db"), \
                 mock.patch.object(deploy, "check_old_container", return_value=True), \
                 mock.patch.object(deploy, which, return_value=None if which == "check_data" else False), \
                 mock.patch.object(deploy, "check_start_guard", side_effect=AssertionError("no guard")), \
                 mock.patch.object(deploy.store, "connect", side_effect=AssertionError("no drain")), \
                 mock.patch("builtins.print"):
                self.assertEqual(deploy.main([]), 1, which)


class SingleLayoutTest(MainFlowTest):
    """The default until K7 (D-339): deploy.py does what main's did, whatever the services layout needs."""

    layout = "single"

    def test_order_drain_quiet_recreate_health(self):
        rc, events, runs = self._main()
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["drain", "top afclaude", "compose"])      # no health wait, the one container
        cmd, version = runs[0]
        self.assertEqual(cmd, ["docker", "compose", "-f", deploy.COMPOSE_FILE, "up", "-d", "--build"])
        self.assertTrue(deploy.COMPOSE_FILE.endswith(os.path.join("docker", "compose.yml")))
        self.assertEqual(version, "abc1234")

    def test_unhealthy_after_the_recreate_is_exit_1_and_alerted(self):
        rc, events, _ = self._main(healthy=False)        # nothing is waited for: exit 0 as main's
        self.assertEqual(rc, 0)

    def test_data_or_old_container_refusal_touches_nothing(self):
        """Neither check applies to the single layout (the live .env has no AFCLAUDE_DATA)."""
        with mock.patch.object(deploy, "check_data", side_effect=AssertionError("not in single")), \
             mock.patch.object(deploy, "check_old_container", side_effect=AssertionError("not in single")):
            self.assertEqual(self._main()[0], 0)

    def test_window_start_refusal_still_comes_first(self):
        with mock.patch.object(deploy, "check_start_guard", return_value=False), \
             mock.patch.object(deploy.store, "connect", side_effect=AssertionError("no drain")), \
             mock.patch.object(deploy, "scheduler_mode", return_value="runner"), \
             mock.patch("builtins.print"):
            self.assertEqual(deploy.main([]), 1)


class LiveLikeEnvTest(unittest.TestCase):
    """Review k4a H1: a run of deploy.py against an environment like the live one (docker/.env without
    AFCLAUDE_DATA and without AFCLAUDE_LAYOUT) takes the single layout end to end: no data or old-container
    refusal, compose.yml, the one container's processes, no health wait."""

    ENV = ("AFCLAUDE_DIR=/srv/afclaude\nAFCLAUDE_HOST_REPO=/srv/afclaude\nHOST_HOME=/home/u\nAFCLAUDE_BRIDGE_USER=u\n"
           "AFCLAUDE_BRIDGE_HOST=host.docker.internal\nAFCLAUDE_SUBNET=172.28.0.0/24\nAFCLAUDE_SCHEDULER=runner\n")

    def _run(self, extra=""):
        import tempfile
        events, runs = [], []
        with tempfile.TemporaryDirectory() as d:
            envfile = os.path.join(d, ".env")
            with open(envfile, "w", encoding="utf-8") as fh:
                fh.write(self.ENV + extra)
            clean = {k: v for k, v in os.environ.items() if not k.startswith("AFCLAUDE_")}
            with mock.patch.dict(os.environ, clean, clear=True), \
                 mock.patch.object(deploy, "ENV_FILE", envfile), \
                 mock.patch.object(deploy, "check_start_guard", return_value=True), \
                 mock.patch.object(deploy, "code_version", return_value="abc1234"), \
                 mock.patch.object(deploy.store, "connect", return_value=mock.Mock()), \
                 mock.patch.object(deploy.runner, "drain", return_value=0), \
                 mock.patch.object(deploy, "container_processes", side_effect=lambda c: events.append(f"top {c}") or []), \
                 mock.patch.object(deploy, "old_container_running", side_effect=AssertionError("single: not asked")), \
                 mock.patch.object(deploy, "wait_healthy", side_effect=AssertionError("single: no health wait")), \
                 mock.patch.object(deploy.subprocess, "run", side_effect=lambda cmd, **k: runs.append(cmd) or FakeCompleted("")), \
                 mock.patch("builtins.print"):
                return deploy.main([]), events, runs

    def test_the_live_env_picks_the_single_layout(self):
        rc, events, runs = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(events, ["top afclaude"])
        self.assertEqual(runs, [["docker", "compose", "-f", deploy.COMPOSE_FILE, "up", "-d", "--build"]])

    def test_the_layout_key_in_the_env_file_chooses_the_services(self):
        """The same env plus AFCLAUDE_LAYOUT=services is the other path: refused for the missing AFCLAUDE_DATA."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            envfile = os.path.join(d, ".env")
            with open(envfile, "w", encoding="utf-8") as fh:
                fh.write(self.ENV + "AFCLAUDE_LAYOUT=services\n")
            clean = {k: v for k, v in os.environ.items() if not k.startswith("AFCLAUDE_")}
            with mock.patch.dict(os.environ, clean, clear=True), mock.patch.object(deploy, "ENV_FILE", envfile), \
                 mock.patch.object(deploy.store, "connect", side_effect=AssertionError("no drain")), \
                 mock.patch("builtins.print") as out:
                self.assertEqual(deploy.layout(), "services")
                self.assertEqual(deploy.main([]), 1)
            self.assertIn("AFCLAUDE_DATA is not set", out.call_args[0][0])


class HostPythonImportTest(unittest.TestCase):
    """tools/deploy.py runs under the host's own python, which has no `cryptography` (the image does): importing
    it, and so runner and service, must not need the master key's library (only service.ensure_key does)."""

    def test_deploy_imports_without_cryptography(self) -> None:
        import subprocess
        code = ("import sys; sys.modules['cryptography'] = None; sys.path.insert(0, 'tools'); import deploy; "
                "assert 'masterkey' not in sys.modules")
        r = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(os.path.abspath(__file__)),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])



class KitGuardTest(unittest.TestCase):
    """review:k6-fixes (a): in kit mode a deploy whose kit files or HOST_KIT_VERSION differ from the installed
    kit's own kit-version answer is refused before anything is drained (the new container's blocking kit check
    would pause every start until the owner re-runs the install)."""

    def setUp(self):
        import tempfile
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="deploy-kit-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def mine(self, **kw):
        info = {"kit_version": deploy.host.HOST_KIT_VERSION, "files": deploy.host.kit_source_hashes(),
                "commands": [], "whitelist_hash": "x"}
        info.update(kw)
        return info

    def test_both_layouts_are_in_kit_mode_and_the_pre_kit_compose_is_not(self):
        self.assertTrue(deploy.kit_mode("single"))
        self.assertTrue(deploy.kit_mode("services"))
        old = os.path.join(self.tmp, "compose.yml")
        with open(old, "w") as fh:
            fh.write("services:\n  afclaude:\n    environment:\n      # AFCLAUDE_BRIDGE_KEY: /app/data/bridge/k (a comment)\n"
                     "    volumes:\n      - ${AFCLAUDE_HOST_REPO}/docker/secrets/host_bridge_ed25519:/run/secrets/host_bridge_ed25519:ro\n")
        with mock.patch.object(deploy, "COMPOSE_FILE", old):
            self.assertFalse(deploy.kit_mode("single"))
            self.assertTrue(CHECK_HOST_KIT("single", answer=mock.Mock(side_effect=AssertionError("not asked"))))

    def compose_with(self, line):
        path = os.path.join(self.tmp, "compose.yml")
        with open(path, "w") as fh:
            fh.write("services:\n  afclaude:\n    environment:\n" + line)
        return mock.patch.object(deploy, "COMPOSE_FILE", path)

    def test_a_key_set_by_interpolation_refuses_the_deploy_instead_of_skipping_the_guard(self):
        for line in ("      AFCLAUDE_BRIDGE_KEY: ${KEY_DIR}/host_bridge_ed25519\n",
                     "      AFCLAUDE_BRIDGE_KEY: \"${AFCLAUDE_BRIDGE_KEY:-/app/data/bridge/k}\"\n",
                     "      - AFCLAUDE_BRIDGE_KEY=$KEY\n"):
            with self.compose_with(line):
                with self.assertRaises(deploy.KitModeUnknown):
                    deploy.kit_mode("single")
                with mock.patch("builtins.print") as out:
                    self.assertFalse(CHECK_HOST_KIT("single", answer=mock.Mock(side_effect=AssertionError("asked"))), line)
                self.assertIn("Nothing was drained", out.call_args[0][0])
                self.assertIn("interpolation", out.call_args[0][0])

    def test_a_passed_through_key_or_an_env_file_refuses_the_deploy(self):
        """No value (compose takes it from the deploying shell) or an env_file: that may set it: fail closed."""
        for line, why in (("      - AFCLAUDE_BRIDGE_KEY\n", "without a value"),
                          ("      - AFCLAUDE_BRIDGE_KEY   # from the shell\n", "without a value"),
                          ("      AFCLAUDE_BRIDGE_KEY:\n", "without a value"),
                          ("      AFCLAUDE_BRIDGE_KEY: ~\n", "without a value"),
                          ("      AFCLAUDE_BRIDGE_KEY:   # unset\n", "without a value"),
                          ("      TZ: UTC\n    env_file:\n      - bridge.env\n", "env_file"),
                          ("      AFCLAUDE_BRIDGE_KEY: /run/secrets/k\n    env_file: bridge.env\n", "env_file")):
            with self.compose_with(line):
                with self.assertRaises(deploy.KitModeUnknown):
                    deploy.kit_mode("single")
                with mock.patch("builtins.print") as out:
                    self.assertFalse(CHECK_HOST_KIT("single", answer=mock.Mock(side_effect=AssertionError("asked"))), line)
                self.assertIn("Nothing was drained", out.call_args[0][0])
                self.assertIn(why, out.call_args[0][0])
        # the kit's key set literally: an env_file cannot override the environment, so it is read as before
        with self.compose_with("      AFCLAUDE_BRIDGE_KEY: /app/data/bridge/k\n    env_file: other.env\n"):
            self.assertTrue(deploy.kit_mode("single"))
        with self.compose_with("      AFCLAUDE_BRIDGE_KEY_FILE: /x\n      # env_file: x\n"):
            self.assertFalse(deploy.kit_mode("single"))

    def test_other_literal_forms_of_the_key_are_read(self):
        with self.compose_with("      - AFCLAUDE_BRIDGE_KEY=/app/data/bridge/k  # kit\n"):
            self.assertTrue(deploy.kit_mode("single"))
        with self.compose_with("      \"AFCLAUDE_BRIDGE_KEY\": '/app/data/bridge/k'\n"):
            self.assertTrue(deploy.kit_mode("single"))
        with self.compose_with("      AFCLAUDE_BRIDGE_KEY: /run/secrets/host_bridge_ed25519\n"):
            self.assertFalse(deploy.kit_mode("single"))

    def test_the_same_kit_passes(self):
        self.assertTrue(CHECK_HOST_KIT("single", answer=lambda: self.mine()))

    def test_another_version_or_other_files_refuse_naming_the_install(self):
        files = dict(deploy.host.kit_source_hashes(), **{"ka_resume.sh": "0" * 64})
        for info in (self.mine(kit_version="5"), self.mine(files=files), self.mine(files=None)):
            with mock.patch("builtins.print") as out:
                self.assertFalse(CHECK_HOST_KIT("services", answer=lambda: info), info)
            text = " ".join(str(c.args[0]) for c in out.call_args_list)
            self.assertIn("install_host_bridge.sh", text)
            self.assertIn("Nothing was drained", text)

    def test_a_newer_installed_kit_refuses_naming_the_matching_code_not_the_install(self):
        with mock.patch("builtins.print") as out:
            self.assertFalse(CHECK_HOST_KIT("services", answer=lambda: self.mine(
                kit_version=str(int(deploy.host.HOST_KIT_VERSION) + 1))))
        text = " ".join(str(c.args[0]) for c in out.call_args_list)
        self.assertIn("NEWER", text)
        self.assertIn("deploy the matching code", text)
        self.assertNotIn("First re-run", text)
        self.assertIn("Nothing was drained", text)

    def test_a_kit_that_does_not_answer_refuses(self):
        with mock.patch("builtins.print") as out:
            self.assertFalse(CHECK_HOST_KIT("single", answer=mock.Mock(side_effect=RuntimeError("no host kit installed"))))
        self.assertIn("install_host_bridge.sh", out.call_args[0][0])

    def test_main_refuses_before_the_drain_even_with_force(self):
        with mock.patch.object(deploy, "check_host_kit", return_value=False), \
             mock.patch.object(deploy, "scheduler_mode", return_value="runner"), \
             mock.patch.object(deploy, "layout", return_value="single"), \
             mock.patch.object(deploy, "check_start_guard", side_effect=AssertionError("refused before")), \
             mock.patch.object(deploy.runner, "drain", side_effect=AssertionError("nothing drained")):
            self.assertEqual(deploy.main(["--force"]), 1)

    def test_the_installed_kit_is_asked_as_sshd_runs_it(self):
        """The real kit file, installed into a temp share directory with its host.json: its answer is this code's."""
        import json
        import shutil
        kit = os.path.join(self.tmp, "share", "afclaude", "host-kit", "current")
        os.makedirs(kit)
        for name, rel in deploy.host.KIT_FILES:
            shutil.copy(os.path.join(HERE, rel), os.path.join(kit, name))
        os.chmod(os.path.join(kit, "host_exec.py"), 0o755)
        root, data = os.path.join(self.tmp, "root"), os.path.join(self.tmp, "data")
        os.makedirs(root)
        os.makedirs(data)
        hj = os.path.join(self.tmp, "share", "afclaude", "host.json")
        with open(hj, "w") as fh:
            json.dump({"format": 2, "roots": [root], "repo": HERE, "claude": "/usr/bin/true", "data": data}, fh)
        os.chmod(hj, 0o600)
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.join(self.tmp, "share"),
                                          "AFCLAUDE_HOST_JSON": "/nonexistent"}):   # ignored, as under sshd
            info = deploy.installed_kit_answer()
            self.assertTrue(CHECK_HOST_KIT("single"))
        self.assertEqual((info["kit_version"], info["files"]), (deploy.host.HOST_KIT_VERSION, deploy.host.kit_source_hashes()))

    def test_the_answer_errors(self):
        import subprocess
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.join(self.tmp, "none")}), \
                self.assertRaisesRegex(RuntimeError, "no host kit installed"):
            deploy.installed_kit_answer()
        kit = os.path.join(self.tmp, "afclaude", "host-kit", "current")
        os.makedirs(kit)
        open(os.path.join(kit, "host_exec.py"), "w").close()
        answers = (FakeCompleted("", 126, "host_exec: denied (host config)"), FakeCompleted("not json"),
                   FakeCompleted("[]"), subprocess.TimeoutExpired("k", 30), OSError("exec format"))
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp}):
            for a in answers:
                run = mock.Mock(side_effect=a) if isinstance(a, BaseException) else mock.Mock(return_value=a)
                with self.assertRaises(RuntimeError, msg=a):
                    deploy.installed_kit_answer(run=run)
                self.assertEqual(run.call_args.kwargs["env"]["SSH_ORIGINAL_COMMAND"], "kit-version")
                self.assertEqual(run.call_args.kwargs["timeout"], deploy.KIT_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
