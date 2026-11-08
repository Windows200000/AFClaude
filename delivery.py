"""Delivery of run summaries and pending proposals to the task-manager's own session (phase 5a A5 part 1,
docs/phase5a_plan.md §3.8; D-188 immediately, D-225 no boilerplate, D-235 fail fast). mypy --strict.

Three paths, one rule (a closed run's summary is delivered once: `agent_runs.delivered_at`, claimed through
the `run.deliver` action, so two paths racing for it show it once):

  agent_hook   the PostToolUse hook on `Agent` (tasks.py run-hook deliver): a foreground subagent's summary as
               additionalContext; a background launch only says how many proposals already wait
  notice       one line on the task-manager's next afclaude MCP / tasks.py result for finished runs not
               delivered yet (a background subagent; a foreground one whose hook did not fire)
  prompt_notice  the same line for the UserPromptSubmit hook (hooks/delegation_reminder.py)

Only the project's own task-manager session receives anything; an empty run (nothing written, nothing
pending) is delivered at close and never says a word. `postponed_insert` is the 4a continue-prompt insert.
Separate sessions' proposals, the inbox notice and the proposal_review wake are part 2.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Any

import actions
import identity
import proposals
import prompts
import store

ACTOR = "runner:delivery"                 # the delivery paths are the harness's, not the agent's (as runner:hook)
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _claim(conn: sqlite3.Connection, ids: list[int]) -> list[int]:
    """The ids this call marked delivered (run.deliver)."""
    r = actions.perform(conn, "run.deliver", {"ids": ids}, actor=ACTOR, via="runner")
    return [int(i) for i in r["delivered"]]


def agent_hook(conn: sqlite3.Connection, payload: object) -> tuple[str, str]:
    """The PostToolUse hook of an `Agent` call -> (context for the task-manager or "", a note for the hook's
    stdout when there is none). A payload without the parent session is an error (D-235); a call from a session
    that is no project's task-manager, a result without an agent id (the call failed to launch one) and a run
    that was never opened (an unhooked or helper agent) deliver nothing and say why in the note."""
    if not isinstance(payload, dict):
        raise ValueError("the hook payload needs session_id and tool_response")
    session = payload.get("session_id")
    if not isinstance(session, str) or not ID_RE.match(session):
        raise ValueError("the hook payload needs session_id and tool_response")
    if payload.get("agent_id"):                          # a subagent's own Agent call: its context is not the task-manager's
        return "", "no delivery: the Agent call is a subagent's own"
    pid = store.manager_project(conn, session)
    if pid is None:
        return "", f"no delivery: {session} is not a task-manager"
    resp = payload.get("tool_response")
    resp = resp if isinstance(resp, dict) else {}
    agent = resp.get("agentId")
    if not isinstance(agent, str) or not ID_RE.match(agent):
        return "", "no delivery: the Agent call has no agentId (it launched nothing)"
    if resp.get("status") == "async_launched":          # background: the run only began, there is nothing to report
        waiting = len(store.list_proposals(conn, project_id=pid, open_only=True))
        return proposals.pending_text(waiting), "no delivery: a background launch, nothing pending" if not waiting else ""
    run = store.subagent_run(conn, session, agent)
    if run is None:
        return "", f"no delivery: no run for subagent {agent}"
    if run["ended_at"] is None or run["summary"] is None:
        return "", f"no delivery: run {run['id']} is still open"
    if run["delivered_at"] is not None or run["id"] not in _claim(conn, [run["id"]]):
        return "", f"no delivery: run {run['id']} has nothing to deliver or was delivered"
    return proposals.delivery_text(run["summary"]), ""


def notice_for_session(conn: sqlite3.Connection, session: str | None) -> str:
    """The one-line notice for the finished runs of this task-manager's project that were not delivered, and
    those runs marked delivered; "" for none (one cheap query when no run waits anywhere)."""
    if not session or not store.any_undelivered_run(conn):
        return ""
    pid = store.manager_project(conn, session)
    if pid is None:
        return ""
    runs = store.undelivered_runs(conn, pid)
    claimed = set(_claim(conn, [int(r["id"]) for r in runs])) if runs else set()
    return proposals.notice_text([r["summary"] for r in runs if r["id"] in claimed and r["summary"] is not None])


def notice(conn: sqlite3.Connection, who: identity.Identity) -> str:
    """notice_for_session for an afclaude MCP / tasks.py caller: only a project's task-manager itself, never an
    agent, a person or a runner."""
    if who.kind != "task-manager" or who.agent_id is not None:
        return ""
    return notice_for_session(conn, who.session)


def postponed_insert(conn: sqlite3.Connection, project_id: int, repo: str | None = None) -> str | None:
    """The 4a continue-prompt insert (prompts/proposals_postponed.md) naming the project's postponed proposals
    (D-188: a postponed one comes back at the next continue); None when it has none."""
    rows = store.list_proposals(conn, project_id=project_id, status="postponed")
    if not rows:
        return None
    system: Mapping[str, Any] = {"postponed": proposals.postponed_text(rows), "tools": prompts.tool_vars(repo)["tools"]}
    return prompts.render_in(conn, "proposals_postponed", None, system)
