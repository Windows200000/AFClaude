"""Agent proposals, the pure part (phase 5a A4, docs/phase5a_plan.md §3.7; D-182, D-185, D-188).

An agent's write that is not one of the approved direct writes (D-185) becomes a proposal for the project's
task-manager instead of being applied (actions.perform stores it; proposal.review decides it). This module
builds the one-line summary of a proposal and holds the vocabulary; it reads no database. mypy --strict.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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


# ---- the run summary (§3.8, D-182, D-243): everything an agent did in one run, for its task-manager

WRITE_KINDS = (("task.", "tasks"), ("handoff.", "handoffs"), ("doc.append", "alerts"))   # the rest: "other"
CHANGE_MAX = 6          # changed fields named per write


def _loads(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _write_line(row: Mapping[str, Any]) -> dict[str, Any]:
    """One audit row as a summary entry: when, what, on which target, and which fields changed to what."""
    after = _loads(row.get("after"))
    changes = ""
    if isinstance(after, dict) and after:
        shown = [f"{k}={_short(v)!r}" if isinstance(v, str) else f"{k}={_short(v)}"
                 for k, v in list(after.items())[:CHANGE_MAX]]
        changes = ", ".join(shown) + (", ..." if len(after) > CHANGE_MAX else "")
    target = f" {row['target_type']}#{row['target_id']}" if row.get("target_type") else ""
    return {"ts": row.get("ts"), "action": row.get("action"), "target": target.strip(),
            "line": f"{row.get('action')}{target}" + (f": {changes}" if changes else "")}


def _kind_of(action: str) -> str:
    return next((label for prefix, label in WRITE_KINDS if action.startswith(prefix)), "other")


def summary(run: Mapping[str, Any], writes: Sequence[Mapping[str, Any]],
            proposals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The summary stored in agent_runs.summary at close. `writes`: the audit rows the agent wrote during the
    run, ALL of them (direct ones too: D-182 wants a summary of every write); `proposals`: the run's
    agent_proposals rows. Proposals are split: `pending` (open, with their payloads), `decided`, and
    `shadow` (the write was applied under roles_enforce=shadow, so it also appears in `writes`: what would
    have been a proposal once enforced). Code changes are not repeated: the task-manager reads the run's
    branch with change_manifest.py (D-243)."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for w in writes:
        by_kind.setdefault(_kind_of(str(w.get("action"))), []).append(_write_line(w))
    pending = [{"id": p["id"], "status": p["status"], "summary": p["summary"], "payload": p["payload"]}
               for p in proposals if not p.get("shadow") and p["status"] in ("proposed", "postponed")]
    decided = [{"id": p["id"], "status": p["status"], "summary": p["summary"], "reviewer": p.get("reviewer")}
               for p in proposals if not p.get("shadow") and p["status"] not in ("proposed", "postponed")]
    shadow = [{"id": p["id"], "summary": p["summary"]} for p in proposals if p.get("shadow")]
    return {"run": run["id"], "kind": run["kind"], "session": run["session_id"], "agent_id": run.get("agent_id"),
            "project_id": run.get("project_id"), "started_at": run["started_at"], "ended_at": run.get("ended_at"),
            "end_reason": run.get("end_reason"), "writes": by_kind, "write_count": len(writes),
            "pending": pending, "decided": decided, "shadow": shadow}


def summary_text(s: Mapping[str, Any]) -> str:
    """The summary in the compact form a task-manager reads."""
    who = f"{s['kind']} {s['session']}" + (f"/{s['agent_id']}" if s.get("agent_id") else "")
    lines = [f"run {s['run']} ({who}, {s['started_at']} to {s.get('ended_at')}, {s.get('end_reason')}): "
             f"{s['write_count']} write(s), {len(s['pending'])} proposal(s) pending"]
    for label, rows in s["writes"].items():
        lines += [f"  {label}: " + r["line"] for r in rows]
    lines += [f"  pending P-{p['id']} ({p['status']}): {p['summary']}" for p in s["pending"]]
    lines += [f"  decided P-{p['id']} ({p['status']} by {p['reviewer']}): {p['summary']}" for p in s["decided"]]
    lines += [f"  applied under shadow P-{p['id']}: {p['summary']}" for p in s["shadow"]]
    return "\n".join(lines)


def alert_text(rows: Sequence[Mapping[str, Any]], hours: float) -> tuple[str, str]:
    """(subject, body) of the owner alert for proposals that waited past `hours`."""
    subject = f"{len(rows)} agent proposal(s) waiting for review for over {hours:g} h"
    body = "\n".join(f"P-{r['id']} ({r['status']}, since {r['created_at']}, {r['actor']}): {r['summary']}"
                     for r in rows)
    return subject, body + "\nThe project's task-manager decides them: tasks.py proposals / tasks.py review."
