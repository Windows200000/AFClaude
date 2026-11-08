"""Agent proposals, the pure part (phase 5a A4, docs/phase5a_plan.md §3.7; D-182, D-185, D-188).

An agent's write that is not one of the approved direct writes (D-185) becomes a proposal for the project's
task-manager instead of being applied (actions.perform stores it; proposal.review decides it). This module
builds the one-line summary of a proposal and holds the vocabulary; it reads no database. mypy --strict.
"""
from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
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


def inbox_notice_text(prop: Mapping[str, Any]) -> str:
    """Phase 5a A5: the inbox notice of a separate session's proposal for its task-manager (store.add_proposal_notice;
    proposal_notice.md frames it): which proposal, whose, what it would do."""
    return f"P-{prop['id']} by {prop['actor']} ({prop['action']}): {prop['summary']}"


def notice_proposal(text: str) -> int | None:
    """The proposal id an inbox_notice_text names, or None (not a proposal notice)."""
    m = NOTICE_RE.match(text)
    return int(m.group(1)) if m else None


NOTICE_RE = re.compile(r"^P-(\d+) by ")


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
            proposals: Sequence[Mapping[str, Any]], noticed: Collection[int] = ()) -> dict[str, Any]:
    """The summary stored in agent_runs.summary at close. `writes`: the audit rows the agent wrote during the
    run, ALL of them (direct ones too: D-182 wants a summary of every write); `proposals`: the run's
    agent_proposals rows. Proposals are split: `pending` (open, with their payloads), `decided`, and
    `shadow` (the write was applied under roles_enforce=shadow, so it also appears in `writes`: what would
    have been a proposal once enforced). `noticed`: the ids of open proposals that already reached the
    task-manager as inbox notices (a separate session's, A5 part 2): listed under `noticed`, not `pending`,
    so a delivery names them by id only. Code changes are not repeated: the task-manager reads the run's
    branch with change_manifest.py (D-243)."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for w in writes:
        by_kind.setdefault(_kind_of(str(w.get("action"))), []).append(_write_line(w))
    open_ = [p for p in proposals if not p.get("shadow") and p["status"] in ("proposed", "postponed")]
    pending = [{"id": p["id"], "status": p["status"], "summary": p["summary"], "payload": p["payload"]}
               for p in open_ if p["id"] not in noticed]
    sent = [{"id": p["id"], "status": p["status"], "summary": p["summary"]} for p in open_ if p["id"] in noticed]
    decided = [{"id": p["id"], "status": p["status"], "summary": p["summary"], "reviewer": p.get("reviewer")}
               for p in proposals if not p.get("shadow") and p["status"] not in ("proposed", "postponed")]
    shadow = [{"id": p["id"], "summary": p["summary"]} for p in proposals if p.get("shadow")]
    return {"run": run["id"], "kind": run["kind"], "session": run["session_id"], "agent_id": run.get("agent_id"),
            "project_id": run.get("project_id"), "started_at": run["started_at"], "ended_at": run.get("ended_at"),
            "end_reason": run.get("end_reason"), "writes": by_kind, "write_count": len(writes),
            "pending": pending, "noticed": sent, "decided": decided, "shadow": shadow}


def summary_text(s: Mapping[str, Any]) -> str:
    """The summary in the compact form a task-manager reads."""
    who = f"{s['kind']} {s['session']}" + (f"/{s['agent_id']}" if s.get("agent_id") else "")
    lines = [f"run {s['run']} ({who}, {s['started_at']} to {s.get('ended_at')}, {s.get('end_reason')}): "
             f"{s['write_count']} write(s), {len(s['pending'])} proposal(s) pending"]
    for label, rows in s["writes"].items():
        lines += [f"  {label}: " + r["line"] for r in rows]
    lines += [f"  pending P-{p['id']} ({p['status']}): {p['summary']}" for p in s["pending"]]
    lines += [f"  sent as a notice P-{p['id']} ({p['status']}): {p['summary']}" for p in s.get("noticed", [])]
    lines += [f"  decided P-{p['id']} ({p['status']} by {p['reviewer']}): {p['summary']}" for p in s["decided"]]
    lines += [f"  applied under shadow P-{p['id']}: {p['summary']}" for p in s["shadow"]]
    return "\n".join(lines)


def alert_text(rows: Sequence[Mapping[str, Any]], hours: float) -> tuple[str, str]:
    """(subject, body) of the owner alert for proposals that waited past `hours`."""
    subject = f"{len(rows)} agent proposal(s) waiting for review for over {hours:g} h"
    body = "\n".join(f"P-{r['id']} ({r['status']}, since {r['created_at']}, {r['actor']}): {r['summary']}"
                     for r in rows)
    return subject, body + "\nThe project's task-manager decides them: tasks.py proposals / tasks.py review."


# ---- delivery to the task-manager (A5, §3.8, D-188, D-225): short forms, nothing for an empty run

DELIVERY_LINES = 10      # writes named in a delivered summary; the rest is a pointer to `tasks.py runs --show`
POSTPONED_LINES = 5      # postponed proposals named in the continue prompt


def nothing_to_deliver(s: Mapping[str, Any]) -> bool:
    """An empty run: no write and no pending proposal (an applied shadow proposal is a write too; one sent as an
    inbox notice is not pending here: the notice told the task-manager)."""
    return not s["write_count"] and not s["pending"]


def delivery_text(s: Mapping[str, Any]) -> str:
    """A closed run for the task-manager's context, in the compact form of the PostToolUse hook: pending proposals
    first (they need a decision), then the direct writes, no code (D-243: `change_manifest.py` on the branch) and
    no report (the parent has the subagent's own). At most DELIVERY_LINES writes."""
    head = f"Run {s['run']}: {s['write_count']} write(s)"
    if s["pending"]:
        head += f", {len(s['pending'])} proposal(s) pending (tasks.py review ID approve|edit|reject|postpone)"
    lines = [head]
    lines += [f"  P-{p['id']} ({p['status']}): {p['summary']}" for p in s["pending"]]
    if s.get("noticed"):
        lines.append(f"  {noticed_text(s['noticed'])}")
    writes = [r["line"] for rows in s["writes"].values() for r in rows]
    lines += [f"  {w}" for w in writes[:DELIVERY_LINES]]
    if len(writes) > DELIVERY_LINES:
        lines.append(f"  ... {len(writes) - DELIVERY_LINES} more: tasks.py runs --show {s['run']}")
    return "\n".join(lines)


def noticed_text(rows: Sequence[Mapping[str, Any]]) -> str:
    """A run's proposals that reached the task-manager as inbox notices, by id only (their text came there)."""
    return "proposals " + ", ".join(f"P-{p['id']}" for p in rows) + ": sent as notices"


def pending_text(n: int) -> str:
    """What the PostToolUse hook says at a background launch (the new run has nothing yet): the proposals already
    waiting from earlier runs; empty for none."""
    return f"{n} proposal(s) pending from earlier runs: tasks.py proposals" if n else ""


def notice_text(summaries: Sequence[Mapping[str, Any]]) -> str:
    """One line for finished runs not delivered yet (a background subagent, or a foreground one whose hook did not
    fire): how many, the writes, the proposals pending and where to look. Empty for no summary."""
    if not summaries:
        return ""
    ids = ", ".join(str(s["run"]) for s in summaries)
    writes = sum(int(s["write_count"]) for s in summaries)
    pending = sum(len(s["pending"]) for s in summaries)
    line = f"AFClaude: {len(summaries)} subagent run(s) finished ({ids}): {writes} write(s)"
    if pending:
        line += f", {pending} proposal(s) pending, run `tasks.py proposals` then `tasks.py review`"
    sent = [p for s in summaries for p in s.get("noticed", [])]
    if sent:
        line += f", {noticed_text(sent)}"
    return line + f"; details: `tasks.py runs --show {summaries[0]['run']}`"


def postponed_text(rows: Sequence[Mapping[str, Any]]) -> str:
    """The postponed proposals named for the continue prompt (the oldest POSTPONED_LINES), empty for none."""
    if not rows:
        return ""
    named = "; ".join(f"P-{r['id']} {r['summary']}" for r in rows[:POSTPONED_LINES])
    more = len(rows) - POSTPONED_LINES
    return f"{len(rows)} postponed ({named}" + (f"; and {more} more" if more > 0 else "") + ")"
