#!/usr/bin/env python3
"""The model and effort a project's task-manager session runs on (D-260).

    "Option per project to set nanager model" (D-260)

A project has two optional settings, projects.manager_model and projects.manager_effort (NULL =
the code default below, which is what every task-manager ran on before D-260). The owner sets
them with the audited action project.manager_launch (owner-only, like every setting: `tasks.py
project launch P --model M --effort E`, the MCP tool afclaude_project action=launch). They apply
whenever the keep-alive starts or resumes that project's task-manager session (keepalive.fire ->
ka_resume.sh --model/--effort: the session window starts, the last stretch, a fill-up, the
dispatcher, an inbox delivery that has to resume it); a session that is already running keeps
its model until its next start/resume (ka_resume.sh only types into a live tmux session).

Values are checked when they are written (a ValueError says why) and again when a start reads
them (for_session): a stored value that is not known, or two projects managed by the same
session that ask for different launches, raise LaunchError, and the start is aborted with an
alert (D-235: no silent fallback to the default).
"""
from __future__ import annotations

import sqlite3
from typing import Any

import compact_timing

# Today's task-manager launch (keepalive.LAUNCH, ka_resume.sh's own defaults): Opus 5.5, high effort.
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"

# `claude --effort` levels (docker/host_exec.py allows the same for ka_resume.sh).
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Full model ids only (an alias like "opus" can move to another model without anyone choosing it),
# and only those compact_timing knows the price of: its compaction break-even fits the session's
# model and fails on an unknown one. Haiku is left out: ka_resume.sh runs a Haiku session in
# permission mode dontAsk (auto falls back to manual prompts there), where a task-manager can't
# work unattended.
MODELS = tuple(m for m in compact_timing.PRICES if "haiku" not in m)
# What project.manager_launch accepts to clear a value back to the default.
RESET = ("", "default")


class LaunchError(ValueError):
    """A stored launch setting a start can't use (unknown value, conflicting projects)."""


def check_model(v: Any) -> str:
    """A known model id (MODELS), else ValueError."""
    if not isinstance(v, str) or v.strip() not in MODELS:
        raise ValueError(f"model must be one of {'|'.join(MODELS)}, got {v!r}")
    return v.strip()


def check_effort(v: Any) -> str:
    """An effort level (EFFORTS, case-insensitive), else ValueError."""
    s = v.strip().lower() if isinstance(v, str) else None
    if s is None or s not in EFFORTS:
        raise ValueError(f"effort must be one of {'|'.join(EFFORTS)}, got {v!r}")
    return s


def for_session(conn: sqlite3.Connection, session_id: str) -> dict[str, str]:
    """The launch overrides for session_id: {"model": .., "effort": ..} with only the keys a project
    it manages sets ({} = not a task-manager, or nothing set: the caller's defaults). Raises
    LaunchError on a stored value that fails its check, or when the projects it manages ask for
    different launches (an unset value counts as the default there)."""
    rows = conn.execute("SELECT name, manager_model, manager_effort FROM projects WHERE manager_session=? "
                        "ORDER BY id", (session_id,)).fetchall()
    picks: dict[tuple[str | None, str | None], list[str]] = {}
    for name, model, effort in (tuple(r) for r in rows):
        try:
            pick = (check_model(model) if model is not None else None,
                    check_effort(effort) if effort is not None else None)
        except ValueError as e:
            raise LaunchError(f"project {name!r}: stored task-manager launch is invalid: {e}") from None
        picks.setdefault(pick, []).append(name)
    if len(picks) > 1:
        raise LaunchError(f"session {session_id[:8]} manages projects with different task-manager launches: "
                          + "; ".join(f"{', '.join(names)}: model {m or 'default'}, effort {e or 'default'}"
                                      for (m, e), names in picks.items()))
    if not picks:
        return {}
    model, effort = next(iter(picks))
    return {k: v for k, v in (("model", model), ("effort", effort)) if v is not None}


def view(p: Any) -> dict[str, Any]:
    """A project row's launch for display: {model, effort, source: {model, effort}: project|default}."""
    return {"model": p["manager_model"] or DEFAULT_MODEL, "effort": p["manager_effort"] or DEFAULT_EFFORT,
            "source": {"model": "project" if p["manager_model"] else "default",
                       "effort": "project" if p["manager_effort"] else "default"}}
