#!/usr/bin/env python3
"""Tests for tools/cutover_5b.py (phase 5b K7, inert groundwork): a dry-run changes and runs nothing that changes,
the steps run in order through ONE injectable runner, every failure alerts, a failure before the old container's stop
only lifts the drain, one after it rolls back (old image, no build), the .env flips are written only by --apply and
restored on a rollback. No test touches docker: the runner is a stub that records argv and answers from a table."""
import testenv  # noqa: F401  (first: the hermetic DB and data dirs)
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
import cutover_5b  # noqa: E402
import deploy  # noqa: E402
from cutover_5b import Cutover, is_read_only  # noqa: E402

ENV_TEXT = """AFCLAUDE_HOST_REPO=/repo
HOST_HOME=/home/x
AFCLAUDE_BRIDGE_USER=x
AFCLAUDE_BRIDGE_HOST=host.docker.internal
AFCLAUDE_SCHEDULER=runner
#AFCLAUDE_LAYOUT=single
"""
HEALTHY = json.dumps([{"State": {"Status": "running", "Health": {"Status": "healthy", "Log": []}}}])


class Done:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class FakeDocker:
    """The injected runner: records every argv, answers from a table. `fail`: a predicate on the joined argv ->
    the return code that call gets. `strict`: a changing command is an error (the dry-run test)."""

    def __init__(self, strict=False):
        self.calls = []
        self.strict = strict
        self.fail = lambda cmd: 0
        self.images = {"afclaude-manager:latest": "sha256:old"}
        self.old_running = True
        self.existing = []            # container names that exist (`ps -a`)
        self.appear = []              # names that exist once `compose up` ran
        self.health = HEALTHY
        self.health_by = {}           # container -> its `docker inspect` answer, else self.health
        self.timeouts = {}

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        cmd = " ".join(argv)
        if self.strict and not is_read_only(list(argv)):
            raise AssertionError(f"a changing command ran in a dry-run: {cmd}")
        if cmd in self.timeouts:
            raise subprocess.TimeoutExpired(argv, 1)
        if argv[:2] == ["docker", "compose"] and "up" in argv and "compose.services.yml" in cmd:
            self.existing = self.existing + self.appear
        rc = self.fail(cmd)
        if rc:
            return Done("", rc, "it broke")
        if argv[:3] == ["docker", "image", "inspect"]:
            ref = argv[-1]
            return Done(self.images[ref] + "\n") if ref in self.images else Done("", 1, "no such image")
        if argv[:2] == ["git", "-C"]:
            return Done("abc1234\n")
        if argv[:2] == ["docker", "ps"] and "-a" in argv and "--filter" in argv:
            name = re.search(r"name=\^(.*)\$", cmd).group(1)
            return Done(name + "\n" if name in self.existing else "")
        if argv[:2] == ["docker", "ps"] and "-a" in argv:
            return Done("\n".join(self.existing) + "\n")
        if argv[:2] == ["docker", "ps"]:
            return Done("afclaude\n" if self.old_running else "")
        if argv[:2] == ["docker", "inspect"]:
            return Done(self.health_by.get(argv[2], self.health))
        if argv[:2] == ["docker", "top"]:
            return Done("PID COMMAND\n1 /usr/bin/tini\n")
        return Done("")

    def commands(self):
        return [" ".join(a) for a, _ in self.calls]

    def changing(self):
        return [c for c in self.commands() if not is_read_only(c.split())]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cutover-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.data, "bridge"))
        open(os.path.join(self.data, "afclaude.db"), "w").close()
        open(os.path.join(self.data, "bridge", "host_bridge_ed25519"), "w").close()
        self.env_file = os.path.join(self.tmp, ".env")
        self.write_env(ENV_TEXT + f"AFCLAUDE_DATA={self.data}\n")
        for p in (mock.patch.object(deploy, "ENV_FILE", self.env_file),
                  mock.patch.object(cutover_5b.store, "DB_PATH", cutover_5b.store.DB_PATH),
                  mock.patch.object(cutover_5b.store, "connect", return_value=mock.Mock()),
                  mock.patch.object(deploy, "container_writes", return_value=True),
                  mock.patch.object(deploy.runner, "drain_span", return_value=__import__("datetime").timedelta(minutes=5)),
                  mock.patch.object(deploy, "mcp_enabled", return_value=False),
                  mock.patch.object(deploy, "name_prefix", return_value="afclaude")):
            p.start()
            self.addCleanup(p.stop)
        self.alerts, self.lines = [], []
        self.docker = FakeDocker()
        self.guard = True
        self.kit = True

    def write_env(self, text):
        with open(self.env_file, "w") as fh:
            fh.write(text)

    def env_text(self):
        with open(self.env_file) as fh:
            return fh.read()

    def make(self, apply=False, rollback=True):
        outer = self

        class C(Cutover):
            def guard_ok(s):
                if not outer.guard:
                    print("a session-window start is at 23:00")
                return outer.guard

            def kit_ok(s):
                if not outer.kit:
                    print("the host kit is not installed")
                return outer.kit

            def mcp_ok(s):
                return True
        return C(self.docker, apply, rollback=rollback, out=self.lines.append,
                 alert=lambda s, b: self.alerts.append((s, b)), sleep=lambda s: None, clock=lambda: 0.0,
                 now=lambda: datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc), environ={})

    def text(self):
        return "\n".join(self.lines)


class ReadOnlyTest(unittest.TestCase):
    def test_what_a_dry_run_may_run(self):
        ro = [["docker", "ps"], ["docker", "ps", "-a", "--format", "{{.Names}}"], ["docker", "inspect", "c"],
              ["docker", "images"], ["docker", "image", "inspect", "x"],
              ["docker", "compose", "-f", "a.yml", "-f", "b.yml", "config", "-q"],
              ["git", "-C", "/r", "rev-parse", "--short", "HEAD"]]
        for argv in ro:
            self.assertTrue(is_read_only(argv), argv)
        rw = [["docker", "stop", "c"], ["docker", "rm", "c"], ["docker", "tag", "a", "b"], ["docker", "exec", "c", "ls"],
              ["docker", "compose", "-f", "a.yml", "up", "-d"], ["docker", "compose", "build"],
              ["docker", "compose", "-f", "config", "up"], ["docker", "image", "rm", "x"], ["docker", "rmi", "x"],
              ["git", "-C", "/r", "checkout", "x"], ["git", "push"], ["rm", "-rf", "/"], ["docker"], []]
        for argv in rw:
            self.assertFalse(is_read_only(argv), argv)

    def test_read_refuses_a_changing_command(self):
        c = Cutover(mock.Mock(), False)
        with self.assertRaises(ValueError):
            c.read(["docker", "stop", "afclaude"])
        c.run.assert_not_called()

    def test_the_runner_has_no_default(self):
        with self.assertRaises(TypeError):
            Cutover()      # a test (or a caller) that forgets to inject one cannot reach a real docker


class SetEnvLinesTest(unittest.TestCase):
    def test_replaces_every_line_of_a_key_and_appends_the_missing_ones(self):
        out = cutover_5b.set_env_lines("A=1\n#B=old\nB=2\nA=3\nC=keep\n", {"A": "9", "D": "4"})
        self.assertEqual(out.splitlines(), ["A=9", "#B=old", "B=2", "A=9", "C=keep",
                                            "# phase 5b K7 cut-over (tools/cutover_5b.py)", "D=4"])

    def test_a_commented_key_is_not_a_line_of_it(self):
        self.assertIn("AFCLAUDE_LAYOUT=services", cutover_5b.set_env_lines(ENV_TEXT, {"AFCLAUDE_LAYOUT": "services"}))
        self.assertIn("#AFCLAUDE_LAYOUT=single", cutover_5b.set_env_lines(ENV_TEXT, {"AFCLAUDE_LAYOUT": "services"}))


class DryRunTest(Base):
    def test_prints_every_step_and_changes_and_runs_nothing(self):
        self.docker = FakeDocker(strict=True)
        before = self.env_text()
        rc = self.make().go()
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.docker.changing(), [])
        self.assertEqual(self.env_text(), before)
        self.assertEqual(os.listdir(self.tmp).count(".env"), 1)          # no backup either
        t = self.text()
        for needle in ("DRY RUN", "docker tag afclaude-manager:latest afclaude-manager:pre-k7",
                       "AFCLAUDE_CODE_VERSION=abc1234", "build afclaude-runner",
                       "docker exec afclaude afclaude runner drain", "docker stop afclaude",
                       "AFCLAUDE_LAYOUT=services", "up -d", "docker exec afclaude-runner afclaude host check",
                       "claude mcp add --scope user afclaude -- afclaude mcp stdio",
                       "compose.single.yml", "Preflight OK"):
            self.assertIn(needle, t)
        self.assertNotIn("--build", t)
        self.assertEqual(self.alerts, [])

    def test_the_flips_are_listed_with_the_overrides(self):
        self.make().go()
        t = self.text()
        self.assertIn("AFCLAUDE_LAYOUT=services", t)
        self.assertIn("AFCLAUDE_COMPOSE_OVERRIDES=compose.host-mounts.yml,compose.subnet.yml", t)
        self.assertNotIn("AFCLAUDE_SCHEDULER=runner,", t)         # already set: not a flip
        self.assertNotIn("AFCLAUDE_DATA=" + self.data + ",", t)

    def test_a_failed_preflight_is_exit_1_with_the_plan_still_printed(self):
        self.docker = FakeDocker(strict=True)
        self.guard = False
        rc = self.make().go()
        self.assertEqual(rc, 1)
        t = self.text()
        self.assertIn("[FAIL] session-window start guard", t)
        self.assertIn("Steps", t)
        self.assertIn("--apply would refuse", t)
        self.assertEqual(self.docker.changing(), [])

    def test_no_kit_is_a_failed_preflight(self):
        self.kit = False
        self.assertEqual(self.make().go(), 1)
        self.assertIn("[FAIL] host kit: the host kit is not installed", self.text())

    def test_the_rollback_is_printed(self):
        self.make().go()
        t = self.text()
        self.assertIn(f"docker compose -f {cutover_5b.COMPOSE_SINGLE} up -d", t)
        self.assertIn("docker stop afclaude-runner && docker rm afclaude-runner", t)


class PreflightTest(Base):
    def run_dry(self):
        return self.make().go(), self.text()

    def test_missing_host_keys(self):
        self.write_env("AFCLAUDE_HOST_REPO=/repo\n")
        rc, t = self.run_dry()
        self.assertEqual(rc, 1)
        self.assertIn("HOST_HOME, AFCLAUDE_BRIDGE_USER not set", t)

    def test_no_env_file(self):
        os.unlink(self.env_file)
        rc, t = self.run_dry()
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", t)

    def test_no_database_or_no_bridge_key(self):
        os.unlink(os.path.join(self.data, "afclaude.db"))
        rc, t = self.run_dry()
        self.assertEqual(rc, 1)
        self.assertIn("no database at", t)

    def test_the_bridge_key_must_be_installed(self):
        os.unlink(os.path.join(self.data, "bridge", "host_bridge_ed25519"))
        rc, t = self.run_dry()
        self.assertEqual(rc, 1)
        self.assertIn("run the host kit install first", t)

    def test_no_old_image_means_nothing_to_roll_back_to(self):
        self.docker.images = {}
        rc, t = self.run_dry()
        self.assertEqual(rc, 1)
        self.assertIn("nothing to roll back to", t)

    def test_leftover_new_containers_refuse(self):
        self.docker.existing = ["afclaude-web"]
        rc, t = self.run_dry()
        self.assertEqual(rc, 1)
        self.assertIn("afclaude-web already exist", t)

    def test_a_compose_config_that_does_not_validate_refuses(self):
        self.docker.fail = lambda cmd: 1 if " config -q" in cmd else 0
        rc, t = self.run_dry()
        self.assertEqual(rc, 1)
        self.assertIn("does not validate", t)

    def test_an_override_that_is_not_a_compose_file_in_docker_refuses(self):
        self.write_env(self.env_text() + "AFCLAUDE_COMPOSE_OVERRIDES=../x.yml\n")
        rc, t = self.run_dry()
        self.assertEqual(rc, 1)
        self.assertIn("each must be a compose file name in docker/", t)

    def test_an_already_flipped_env_needs_no_flip_and_no_backup(self):
        self.write_env(ENV_TEXT + f"AFCLAUDE_DATA={self.data}\nAFCLAUDE_LAYOUT=services\n"
                       "AFCLAUDE_COMPOSE_OVERRIDES=compose.host-mounts.yml,compose.subnet.yml\n")
        rc, t = self.run_dry()
        self.assertEqual(rc, 0, t)
        self.assertIn("already set for the services layout", t)

    def test_the_data_dir_defaults_to_the_repos_data(self):
        self.write_env(ENV_TEXT)
        c = self.make()
        c.check_env()
        self.assertEqual(c.target["AFCLAUDE_DATA"], "/repo/data")

    def test_the_old_container_not_running_skips_drain_and_stop(self):
        self.docker.old_running = False
        self.assertEqual(self.make().go(), 0)
        self.assertIn("skipped, the old container is not running", self.text())


class ApplyTest(Base):
    def apply(self, **kw):
        return self.make(True, **kw).go()

    def test_the_happy_path_in_order(self):
        rc = self.apply()
        self.assertEqual(rc, 0, self.text())
        self.assertEqual(self.alerts, [])
        ch = self.docker.changing()
        names = [re.sub(r"^docker compose.* (build|up -d).*$", r"compose \1", c) for c in ch]
        self.assertEqual(names, ["docker tag afclaude-manager:latest afclaude-manager:pre-k7",
                                 "compose build",
                                 "docker exec afclaude afclaude runner drain",
                                 "docker stop afclaude",
                                 "compose up -d",
                                 "docker exec afclaude-runner afclaude host check"])
        up = [(a, kw) for a, kw in self.docker.calls if a[:2] == ["docker", "compose"] and "up" in a][0]
        self.assertNotIn("--build", up[0])
        self.assertEqual(up[1]["env"]["AFCLAUDE_CODE_VERSION"], "abc1234")
        self.assertEqual(up[1]["env"]["AFCLAUDE_LAYOUT"], "services")
        text = self.env_text()
        self.assertIn("AFCLAUDE_LAYOUT=services", text)
        self.assertIn("AFCLAUDE_COMPOSE_OVERRIDES=compose.host-mounts.yml,compose.subnet.yml", text)
        backups = [f for f in os.listdir(self.tmp) if f.startswith(".env.pre-k7-")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.tmp, backups[0])) as fh:
            self.assertEqual(fh.read(), ENV_TEXT + f"AFCLAUDE_DATA={self.data}\n")
        t = self.text()
        self.assertIn("claude mcp add --scope user afclaude -- afclaude mcp stdio", t)
        self.assertIn("docker rmi afclaude-manager:pre-k7", t)
        self.assertIn("2026-11-27", t)               # the rollback week

    def test_the_health_wait_names_runner_and_web(self):
        self.apply()
        inspected = [a[2] for a, _ in self.docker.calls if a[:2] == ["docker", "inspect"]]
        self.assertEqual(sorted(set(inspected)), ["afclaude-runner", "afclaude-web"])

    def test_an_existing_rollback_tag_is_kept_not_retagged(self):
        self.docker.images["afclaude-manager:pre-k7"] = "sha256:older"
        self.assertEqual(self.apply(), 0)
        self.assertNotIn("docker tag afclaude-manager:latest afclaude-manager:pre-k7", self.docker.commands())

    def test_a_refused_preflight_touches_nothing_and_does_not_alert(self):
        self.guard = False
        self.assertEqual(self.apply(), 1)
        self.assertEqual(self.docker.changing(), [])
        self.assertEqual(self.alerts, [])
        self.assertNotIn("DRY RUN", self.text())

    def test_a_failed_build_alerts_and_stops_nothing(self):
        self.docker.fail = lambda cmd: 1 if " build " in cmd else 0
        self.assertEqual(self.apply(), 1)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("before the old container was stopped", self.alerts[0][0])
        self.assertNotIn("docker stop afclaude", self.docker.commands())
        self.assertNotIn("afclaude runner undrain", " ".join(self.docker.commands()))   # never drained
        self.assertEqual(self.env_text(), ENV_TEXT + f"AFCLAUDE_DATA={self.data}\n")

    def test_a_drain_that_fails_lifts_it_and_stops_nothing(self):
        self.docker.fail = lambda cmd: 1 if "runner drain" in cmd else 0
        self.assertEqual(self.apply(), 1)
        self.assertNotIn("docker stop afclaude", self.docker.commands())
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("could not be drained", self.alerts[0][1])

    def test_a_guard_that_closes_during_the_drain_lifts_it_and_stops_nothing(self):
        c = self.make(True)
        seen = []
        original = c.guard_ok
        c.guard_ok = lambda: (seen.append(1), len(seen) < 2)[1] and original()   # ok in the preflight, refused after
        with mock.patch("builtins.print"):
            rc = c.go()
        self.assertEqual(rc, 1)
        self.assertIn("afclaude runner undrain", " ".join(self.docker.commands()))
        self.assertNotIn("docker stop afclaude", self.docker.commands())
        self.assertEqual(self.env_text(), ENV_TEXT + f"AFCLAUDE_DATA={self.data}\n")

    def test_a_stop_that_fails_while_the_container_still_runs_lifts_the_drain_only(self):
        self.docker.fail = lambda cmd: 1 if cmd == "docker stop afclaude" else 0
        self.assertEqual(self.apply(), 1)
        cmds = self.docker.commands()
        self.assertIn("docker exec afclaude afclaude runner undrain", cmds)
        self.assertFalse([c for c in cmds if "compose.single.yml" in c])
        self.assertIn("before the old container was stopped", self.alerts[0][0])
        self.assertEqual(self.env_text(), ENV_TEXT + f"AFCLAUDE_DATA={self.data}\n")

    def test_a_stop_timeout_is_an_alert_not_a_hang(self):
        self.docker.timeouts = {"docker stop afclaude": 1}
        self.assertEqual(self.apply(), 1)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("did not finish within", self.alerts[0][1])

    def assertRolledBack(self):
        cmds = self.docker.commands()
        single = [c for c in cmds if f"-f {cutover_5b.COMPOSE_SINGLE}" in c]
        self.assertEqual(len(single), 1)
        self.assertTrue(single[0].endswith(" up -d"))
        self.assertNotIn("--build", single[0])
        self.assertEqual(self.env_text(), ENV_TEXT + f"AFCLAUDE_DATA={self.data}\n")      # restored

    def test_a_failed_up_rolls_back_to_the_old_image(self):
        self.docker.appear = ["afclaude-runner"]
        self.docker.fail = lambda cmd: 1 if " up -d" in cmd and "compose.services.yml" in cmd else 0
        self.assertEqual(self.apply(), 1)
        self.assertRolledBack()
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("rolling back", self.alerts[0][0])
        cmds = self.docker.commands()
        self.assertIn("docker stop afclaude-runner", cmds)
        self.assertIn("docker rm afclaude-runner", cmds)
        self.assertNotIn("docker stop afclaude-web", cmds)           # it was not there: nothing to remove

    def test_unhealthy_services_roll_back(self):
        self.docker.health_by = {"afclaude-runner": json.dumps([{"State": {"Status": "running", "Health": {
            "Status": "unhealthy", "Log": [{"Output": "no heartbeat"}]}}}])}
        self.docker.appear = ["afclaude-runner", "afclaude-web"]
        self.assertEqual(self.apply(), 1)
        self.assertIn("not healthy", self.alerts[0][1])
        self.assertIn("no heartbeat", self.alerts[0][1])
        self.assertRolledBack()

    def test_a_failed_host_check_rolls_back(self):
        self.docker.fail = lambda cmd: 1 if "afclaude host check" in cmd else 0
        self.assertEqual(self.apply(), 1)
        self.assertIn("host check", self.alerts[0][1])
        self.assertRolledBack()

    def test_a_failed_rollback_is_a_second_alert_with_the_manual_commands(self):
        self.docker.fail = lambda cmd: 1 if "compose.services.yml" in cmd and " up -d" in cmd or (
            "compose.single.yml" in cmd) else 0
        self.assertEqual(self.apply(), 1)
        self.assertEqual(len(self.alerts), 2)
        self.assertIn("ROLLBACK failed", self.alerts[1][0])
        self.assertIn("compose.single.yml", self.alerts[1][1])
        self.assertIn("ROLLBACK FAILED", self.text())

    def test_no_rollback_leaves_things_and_prints_the_way_back(self):
        self.docker.fail = lambda cmd: 1 if "afclaude host check" in cmd else 0
        self.assertEqual(self.apply(rollback=False), 1)
        self.assertFalse([c for c in self.docker.commands() if "compose.single.yml" in c])
        self.assertIn("no rollback requested", self.alerts[0][0])
        self.assertIn("compose.single.yml", self.alerts[0][1])
        self.assertIn("AFCLAUDE_LAYOUT=services", self.env_text())

    def test_the_drain_goes_through_the_injected_runner(self):
        self.apply()
        drains = [(a, kw) for a, kw in self.docker.calls if a[-2:] == ["runner", "drain"]]
        self.assertEqual(len(drains), 1)
        self.assertEqual(drains[0][0][:3], ["docker", "exec", "afclaude"])
        self.assertIsNotNone(drains[0][1]["timeout"])      # a hung exec cannot hold the cut-over (D-234)

    def test_a_cutover_with_nothing_running_skips_the_old_container_steps(self):
        self.docker.old_running = False
        self.assertEqual(self.apply(), 0, self.text())
        self.assertNotIn("docker stop afclaude", self.docker.commands())

    def test_no_real_command_is_made_anywhere_but_the_runner(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("real subprocess")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("real subprocess")):
            self.assertEqual(self.apply(), 0, self.text())


class MainTest(unittest.TestCase):
    def test_dry_run_is_the_default_and_the_runner_is_subprocess_run(self):
        with mock.patch.object(cutover_5b, "Cutover") as C:
            C.return_value.go.return_value = 0
            self.assertEqual(cutover_5b.main([]), 0)
            C.assert_called_once_with(subprocess.run, False, rollback=True)
            C.reset_mock()
            cutover_5b.main(["--apply", "--no-rollback"])
            C.assert_called_once_with(subprocess.run, True, rollback=False)


class ComposeSingleTest(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(HERE, "docker", name), encoding="utf-8") as fh:
            return fh.read()

    def body(self, text):
        return [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]

    def test_runs_the_pinned_old_image_and_never_builds(self):
        t = self.read("compose.single.yml")
        self.assertIn(f"image: {cutover_5b.ROLLBACK_TAG}", t)
        self.assertIn("pull_policy: never", t)
        self.assertNotRegex(t, r"(?m)^\s*build:")
        self.assertNotIn("afclaude-manager:latest", "\n".join(self.body(t)))

    def test_is_the_old_layout_otherwise(self):
        build = ["    build:", "      context: ..", "      dockerfile: docker/Dockerfile", "      args:",
                 "        AFCLAUDE_CODE_VERSION: ${AFCLAUDE_CODE_VERSION:-}   # git commit, from the Start line",
                 "    image: afclaude-manager:latest"]
        old = [l for l in self.body(self.read("compose.yml")) if l not in build]
        new = [l for l in self.body(self.read("compose.single.yml"))
               if not l.strip().startswith(("image:", "pull_policy:"))]
        self.assertEqual(new, old)
        self.assertIn("    container_name: afclaude", new)

    def test_the_tag_matches_the_script(self):
        self.assertEqual(cutover_5b.ROLLBACK_TAG, "afclaude-manager:pre-k7")
        self.assertEqual(cutover_5b.COMPOSE_SINGLE, os.path.join(HERE, "docker", "compose.single.yml"))


if __name__ == "__main__":
    unittest.main()
