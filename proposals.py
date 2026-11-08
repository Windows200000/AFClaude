"""Agent proposals, the pure part (phase 5a A4, docs/phase5a_plan.md §3.7; D-182, D-185, D-188).

An agent's write that is not one of the approved direct writes (D-185) becomes a proposal for the project's
task-manager instead of being applied (actions.perform stores it; proposal.review decides it). This module
builds the one-line summary of a proposal and holds the vocabulary; it reads no database. mypy --strict.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

REVIEW_DECISIONS = ("approve", "edit", "reject", "postpone")
SUMMARY_MAX = 240
VALUE_MAX = 48
# Params that name the target rather than change it: shown first, without a key.
TARGETS = ("task_id", "decision", "name", "project", "kind", "key", "notice_id", "message_id")


def _short(v: Any, limit: int = VALUE_MAX) -> str:
    s = " ".join(str(v).split())
    return s if len(s) <= limit else s[:limit - 1] + "..."


def summarize(action: str, params: Mapping[str, Any]) -> str:
    """`task.edit #12: title="...", priority=high` -- the action, its target, the fields it sets; at most
    SUMMARY_MAX characters (the full params stay in the proposal's payload)."""
    target = next((f"#{params[k]}" if k == "task_id" else str(params[k]) for k in TARGETS if params.get(k) is not None),
                  "")
    fields = [f"{k}={_short(v)!r}" if isinstance(v, str) else f"{k}={_short(v)}"
              for k, v in params.items() if v is not None and k not in TARGETS and k != "version"]
    line = f"{action}{' ' + target if target else ''}"
    if fields:
        line += ": " + ", ".join(fields)
    return line if len(line) <= SUMMARY_MAX else line[:SUMMARY_MAX - 3] + "..."


def version_seen(params: Mapping[str, Any]) -> int | None:
    """The `version` the agent saw (an int), else None."""
    v = params.get("version")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def proposed_message(proposal_id: int, summary: str) -> str:
    """What the agent's tool says instead of the write's result."""
    return (f"proposed as P-{proposal_id}, waiting for the task-manager's review (nothing was changed): {summary}")
