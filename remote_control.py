#!/usr/bin/env python3
"""Whether a session AFClaude launches gets Remote Control (`claude --remote-control`), per agent
category (D-269).

    "Make claude rc be a setting per agent category" (D-269)

One owner-only setting per category (actions.SETTINGS, `remote_control_<category>`, true/false;
set with `setting.set`, e.g. the MCP tool / the dashboard's settings page). The default is true
for every category: every ka_resume.sh launch had --remote-control before D-269, so nothing changes
until the owner turns one off (D-268: the final build may need none, the dashboard being the way
to talk to the task-manager).

It applies when keepalive.fire() starts or resumes a session (ka_resume.sh --no-remote-control
when off); a session that is already running in its tmux session keeps what it was started with
until its next start/resume (ka_resume.sh then only types into it), like the model (D-260).

The categories (who launches them):
  task_manager  the task-manager sessions: AFClaude's own (the watcher's session-window starts,
                last stretch, fill-up, compaction hand-off), a managed project's, a backlog
                project's first start (keepalive.py --work-on), an inbox delivery that has to
                resume it (inbox_worker.py); also the legacy launch_nightly.sh
  review        the one-off review sessions usage_review.py starts (usage / stage-eta, the D-228 chain)
  continue      the owner's approved stalled sessions the dispatcher continues after a limit reset
                (the owner's own sessions and AFClaude sessions that are not task-managers)
  test          test sessions launched through ka_resume.sh (test_e2e_tmux.sh)

A launch reads its value strictly (D-235): a database that can't be read, or a saved value that
fails the setting's check, raises RemoteControlError and the launch is aborted with the caller's
alert; it is never launched on the default instead.

    python3 remote_control.py flags <category>   ka_resume.sh's flag for that category ("" when on),
                                                 for shell launchers; exit 1 with the reason on an error
    python3 remote_control.py show               the effective value of every category (JSON)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

TASK_MANAGER, REVIEW, CONTINUE, TEST = "task_manager", "review", "continue", "test"
# category -> what it covers (the setting's one-line explanation, D-075)
CATEGORIES: dict[str, str] = {
    TASK_MANAGER: "the task-manager sessions (AFClaude's and each managed project's: window starts, "
                  "last stretch, fill-up, compaction, inbox delivery, a backlog project's first start)",
    REVIEW: "the one-off review sessions (usage review, stage-ETA review, their chain)",
    CONTINUE: "the owner's approved stalled sessions the dispatcher continues after a limit reset",
    TEST: "test sessions launched through ka_resume.sh (test_e2e_tmux.sh)",
}
DEFAULT = True             # before D-269 every ka_resume.sh launch had --remote-control
PREFIX = "remote_control_"
NO_RC_FLAG = "--no-remote-control"   # ka_resume.sh (and docker/host_exec.py's whitelist)


class RemoteControlError(ValueError):
    """A launch can't tell whether to use Remote Control (unknown category, invalid saved value)."""


def setting_key(category: str) -> str:
    """The setting of `category`, else RemoteControlError."""
    if category not in CATEGORIES:
        raise RemoteControlError(f"unknown session category {category!r} (known: {', '.join(CATEGORIES)})")
    return PREFIX + category


def enabled(conn: sqlite3.Connection, category: str) -> bool:
    """Whether `category`'s launches get Remote Control: the saved setting, else DEFAULT. A saved
    value that fails the check raises RemoteControlError (never the default, D-235)."""
    import actions
    key = setting_key(category)
    value, source = actions.effective_setting(conn, key)
    if source not in ("db", "default"):
        raise RemoteControlError(f"setting {key}: the saved value is {source}")
    return bool(value)


def launch_flags(category: str) -> list[str]:
    """ka_resume.sh's extra arguments for a launch of `category`: [] (Remote Control on, its
    default) or [NO_RC_FLAG]. Reads the DB strictly: an unreadable DB or an invalid saved value
    raises (the caller aborts the launch)."""
    import store
    setting_key(category)                 # an unknown category fails before the DB is opened
    conn = store.connect()
    try:
        return [] if enabled(conn, category) else [NO_RC_FLAG]
    finally:
        conn.close()


def view(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """{category: {enabled, source: db|default, setting, covers}} for display; a saved value that
    fails its check shows enabled None and its source "invalid: <why>" (a launch of it would fail)."""
    import actions
    out: dict[str, dict[str, Any]] = {}
    for cat, covers in CATEGORIES.items():
        key = setting_key(cat)
        value, source = actions.effective_setting(conn, key)
        out[cat] = {"enabled": bool(value) if source in ("db", "default") else None, "source": source,
                    "setting": key, "covers": covers}
    return out


def main(argv: list[str]) -> int:
    import store
    if len(argv) == 2 and argv[0] == "flags":
        try:
            print(" ".join(launch_flags(argv[1])))
        except (store.DBError, sqlite3.Error, ValueError) as e:   # ValueError: RemoteControlError too
            print(f"remote_control: {e}", file=sys.stderr)
            return 1
        return 0
    if argv == ["show"]:
        conn = store.connect()
        try:
            print(json.dumps(view(conn), indent=1))
        finally:
            conn.close()
        return 0
    print("usage: remote_control.py flags <category> | show", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
