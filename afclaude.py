#!/usr/bin/env python3
"""The `afclaude` entry command of the image (phase 5b K1, docs/phase5b_plan.md §5): one name for
every AFClaude program, so nothing outside the image needs to know where the code lives (/app) or
which module does what.

    afclaude runner serve|status|drain|undrain|wake|health|...   the resident runner (runner.py)
    afclaude mcp [stdio|serve]                                   the MCP server: stdio (default) or HTTP (mcp_server.py)
    afclaude web serve|health                                    the web service: /health and the inbox API on its unix socket (web.py)
    afclaude tasks ...                                           the task store CLI (tasks.py)
    afclaude machine ...                                         machine users and tokens (tasks.py machine ...)
    afclaude keepalive --decide|--run-stopped|... | usage-report | prompts ...   the keep-alive, usage report and prompt CLIs (keepalive.py, usage_report.py, prompts.py)
    afclaude hook session-rules|decisions|identity ...           the session hooks (keepalive.py, decisions.py, identity_hook.py)
    afclaude host check [name ...] [--blocking] | status          the host checks (hostcheck.py; K6)
    afclaude key|setup                                           not built yet (K4, 5d/6a): refused with a message

The command finds the modules next to this file (the symlink /usr/local/bin/afclaude is followed) and
replaces itself with the module's own process (os.execv): the same argv, exit code, signals and
process line the module has when it is started directly, so the runner's `runner.py serve` check
and `docker stop` behave as before. Stdlib only; no database access here.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass

CODE_DIR = os.path.dirname(os.path.realpath(__file__))

EXIT_USAGE = 2


@dataclass(frozen=True)
class Route:
    script: str                  # the module, relative to the code directory
    args: tuple[str, ...] = ()   # put in front of the user's arguments
    unbuffered: bool = False     # python3 -u (a long-running service whose stdout is the container log)
    help: str = ""


ROUTES: dict[str, Route] = {
    "runner": Route("runner.py", unbuffered=True, help="the resident runner: serve | status | drain | undrain | wake | health | ..."),
    "mcp": Route("mcp_server.py", help="the MCP server: stdio (the default) | serve (HTTP, the mcp service)"),
    "web": Route("web.py", unbuffered=True, help="the web service: serve | health (/health and the inbox API on a unix socket)"),
    "tasks": Route("tasks.py", help="the task store CLI (projects, tasks, decisions, docs, questions, ...)"),
    "machine": Route("tasks.py", ("machine",), help="machine users: code | register | register-host (on the host, not here) | list | rename | revoke"),
    "keepalive": Route("keepalive.py", help="the keep-alive CLI: --decide | --run-stopped | ... (keepalive.py)"),
    "usage-report": Route("usage_report.py", help="the usage report (usage_report.py)"),
    "prompts": Route("prompts.py", help="the prompt store: vars | override | ... (prompts.py)"),
    "host": Route("hostcheck.py", help="host check [name ...] [--blocking]: validate the host setup (hostcheck.py); status"),
}
# `afclaude hook <name>`: the session hooks the repo's .claude/settings.json runs (K5 moves them here).
HOOKS: dict[str, Route] = {
    "session-rules": Route("keepalive.py", ("--session-rules",), help="the standing rules for a session start: manager | afclaude"),
    "decisions": Route("decisions.py", ("hook",), help="the decisions TLDR for a session start: --part N --of M"),
    "identity": Route("identity_hook.py", help="the identity stamp for a tool call: mcp | bash"),
}
# Named in the plan, not built yet: refused loudly instead of doing something else (D-235).
PLANNED: dict[str, str] = {
    "key": "phase 5b K4 (the services call masterkey.ensure(); a key command comes with the setup)",
    "setup": "phase 5d / 6a (the setup wizard)",
}


def usage() -> str:
    lines = ["usage: afclaude <command> [args ...]", "", "commands:"]
    lines += [f"  {name:<9} {r.help}" for name, r in ROUTES.items()]
    lines += [f"  hook      {', '.join(HOOKS)}: the session hooks"]
    lines += [f"  {name:<9} not built yet ({why})" for name, why in PLANNED.items()]
    lines += ["", "The arguments after the command go to the module unchanged (afclaude runner --help, afclaude tasks --help)."]
    return "\n".join(lines)


def resolve(argv: Sequence[str]) -> list[str]:
    """The argv of the process to run for `afclaude <argv>`. Raises SystemExit(2) with a message
    for no command, an unknown one or one that is not built yet."""
    if not argv:
        raise SystemExit(f"afclaude: no command\n{usage()}")
    cmd, rest = argv[0], list(argv[1:])
    route = ROUTES.get(cmd)
    if cmd == "hook":
        if not rest or rest[0] not in HOOKS:
            raise SystemExit(f"afclaude hook: expected one of {', '.join(HOOKS)}, got {rest[0] if rest else 'nothing'}")
        route, rest = HOOKS[rest[0]], rest[1:]
    if cmd in PLANNED:
        raise SystemExit(f"afclaude {cmd}: not built yet ({PLANNED[cmd]})")
    if route is None:
        raise SystemExit(f"afclaude: unknown command {cmd!r}\n{usage()}")
    script = os.path.join(CODE_DIR, route.script)
    if not os.path.isfile(script):
        raise SystemExit(f"afclaude {cmd}: {route.script} is missing from {CODE_DIR}")
    return [sys.executable] + (["-u"] if route.unbuffered else []) + [script, *route.args, *rest]


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["--help"], ["-h"], ["help"]):
        print(usage())
        return 0
    try:
        full = resolve(args)
    except SystemExit as e:
        print(e.code, file=sys.stderr)
        return EXIT_USAGE
    sys.stdout.flush()
    os.execv(full[0], full)


if __name__ == "__main__":
    sys.exit(main())
