#!/usr/bin/env python3
"""The identity hooks (phase 5a A2, docs/phase5a_plan.md §3.3): stamp who calls, so identity.py can tell a
subagent from its parent and a /clear'd session from the id the MCP server started with.

    identity_hook.py mcp     PreToolUse, matcher mcp__afclaude__.*: the call's arguments plus
                             `_afc: {session, agent, agent_type}` (agent / agent_type only inside a subagent)
    identity_hook.py bash    PreToolUse, matcher Bash: inside a subagent, a command that runs one of our CLIs
                             gets `export AFCLAUDE_AGENT_ID=<id>; ` in front (the one-command prefix form
                             would not reach the second command of `cd x && python3 tasks.py ...`), only in
                             permission mode auto or bypassPermissions: a rewritten command no longer matches
                             a prefix allow rule (`Bash(python3 tasks.py:*)`), so in default / dontAsk mode it
                             would be refused or ask; there a subagent counts as its parent

Stdlib only and no database: a bare python3 start is ~14 ms, the whole hook ~24 ms (the plan's latency target
is 50 ms). It returns updatedInput and no permissionDecision, so the permission rules keep applying. It fails
open: an error is one line on stderr and exit code 1 (a non-blocking hook error), the tool call goes on
unstamped, and the MCP server logs that once (mcp_server.UNSTAMPED_NOTE). A model-supplied `_afc` is never
passed on: every path that stamps nothing still removes it from the call's arguments (updatedInput).
"""
from __future__ import annotations

import json
import re
import sys

STAMP_KEY = "_afc"                       # identity.STAMP_KEY (not imported: no store/actions import here)
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CLI_RE = re.compile(r"\b(tasks|keepalive|usage_report|limit_ratio|runner|project_docs|decisions|prompts|backup|"
                    r"telemetry|compact_timing|deploy|export_quickview|questions|setup|runner_state)\.py\b"
                    r"|(?:^|[;&|(/\s])afclaude\s+(?:tasks|machine|keepalive|usage-report|prompts|runner|hook)\b")
REWRITE_MODES = ("auto", "bypassPermissions")   # payload["permission_mode"] values where the Bash rewrite is safe


def _updated(new_input: dict) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": new_input}}


def mcp_output(payload: dict) -> dict | None:
    """The stamped input of an afclaude MCP call. Without a usable stamp the call still goes on with any
    `_afc` the model wrote removed (None only when there is none to remove)."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    clean = {k: v for k, v in tool_input.items() if k != STAMP_KEY}
    unstamped = _updated(clean) if STAMP_KEY in tool_input else None
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not ID_RE.match(sid):
        return unstamped
    stamp = {"session": sid}
    agent, agent_type = payload.get("agent_id"), payload.get("agent_type")
    if isinstance(agent, str) and agent:
        if not ID_RE.match(agent):
            return unstamped             # never put an odd id into the stamp: the call goes unstamped
        stamp["agent"] = agent
    if isinstance(agent_type, str) and agent_type:
        stamp["agent_type"] = agent_type[:64]
    return _updated(dict(clean, **{STAMP_KEY: stamp}))


def bash_output(payload: dict) -> dict | None:
    """The command with the agent id exported, only inside a subagent, only for our CLIs and only in a
    permission mode where the rewritten command is still allowed (REWRITE_MODES)."""
    agent = payload.get("agent_id")
    tool_input = payload.get("tool_input")
    if payload.get("permission_mode") not in REWRITE_MODES:
        return None
    if not isinstance(agent, str) or not ID_RE.match(agent) or not isinstance(tool_input, dict):
        return None
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or not CLI_RE.search(cmd):
        return None
    return _updated(dict(tool_input, command=f"export AFCLAUDE_AGENT_ID={agent}; {cmd}"))


def main(argv: list[str]) -> int:
    try:
        which = argv[1] if len(argv) > 1 else ""
        fn = {"mcp": mcp_output, "bash": bash_output}.get(which)
        if fn is None:
            raise ValueError(f"usage: identity_hook.py mcp|bash (got {which!r})")
        payload = json.load(sys.stdin)
        out = fn(payload if isinstance(payload, dict) else {})
        if out is not None:
            json.dump(out, sys.stdout)
        return 0
    except Exception as e:               # noqa: BLE001 - fail open: reported, never blocks the tool call
        print(f"identity_hook: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
