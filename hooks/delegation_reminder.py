#!/usr/bin/env python3
"""UserPromptSubmit hook: remind AFClaude manager sessions, on EVERY user message,
to delegate instead of doing the work themselves (user rule, 2026-09-29).

Registered in the user's ~/.claude/settings.json, so it runs for all sessions, but it
only adds context for AFClaude sessions:
  - the session's cwd is inside an AFClaude work dir (AFCLAUDE_DIRS), or
  - CLAUDE_GUARD_DISABLE=1 (set for every session AFClaude launches via ka_resume.sh).
Other sessions get no output. It never blocks a prompt.

Phase 5a A5 (docs/phase5a_plan.md §3.8): it also carries the one-line notice of finished subagent runs not
delivered yet (delivery.notice_for_session), once, to the project's task-manager only. No run waiting is one
read-only query on the database (stdlib sqlite3, no repo import); the repo's modules load only when one waits.
A failure of that part is a line in the context and on stderr (D-235), the reminder itself still goes out.
"""
import json
import os
import sqlite3
import sys

AFCLAUDE_DIRS = [d for d in os.environ.get(
    "AFCLAUDE_DIRS", "/mnt/BlockVolume/Claude/work/AFClaude").split(":") if d]

REMINDER = (
    "AFClaude manager reminder: you are the manager, not the worker. Delegate this "
    "request to a subagent (Agent tool) unless it is so small that delegating would cost "
    "more context than doing it (a one-line check, a status-file update, a commit). This "
    "also applies when the user asks you directly to do or test something. If the user "
    "just resolved or answered something, close its question in the AFClaude DB first."
)


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAITING_SQL = ("SELECT 1 FROM agent_runs WHERE delivered_at IS NULL AND ended_at IS NOT NULL AND kind='subagent' "
               "LIMIT 1")


def run_notice(session):
    """The notice of undelivered subagent runs for this session if it is a task-manager, else ''. A DB that is not
    there is an error in the live checkout (D-235); in another checkout (a worktree, .git is no directory) there
    is nothing to deliver."""
    db = os.environ.get("AFCLAUDE_DB") or os.path.join(REPO, "data", "afclaude.db")
    if not session:
        return ""
    if not os.path.isfile(db) and not os.path.isdir(os.path.join(REPO, ".git")):
        return ""
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        waiting = ro.execute(WAITING_SQL).fetchone()
    finally:
        ro.close()
    if waiting is None:
        return ""
    sys.path.insert(0, REPO)
    import delivery
    import store
    conn = store.connect(db)
    try:
        return delivery.notice_for_session(conn, session)
    finally:
        conn.close()


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
        context = REMINDER
        try:
            notice = run_notice(data.get("session_id"))
        except Exception as e:      # noqa: BLE001 - shown, not swallowed (D-235); the prompt still goes through
            notice = f"AFClaude: the run-summary notice failed: {type(e).__name__}: {e}"
            print(notice, file=sys.stderr)
        if notice:
            context += "\n" + notice
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": context}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
