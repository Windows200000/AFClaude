#!/usr/bin/env python3
"""K5 part 2b Brief A (inert groundwork for the session switch, D-217, D-344): the wrapper's routes, the
identity hook's CLI pattern and the repo's Claude settings. Every permission rule and hook command the repo
uses today has a wrapper route, and the additive wrapper allow rules are in place (nothing removed).

    python3 -m unittest test_wrapper_switch
"""
import json
import os
import re
import shlex
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import afclaude  # noqa: E402
import identity_hook  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, ".claude", "settings.json"), encoding="utf-8") as _f:
    SETTINGS = json.load(_f)
ALLOW = SETTINGS["permissions"]["allow"]
HOOK_COMMANDS = [h["command"] for groups in SETTINGS["hooks"].values() for g in groups for h in g["hooks"]]
# host-only commands with no wrapper form: the deploy runs on the host (it drives docker), and the rest are not AFClaude CLIs
NOT_WRAPPED = ("tools/deploy.py", "tools/test_compact_live.sh", "tools/write_docs.py", "claude agents")


def routes():
    """[(script, route args)] of every `afclaude <command>` and `afclaude hook <name>` form."""
    return [(r.script, r.args) for r in list(afclaude.ROUTES.values()) + list(afclaude.HOOKS.values())]


def covered(script, args):
    return any(s == script and tuple(args[:len(a)]) == a for s, a in routes())


def legacy_call(text):
    """(script, args) of `python3 <path>/script.py args ...`, None for anything else."""
    parts = shlex.split(text.replace("$CLAUDE_PROJECT_DIR/", ""))
    if len(parts) >= 2 and parts[0] == "python3" and parts[1].endswith(".py"):
        return os.path.basename(parts[1]), parts[2:]
    return None


class Routes(unittest.TestCase):
    def test_the_new_routes_resolve_to_their_modules(self):
        py = sys.executable
        self.assertEqual(afclaude.resolve(["keepalive", "--decide", "x"]),
                         [py, os.path.join(HERE, "keepalive.py"), "--decide", "x"])
        self.assertEqual(afclaude.resolve(["usage-report", "--json"]), [py, os.path.join(HERE, "usage_report.py"), "--json"])
        self.assertEqual(afclaude.resolve(["prompts", "vars"]), [py, os.path.join(HERE, "prompts.py"), "vars"])

    def test_they_are_in_the_help(self):
        text = afclaude.usage()
        for name in ("keepalive", "usage-report", "prompts"):
            self.assertIn(f"  {name}", text)


class AllowRules(unittest.TestCase):
    def test_every_legacy_allow_rule_has_a_wrapper_route_and_a_wrapper_rule(self):
        seen = 0
        for rule in ALLOW:
            m = re.fullmatch(r"Bash\((.*?)(?::\*)?\)", rule)
            if not m or m.group(1).startswith("afclaude") or any(x in m.group(1) for x in NOT_WRAPPED):
                continue
            call = legacy_call(m.group(1))
            if call is None:
                continue
            seen += 1
            script, args = call
            self.assertTrue(covered(script, args), f"{rule}: no afclaude route runs {script} {args}")
            name = next(n for n, r in afclaude.ROUTES.items() if r.script == script)
            wrapped = f"Bash(afclaude {name}" + (f" {' '.join(args)}" if args else "") + ":*)"
            self.assertIn(wrapped, ALLOW, f"{rule}: the wrapper rule {wrapped} is missing (additive, D-217)")
        self.assertEqual(seen, 4)       # tasks, keepalive --decide, keepalive --run-stopped, usage_report

    def test_every_wrapper_rule_names_a_real_route_and_a_flag_its_module_has(self):
        wrapper = [r for r in ALLOW if r.startswith("Bash(afclaude ")]
        self.assertEqual(len(wrapper), 4)
        for rule in wrapper:
            words = re.fullmatch(r"Bash\(afclaude (.*?):\*\)", rule).group(1).split()
            route = afclaude.ROUTES.get(words[0])
            self.assertIsNotNone(route, rule)
            with open(os.path.join(HERE, route.script), encoding="utf-8") as f:
                src = f.read()
            for flag in (w for w in words[1:] if w.startswith("--")):
                self.assertIn(f'"{flag}"', src, f"{rule}: {route.script} has no {flag}")

    def test_nothing_was_removed(self):
        for rule in ("Bash(python3 tasks.py:*)", "Bash(python3 keepalive.py --decide:*)",
                     "Bash(python3 keepalive.py --run-stopped:*)", "Bash(python3 usage_report.py:*)",
                     "Bash(python3 tools/deploy.py)", "Bash(python3 tools/deploy.py:*)", "mcp__afclaude",
                     "Bash(claude agents:*)", "Bash(bash tools/test_compact_live.sh:*)"):
            self.assertIn(rule, ALLOW)

    def test_the_doc_export_helper_is_allowed_as_a_relative_pattern(self):
        self.assertIn("Bash(python3 tools/write_docs.py:*)", ALLOW)
        self.assertFalse([r for r in ALLOW if "/mnt/" in r or "/home/" in r], "D-217: no personal paths")


class HookCommands(unittest.TestCase):
    def test_every_hook_command_has_a_route_so_the_switch_changes_only_the_command(self):
        n = 0
        for cmd in HOOK_COMMANDS:
            if cmd.startswith("afclaude"):
                continue
            body = cmd.split(" || ")[0]
            call = legacy_call(body)
            self.assertIsNotNone(call, cmd)
            n += 1
            script, args = call
            self.assertTrue(covered(script, args), f"{cmd}: no afclaude route runs {script} {args}")
        self.assertGreaterEqual(n, 8)


class CliPattern(unittest.TestCase):
    def test_legacy_and_wrapper_forms_match(self):
        for cmd in ("python3 tasks.py list", "cd x && python3 keepalive.py --decide a b",
                    "afclaude tasks list", "afclaude tasks doc append progress x", "cd x && afclaude keepalive --decide s",
                    "echo 1; afclaude usage-report", "(afclaude prompts vars)", "/home/u/.local/bin/afclaude tasks add t",
                    "afclaude runner status", "afclaude  tasks list"):
            self.assertTrue(identity_hook.CLI_RE.search(cmd), cmd)

    def test_other_commands_do_not(self):
        for cmd in ("ls", "git status", "echo afclaude", "afclaude", "afclaude --help", "myafclaude tasks",
                    "afclaude-mcp tasks", "docker exec afclaude ls", "cat tasks.txt"):
            self.assertFalse(identity_hook.CLI_RE.search(cmd), cmd)

    def test_a_subagent_command_gets_the_agent_id_for_the_wrapper_too(self):
        payload = {"agent_id": "a1", "permission_mode": "auto", "tool_input": {"command": "afclaude tasks list"}}
        out = identity_hook.bash_output(payload)
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"]["command"],
                         "export AFCLAUDE_AGENT_ID=a1; afclaude tasks list")
        self.assertIsNone(identity_hook.bash_output(dict(payload, tool_input={"command": "ls"})))


if __name__ == "__main__":
    unittest.main()
