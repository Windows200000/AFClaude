#!/usr/bin/env python3
"""UserPromptSubmit hook: remind AFClaude manager sessions, on EVERY user message,
to delegate instead of doing the work themselves (user rule, 2026-09-29).

Registered in the user's ~/.claude/settings.json, so it runs for all sessions, but it
only adds context for AFClaude sessions:
  - the session's cwd is inside an AFClaude work dir (AFCLAUDE_DIRS), or
  - CLAUDE_GUARD_DISABLE=1 (set for every session AFClaude launches via ka_resume.sh).
Other sessions get no output. It never blocks a prompt.
"""
import json
import os
import sys

AFCLAUDE_DIRS = [d for d in os.environ.get(
    "AFCLAUDE_DIRS", "/mnt/BlockVolume/Claude/work/AFClaude").split(":") if d]

REMINDER = (
    "AFClaude manager reminder: you are the manager, not the worker. Delegate this "
    "request to a subagent (Agent tool) unless it is so small that delegating would cost "
    "more context than doing it (a one-line check, a status-file update, a commit). This "
    "also applies when the user asks you directly to do or test something. If the user "
    "just resolved or answered something, delete it from OPEN_QUESTIONS.md first."
)


def is_afclaude(cwd):
    if os.environ.get("CLAUDE_GUARD_DISABLE") == "1":
        return True
    cwd = os.path.realpath(cwd or "")
    return any(cwd == os.path.realpath(d) or cwd.startswith(os.path.realpath(d) + os.sep)
               for d in AFCLAUDE_DIRS)


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}
    if is_afclaude(data.get("cwd") or os.getcwd()):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": REMINDER}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
