"""afclaude.py: the entry command of the image (phase 5b K1): routes, refusals, help, a real hand-over."""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: a temp DB and the live-store guard; before the AFClaude imports)
import afclaude  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "afclaude.py")


def run(*args):
    return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, timeout=60, cwd="/")


class Routes(unittest.TestCase):
    def test_every_route_points_at_a_module_that_exists(self):
        for name, r in list(afclaude.ROUTES.items()) + list(afclaude.HOOKS.items()):
            self.assertTrue(os.path.isfile(os.path.join(HERE, r.script)), (name, r.script))

    def test_resolve_puts_the_route_args_before_the_users(self):
        py = sys.executable
        self.assertEqual(afclaude.resolve(["runner", "serve"]),
                         [py, "-u", os.path.join(HERE, "runner.py"), "serve"])
        self.assertEqual(afclaude.resolve(["mcp"]), [py, os.path.join(HERE, "mcp_server.py")])
        self.assertEqual(afclaude.resolve(["web", "serve"]), [py, "-u", os.path.join(HERE, "web.py"), "serve"])
        self.assertEqual(afclaude.resolve(["web", "health"]), [py, "-u", os.path.join(HERE, "web.py"), "health"])
        self.assertEqual(afclaude.resolve(["mcp", "serve"]), [py, os.path.join(HERE, "mcp_server.py"), "serve"])
        self.assertEqual(afclaude.resolve(["machine", "list"]), [py, os.path.join(HERE, "tasks.py"), "machine", "list"])
        self.assertEqual(afclaude.resolve(["hook", "session-rules", "manager"]),
                         [py, os.path.join(HERE, "keepalive.py"), "--session-rules", "manager"])
        self.assertEqual(afclaude.resolve(["hook", "decisions", "--part", "1", "--of", "3"]),
                         [py, os.path.join(HERE, "decisions.py"), "hook", "--part", "1", "--of", "3"])
        self.assertEqual(afclaude.resolve(["hook", "identity", "bash"]), [py, os.path.join(HERE, "identity_hook.py"), "bash"])

    def test_the_planned_commands_and_unknown_ones_are_refused(self):
        for cmd in ("key", "setup"):
            with self.assertRaises(SystemExit) as cm:
                afclaude.resolve([cmd, "x"])
            self.assertIn("not built yet", str(cm.exception.code))
        for bad in ([], ["nope"], ["hook"], ["hook", "nope"]):
            with self.assertRaises(SystemExit):
                afclaude.resolve(bad)

    def test_the_planned_commands_are_the_plans_list(self):
        listed = set(afclaude.ROUTES) | set(afclaude.PLANNED) | {"hook"}
        self.assertEqual(listed, {"runner", "mcp", "web", "host", "key", "setup", "machine", "tasks", "hook"})


class Command(unittest.TestCase):
    def test_help(self):
        r = run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for word in ("runner", "mcp", "web", "tasks", "machine", "hook", "host", "key", "setup"):
            self.assertIn(word, r.stdout)

    def test_refusals_exit_2_with_a_message(self):
        for args in (("nope",), ("key",), ()):
            r = run(*args)
            self.assertEqual(r.returncode, afclaude.EXIT_USAGE, args)
            self.assertTrue(r.stderr.startswith("afclaude"), (args, r.stderr))

    def test_it_hands_over_to_the_module(self):
        r = run("runner", "--help")          # runner.py's own argparse help, through the exec
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("serve", r.stdout)
        self.assertIn("health", r.stdout)


if __name__ == "__main__":
    unittest.main()
