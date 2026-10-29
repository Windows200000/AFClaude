#!/usr/bin/env python3
"""
actions.py: the ONE write path into the AFClaude store (dashboard design §3, §4.4-4.6, §5).

tasks.py (CLI), mcp_server.py and, later, the dashboard call

    actions.perform(conn, "task.answer", {"task_id": 7, "answer": "yes"},
                    actor="cli", via="cli", key=None)

and never write through store.py directly. One call is one action:

  - validation (names and types of the parameters, the values via store.py's
    checks, size limits for the dashboard, the autonomous-writer rule);
  - ONE transaction (BEGIN IMMEDIATE; a savepoint inside a caller's transaction)
    holding the change, its audit_log row and the idempotency key;
  - an optional idempotency key: replaying it returns the stored response without
    acting again (a different request under the same key is a Conflict); keys are
    pruned after 7 days;
  - an optional `version` (what the caller saw): a mismatch raises Conflict with
    the current row (the dashboard's 409). Versions are bumped by DB triggers on
    every UPDATE (store.VERSION_TRIGGERS), whoever writes.

A no-op (nothing changed) writes no audit row. Errors: ValueError (bad input),
store.NotFound (LookupError), store.InvalidTransition (wrong state), Conflict
(an InvalidTransition: stale version / reused key / the session stalled again),
Forbidden (a ValueError: an autonomous session may not do this through MCP).
Database errors take the DB error path (store.retrying, design §7.8, D-171): a transient
one (locked, a short I/O error) is retried with backoff; one that persists is a
store.DBError (SchemaMismatch included), and the failed action is recorded in the fallback
file next to the DB (store.report_db_error: actor, action, payload and its hash, time) so it
can be replayed or dropped; the same request failing again alerts the owner, once per episode.

The autonomous-writer rule (§6.2): AFClaude's own sessions (driven_sessions,
data/own_sessions.txt, the manager session, the manager_session of a managed
project, or a caller that says so, e.g. CLAUDE_GUARD_DISABLE=1 in the MCP server)
may add tasks and projects through MCP like anyone (owner decision Q3: no approval
step), but never decide sessions, change rules, settings, prompts or request runs.

Settings (§7.4, D-146) are typed here (SETTINGS): a key without a row (or reset
to null) is its code default (D-169), so an empty table reproduces today's behaviour.
The runners read them through afclaude_config.setting() (effective_setting); the file
tunables of data/afclaude.json, data/dispatcher.json and the pacing keys of
data/user_model.json are imported once (setting.import, actor runner:import).

Types (mypy --strict, D-209): action parameters arrive as untrusted JSON values
(perform() checks only their names), so the action functions take them as Any and
pass them to the checks that validate them (store._text/_enum/_position/_priority,
the setting checks, _check_version) or use them only as lookup keys.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, TypeVar
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402
import afclaude_config  # noqa: E402
import schedule  # noqa: E402
import manager_launch  # noqa: E402
import remote_control  # noqa: E402
import names  # noqa: E402  (pure: the tmux names, phase 3b S2)
import prompts  # noqa: E402

VIAS = ("cli", "mcp", "dashboard", "runner")
# user:<id>: a person signed in to the dashboard (5a's users.id; phase 6a W5's sessions write as it).
# setup: an anonymous browser spending a one-time setup/reset code (phase 6a W7) -- the one write a
# pre-session makes, before there is a user_id to act as.
ACTOR_RE = re.compile(r"^(owner|cli|dispatcher|keepalive|setup|runner(:[a-z0-9_-]{1,32})?"
                      r"|mcp(:[A-Za-z0-9._-]{1,64})?|user:[1-9][0-9]{0,17})$")
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
IDEMPOTENCY_TTL = timedelta(days=7)
OWN_LIST = os.environ.get("AFCLAUDE_OWN_LIST", os.path.join(HERE, "data", "own_sessions.txt"))
PROMPTS_DIR = os.path.join(HERE, "prompts")
PROMPT_MAX = 16 * 1024
# §6.3 size limits, for writes from the dashboard (the CLI and MCP keep their old behaviour)
DASHBOARD_LIMITS = {"title": 200, "name": 200, "answer": 8192, "question": 8192, "description": 16384,
                    "note": 2000, "reason": 2000, "summary": 8192, "path": 1024, "match": 1024,
                    "text": 65536}


class Conflict(store.InvalidTransition):
    """Stale version, reused idempotency key, or the target changed since the caller
    looked (HTTP 409). `current` is the current row, when there is one."""
    def __init__(self, msg: str, current: Any = None) -> None:
        super().__init__(msg)
        self.current = current


class Forbidden(ValueError):
    """The actor may not do this (an autonomous session writing rules/decisions via MCP)."""


class Ctx:
    """Who acts: actor (owner | cli | mcp[:<session>] | dispatcher | keepalive | runner[:<job>] | user:<id>), via
    (cli | mcp | dashboard | runner), the idempotency key, and whether the caller
    knows it is an autonomous AFClaude session."""
    def __init__(self, actor: str, via: str, key: str | None = None, autonomous: bool = False) -> None:
        self.actor, self.via, self.key, self.autonomous = actor, via, key, bool(autonomous)

    @property
    def session(self) -> str | None:
        return self.actor.split(":", 1)[1] if self.actor.startswith("mcp:") else None


class Result:
    """What an action returns to perform(): the caller's result plus the audit data.
    before/after are the target's state (rows or small dicts); only the keys that
    changed go into the audit row. changed=False (or before == after) = no-op."""
    NOISE = ("version", "updated_at")

    def __init__(self, result: Any, target_id: object = None, before: Any = None, after: Any = None,
                 changed: bool | None = None) -> None:
        self.result, self.target_id, self.before, self.after = result, target_id, before, after
        self.changed = changed

    def audit_pair(self) -> tuple[Any, Any]:
        b, a = self.before, self.after
        if isinstance(b, dict) and isinstance(a, dict):
            keys = [k for k in dict.fromkeys(list(b) + list(a)) if k not in self.NOISE and b.get(k) != a.get(k)]
            return {k: b.get(k) for k in keys}, {k: a.get(k) for k in keys}
        return b, a

    def is_noop(self) -> bool:
        if self.changed is not None:
            return not self.changed
        b, a = self.audit_pair()
        return bool(b == a)


ActionFn = Callable[..., Result]          # fn(conn, ctx, **params) -> Result
_A = TypeVar("_A", bound=ActionFn)
_T = TypeVar("_T")


class Spec:
    def __init__(self, name: str, fn: ActionFn, target_type: str, owner_only: bool) -> None:
        self.name, self.fn, self.target_type, self.owner_only = name, fn, target_type, owner_only
        sig = inspect.signature(fn)
        ps = list(sig.parameters.values())[2:]                      # after (conn, ctx)
        self.params = {p.name for p in ps if p.kind is p.POSITIONAL_OR_KEYWORD}
        self.required = {p.name for p in ps if p.kind is p.POSITIONAL_OR_KEYWORD and p.default is p.empty}
        self.extra = next((p.name for p in ps if p.kind is p.VAR_KEYWORD), None)
        self.extra_names: tuple[str, ...] = ()


ACTIONS: dict[str, Spec] = {}


def action(name: str, target_type: str, owner_only: bool = False,
           extra: Optional[Iterable[str]] = None) -> Callable[[_A], _A]:
    """Register fn(conn, ctx, **params) -> Result as action `name`. extra: the names
    a **fields parameter accepts."""
    def deco(fn: _A) -> _A:
        spec = Spec(name, fn, target_type, owner_only)
        spec.extra_names = tuple(extra or ())
        ACTIONS[name] = spec
        return fn
    return deco


# ---------------------------------------------------------------- the write path

def perform(conn: sqlite3.Connection, name: str, params: Mapping[str, Any] | None = None, *, actor: str,
            via: str, key: str | None = None, autonomous: bool = False) -> Any:
    """Run action `name` with `params` (a dict) as one transaction; see the module doc.
    -> the action's result (a JSON-compatible value; a replayed key returns the stored one)."""
    spec = ACTIONS.get(name)
    if spec is None:
        raise ValueError(f"unknown action {name!r}")
    if via not in VIAS:
        raise ValueError(f"via must be one of {'|'.join(VIAS)}, got {via!r}")
    if not isinstance(actor, str) or not ACTOR_RE.match(actor):
        raise ValueError(f"bad actor {actor!r}")
    if key is not None and (not isinstance(key, str) or not KEY_RE.match(key)):
        raise ValueError("idempotency key must be 8-128 characters of [A-Za-z0-9_.:-]")
    params = dict(params or {})
    _check_params(spec, params)
    if via == "dashboard":
        _check_limits(params)
    ctx = Ctx(actor, via, key, autonomous)
    request_sha = hashlib.sha256(json.dumps({"action": name, "params": params}, sort_keys=True,
                                            ensure_ascii=False, default=str).encode()).hexdigest()
    req: Mapping[str, Any] = params
    try:   # the DB error path (§7.8): transient errors retried, persistent ones escalated
        return store.retrying(lambda: _perform(conn, spec, name, req, ctx, request_sha), conn)
    except store.DBError as e:
        if e.escalation is None:
            store.report_db_error(e, actor=actor, action=name, params=req, payload_sha=request_sha,
                                  db_path=_db_path(conn))
        raise


def _db_path(conn: sqlite3.Connection) -> str | None:
    try:
        return store._db_file(conn)
    except sqlite3.Error:
        return None


def _perform(conn: sqlite3.Connection, spec: Spec, name: str, params: Mapping[str, Any], ctx: Ctx,
             request_sha: str) -> Any:
    """One try of perform(): the action, its audit row and idempotency key in one transaction."""
    actor, via, key = ctx.actor, ctx.via, ctx.key
    with store.transaction(conn):
        if key is not None:
            hit = store.get_idempotency(conn, key)
            if hit is not None:
                if (hit["action"], hit["actor"], hit["request_sha256"]) != (name, actor, request_sha):
                    raise Conflict(f"idempotency key {key!r} was already used for a different request "
                                   f"({hit['action']} by {hit['actor']})")
                return json.loads(hit["response"]) if hit["response"] is not None else None
        if spec.owner_only and is_autonomous(conn, ctx):
            raise Forbidden(f"{name} is not allowed for an autonomous AFClaude session via {via} "
                            "(the owner decides sessions, rules, settings and prompts)")
        res = spec.fn(conn, ctx, **params)
        if not res.is_noop():
            before, after = res.audit_pair()
            store.add_audit(conn, actor, via, name, spec.target_type, res.target_id, before, after, key)
        if key is not None:
            store.prune_idempotency(conn, store._utcnow() - IDEMPOTENCY_TTL)
            store.put_idempotency(conn, key, actor, name, request_sha, res.result)
        return res.result


def _check_params(spec: Spec, params: Mapping[str, Any]) -> None:
    unknown = set(params) - spec.params - (set(spec.extra_names) if spec.extra else set())
    if unknown:
        raise ValueError(f"{spec.name}: unknown parameter(s) {sorted(unknown)}")
    missing = spec.required - set(params)
    if missing:
        raise ValueError(f"{spec.name}: missing parameter(s) {sorted(missing)}")


def _check_limits(params: Mapping[str, Any]) -> None:
    for k, v in params.items():
        if isinstance(v, dict):
            _check_limits(v)
        elif isinstance(v, str) and k in DASHBOARD_LIMITS and len(v) > DASHBOARD_LIMITS[k]:
            raise ValueError(f"{k} is too long ({len(v)} > {DASHBOARD_LIMITS[k]} characters)")


def _check_version(row: Mapping[str, Any] | None, version: object, what: str) -> None:
    """row: the current state (None = never saved: version 0)."""
    if version is None:
        return
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"version must be an integer, got {version!r}")
    have = row.get("version", 0) if row is not None else 0
    if have != version:
        raise Conflict(f"{what} changed since it was read (version {have}, not {version}); reload and retry",
                       current=row)


# ---------------------------------------------------------------- autonomous writers

def _own_list() -> set[str]:
    """The own-session list: the DB (driven_sessions kind 'own', D-161) for the live
    data/own_sessions.txt, else that side file."""
    import runner_state
    return runner_state.own_sessions(OWN_LIST)


def autonomous_sessions(conn: sqlite3.Connection) -> set[str]:
    """Session ids AFClaude drives: driven_sessions (incl. the own sessions), the
    manager session (data/afclaude.json) and every managed project's manager_session. A 'stall'
    row is the owner's own session AFClaude only continued (phase 3b): its id stays the owner's
    (a continue AFClaude launches runs with CLAUDE_GUARD_DISABLE=1, so that one is autonomous anyway)."""
    ids = {r[0] for r in conn.execute("SELECT session_id FROM driven_sessions WHERE kind<>'stall'")}
    ids |= {r[0] for r in conn.execute("SELECT manager_session FROM projects WHERE manager_session IS NOT NULL")}
    ids |= _own_list()
    ids.add(afclaude_config.manager_session())
    return ids


def is_autonomous(conn: sqlite3.Connection, ctx: Ctx) -> bool:
    """Only MCP writes are checked (the CLI and the dashboard are the owner's)."""
    if ctx.via != "mcp":
        return False
    return ctx.autonomous or (ctx.session is not None and ctx.session in autonomous_sessions(conn))


# ---------------------------------------------------------------- tasks

def _task(conn: sqlite3.Connection, task_id: Any) -> store.Row:
    return store._task_row(conn, store._position(task_id, "task id"))


def _task_change(conn: sqlite3.Connection, task_id: Any, version: Any, fn: Callable[[], store.Row]) -> Result:
    before = _task(conn, task_id)
    _check_version(before, version, f"task #{task_id}")
    after = fn()
    return Result(after, task_id, before, after)


@action("task.add", "task")
def task_add(conn: sqlite3.Connection, ctx: Ctx, title: Any, description: Any = None, project: Any = None,
             priority: Any = store.DEFAULT_PRIORITY, kind: Any = "task", created_by_session: Any = None) -> Result:
    t = store.add_task(conn, title, description, project, priority, kind, created_by_session)
    return Result(t, t["id"], None, t)


@action("task.ask", "task")
def task_ask(conn: sqlite3.Connection, ctx: Ctx, question: Any, project: Any = None, created_by_session: Any = None,
             title: Any = None) -> Result:
    """A manager question: a task of kind 'question', born blocked (§4.6)."""
    t = store.ask_question(conn, question, project, created_by_session or ctx.session, title)
    return Result(t, t["id"], None, t)


@action("task.edit", "task", extra=store.TASK_EDITABLE)
def task_edit(conn: sqlite3.Connection, ctx: Ctx, task_id: Any, version: Any = None, **fields: Any) -> Result:
    """title / description / project (None = no project) / priority / kind."""
    return _task_change(conn, task_id, version, lambda: store.update_task(conn, task_id, **fields))


@action("task.priority", "task")
def task_priority(conn: sqlite3.Connection, ctx: Ctx, task_id: Any, priority: Any, version: Any = None) -> Result:
    return _task_change(conn, task_id, version, lambda: store.set_stage_priority(conn, task_id, priority))


@action("task.move", "task")
def task_move(conn: sqlite3.Connection, ctx: Ctx, task_id: Any, stage: Any, version: Any = None) -> Result:
    return _task_change(conn, task_id, version, lambda: store.move_stage(conn, task_id, stage))


@action("task.block", "task")
def task_block(conn: sqlite3.Connection, ctx: Ctx, task_id: Any, question: Any, version: Any = None) -> Result:
    return _task_change(conn, task_id, version, lambda: store.block_task(conn, task_id, question))


@action("task.answer", "task")
def task_answer(conn: sqlite3.Connection, ctx: Ctx, task_id: Any, answer: Any, version: Any = None) -> Result:
    """blocked -> pending (a question: -> done). From the dashboard, the same answer
    again right after it was given is a no-op success (a double submit), not an error."""
    t = _task(conn, task_id)
    if (ctx.via == "dashboard" and t["status"] != "blocked" and t["answer"] is not None
            and isinstance(answer, str) and answer.strip() == t["answer"]
            and store.task_events(conn, task_id)[-1]["event"] == "answered"):
        return Result(t, task_id, t, t, changed=False)
    return _task_change(conn, task_id, version, lambda: store.answer_task(conn, task_id, answer))


@action("task.start", "task")
def task_start(conn: sqlite3.Connection, ctx: Ctx, task_id: Any, session: Any = None, version: Any = None) -> Result:
    return _task_change(conn, task_id, version, lambda: store.start_task(conn, task_id, session))


@action("task.finish", "task")
def task_finish(conn: sqlite3.Connection, ctx: Ctx, task_id: Any, summary: Any = None, version: Any = None) -> Result:
    return _task_change(conn, task_id, version, lambda: store.finish_task(conn, task_id, summary))


@action("task.cancel", "task")
def task_cancel(conn: sqlite3.Connection, ctx: Ctx, task_id: Any, reason: Any = None, version: Any = None) -> Result:
    return _task_change(conn, task_id, version, lambda: store.cancel_task(conn, task_id, reason))


@action("task.reopen", "task")
def task_reopen(conn: sqlite3.Connection, ctx: Ctx, task_id: Any, reason: Any = None, version: Any = None) -> Result:
    return _task_change(conn, task_id, version, lambda: store.reopen_task(conn, task_id, reason))


@action("task.update", "task")
def task_update(conn: sqlite3.Connection, ctx: Ctx, task_id: Any, fields: Any = None, stage: Any = None,
                status: Any = None, note: Any = None, session: Any = None, version: Any = None) -> Result:
    """Several changes to one task at once (the MCP tool afclaude_update_task): fields
    (as task.edit), then stage (position in its project), then status: done (note =
    summary), cancelled (note = reason), blocked (note = the question), in_progress
    (session), pending (reopen; a blocked task must be answered instead)."""
    if fields is not None and not isinstance(fields, dict):
        raise ValueError("fields must be an object")

    def apply() -> store.Row:
        if fields:
            store.update_task(conn, task_id, **fields)
        if stage is not None:
            store.move_stage(conn, task_id, stage)
        if status is not None:
            cur = store._task_row(conn, task_id)
            if status == "done":
                store.finish_task(conn, task_id, note)
            elif status == "cancelled":
                store.cancel_task(conn, task_id, note)
            elif status == "blocked":
                store.block_task(conn, task_id, note)
            elif status == "in_progress":
                store.start_task(conn, task_id, session)
            elif status == "pending":
                if cur["status"] == "blocked":
                    raise store.InvalidTransition(f"task #{task_id} is blocked; answer it with afclaude_answer_task")
                store.reopen_task(conn, task_id, note)
            else:
                store._enum(status, store.TASK_STATUSES, "status")
        return store._task_row(conn, task_id)
    return _task_change(conn, task_id, version, apply)


# ---------------------------------------------------------------- projects

def _project(conn: sqlite3.Connection, project: Any) -> store.Row:
    return store._project_row(conn, project)


@action("project.add", "project")
def project_add(conn: sqlite3.Connection, ctx: Ctx, name: Any, description: Any = None, path: Any = None,
                rank: Any = None) -> Result:
    p = store.add_project(conn, name, description, path, rank)
    return Result(p, p["id"], None, p)


@action("project.edit", "project", extra=store.PROJECT_EDITABLE)
def project_edit(conn: sqlite3.Connection, ctx: Ctx, project: Any, version: Any = None, **fields: Any) -> Result:
    """name / description / path / manager_session (None = unmanaged)."""
    before = _project(conn, project)
    _check_version(before, version, f"project {before['name']!r}")
    after = store.update_project(conn, before["id"], **fields)
    return Result(after, after["id"], before, after)


@action("project.move", "project")
def project_move(conn: sqlite3.Connection, ctx: Ctx, project: Any, rank: Any, version: Any = None) -> Result:
    before = _project(conn, project)
    _check_version(before, version, f"project {before['name']!r}")
    after = store.move_project(conn, before["id"], rank)
    return Result(after, after["id"], {"rank": before["rank"]}, {"rank": after["rank"]})


@action("project.priority", "project")
def project_priority(conn: sqlite3.Connection, ctx: Ctx, project: Any, priority: Any) -> Result:
    """Every open stage of the project -> priority (naturally idempotent)."""
    p = _project(conn, project)
    r = store.set_project_priority(conn, p["id"], priority)
    return Result(r, p["id"], {"changed": []}, {"priority": r["priority"], "changed": r["changed"]},
                  changed=bool(r["changed"]))


@action("project.manager_launch", "project", owner_only=True)
def project_manager_launch(conn: sqlite3.Connection, ctx: Ctx, project: Any, model: Any = None, effort: Any = None,
                           version: Any = None) -> Result:
    """The model / effort the project's task-manager session starts or resumes with (D-260,
    manager_launch.py). model / effort: a known value sets it, "default" (or "") clears it to the
    code default, None leaves it. Owner-only like every setting. It applies at the session's next
    start or resume; a running session keeps its model until then. -> the project row + "launch"."""
    before = _project(conn, project)
    _check_version(before, version, f"project {before['name']!r}")
    fields: dict[str, str | None] = {}
    for key, raw, check in (("manager_model", model, manager_launch.check_model),
                            ("manager_effort", effort, manager_launch.check_effort)):
        if raw is None:
            continue
        reset = isinstance(raw, str) and raw.strip().lower() in manager_launch.RESET
        fields[key] = None if reset else check(raw)
    if not fields:
        raise ValueError("project.manager_launch needs model and/or effort")
    after = store.set_project_launch(conn, before["id"], **fields)
    return Result(dict(after, launch=manager_launch.view(after)), after["id"],
                  {k: before[k] for k in fields}, {k: after[k] for k in fields})


# ---------------------------------------------------------------- stalled sessions and rules

@action("session.decide", "session", owner_only=True)
def session_decide(conn: sqlite3.Connection, ctx: Ctx, session_id: Any, decision: Any, note: Any = None,
                   stall_ref: Any = None, version: Any = None) -> Result:
    """continue / ignore for the session's CURRENT stall. stall_ref (what the caller
    showed): if the session has stalled again since, Conflict (§5)."""
    s = store.get_session(conn, session_id)
    if s is None:
        raise store.NotFound(f"unknown session {session_id}")
    if stall_ref is not None and stall_ref != store._stall_ref(s):
        raise Conflict(f"session {session_id[:8]} has stalled again since (decide the new stall)",
                       current={"session_id": session_id, "stall_ref": store._stall_ref(s)})
    before = store.get_decision(conn, session_id)
    _check_version(before, version, f"the decision for {session_id[:8]}")
    after = store.decide_session(conn, session_id, decision, note)
    return Result(after, session_id, before, after)


@action("session.clear", "session", owner_only=True)
def session_clear(conn: sqlite3.Connection, ctx: Ctx, session_id: Any) -> Result:
    before = store.get_decision(conn, session_id)
    cleared = store.clear_decision(conn, session_id)
    return Result({"session_id": session_id, "cleared": cleared}, session_id, before, None, changed=cleared)


@action("rule.add", "rule", owner_only=True)
def rule_add(conn: sqlite3.Connection, ctx: Ctx, scope: Any, match: Any, decision: Any, note: Any = None) -> Result:
    """Standing rule; the same scope+match again replaces the old one (store.add_rule)."""
    old: store.Row | None = None
    if scope in store.RULE_SCOPES and isinstance(match, str) and match.strip():
        m = store._norm_match(scope, match)
        r = conn.execute("SELECT * FROM standing_rules WHERE scope=? AND match=?", (scope, m)).fetchone()
        old = dict(r) if r else None
    r = store.add_rule(conn, scope, match, decision, note)
    changed = old is None or (old["decision"], old["note"]) != (r["decision"], r["note"])
    return Result(r, r["id"], old, r, changed=changed)


@action("rule.remove", "rule", owner_only=True)
def rule_remove(conn: sqlite3.Connection, ctx: Ctx, rule_id: Any, version: Any = None) -> Result:
    r = conn.execute("SELECT * FROM standing_rules WHERE id=?", (store._position(rule_id, "rule id"),)).fetchone()
    if r is None:
        raise store.NotFound(f"no rule #{rule_id}")
    _check_version(dict(r), version, f"rule #{rule_id}")
    store.remove_rule(conn, rule_id)
    return Result({"removed": rule_id}, rule_id, dict(r), None, changed=True)


# ---------------------------------------------------------------- settings (§7.4, D-146, D-075)

DAYS = schedule.DAYS
GROUP_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
WINDOW_MODES = ("linked", "individual", "week", "link")
SESSION_HOURS = afclaude_config.SESSION_LENGTH.total_seconds() / 3600   # 5: one session-limit window
SECTIONS = ("schedule", "budget", "automation", "storage", "auth", "host")


SettingCheck = Callable[[Any, Optional[sqlite3.Connection]], Any]   # (value, conn) -> normalized value


def _number(lo: float, hi: float) -> Callable[[Any, Optional[sqlite3.Connection]], float]:
    def check(v: Any, conn: sqlite3.Connection | None = None) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"must be a number in {lo:g}..{hi:g}, got {v!r}")
        return float(v)
    return check


def _bool(v: Any, conn: sqlite3.Connection | None = None) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"must be true or false, got {v!r}")
    return v


def _choice(options: tuple[str, ...]) -> Callable[[Any, Optional[sqlite3.Connection]], str]:
    def check(v: Any, conn: sqlite3.Connection | None = None) -> str:
        s = v.strip().lower() if isinstance(v, str) else None
        if s is None or s not in options:
            raise ValueError(f"must be one of {'|'.join(options)}, got {v!r}")
        return s
    return check


def _tz(v: Any, conn: sqlite3.Connection | None = None) -> str:
    try:
        ZoneInfo(v)
    except Exception:
        raise ValueError(f"unknown time zone {v!r}") from None
    if not isinstance(v, str):      # ZoneInfo only accepts str keys; this states it for the type checker
        raise ValueError(f"unknown time zone {v!r}")
    return v


def _hhmm(v: Any) -> str:
    m = re.match(r"^(\d{1,2}):(\d{2})$", v) if isinstance(v, str) else None
    if not m or int(m[1]) > 23 or m[2] not in ("00", "30"):
        raise ValueError(f"start must be HH:MM on the 30-minute grid (e.g. 23:00, 23:30), got {v!r}")
    return f"{int(m[1]):02d}:{m[2]}"


def _hours_or_auto(lo: float, hi: float) -> Callable[[Any, Optional[sqlite3.Connection]], str | float]:
    """"auto" or a number in lo..hi (last_mile_hours: hours; reserve_threshold: weekly %)."""
    num = _number(lo, hi)

    def check(v: Any, conn: sqlite3.Connection | None = None) -> str | float:
        if isinstance(v, str) and v.strip().lower() == "auto":
            return "auto"
        try:
            return num(v, conn)
        except ValueError:
            raise ValueError(f'must be "auto" or a number in {lo:g}..{hi:g}, got {v!r}') from None
    return check


REASON_MAX = 2000


def _reason(v: Any, conn: sqlite3.Connection | None = None) -> str:
    """Free text (one line), at most REASON_MAX characters; "" = none."""
    if not isinstance(v, str):
        raise ValueError(f"must be text, got {v!r}")
    s = " ".join(v.split())
    if len(s) > REASON_MAX:
        raise ValueError(f"at most {REASON_MAX} characters, got {len(s)}")
    return s


def _window_days(v: Any, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Validate + normalize {mon..sun: {start, n, group} | null}: start on the 30-min
    grid, 1 <= n whole session windows (D-148) with n x session_hours <= 24 h, identical
    windows within a link group, no overlap between any two days' windows (the week
    wraps around)."""
    if not isinstance(v, dict) or set(v) != set(DAYS):
        raise ValueError(f"window_days needs exactly the keys {', '.join(DAYS)}")
    sh = get_setting(conn, "session_hours") if conn is not None else SESSION_HOURS
    out: dict[str, Any] = {}
    groups: dict[str, tuple[str, str, int]] = {}
    for d in DAYS:
        w = v[d]
        if w is None:
            out[d] = None
            continue
        if not isinstance(w, dict) or set(w) != {"start", "n", "group"}:
            raise ValueError(f"{d}: a window is {{start, n, group}} or null")
        n = w["n"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError(f"{d}: n must be an integer >= 1, got {n!r}")
        if n * sh > 24:
            raise ValueError(f"{d}: {n} x {sh:g} h is longer than a day")
        if not isinstance(w["group"], str) or not GROUP_RE.match(w["group"]):
            raise ValueError(f"{d}: group must be 1-32 characters of [A-Za-z0-9_-]")
        out[d] = {"start": _hhmm(w["start"]), "n": n, "group": w["group"]}
        g = groups.setdefault(w["group"], (d, out[d]["start"], n))
        if (g[1], g[2]) != (out[d]["start"], n):
            raise ValueError(f"{d} and {g[0]} are linked (group {w['group']}) but have different windows")
    schedule.check_overlap(out, sh)
    return out


def _log_retention_overrides(v: Any, conn: sqlite3.Connection | None = None) -> dict[str, float]:
    """{component: days}, overriding log_retention_days for that one app_log component (D-130:
    this narrows only app_log's own retention, never telemetry or usage data); 1..3650 days,
    components from LOG_COMPONENTS (checked by name, not import order: LOG_COMPONENTS is defined
    later in this module, but by the time a setting is actually checked the module is fully
    loaded). A component not listed here uses log_retention_days."""
    if not isinstance(v, dict):
        raise ValueError("must be an object {component: days}")
    out: dict[str, float] = {}
    for k, days in v.items():
        if not isinstance(k, str) or k not in LOG_COMPONENTS:
            raise ValueError(f"unknown log component {k!r} (known: {', '.join(LOG_COMPONENTS)})")
        if isinstance(days, bool) or not isinstance(days, (int, float)) or not 1 <= days <= 3650:
            raise ValueError(f"{k}: must be a number of days in 1..3650, got {days!r}")
        out[k] = float(days)
    return out


LOGIN_RATE_FIELDS = {"account_failures": (1, 100), "ip_failures": (1, 1000), "window_min": (1, 1440)}


def _login_rate(v: Any, conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """auth_login_rate (design §9.5): {account_failures, ip_failures, window_min}, whole numbers."""
    if not isinstance(v, dict) or set(v) != set(LOGIN_RATE_FIELDS):
        raise ValueError(f"must be an object with exactly {', '.join(LOGIN_RATE_FIELDS)}, got {v!r}")
    out: dict[str, int] = {}
    for k, (lo, hi) in LOGIN_RATE_FIELDS.items():
        n = v[k]
        if isinstance(n, bool) or not isinstance(n, int) or not lo <= n <= hi:
            raise ValueError(f"{k}: must be a whole number in {lo}..{hi}, got {n!r}")
        out[k] = n
    return out


def _whole(lo: int, hi: int) -> Callable[[Any, Optional[sqlite3.Connection]], int]:
    def check(v: Any, conn: sqlite3.Connection | None = None) -> int:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or not lo <= v <= hi:
            raise ValueError(f"must be a whole number in {lo}..{hi}, got {v!r}")
        return int(v)
    return check


class Setting:
    """One registry entry (§7.4): the code default (D-169: shipped with the code, never read
    from a file), the check (validates and normalizes a value; a ValueError says why), a
    one-line explanation (D-075), its section, the type the UI renders, whether it belongs to
    the few important settings shown up front (D-075), and the range / choices of a number or
    an enum. Every setting is owner-only to change (setting.set, actions with owner_only)."""
    def __init__(self, default: Any, check: SettingCheck, doc: str, *, section: str, kind: str,
                 important: bool = False, bounds: tuple[float, float] | None = None,
                 choices: tuple[str, ...] | None = None) -> None:
        if section not in SECTIONS:
            raise ValueError(f"unknown section {section!r}")
        self._default = default
        self.check, self.doc, self.section, self.kind = check, doc, section, kind
        self.important, self.bounds, self.choices = important, bounds, choices

    def default(self) -> Any:
        """The code default (a fresh copy: callers may change it)."""
        return copy.deepcopy(self._default)

    def describe(self) -> dict[str, Any]:
        """The static part for the UI (§8: the settings page is rendered from the registry)."""
        d: dict[str, Any] = {"section": self.section, "type": self.kind, "important": self.important,
                             "doc": self.doc}
        if self.bounds is not None:
            d["min"], d["max"] = self.bounds
        if self.choices is not None:
            d["choices"] = list(self.choices)
        return d


def _num(default: float, lo: float, hi: float, doc: str, section: str, important: bool = False) -> Setting:
    return Setting(float(default), _number(lo, hi), doc, section=section, kind="number", important=important,
                   bounds=(lo, hi))


def _auto_or_num(lo: float, hi: float, doc: str, section: str, important: bool = False) -> Setting:
    return Setting("auto", _hours_or_auto(lo, hi), doc, section=section, kind="auto|number",
                   important=important, bounds=(lo, hi))


def _int(default: int, lo: int, hi: int, doc: str, section: str) -> Setting:
    return Setting(default, _whole(lo, hi), doc, section=section, kind="number", bounds=(lo, hi))


def _flag(default: bool, doc: str, section: str, important: bool = False) -> Setting:
    return Setting(default, _bool, doc, section=section, kind="bool", important=important)


def _remote_control(category: str) -> Setting:
    """D-269: Remote Control on/off for one session category (remote_control.py; its key is
    remote_control.setting_key(category), test_remote_control checks every category has one)."""
    return _flag(remote_control.DEFAULT, f"Remote Control (claude --remote-control, D-269) for "
                 f"{remote_control.CATEGORIES[category]}; applies at the session's next start or resume",
                 "automation")


USAGE_MODELS = ("pacing", "linear")
# D-229's plan names: interpretation, since Anthropic's own naming (and the owner's exact seat type,
# standard vs premium) is not settled by the decision; 5d's X3 is free to widen this list.
# CLAUDE_PLAN_UNSET is not a plan: it is claude_plan's code default (D-146/D-169), so the wizard has
# something to ask about instead of a guessed real one (D-229: AFClaude asks at first startup). A
# step that sets the owner's plan must require one of the other choices; nothing reads this setting
# today, but anything that does must treat CLAUDE_PLAN_UNSET as "not answered yet", never as a plan.
CLAUDE_PLAN_UNSET = "unset"
CLAUDE_PLANS = (CLAUDE_PLAN_UNSET, "free", "pro", "max_5x", "max_20x", "team", "enterprise")

# Everything the dashboard can change (D-146): the runners read these (afclaude_config.setting()),
# DB value > code default; data/afclaude.json keeps only machine identity. Names are flat (§7.4).
SETTINGS: dict[str, Setting] = {
    # --- schedule (§6, D-148): windows are whole session windows; the runners read them through schedule.py
    "window_days": Setting({d: {"start": "23:00", "n": 2, "group": "weekly"} for d in DAYS}, _window_days,
                           "Automation window per weekday {mon..sun: {start, n, group} | null}: AFClaude works "
                           "from start for n whole session windows (default every night 23:00 x 2 = until "
                           "09:00); null = no window that night; linked days change together",
                           section="schedule", kind="window_days", important=True),
    "window_tz": Setting("Europe/Berlin", _tz, "Time zone of the window start times (set from the browser "
                                               "on the first login, D-148)", section="schedule", kind="tz"),
    "session_hours": _num(SESSION_HOURS, 1, 24, "Length of one Claude session-limit window in hours (a "
                          "window is n of these)", "schedule"),
    # --- budget (pacing.py night gate, keepalive.py linear rule)
    "usage_model": Setting("pacing", _choice(USAGE_MODELS),
                           'Weekly budget model: "pacing" (forecast-driven night gate) or "linear" (the '
                           'original projection rule, also pacing\'s fallback on an error)',
                           section="budget", kind="enum", choices=USAGE_MODELS),
    "reserve_threshold": _auto_or_num(50, 99, 'Night gate: a night runs a full session window only if the '
                                      'week is then predicted to end at or below this weekly %; "auto" = one '
                                      'session window left (100 - the measured full-session cost, D-141)',
                                      "budget", important=True),
    "last_mile_hours": _auto_or_num(0, 168, 'Last stretch before the weekly reset that fills the week to '
                                    '100%: "auto" = min(ceil(session windows of quota left), 2) x '
                                    'session_hours, or hours (0 = off, D-015/D-020)', "budget", important=True),
    "stage_share": _num(45, 1, 100, "Stage ETAs (D-264, 25.10. review): the % of AFClaude capacity that "
                        "actually goes into roadmap stages rather than live fixes/overhead; a review-tuned "
                        "figure (stage_eta.py --share measures a trailing-window estimate for the review)",
                        "budget"),
    "pacing_idle_min": _num(60, 0, 1440, "Yield to the user: after their last activity AFClaude waits this "
                            "many minutes before it starts (a hold postpones to then)", "budget"),
    "pacing_min_gap": _num(1, 0, 50, "Night gate: no run if the budget for it is at most this many weekly %",
                           "budget"),
    "pacing_session_cap": _num(85, 1, 100, "Session guard: no night start while the current session window "
                               "is at or above this % (the last stretch uses 100%)", "budget"),
    "pacing_last_mile_yield": _flag(False, "Also yield to an active user in the last stretch before the "
                                    "weekly reset (off: the last stretch runs regardless of the owner's "
                                    "activity, D-259)", "budget"),
    "projection_threshold": _num(90, 1, 100, 'Linear model: continue while the end of the week is projected '
                                 'below this weekly %', "budget"),
    "cutoff_after_window_hours": _num(2, 0, 24, "Linear model: a weekly reset this many hours after the "
                                      "window end still continues (11:00 after a 09:00 end)", "budget"),
    # --- the task-manager's session stop and the fill-up run (D-014, D-212)
    "session_stop_pct": _num(95, 50, 99, "At night the task-manager stops cleanly once the session window is at "
                             "this % (D-014; the manager prompt reads it); a fill-up run uses the rest",
                             "budget"),
    "fillup_enabled": _flag(True, "Fill-up run (D-212): after a run ended early, continue the task-manager "
                            "once more shortly before the session reset to use the rest of the session window",
                            "budget"),
    "fillup_factor": _num(1.10, 1.0, 3.0, "Fill-up start = session reset - (100 - session %) / measured fill "
                          "rate x this factor (D-212)", "budget"),
    "run_start_factor": _num(1.5, 0.0, 5.0, "Run timing (D-274): a night run starts at its session window's "
                             "end - the session % it can use / measured fill rate x this factor, so it ends at "
                             "the window's end (0 = at the session-window start, as before)", "budget"),
    "window_ping": _flag(True, "Window-start ping (D-277): at a session-window start whose run starts later or is "
                         "held, while no Claude session window is open, one tiny context-free Haiku request opens "
                         "it, so the session windows line up with the schedule", "budget"),
    # --- automation
    # --- compaction of long-running sessions (D-144, D-191, D-214; design §5.5)
    "compaction_enabled": _flag(True, "Compaction (D-144/D-233/D-238/D-307): the watcher compacts the "
                                "task-manager (save a hand-off, /compact, inject the hand-off) when a run ends, "
                                "after 50 min idle before its prompt cache expires, and during a run at an idle "
                                "point, if its context is at or above compaction_threshold_tokens", "automation"),
    "compaction_threshold_tokens": _int(150_000, 50_000, 1_000_000, "Compaction threshold (D-307): the watcher "
                                        "compacts the task-manager only once its context is at or above this many "
                                        "tokens, and during a run as soon as it passes it (replaced the computed "
                                        "break-even of D-238, which the quickview still shows)", "automation"),
    "automation_paused": _flag(False, "Every runner holds new starts and continues (running sessions go "
                               "on). A runner sets it itself on any failure or timeout (D-234/D-235, "
                               "failstop.py); the owner clears it: `tasks.py automation resume`",
                               "automation", important=True),
    "automation_paused_reason": Setting("", _reason, "Why automation is paused (set with automation.set; a "
                                        "runner's failure says what failed); cleared on resume",
                                        section="automation", kind="text"),
    "ui_chat_reset_hours": _num(5, 1, 168, "Dashboard message box to the task-manager (D-178): it starts "
                              "empty again after this many hours without a new message or answer", "automation"),
    "preview_publish_enabled": _flag(False, "Owner preview build of the dashboard UI (D-211): "
                                     "tools/build_preview.py (host-only, needs the pinned Node, D-292) builds and "
                                     "publishes dashboard/web's dist-preview/ next to the quickview export, and "
                                     "export_quickview.py's preview_block reports it in status.json; off = no "
                                     "build, no publish, no new status.json field (default, until ovm3 has the "
                                     "matching /private proxy, D-253's pattern)", "automation"),
    "stall_take_over_idle": _flag(True, "Dispatcher: an idle interactive process (e.g. an open terminal) "
                                  "that holds an approved stalled session is stopped so it can be continued",
                                  "automation"),
    "stall_verify_minutes": _num(15, 1, 240, "Dispatcher: alert if a continued session shows no reply within "
                                 "this many minutes", "automation"),
    "cleanup_finished_grace_minutes": _num(10, 0, 1440, "Dispatcher: the tmux session of a finished task "
                                           "(done/blocked/cancelled) is closed after it was idle this long",
                                           "automation"),
    "cleanup_idle_hours": _num(2, 0.25, 168, "Dispatcher: a tmux session it started is closed after it was "
                               "idle this long after its last turn", "automation"),
    # --- Remote Control per session category (D-269, remote_control.py): applied when a session is
    # started or resumed (ka_resume.sh --no-remote-control when off); default on = the launch before D-269
    "remote_control_task_manager": _remote_control(remote_control.TASK_MANAGER),
    "remote_control_review": _remote_control(remote_control.REVIEW),
    "remote_control_continue": _remote_control(remote_control.CONTINUE),
    "remote_control_test": _remote_control(remote_control.TEST),
    # --- app_log retention (phase 2d part 2, D-161: everything in the DB; D-130: never drop
    # usage/telemetry data -- this prunes only the app_log lines). A daily cron run of
    # `python3 store.py prune-logs` deletes rows older than the retention through actions.log_prune
    # (audited, actor runner:retention); the no-delete trigger only opens for that one transaction
    # (store.app_log_prune_gate). Log FILES are untouched (retired separately, later).
    "log_retention_days": _num(30, 1, 3650, "How many days of app_log lines (keepalive, dispatcher, "
                               "sampler, watchdog, host_exec, at_shim, runner) to keep before the daily prune "
                               "deletes them; a component in log_retention_overrides uses that instead",
                               "storage"),
    "log_retention_overrides": Setting({}, _log_retention_overrides, 'Per-component days overriding '
                               'log_retention_days (e.g. {"host_exec": 14} for the noisiest log); a '
                               'component not listed here uses log_retention_days', section="storage",
                               kind="component_days"),
    # --- the resident runner (phase 3a, runner.py; docs/phase3a_plan.md §3.2-3.5). Each job's outer
    # timeout is the setting RUNNER_TIMEOUT + <job> (minutes; D-234: a run past it is killed, recorded
    # as a timeout and fails), defaulting to its runner.JOBS entry (a test keeps both equal). A tick's
    # default is above its handler's own worst-case inner timeouts + 5 min (runner_jobs.TICK_INNER).
    "runner_timeout_stall_scan": _num(2, 0.5, 720, "Runner: minutes a stall scan may run before it is killed "
                                      "and fails (D-234)", "automation"),
    "runner_timeout_sampler": _num(20, 0.5, 720, "Runner: minutes a usage-sampler run may take before it is "
                                   "killed and fails (D-234)", "automation"),
    "runner_timeout_dispatcher": _num(20, 0.5, 720, "Runner: minutes a dispatcher pass may take before it is "
                                      "killed and fails (D-234)", "automation"),
    "runner_timeout_quickview": _num(2, 0.5, 720, "Runner: minutes a quickview export may take before it is "
                                     "killed (an alert, no pause: D-239)", "automation"),
    "runner_timeout_docs_import": _num(5, 0.5, 720, "Runner: minutes a docs import may take before it is killed "
                                       "and fails (D-234)", "automation"),
    "runner_timeout_prune_logs": _num(10, 0.5, 720, "Runner: minutes the daily log prune may take before it is "
                                      "killed and fails (D-234)", "automation"),
    "runner_timeout_prune_runs": _num(10, 0.5, 720, "Runner: minutes the daily prune of its run records may "
                                      "take before it is killed and fails (D-234)", "automation"),
    "runner_timeout_usage_review": _num(30, 0.5, 720, "Runner: minutes a usage-review run may take before it "
                                        "is killed and fails (D-234)", "automation"),
    # an at-job's payload `timeout` (minutes, `at --timeout`) overrides it for that job (phase 3a S4)
    "runner_timeout_at": _num(30, 0.5, 720, "Runner: minutes an at-job (scheduled_jobs: the sampler's "
                              "weekly-reset samples, one-off reviews, the review chain's steps) may run before "
                              "it is killed and fails (D-234), unless the job sets its own", "automation"),
    # the lower bound is the sum of the start's own inner timeouts + 5 min (runner_jobs.window_start_inner;
    # test_runner checks it), so an inner timeout, with its own clear error, always fires first
    "runner_timeout_window_start": _num(70, 70, 720, "Runner: minutes a session-window start may take (its "
                                        "waits for a compaction, the resume and its verification) before it "
                                        "is killed and fails (D-234)", "automation"),
    # at most 30 for now: keepalive.py --window-start acts only within CRON_STEP (30 min) of a start
    "runner_start_catchup": _num(30, 0, 30, "Runner: a session-window start the runner missed (down at that "
                                 "time) still runs when it is back within this many minutes; a later one is "
                                 "an error that pauses automation (missed start, D-257)", "automation"),
    "runner_runs_retention_days": _num(30, 1, 3650, "Runner: days of run records (runner_runs) kept before the "
                                       "runner's daily prune deletes them", "storage"),
    # --- backup / restore (phase 5c, docs/phase5c_plan.md; backup.py). Read from the RESTORED database
    # by install.restored, so it is the value the install had when the backup was made.
    "restore_starts_paused": _flag(True, "A restored install starts with automation paused (D-173) and the "
                                   "reason \"restored from a backup of <time>: check and resume\"",
                                   "storage"),
    # --- auth (design §9.5, D-162: secure defaults that stay out of the way; dashboard/sessions.py and the
    # login of phase 6a W6 read them). Owner-only like every setting; changing one is a high-impact write
    # (step-up from 6b).
    "auth_idle_timeout_min": _int(30, 5, 480, "Dashboard: minutes without a request after which a browser "
                                  "session ends and the user signs in again", "auth"),
    "auth_absolute_lifetime_h": _int(12, 1, 168, "Dashboard: hours one login lasts at most, active or not",
                                     "auth"),
    "auth_remember_device_days": _int(7, 0, 90, "Dashboard: days a remembered device lets a password login skip "
                                      "the second factor (never the first factor, never a step-up); 0 = off "
                                      "(D-179)", "auth"),
    "auth_reauth_window_min": _int(10, 1, 60, "Dashboard: minutes after a full sign-in within which high-impact "
                                   "changes need no new authentication (step-up)", "auth"),
    "auth_max_sessions_per_user": _int(10, 1, 50, "Dashboard: browser sessions one user may have at once; a new "
                                       "login ends the oldest", "auth"),
    "auth_login_rate": Setting({"account_failures": 5, "ip_failures": 20, "window_min": 15}, _login_rate,
                               "Dashboard: failed sign-ins allowed per account and per client address within "
                               "window_min minutes before exponential backoff (repeated failures alert)",
                               section="auth", kind="login_rate"),
    # --- host (phase 6a W7 / 5d; design §10.4, D-229). 5d's X3 (not built) adds working_root and the
    # rest of this section's settings; it reuses this key rather than redefining it.
    "claude_plan": Setting(CLAUDE_PLAN_UNSET, _choice(CLAUDE_PLANS),
                           "The owner's Claude subscription plan, asked once at first startup (D-229); "
                           "\"unset\" until the wizard's step is done (it must get a real choice, never "
                           "leave this as \"unset\") so AFClaude can adjust plan-dependent numbers (weekly "
                           "limit, session length, available models); changing it later does not retune "
                           "those on its own, a measured value always wins where one exists",
                           section="host", kind="enum", choices=CLAUDE_PLANS),
}
RUNNER_TIMEOUT = "runner_timeout_"


def _setting_spec(key: str) -> Setting:
    s = SETTINGS.get(key)
    if s is None:
        raise ValueError(f"unknown setting {key!r} (known: {', '.join(SETTINGS)})")
    return s


def get_setting(conn: sqlite3.Connection | None, key: str) -> Any:
    """The effective value: the saved one, else the code default."""
    spec = _setting_spec(key)
    row = store.get_setting_row(conn, key) if conn is not None else None
    return copy.deepcopy(row["value"]) if row and row["value"] is not None else spec.default()


def effective_setting(conn: sqlite3.Connection | None, key: str) -> tuple[Any, str]:
    """What a runner uses (afclaude_config.setting): -> (value, source). The saved value is
    checked again (a row written around actions.py, or by a newer version with other rules,
    must not steer a runner): source "db"; no row = "default"; a saved value that fails the
    check = the code default with source "invalid: <why>"."""
    spec = _setting_spec(key)
    row = store.get_setting_row(conn, key) if conn is not None else None
    if not row or row["value"] is None:
        return spec.default(), "default"
    try:
        return spec.check(copy.deepcopy(row["value"]), conn), "db"
    except ValueError as e:
        return spec.default(), f"invalid: {e}"


def settings(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """{key: {value, default, source: db|default, version, updated_at, updated_by, section,
    type, important, doc, (min, max | choices)}}."""
    rows = store.setting_rows(conn)
    out: dict[str, dict[str, Any]] = {}
    for k, spec in SETTINGS.items():
        r = rows.get(k)
        saved = r is not None and r["value"] is not None
        out[k] = {"value": r["value"] if r is not None and saved else spec.default(), "default": spec.default(),
                  "source": "db" if saved else "default", "version": r["version"] if r else 0,
                  "updated_at": r["updated_at"] if r else None, "updated_by": r["updated_by"] if r else None,
                  **spec.describe()}
    return out


def _setting_view(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    r = store.get_setting_row(conn, key)
    return {"key": key, "value": get_setting(conn, key), "version": r["version"] if r else 0,
            "source": "db" if r and r["value"] is not None else "default"}


def _write_setting(conn: sqlite3.Connection, ctx: Ctx, key: str, value: Any, version: Any) -> Result:
    """value None = reset to the default. -> Result."""
    spec = _setting_spec(key)
    row = store.get_setting_row(conn, key)
    _check_version(row, version, f"setting {key}")
    if value is not None:
        try:
            value = spec.check(value, conn)
        except ValueError as e:
            raise ValueError(f"{key}: {e}") from None
    old = row["value"] if row else None
    if old == value:
        return Result(_setting_view(conn, key), key, {"value": old}, {"value": value}, changed=False)
    store.put_setting(conn, key, value, ctx.actor)
    return Result(_setting_view(conn, key), key, {"value": old}, {"value": value})


@action("setting.set", "setting", owner_only=True)
def setting_set(conn: sqlite3.Connection, ctx: Ctx, key: Any, value: Any, version: Any = None) -> Result:
    if value is None:
        raise ValueError("value must not be null (use setting.reset)")
    return _write_setting(conn, ctx, key, value, version)


@action("setting.reset", "setting", owner_only=True)
def setting_reset(conn: sqlite3.Connection, ctx: Ctx, key: Any, version: Any = None) -> Result:
    return _write_setting(conn, ctx, key, None, version)


@action("setting.import", "setting", owner_only=True)
def setting_import(conn: sqlite3.Connection, ctx: Ctx, source: Any, values: Any, notes: Any = None) -> Result:
    """The one-time import of the file tunables (phase 2a, D-146; afclaude_config does it on
    first use, actor runner:import). values {setting key: value} from the file `source`;
    notes {file key: text} for what the file had but is not imported (obsolete, invalid,
    renamed), kept in the audit row so nothing is lost silently. Per key: a value that fails
    the check is not imported (noted); a key the DB already saved keeps its saved value (the
    DB wins); a value equal to the code default is not saved (it stays the default and follows
    later default changes). One transaction, one audit row (target = source).
    -> {source, imported, kept, default, notes}."""
    src = store._text(source, "source", required=True)
    if not isinstance(values, dict):
        raise ValueError("values must be an object {setting key: value}")
    if notes is not None and (not isinstance(notes, dict)
                              or not all(isinstance(k, str) and isinstance(t, str) for k, t in notes.items())):
        raise ValueError("notes must be an object {file key: text}")
    for k in values:
        _setting_spec(k)
    imported: dict[str, Any] = {}
    kept: dict[str, Any] = {}
    same: list[str] = []
    out_notes: dict[str, str] = dict(notes or {})
    for k, v in values.items():
        spec = SETTINGS[k]
        try:
            v = spec.check(v, conn)
        except ValueError as e:
            out_notes[k] = f"not imported, invalid: {e}"
            continue
        row = store.get_setting_row(conn, k)
        if row is not None and row["value"] is not None:
            kept[k] = row["value"]
        elif v == spec.default():
            same.append(k)
        else:
            store.put_setting(conn, k, v, ctx.actor)
            imported[k] = v
    result = {"source": src, "imported": imported, "kept": kept, "default": same, "notes": out_notes}
    changed = bool(imported or kept or same or out_notes)
    before = {"settings": {k: None for k in imported}}
    after = {"settings": imported, "kept": kept or None, "default": same or None, "notes": out_notes or None}
    return Result(result, src, before, after, changed=changed)


@action("automation.set", "setting", owner_only=True)
def automation_set(conn: sqlite3.Connection, ctx: Ctx, paused: Any, reason: Any = None) -> Result:
    """Pause / resume every runner (idempotent). Running sessions keep running. A pause records
    `reason` (why: a runner's failure, D-234/D-235; None keeps the current one); a resume clears
    it. One audit row with both values. -> the automation_paused view + "reason"."""
    p = _bool(paused)
    r = (_reason(reason) if reason is not None else None) if p else ""
    keys = ("automation_paused", "automation_paused_reason")
    rows = {k: store.get_setting_row(conn, k) for k in keys}
    old = {k: (row["value"] if row else None) for k, row in rows.items()}
    new = {"automation_paused": p,
           "automation_paused_reason": old["automation_paused_reason"] if r is None else (r or None)}
    for k in keys:
        if new[k] != old[k]:
            store.put_setting(conn, k, new[k], ctx.actor)
    view = dict(_setting_view(conn, "automation_paused"), reason=get_setting(conn, "automation_paused_reason"))
    return Result(view, "automation_paused", {"value": old["automation_paused"], "reason": old["automation_paused_reason"]},
                  {"value": new["automation_paused"], "reason": new["automation_paused_reason"]})


@action("automation.set_project", "project", owner_only=True)
def automation_set_project(conn: sqlite3.Connection, ctx: Ctx, project: Any, paused: Any, reason: Any = None) -> Result:
    """Pause / resume one project's automation (D-244; idempotent): its runner checks hold while it
    is set, on top of the install-wide automation.set. project: id, name or directory. A pause
    records `reason` (None keeps the current one); a resume clears it. -> {project, name, paused,
    reason, version, updated_at, updated_by}."""
    pr = _project(conn, project)
    p = _bool(paused)
    r = (_reason(reason) if reason is not None else None) if p else ""
    row = store.get_project_pause(conn, pr["id"])
    old = {"paused": row["paused"] if row else None, "reason": row["reason"] if row else None}
    new_reason: str | None = old["reason"] if r is None else (r or None)
    new = {"paused": p, "reason": new_reason}
    if row is None and not p:
        cur = None                                  # never paused: a resume changes nothing
    elif new != old:
        cur = store.put_project_pause(conn, pr["id"], p, new_reason, ctx.actor)
    else:
        cur = row
    view = {"project": pr["id"], "name": pr["name"], "paused": bool(cur and cur["paused"]),
            "reason": cur["reason"] if cur else None, "version": cur["version"] if cur else 0,
            "updated_at": cur["updated_at"] if cur else None, "updated_by": cur["updated_by"] if cur else None}
    return Result(view, pr["id"], old, new if cur is not None else old)


# ---------------------------------------------------------------- installation identity (phase 5c)

RESTORE_SOURCE_KEYS = ("install_id", "install_epoch", "created_at", "app_version", "schema_version",
                       "bundle_sha256", "kind")


def install_epoch(conn: sqlite3.Connection) -> int:
    """meta.install_epoch (absent = 0): bumped by every restore (install.restored), so the bundles
    and processes of the install before it can be told apart from the one after it."""
    raw = store.get_meta(conn, "install_epoch")
    if raw is None:
        return 0
    if not raw.isdigit():
        raise ValueError(f"meta.install_epoch is not a non-negative integer: {raw!r}")
    return int(raw)


def _restore_source(source: Any) -> dict[str, Any]:
    """The bundle a restore came from (backup.py's manifest summary): exactly RESTORE_SOURCE_KEYS."""
    if not isinstance(source, dict) or set(source) != set(RESTORE_SOURCE_KEYS):
        raise ValueError(f"source must be an object with exactly {', '.join(RESTORE_SOURCE_KEYS)}")
    for k in ("install_id", "created_at", "bundle_sha256", "kind"):
        if not isinstance(source[k], str) or not source[k] or len(source[k]) > 200:
            raise ValueError(f"source.{k} must be a non-empty string")
    for k in ("install_epoch", "schema_version"):
        if isinstance(source[k], bool) or not isinstance(source[k], int) or source[k] < 0:
            raise ValueError(f"source.{k} must be a non-negative integer")
    if source["app_version"] is not None and not isinstance(source["app_version"], str):
        raise ValueError("source.app_version must be a string or null")
    return dict(source)


@action("install.stamp", "install", owner_only=True)
def install_stamp(conn: sqlite3.Connection, ctx: Ctx) -> Result:
    """Give a database that predates the install marker its meta.install_id (store.init stamps one
    only on a brand-new database; backup.py refuses to bundle a database without one). A one-off:
    a database that already has an id is a ValueError, never re-stamped. -> {install_id}."""
    have = store.get_meta(conn, "install_id")
    if have is not None:
        raise ValueError(f"this database already has install_id {have}: nothing to stamp")
    iid = uuid.uuid4().hex
    store.set_meta(conn, "install_id", iid)
    store.set_meta(conn, "install_stamped_at", store.now_iso())
    return Result({"install_id": iid}, iid, {"install_id": None}, {"install_id": iid})


@action("install.restored", "install", owner_only=True)
def install_restored(conn: sqlite3.Connection, ctx: Ctx, source: Any) -> Result:
    """Mark this (just restored) database as a restore of `source` (backup.py restore, design
    §11.3): install_epoch + 1, meta.restored_from / restored_at, and, when restore_starts_paused
    is on (D-173, read from this database), automation paused with the reason "restored from a
    backup of <time>: check and resume" (a pause the backup already carried is kept in the reason).
    -> {install_id, install_epoch, restored_from, paused, reason}."""
    src = _restore_source(source)
    iid = store.get_meta(conn, "install_id")
    if iid != src["install_id"]:
        raise ValueError(f"the database's install_id {iid} is not the bundle's {src['install_id']}")
    old_epoch = install_epoch(conn)
    epoch = max(old_epoch, src["install_epoch"]) + 1
    old_from = store.get_meta(conn, "restored_from")
    store.set_meta(conn, "install_epoch", epoch)
    store.set_meta(conn, "restored_from", json.dumps(src, sort_keys=True))
    store.set_meta(conn, "restored_at", store.now_iso())
    paused = _bool(get_setting(conn, "automation_paused"))
    reason: str | None = get_setting(conn, "automation_paused_reason") or None
    if _bool(get_setting(conn, "restore_starts_paused")):
        why = f"restored from a backup of {src['created_at']}: check and resume (D-173)"
        if paused and reason:
            why = f"{why}; paused at backup time: {reason}"
        reason = automation_set(conn, ctx, True, why[:REASON_MAX]).result["reason"]
        paused = True
    out = {"install_id": iid, "install_epoch": epoch, "restored_from": src, "paused": paused, "reason": reason}
    return Result(out, iid, {"install_epoch": old_epoch, "restored_from": old_from},
                  {"install_epoch": epoch, "restored_from": src, "paused": paused})


def _fresh_group(days: Mapping[str, Any]) -> str:
    used = {w["group"] for w in days.values() if w}
    i = 1
    while f"g{i}" in used:
        i += 1
    return f"g{i}"


@action("window.set", "setting", owner_only=True)
def window_set(conn: sqlite3.Connection, ctx: Ctx, day: Any, start: Any, n: Any = None, mode: Any = "linked",
               group: Any = None, version: Any = None) -> Result:
    """Edit the per-weekday windows (§4.2.1, F4). start None = no window ("off").
    mode: linked (every day of `day`'s link group moves together), individual (only
    `day`; it gets a fresh group), week (all seven days, one fresh group), link (`day`
    joins `group` and takes its window; start/n are ignored). version = the
    window_days setting's version. -> {key, value, version, source, changed_days}."""
    if mode not in WINDOW_MODES:
        raise ValueError(f"mode must be one of {'|'.join(WINDOW_MODES)}, got {mode!r}")
    if mode != "week" and day not in DAYS:
        raise ValueError(f"day must be one of {', '.join(DAYS)}, got {day!r}")
    cur = get_setting(conn, "window_days")
    new = copy.deepcopy(cur)
    if mode == "link":
        src = next((w for w in cur.values() if w and w["group"] == group), None)
        if src is None:
            raise ValueError(f"no window has link group {group!r}")
        new[day] = dict(src)
    else:
        if start is not None and n is None:
            raise ValueError("n (number of session windows) is needed with a start")
        if mode == "week":
            g = _fresh_group(cur)
            for d in DAYS:
                new[d] = None if start is None else {"start": start, "n": n, "group": g}
        elif mode == "linked" and cur[day] is not None:
            g = cur[day]["group"]
            for d in DAYS:
                if cur[d] and cur[d]["group"] == g:
                    new[d] = None if start is None else {"start": start, "n": n, "group": g}
        else:                                   # individual, or linked on a day that is off
            new[day] = None if start is None else {"start": start, "n": n, "group": _fresh_group(cur)}
    res = _write_setting(conn, ctx, "window_days", new, version)
    res.result["changed_days"] = [d for d in DAYS if cur[d] != res.result["value"][d]]
    return res


# ---------------------------------------------------------------- prompt overrides (§4.3)

PROMPT_NAME_RE = re.compile(r"^[a-z0-9_]+\.md$")


def prompt_default(name: object) -> str:
    """The default text of prompts/<name> (name = file name, e.g. continue.md)."""
    if not isinstance(name, str) or not PROMPT_NAME_RE.match(name) or name == "README.md":
        raise ValueError(f"bad prompt name {name!r} (a file name under prompts/, e.g. continue.md)")
    path = os.path.join(PROMPTS_DIR, name)
    try:
        with open(path) as fh:
            return fh.read()
    except FileNotFoundError:
        raise store.NotFound(f"no prompt {name}") from None


placeholders = prompts.placeholders     # the {placeholder} names of a template (None: unbalanced braces)


def check_prompt(name: object, body: object) -> str:
    """An override must keep the default's placeholder set (literal braces doubled) and
    render with dummy values. -> the default text."""
    default = prompt_default(name)
    if not isinstance(body, str) or not body.strip():
        raise ValueError("the prompt body must not be empty")
    if len(body) > PROMPT_MAX:
        raise ValueError(f"the prompt is too long ({len(body)} > {PROMPT_MAX} characters)")
    want, have = placeholders(default), placeholders(body)
    if have is None:
        raise ValueError("unbalanced braces: write literal braces as {{ and }}")
    if want is not None and have != want:
        raise ValueError(f"placeholders must be exactly those of the default {sorted(want)}; "
                         f"missing {sorted(want - have)}, unknown {sorted(have - want)}")
    try:
        body.format(**{p: "x" for p in have})
    except (KeyError, IndexError, ValueError, AttributeError) as e:
        raise ValueError(f"the prompt does not render: {type(e).__name__}: {e}") from None
    return default


def prompt_text(conn: sqlite3.Connection, name: str) -> str:
    """What a sender should use: the override if there is one, else the file (phase 4a P4 wires it in)."""
    o = store.get_prompt_override(conn, name)
    if o and o["body"] is not None:
        body: str = o["body"]
        return body
    return prompt_default(name)


@action("prompt.set", "prompt", owner_only=True)
def prompt_set(conn: sqlite3.Connection, ctx: Ctx, name: Any, body: Any, version: Any = None) -> Result:
    default = check_prompt(name, body)
    row = store.get_prompt_override(conn, name)
    _check_version(row, version, f"prompt {name}")
    old = row["body"] if row else None
    if old == body:
        return Result(row, name, {"body": old}, {"body": body}, changed=False)
    r = store.put_prompt_override(conn, name, body, hashlib.sha256(default.encode()).hexdigest(), ctx.actor)
    return Result(r, name, {"body": old}, {"body": body})


@action("prompt.reset", "prompt", owner_only=True)
def prompt_reset(conn: sqlite3.Connection, ctx: Ctx, name: Any, version: Any = None) -> Result:
    prompt_default(name)
    row = store.get_prompt_override(conn, name)
    _check_version(row, version, f"prompt {name}")
    if row is None or row["body"] is None:
        return Result(row, name, None, None, changed=False)
    r = store.reset_prompt_override(conn, name, ctx.actor)
    return Result(r, name, {"body": row["body"]}, {"body": None})


# ---------------------------------------------------------------- prompt defaults (D-227)

CODE_VERSION_FILE = os.path.join(HERE, "CODE_VERSION")   # baked into the image (docker/Dockerfile)


def _code_version() -> str | None:
    """git commit or image build id, best-effort (D-227: never an error, just unknown). Checks
    AFCLAUDE_CODE_VERSION first (an operator override), then CODE_VERSION next to the code:
    the image has no .git (.dockerignore), so the build writes the commit it was built from
    there (build arg AFCLAUDE_CODE_VERSION via docker/compose.yml; a dev mount of a checkout
    over the code hides the file), else the repo's own git commit (host / dev checkout)."""
    env = os.environ.get("AFCLAUDE_CODE_VERSION")
    if env:
        return env
    try:
        with open(CODE_VERSION_FILE) as fh:
            baked = fh.read().strip()
        if baked:
            return baked
    except OSError:
        pass
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True,
                             text=True, timeout=5, check=True)
        sha = out.stdout.strip()
        return sha or None
    except (OSError, subprocess.SubprocessError):
        return None


def _prompt_default_files() -> dict[str, tuple[str, str]]:
    """{name: (body, sha256)} for every prompts/*.md except README.md, the code's current
    defaults (D-169: defaults ship with the code; the DB only ever records them)."""
    out: dict[str, tuple[str, str]] = {}
    try:
        names = os.listdir(PROMPTS_DIR)
    except OSError:
        return out
    for fname in sorted(names):
        if fname == "README.md" or not PROMPT_NAME_RE.match(fname):
            continue
        path = os.path.join(PROMPTS_DIR, fname)
        if not os.path.isfile(path):
            continue
        with open(path) as fh:
            body = fh.read()
        out[fname] = (body, hashlib.sha256(body.encode()).hexdigest())
    return out


def prompt_defaults_preview(conn: sqlite3.Connection) -> dict[str, Any]:
    """The dry-run: what `prompt.defaults_sync` would import right now. Read-only (no gate, no
    transaction, not an audited action; mirrors log_prune_preview)."""
    files = _prompt_default_files()
    changed = []
    for name, (_body, sha) in files.items():
        cur = store.get_prompt_default(conn, name)
        if cur is None:
            changed.append({"name": name, "change": "new", "sha256": sha})
        elif cur["sha256"] != sha:
            changed.append({"name": name, "change": "updated", "sha256": sha,
                            "previous_sha256": cur["sha256"]})
    return {"files": sorted(files), "changed": changed, "code_version": _code_version()}


@action("prompt.defaults_sync", "prompt_defaults")
def prompt_defaults_sync(conn: sqlite3.Connection, ctx: Ctx) -> Result:
    """Import prompts/*.md (except README.md) into prompt_defaults -- the code's current
    defaults, which still ship as files (D-169); the DB holds the owner's edits
    (prompt_overrides, D-193/D-197). Meant to run at every deploy (`python3 store.py
    import-prompts`, docker/entrypoint.sh, actor runner:deploy), so it must stay idempotent: a
    file whose sha256 already matches writes nothing (store.put_prompt_default). Where an
    override exists, also refreshes prompt_overrides.base_outdated: whether its base_sha256 (the
    default it was edited against) still matches the current default's sha256, so a reader can
    flag a stale edit without re-reading the file (no-op, defensive, if that column is somehow
    missing: store.mark_override_base_outdated). No audit row when nothing changed (is_noop)."""
    code_version = _code_version()
    files = _prompt_default_files()
    changed = []
    for name, (body, sha) in files.items():
        row, did_change = store.put_prompt_default(conn, name, body, sha, code_version)
        if did_change:
            changed.append({"name": name, "sha256": sha, "previous_sha256": row.get("previous_sha256")})
    defaults_sha = {name: sha for name, (_b, sha) in files.items()}
    for o in store.list_prompt_overrides(conn):
        default_sha = defaults_sha.get(o["name"])
        outdated = None if default_sha is None or o["base_sha256"] is None else o["base_sha256"] != default_sha
        store.mark_override_base_outdated(conn, o["name"], outdated)
    after = {"changed": changed, "total_files": len(files), "code_version": code_version}
    return Result(after, "prompt_defaults", None, after, changed=bool(changed))


# ---------------------------------------------------------------- project prompt variables (D-197)
# Prompts are system-wide (D-193); a project only fills the variables prompts.PROJECT_VARS declares
# (prompts.py, phase 4a P1). Owner-only for now: D-197 gives them to the project's managers and
# editors, which the roles of phase 5a bring; an autonomous session never sets them.

def _prompt_var_audit(project: store.Row, row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if row is None else {"project": project["name"], "name": row["name"], "value": row["value"]}


@action("prompt_var.set", "prompt_var", owner_only=True)
def prompt_var_set(conn: sqlite3.Connection, ctx: Ctx, project: Any, name: Any, value: Any,
                   version: Any = None) -> Result:
    prompts.check_var(name, value)
    p = store._project_row(conn, project)
    row = store.get_prompt_var(conn, p["id"], name)
    _check_version(row, version, f"prompt variable {name} of {p['name']}")
    target = f"{p['id']}:{name}"
    if row is not None and row["value"] == value:
        return Result(row, target, None, None, changed=False)
    after = store.put_prompt_var(conn, p["id"], name, value, ctx.actor)
    return Result(after, target, _prompt_var_audit(p, row), _prompt_var_audit(p, after))


@action("prompt_var.reset", "prompt_var", owner_only=True)
def prompt_var_reset(conn: sqlite3.Connection, ctx: Ctx, project: Any, name: Any, version: Any = None) -> Result:
    """Back to the variable's declared default (value NULL; the row and its version stay)."""
    prompts.check_var(name)
    p = store._project_row(conn, project)
    row = store.get_prompt_var(conn, p["id"], name)
    _check_version(row, version, f"prompt variable {name} of {p['name']}")
    target = f"{p['id']}:{name}"
    if row is None or row["value"] is None:
        return Result(row, target, None, None, changed=False)
    after = store.put_prompt_var(conn, p["id"], name, None, ctx.actor)
    return Result(after, target, _prompt_var_audit(p, row), _prompt_var_audit(p, after))


@action("prompt_var.migrate", "prompt_var", owner_only=True)
def prompt_var_migrate(conn: sqlite3.Connection, ctx: Ctx) -> Result:
    """The migrate step (prompts.py migrate): the seeds of the project at this checkout
    (prompts.CHECKOUT_SEED), each only where that project never had a row for it, so a value
    someone set or reset stays. Idempotent: a second run writes nothing (no audit row)."""
    plan = prompts.migrate_plan(conn)
    for w in plan:
        prompts.check_var(w["name"], w["value"])
        store.put_prompt_var(conn, w["project_id"], w["name"], w["value"], ctx.actor)
    after = {"seeded": plan}
    return Result(after, plan[0]["project_id"] if plan else None, None, after, changed=bool(plan))


# ---------------------------------------------------------------- execution requests (§4.6)
#
# Phase 3b (docs/phase3b_plan.md §2, §3.1): work_on_now (target = a project; run-now, D-153) joins
# continue_now and review_now. A request goes open -> started | refused | failed; the runner
# (S5) records that through request.start / request.finish, audited: that is what the requester
# sees, unlike the run row itself (D-256). Nothing consumes requests before S5.

REQUEST_KINDS = ("continue_now", "review_now", "work_on_now")
REQUEST_STATUSES = ("open", "started", "refused", "failed", "done", "cancelled")   # done, cancelled: legacy
REQUEST_FINISH = {"refused": ("open",), "failed": ("open", "started")}   # outcome -> from


@action("request.add", "request", owner_only=True)
def request_add(conn: sqlite3.Connection, ctx: Ctx, kind: Any, target: Any = None) -> Result:
    """Queue continue_now (target = a stalled session id), review_now (no target) or work_on_now
    (target = a project, name or id; stored as its id). One open request per (kind, target):
    asking again returns the open one. A project whose task-manager session doesn't exist yet
    is a valid target: its first start creates it (D-281)."""
    store._enum(kind, REQUEST_KINDS, "kind")
    if kind == "continue_now":
        target = store._text(target, "target", required=True)
        if store.get_session(conn, target) is None:
            raise store.NotFound(f"unknown session {target}")
    elif kind == "work_on_now":
        if target is None:
            raise ValueError("work_on_now needs a target (a project)")
        target = str(store._project_row(conn, target)["id"])
    elif target is not None:
        raise ValueError("review_now takes no target")
    open_ = store.open_request(conn, kind, target)
    if open_:
        return Result(open_, open_["id"], open_, open_, changed=False)
    r = store.add_request(conn, ctx.actor, kind, target)
    return Result(r, r["id"], None, r)


def _request(conn: sqlite3.Connection, request_id: Any) -> store.Row:
    r = store.get_request(conn, store._position(request_id, "request id"))
    if r is None:
        raise store.NotFound(f"no request #{request_id}")
    return r


def _run_id(run_id: Any) -> int | None:
    return None if run_id is None else store._position(run_id, "run id")


@action("request.start", "request")
def request_start(conn: sqlite3.Connection, ctx: Ctx, request_id: Any, tmux: Any, run_id: Any = None,
                  session_id: Any = None) -> Result:
    """open -> started: the runner's run `run_id` started the requested session in tmux `tmux`
    (session_id: its id, when known). The runner's step, not for MCP callers."""
    _runner_only(ctx, "request.start")
    before = _request(conn, request_id)
    name = _tmux_name(tmux)
    sid = None if session_id is None else store._session_id(session_id)
    if before["status"] != "open":
        raise store.InvalidTransition(f"request #{before['id']} is {before['status']}, not open")
    result = json.dumps({"tmux": name, "session_id": sid}, ensure_ascii=False)
    after = store.set_request_state(conn, int(before["id"]), "started", result, _run_id(run_id))
    return Result(after, after["id"], before, after)


@action("request.finish", "request")
def request_finish(conn: sqlite3.Connection, ctx: Ctx, request_id: Any, status: Any, reason: Any,
                   run_id: Any = None) -> Result:
    """open -> refused (it can't run now: running, at its limit, held, unknown target) or
    open | started -> failed (it broke; the runner also alerts and pauses, D-235/D-282), with
    the reason. The runner's step, not for MCP callers."""
    _runner_only(ctx, "request.finish")
    st = store._enum(status, tuple(REQUEST_FINISH), "status")
    why = _short(reason, "reason", 2000)
    before = _request(conn, request_id)
    if before["status"] not in REQUEST_FINISH[st]:
        raise store.InvalidTransition(f"request #{before['id']} is {before['status']}: it can't become {st}")
    result = json.dumps({"reason": why}, ensure_ascii=False)
    after = store.set_request_state(conn, int(before["id"]), st, result, _run_id(run_id))
    return Result(after, after["id"], before, after)


# ---------------------------------------------------------------- driven sessions (phase 3b)
#
# driven.set: a driven_sessions row of the phase 3b kinds (docs/phase3b_plan.md §2, §3.1). The tmux
# name is stored, never computed from the id (S2 makes every launcher write its row first). A
# live name (ended_at NULL) belongs to one row; a project has one live task-manager row. The own
# sessions (kind 'own') keep their own writer, own_session_add.

DRIVEN_KINDS = names.KINDS
# today's ka-<id8> names (until S6) and the readable afc-... ones (§3.2: [a-z0-9-], at most 40
# characters): names.py's, the same the bridge accepts (\Z: no trailing newline)
TMUX_RE = names.TMUX_RE


def _tmux_name(value: Any) -> str:
    name = store._text(value, "tmux", required=True)
    if not TMUX_RE.match(name):
        raise ValueError(f"tmux must be ka-<8 hex> or afc-<a-z 0-9 -, at most 36>, got {name!r}")
    return name


@action("driven.set", "driven_session")
def driven_set(conn: sqlite3.Connection, ctx: Ctx, session_id: Any, kind: Any = None, tmux: Any = None,
               project: Any = None, task_id: Any = None, started_at: Any = None, ended_at: Any = None,
               live: Any = None) -> Result:
    """Add or change a driven session's row: kind (DRIVEN_KINDS), tmux (its name), project (name
    or id; required for a task-manager), task_id, started_at, ended_at (ISO times; a set ended_at
    frees the name), live (true: an ended row is live again, ended_at cleared; S2's launchers when
    they continue a session whose row ended). A new row needs kind and tmux. A row of another kind
    ('own', legacy rows) is not changed here. The runners' step (S2 on), and the backfill's; not
    for MCP callers."""
    _runner_only(ctx, "driven.set")
    sid = store._session_id(session_id)
    before = store.get_driven_session(conn, sid)
    if before is not None and before["kind"] not in DRIVEN_KINDS:
        raise ValueError(f"the row of {sid} is of kind {before['kind']!r}: driven.set changes only "
                         f"{'|'.join(DRIVEN_KINDS)} rows")
    fields: dict[str, Any] = {}
    if kind is not None:
        fields["kind"] = store._enum(kind, DRIVEN_KINDS, "kind")
    if tmux is not None:
        fields["tmux"] = _tmux_name(tmux)
    if project is not None:
        fields["project_id"] = int(store._project_row(conn, project)["id"])
    if task_id is not None:
        fields["task_id"] = int(_task(conn, task_id)["id"])
    for k, v in (("started_at", started_at), ("ended_at", ended_at)):
        if v is not None:
            t = store._text(v, k, required=True)
            try:
                store.parse_iso(t)
            except ValueError:
                raise ValueError(f"{k} must be an ISO time, got {t!r}") from None
            fields[k] = t
    if live is not None:
        if live is not True:
            raise ValueError(f"live must be true (to end a row, give ended_at), got {live!r}")
        if ended_at is not None:
            raise ValueError("live and ended_at contradict each other")
        if before is not None and before["ended_at"] is not None:
            fields["ended_at"] = None
    if before is None and ("kind" not in fields or "tmux" not in fields):
        raise ValueError(f"a new driven session needs kind and tmux ({sid})")
    row = dict(before or {}, **fields)
    if row["kind"] == "task-manager" and row.get("project_id") is None:
        raise ValueError("a task-manager row needs its project")
    if row.get("ended_at") is None:
        holders = store.live_driven_with(conn, tmux=row["tmux"], other_than=sid) if row.get("tmux") else []
        if holders:
            taken = holders[0]
            raise Conflict(f"tmux {row['tmux']} is the live name of {taken['session_id']} ({taken['kind']})",
                           current=taken)
        if row["kind"] == "task-manager":
            other = store.live_driven_with(conn, task_manager_of=row["project_id"], other_than=sid)
            if other:
                raise Conflict(f"project #{row['project_id']} already has the live task-manager "
                               f"{other[0]['session_id']} (tmux {other[0]['tmux']})", current=other[0])
    if before is not None and all(before.get(k) == v for k, v in fields.items()):
        return Result(before, sid, before, before, changed=False)
    after = store.upsert_driven_session(conn, sid, **fields)
    return Result(after, sid, before, after)


# ---------------------------------------------------------------- compaction hand-offs (§5.5)

@action("handoff.write", "handoff")
def handoff_write(conn: sqlite3.Connection, ctx: Ctx, session_id: Any, body: Any, project: Any = None) -> Result:
    """A session's hand-off for its compacted self (D-144/D-155): validated by store.handoff_body.
    Any session may write its own, an autonomous one included (D-185: an agent's direct write);
    through MCP a session that names itself may not write another session's."""
    if ctx.session is not None and isinstance(session_id, str) and session_id.strip() != ctx.session:
        raise Forbidden(f"a session writes only its own hand-off (this is {ctx.session}, not {session_id})")
    h = store.add_handoff(conn, session_id, body, project, created_by=ctx.actor)
    return Result(h, h["id"], None, h)


@action("handoff.consume", "handoff")
def handoff_consume(conn: sqlite3.Connection, ctx: Ctx, handoff_id: Any, start_key: Any) -> Result:
    """The runner injected the hand-off into the continue message of the start `start_key`:
    mark it (and the older unconsumed ones of the session) consumed. Not for MCP callers."""
    if ctx.via == "mcp":
        raise Forbidden("hand-offs are consumed by the runner, not through MCP")
    before = store.get_handoff(conn, store._position(handoff_id, "handoff id"))
    after = store.consume_handoff(conn, handoff_id, start_key)
    return Result(after, after["id"], before, after)


# ---------------------------------------------------------------- the owner's inbox (D-178, D-241)
#
# The owner's messages to the task-manager (store.inbox_messages; inbox.py, inbox_api.py). The row
# holds the text as typed and is its only copy (D-241): the audit rows name a text by its size and
# sha256, never repeat it. inbox.post is the owner's (the dashboard, the CLI); the triage and the
# delivery steps are the runner's (inbox_api.py's worker, the keep-alive), never MCP's; inbox.reply
# is the task-manager's answer (`tasks.py inbox reply`); inbox.dismiss is the owner's: an answered
# thread leaves the dashboard's questions section (D-304).

INBOX_REASON_MAX = 300
INBOX_ERROR_MAX = 1000
INBOX_VIA_MAX = 200


def _digest(text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    return {"chars": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}


def _inbox_audit(m: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A message for the audit row: every field but the texts, which are named by size and sha256."""
    if m is None:
        return None
    out = {k: v for k, v in m.items() if k not in ("text", "answer", "triage_usage")}
    out.update(text=_digest(m.get("text")), answer=_digest(m.get("answer")))
    return out


def _inbox_change(before: store.Row, after: store.Row) -> Result:
    return Result(after, after["id"], _inbox_audit(before), _inbox_audit(after))


def _runner_only(ctx: Ctx, what: str) -> None:
    if ctx.via == "mcp":
        raise Forbidden(f"{what} is the runner's step (inbox_api.py, the keep-alive), not for MCP callers")


def _short(value: Any, what: str, limit: int, required: bool = True) -> str | None:
    t = store._text(value, what, required=required)
    if t is not None and len(t) > limit:
        t = t[:limit - 3] + "..."
    return t


@action("inbox.post", "inbox", owner_only=True)
def inbox_post(conn: sqlite3.Connection, ctx: Ctx, text: Any, session_id: Any = None) -> Result:
    """A message from the owner to the task-manager (default: the AFClaude manager session),
    pending until the triage sorts it."""
    sid = session_id if session_id is not None else afclaude_config.manager_session()
    m = store.add_inbox(conn, text, sid, ctx.actor)
    return Result(m, m["id"], None, _inbox_audit(m))


@action("inbox.triage", "inbox")
def inbox_triage(conn: sqlite3.Connection, ctx: Ctx, message_id: Any, verdict: Any, reason: Any,
                 cost_usd: Any = None, usage: Any = None) -> Result:
    """The Haiku verdict (D-241): "note" -> noted (it waits for the next message into the session),
    "now" -> stays pending until it is delivered."""
    _runner_only(ctx, "the triage")
    store._enum(verdict, store.INBOX_VERDICTS, "verdict")
    if cost_usd is not None and (isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float))):
        raise ValueError(f"cost_usd must be a number, got {cost_usd!r}")
    if usage is not None and not isinstance(usage, dict):
        raise ValueError("usage must be an object")
    before = store.inbox_row(conn, message_id)
    if before["verdict"] is not None:
        raise store.InvalidTransition(f"inbox message #{before['id']} was already triaged ({before['verdict']})")
    after = store.update_inbox(conn, before["id"], ("pending",), verdict=verdict,
                               verdict_reason=_short(reason, "reason", INBOX_REASON_MAX),
                               triaged_at=store.now_iso(), triage_cost_usd=cost_usd, triage_usage=usage,
                               status="noted" if verdict == "note" else "pending",
                               hold=None, hold_since=None, retry_after=None)   # a triage hold is over
    return _inbox_change(before, after)


@action("inbox.triage_hold", "inbox")
def inbox_triage_hold(conn: sqlite3.Connection, ctx: Ctx, message_id: Any, reason: Any, retry_after: Any) -> Result:
    """The triage call hit the account's usage limit (D-244): the message stays pending and
    untriaged, held until `retry_after` (the limit's reset, an ISO time), then triaged again once."""
    _runner_only(ctx, "a triage hold")
    text = store._text(retry_after, "retry_after", required=True)
    try:
        at = store.parse_iso(text)
    except ValueError:
        at = None
    if at is None or at.tzinfo is None:
        raise ValueError(f"retry_after must be an ISO time with a zone, got {retry_after!r}")
    before = store.inbox_row(conn, message_id)
    if before["status"] != "pending" or before["verdict"] is not None:
        raise store.InvalidTransition(f"inbox message #{before['id']} is not waiting for its triage "
                                      f"(status {before['status']}, verdict {before['verdict']})")
    after = store.update_inbox(conn, before["id"], ("pending",), hold=_short(reason, "reason", INBOX_REASON_MAX),
                               hold_since=before["hold_since"] or store.now_iso(), retry_after=store.iso(at))
    return _inbox_change(before, after)


def _pending_now(conn: sqlite3.Connection, message_id: Any) -> store.Row:
    m = store.inbox_row(conn, message_id)
    if m["status"] != "pending" or m["verdict"] != "now":
        raise store.InvalidTransition(f"inbox message #{m['id']} is not waiting for delivery "
                                      f"(status {m['status']}, verdict {m['verdict']})")
    return m


@action("inbox.hold", "inbox")
def inbox_hold(conn: sqlite3.Connection, ctx: Ctx, message_id: Any, reason: Any) -> Result:
    """A "now" message can't be typed in yet (the owner is typing, a compaction runs, ...): why,
    and since when (kept while the reason stays the same: a no-op, no audit row)."""
    _runner_only(ctx, "a delivery hold")
    before = _pending_now(conn, message_id)
    why = _short(reason, "reason", INBOX_REASON_MAX)
    if before["hold"] == why:
        return Result(before, before["id"], None, None, changed=False)
    after = store.update_inbox(conn, before["id"], ("pending",), hold=why,
                               hold_since=before["hold_since"] or store.now_iso())
    return _inbox_change(before, after)


@action("inbox.deliver", "inbox")
def inbox_deliver(conn: sqlite3.Connection, ctx: Ctx, message_id: Any, plan: Any) -> Result:
    """A "now" message was typed into the session (D-186) with the send plan `plan`."""
    _runner_only(ctx, "the delivery")
    before = _pending_now(conn, message_id)
    after = store.update_inbox(conn, before["id"], ("pending",), status="delivered", delivered_at=store.now_iso(),
                               delivered_via=_short(plan, "plan", INBOX_VIA_MAX), hold=None)
    return _inbox_change(before, after)


@action("inbox.include", "inbox")
def inbox_include(conn: sqlite3.Connection, ctx: Ctx, message_ids: Any, by: Any) -> Result:
    """Noted messages went into the session with the message `by` (a start, a compaction's
    hand-off, a delivered "now" message): noted -> included."""
    _runner_only(ctx, "an inclusion")
    if not isinstance(message_ids, list) or not message_ids:
        raise ValueError("message_ids must be a non-empty list of message ids")
    who = _short(by, "by", INBOX_VIA_MAX)
    now = store.now_iso()
    befores, afters = [], []
    for mid in message_ids:
        b = store.inbox_row(conn, mid)
        befores.append(b)
        afters.append(store.update_inbox(conn, b["id"], ("noted",), status="included", included_at=now,
                                         included_by=who))
    return Result(afters, ",".join(str(a["id"]) for a in afters),
                  [{"id": b["id"], "status": b["status"]} for b in befores],
                  [{"id": a["id"], "status": a["status"], "included_by": who} for a in afters])


@action("inbox.fail", "inbox")
def inbox_fail(conn: sqlite3.Connection, ctx: Ctx, message_id: Any, error: Any) -> Result:
    """The triage or the delivery failed (D-235): the message shows the error on the dashboard
    until the owner retries it; it is never dropped."""
    _runner_only(ctx, "a failure")
    before = store.inbox_row(conn, message_id)
    after = store.update_inbox(conn, before["id"], ("pending", "delivered"), status="error",
                               error=_short(error, "error", INBOX_ERROR_MAX), error_at=store.now_iso(), hold=None,
                               retry_after=None)
    return _inbox_change(before, after)


@action("inbox.retry", "inbox", owner_only=True)
def inbox_retry(conn: sqlite3.Connection, ctx: Ctx, message_id: Any) -> Result:
    """The owner retries a failed message: pending again (triaged again if it has no verdict yet,
    otherwise delivered or noted per its verdict). The old error stays in the audit row."""
    before = store.inbox_row(conn, message_id)
    status = "noted" if before["verdict"] == "note" else "pending"
    after = store.update_inbox(conn, before["id"], ("error",), status=status, error=None, error_at=None,
                               hold=None, hold_since=None, retry_after=None)
    return _inbox_change(before, after)


@action("inbox.reply", "inbox")
def inbox_reply(conn: sqlite3.Connection, ctx: Ctx, message_id: Any, answer: Any) -> Result:
    """The task-manager's answer, shown on the dashboard. A second reply is added below the first and
    brings a dismissed thread back (D-304)."""
    text = store._text(answer, "answer", required=True)
    if len(text) > store.INBOX_TEXT_MAX:
        raise ValueError(f"answer is too long ({len(text)} > {store.INBOX_TEXT_MAX} characters)")
    before = store.inbox_row(conn, message_id)
    full = f"{before['answer']}\n\n{text}" if before["answer"] else text
    after = store.update_inbox(conn, before["id"], ("pending", "delivered", "noted", "included", "answered"),
                               status="answered", answer=full, answered_at=store.now_iso(), hold=None,
                               dismissed_at=None)
    return _inbox_change(before, after)


@action("inbox.dismiss", "inbox", owner_only=True)
def inbox_dismiss(conn: sqlite3.Connection, ctx: Ctx, message_id: Any) -> Result:
    """The owner dismisses an answered thread: it leaves the dashboard's questions section (D-304)."""
    before = store.inbox_row(conn, message_id)
    if before["dismissed_at"] is not None:
        raise store.InvalidTransition(f"inbox message #{before['id']} is already dismissed")
    after = store.update_inbox(conn, before["id"], ("answered",), dismissed_at=store.now_iso())
    return _inbox_change(before, after)


# ---------------------------------------------------------------- notices for the owner (D-306)
#
# A notice is a title and a short text on the dashboard's "Needs your input" section until the owner
# dismisses it: anything meant for the owner that is not an immediate chat answer. notice.post is the
# task-manager's (`tasks.py notice post`, MCP afclaude_notice), notice.dismiss the owner's (the inbox
# API, the CLI). Every review report doc (review:*) writes its own notice (review_notice below, from
# doc.set and doc.sync), audited as notice.post / notice.update in the doc action's transaction.

REVIEW_SUMMARY_MAX = 1200          # characters of a review's summary in its notice
REVIEW_SUMMARY_HEADS = re.compile(r"^#{2,6}\s+(tl;?dr|summary|short summary|result|results|conclusions?|findings|"
                                  r"verdict|outcome)\b", re.I)


@action("notice.post", "notice")
def notice_post(conn: sqlite3.Connection, ctx: Ctx, title: Any, text: Any, project: Any = None) -> Result:
    """A notice for the owner (D-306): `title`, `text` (the full content, D-279), optionally about
    one project. It stays on the dashboard until the owner dismisses it."""
    pid = store._project_row(conn, project)["id"] if project is not None else None
    n = store.add_notice(conn, title, text, pid, ctx.actor)
    return Result(n, n["id"], None, n)


@action("notice.dismiss", "notice", owner_only=True)
def notice_dismiss(conn: sqlite3.Connection, ctx: Ctx, notice_id: Any) -> Result:
    """The owner dismisses a notice: it leaves the dashboard (D-306)."""
    before = store.notice_row(conn, notice_id)
    if before["dismissed_at"] is not None:
        raise store.InvalidTransition(f"notice #{before['id']} is already dismissed")
    after = store.update_notice(conn, before["id"], dismissed_at=store.now_iso())
    return Result(after, after["id"], before, after)


def review_summary(text: str, fallback_title: str) -> tuple[str, str]:
    """A review report's (title, short summary) for its notice: the first '# ' heading (else
    `fallback_title`); the first block (paragraph or list) under a TL;DR / Summary / Results-like
    heading, else the first block after the title. Cut at a line to REVIEW_SUMMARY_MAX."""
    lines = text.splitlines()
    title, start = fallback_title, 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title, start = line[2:].strip() or fallback_title, i + 1
            break
        if line.strip():
            break
    hit = next((i + 1 for i, line in enumerate(lines) if REVIEW_SUMMARY_HEADS.match(line.strip())), None)
    block: list[str] = []
    for line in lines[hit if hit is not None else start:]:
        if not line.strip():
            if block:
                break
            continue
        if line.lstrip().startswith("#"):
            if block:
                break
            continue
        block.append(line.rstrip())
    summary = "\n".join(block).strip() or "(the report has no summary paragraph)"
    if len(summary) > REVIEW_SUMMARY_MAX:
        cut = summary.rfind("\n", 0, REVIEW_SUMMARY_MAX - 2)
        summary = summary[:cut if cut > 0 else REVIEW_SUMMARY_MAX - 2].rstrip() + " …"
    return title[:store.NOTICE_TITLE_MAX].replace("\n", " "), summary


def review_notice(conn: sqlite3.Connection, ctx: Ctx, project: store.Row, name: str) -> None:
    """D-306: a review report doc (review:*) was written: post its notice (title and short summary,
    review_summary), or refresh the one still shown; a dismissed one stays dismissed (a rewrite of
    the report doesn't bring it back). Its own audit row, in the doc action's transaction."""
    if not name.startswith("review:"):
        return
    doc = store.get_doc(conn, project["id"], name)
    if doc is None:
        return
    title, summary = review_summary(store.doc_render(conn, doc["id"]), name)
    source = f"{project['name']}/{name}"
    before = store.notice_by_source(conn, source)
    if before is None:
        n = store.add_notice(conn, title, summary, project["id"], ctx.actor, source=source)
        store.add_audit(conn, ctx.actor, ctx.via, "notice.post", "notice", n["id"], None, n, None)
        return
    if before["dismissed_at"] is not None or (before["title"], before["text"]) == (title, summary):
        return
    after = store.update_notice(conn, before["id"], title=title, text=summary, updated_at=store.now_iso())
    b, a = Result(None, None, before, after).audit_pair()
    store.add_audit(conn, ctx.actor, ctx.via, "notice.update", "notice", after["id"], b, a, None)


# ---------------------------------------------------------------- project docs (phase 2d part 2, §7.2, §7.7)
#
# PROGRESS / GOALS / EXCEPTIONS / ALERTS / review reports per project (store.DOCS_SCHEMA; the files are
# mapped by project_docs.py). The entries are append-only; the audit row names the entry (id, op,
# size, sha256), not its text, which is in doc_entries already. D-185/D-182: a project's docs are
# written by its task-manager (agents propose); the roles that enforce this come with phase 5a,
# until then any CLI/MCP caller may write (like tasks).

def _doc_ref(conn: sqlite3.Connection, project: Any, name: Any) -> tuple[store.Row, str]:
    p = store._project_row(conn, project)
    store.doc_kind(name)
    return p, str(name)


def _doc_state(conn: sqlite3.Connection, doc: store.Row | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    text = store.doc_render(conn, doc["id"])
    return {"chars": len(text), "sha256": store.sha256_text(text), "last_entry": store.doc_last_entry(conn, doc["id"])}


def _doc_result(doc: store.Row, project: store.Row, **extra: Any) -> dict[str, Any]:
    return dict({"project": project["name"], "name": doc["name"], "kind": doc["kind"]}, **extra)


@action("doc.append", "doc")
def doc_append(conn: sqlite3.Connection, ctx: Ctx, project: Any, name: Any, text: Any) -> Result:
    """Append an entry to a log doc (progress, exceptions, alerts), exactly as given: the caller
    adds the trailing newline (tasks.py and the MCP tool do)."""
    p, n = _doc_ref(conn, project, name)
    if store.doc_kind(n) != "log":
        raise ValueError(f"{n} is a whole document, not a log: use doc.set")
    body = store.doc_text(text)
    doc = store.ensure_doc(conn, p["id"], n, ctx.actor)
    eid = store.add_doc_entry(conn, doc["id"], "append", body, ctx.actor, ctx.via)
    after = {"entry": eid, "op": "append", "chars": len(body), "sha256": store.sha256_text(body)}
    return Result(_doc_result(doc, p, entry=eid), f"{p['name']}/{n}", None, after, changed=True)


@action("doc.set", "doc")
def doc_set(conn: sqlite3.Connection, ctx: Ctx, project: Any, name: Any, text: Any, version: Any = None) -> Result:
    """Replace a doc's whole text (goals, a review; for a log a full rewrite): one 'replace' entry,
    the earlier versions stay as history. The same text again is a no-op."""
    p, n = _doc_ref(conn, project, name)
    body = store.doc_text(text, allow_empty=True)
    doc = store.get_doc(conn, p["id"], n)
    _check_version(doc, version, f"doc {p['name']}/{n}")
    before = _doc_state(conn, doc)
    if doc is not None and store.doc_render(conn, doc["id"]) == body:
        return Result(_doc_result(doc, p, entry=None, unchanged=True), f"{p['name']}/{n}", before, before,
                      changed=False)
    doc = doc if doc is not None else store.ensure_doc(conn, p["id"], n, ctx.actor)
    eid = store.add_doc_entry(conn, doc["id"], "replace", body, ctx.actor, ctx.via)
    review_notice(conn, ctx, p, n)
    after = {"entry": eid, "op": "replace", "chars": len(body), "sha256": store.sha256_text(body)}
    return Result(_doc_result(doc, p, entry=eid), f"{p['name']}/{n}", before, after, changed=True)


def doc_sync_plan(kind: str, chunks: list[str], text: str) -> tuple[int, list[str]] | None:
    """How to make a doc whose current entries are `chunks` read `text`: None = it already does;
    else (keep, new): keep the first `keep` entries, then add `new` (keep == len(chunks): plain
    appends; less: the first of `new` is a 'replace' entry with that keep). A log keeps the longest
    run of whole entries the text starts with and cuts the rest of the text into entries
    (store.split_chunks); a body doc is replaced as a whole."""
    if "".join(chunks) == text:
        return None
    if kind == "body":
        return 0, [text]
    keep, pos = 0, 0
    while keep < len(chunks) and text.startswith(chunks[keep], pos):
        pos += len(chunks[keep])
        keep += 1
    new = store.split_chunks(text[pos:])
    if keep < len(chunks) and not new:
        new = [""]          # the text ends where an entry began: a replace with an empty body cuts the rest
    return keep, new


def merge3(base: str, ours: str, theirs: str) -> str | None:
    """Line-based 3-way merge of two texts that both changed from `base`: `ours` (the DB) and
    `theirs` (the file, hand-edited). Each side's changes are applied where they touch different
    lines of base; where both insert at the same place, ours goes first (a log: the runners'
    notes, then the hand-appended lines); the same change on both sides counts once.
    -> the merged text, None if both changed the same lines differently (a conflict)."""
    a = base.splitlines(keepends=True)

    def hunks(side: int, text: str) -> list[tuple[int, int, int, list[str]]]:
        b = text.splitlines(keepends=True)
        return [(i1, i2, side, b[j1:j2]) for tag, i1, i2, j1, j2
                in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if tag != "equal"]

    out: list[str] = []
    pos = 0
    prev: tuple[int, int, list[str]] | None = None
    for i1, i2, _side, lines in sorted(hunks(0, ours) + hunks(1, theirs), key=lambda h: (h[0], h[1], h[2])):
        if prev is not None and (i1, i2, lines) == prev:
            continue                      # both sides made the same change
        if i1 < pos:
            return None                   # overlaps the other side's change
        out.extend(a[pos:i1])
        out.extend(lines)
        pos, prev = i2, (i1, i2, lines)
    out.extend(a[pos:])
    return "".join(out)


def _sync_base(conn: sqlite3.Connection, doc: store.Row) -> str | None:
    """The file's text at the last sync mark (the merge base): stored with the mark, or (a mark
    from before file_text existed) the DB's text as of the mark's entry if it hashes the same."""
    if doc.get("file_sha256") is None:
        return None
    if doc.get("file_text") is not None and store.sha256_text(str(doc["file_text"])) == doc["file_sha256"]:
        return str(doc["file_text"])        # (an older writer moves the sha without the text: not trusted then)
    text = "".join(store.doc_chunks(conn, doc["id"], upto=int(doc["file_entry"] or 0)))
    return text if store.sha256_text(text) == doc["file_sha256"] else None


@action("doc.sync", "doc")
def doc_sync(conn: sqlite3.Connection, ctx: Ctx, project: Any, name: Any, text: Any, force: Any = False) -> Result:
    """The importer for one doc (project_docs.py: the scheduled import, the export's first step):
    make the DB take in the file `text` (hand edits), idempotently, and record the sync mark
    (the file's text and the newest entry then).
      - the file reads like the DB: in sync (the mark is moved to it);
      - the file is unchanged since the last sync but the DB moved on (doc.append/doc.set): the
        DB is ahead, nothing is imported (the export writes it out);
      - the file changed, the DB didn't: its new text is imported (appends; an in-place edit of a
        log is one 'replace' entry keeping the unchanged entries, plus the changed tail);
      - both changed: a 3-way merge against the file's text at the mark (merge3: the runners'
        notes and the hand edits touch different lines, e.g. both appended) is imported, and the
        DB is then ahead of the file ('merged'; the export writes the merged text out);
      - both changed the same lines and the import would drop DB text written since the last
        sync: Conflict, nothing written, unless force (the file wins; the dropped entries stay
        in the history)."""
    p, n = _doc_ref(conn, project, name)
    body = store.doc_text(text, allow_empty=True)
    kind = store.doc_kind(n)
    doc = store.ensure_doc(conn, p["id"], n, ctx.actor)
    chunks = store.doc_chunks(conn, doc["id"])
    last = store.doc_last_entry(conn, doc["id"])
    sha = store.sha256_text(body)
    before = {"chars": len("".join(chunks)), "entries": len(chunks), "last_entry": last}
    target = f"{p['name']}/{n}"
    plan = doc_sync_plan(kind, chunks, body)
    if plan is None:
        store.set_doc_mark(conn, doc["id"], body, last)
        return Result(_doc_result(doc, p, outcome="in_sync", appended=0), target, before, before, changed=False)
    if doc["file_sha256"] == sha:
        return Result(_doc_result(doc, p, outcome="db_ahead", appended=0), target, before, before, changed=False)
    keep, new = plan
    base = _sync_base(conn, doc)
    # the DB has text the file at the mark didn't (a note, a doc.set, or an earlier merge not yet exported)
    db_moved = ("".join(chunks) != base if base is not None else
                last > (doc["file_entry"] or 0) if doc["file_sha256"] is not None else last > 0)
    outcome = "imported"
    if db_moved and keep < len(chunks) and not _bool(force):
        merged = merge3(base, "".join(chunks), body) if base is not None else None
        if merged is None:
            raise Conflict(f"doc {target}: the file and the DB both changed the same part since the last sync "
                           f"(DB entries after #{doc['file_entry'] or 0}); export or merge by hand, or import "
                           "with force (the file wins)")
        outcome = "merged"
        merged_plan = doc_sync_plan(kind, chunks, merged)
        if merged_plan is None:          # the file's change is in the DB already
            store.set_doc_mark(conn, doc["id"], body, last)
            return Result(_doc_result(doc, p, outcome=outcome, appended=0, kept=len(chunks), replaced=False),
                          target, before, before, changed=True)
        keep, new = merged_plan
    first = True
    for chunk in new:
        replace = first and (kind == "body" or keep < len(chunks))     # a body doc changes by replace only
        last = store.add_doc_entry(conn, doc["id"], "replace" if replace else "append", chunk, ctx.actor, ctx.via,
                                   keep=keep if replace else 0)
        first = False
    store.set_doc_mark(conn, doc["id"], body, last)
    review_notice(conn, ctx, p, n)
    after = {"chars": len(store.doc_render(conn, doc["id"])), "entries": keep + len(new), "last_entry": last,
             "kept": keep, "added": len(new), "replaced": keep < len(chunks), "merged": outcome == "merged"}
    return Result(_doc_result(doc, p, outcome=outcome, appended=len(new), kept=keep,
                              replaced=keep < len(chunks)), target, before, after, changed=True)


# ---------------------------------------------------------------- the decision store (D-242, phase 4b)

KEYWORD_MAX = 60


@action("decision.import", "decision")
def decision_import(conn: sqlite3.Connection, ctx: Ctx, path: Any, force: Any = False) -> Result:
    """Mirror DECISIONS.md (`path`, read only) into the decisions table (decisions.py parses it):
    new entries added, changed ones rewritten (the task-manager's keyword changes kept), entries
    no longer in the file removed. Idempotent: an unchanged file changes nothing (no audit row).
    The audit row names the ids and holds a line diff of each changed entry and the full text of
    each removed one (D-158: every change in the audit log). The result's "no_status" names every
    entry with no Status line (decisions.state_of defaults those to "active", or infers a state
    from a title marker like "SUPERSEDED" -- the list still names them all, so the plain ones stay
    visible without editing DECISIONS.md). Refused (decisions.SourceError): a file without entries
    or with a duplicate id, and one that would remove more than half of the stored entries
    (force=True allows that). An entry whose text is byte-for-byte the same as what's stored but
    whose derived fields (e.g. state) now come out different -- the derivation logic changed, not
    DECISIONS.md -- is 'restated': updated, with an audit row naming the fields that changed. A
    truly unchanged entry still writes nothing."""
    import decisions as dec
    src = os.path.abspath(store._text(path, "path", required=True))
    try:
        text, sha = dec.read_source(src)
    except (OSError, UnicodeDecodeError) as err:
        raise dec.SourceError(f"can't read {src}: {err}") from err
    entries = dec.parse(text)
    dec.check_entries(entries, src)
    have = {r["id"]: r for r in store.list_decision_entries(conn)}
    ids = {e.id for e in entries}
    gone = [i for i in have if i not in ids]
    if have and len(gone) * 2 > len(have) and force is not True:
        raise dec.SourceError(f"{src}: the import would remove {len(gone)} of {len(have)} decisions "
                              f"({', '.join(gone[:10])}{' ...' if len(gone) > 10 else ''}); "
                              "check the file, or import with force")
    added: list[str] = []
    changed: dict[str, str] = {}
    restated: dict[str, list[str]] = {}
    moved = 0
    for n, e in enumerate(entries):
        row = dict(e.row(), ord=n)
        outcome, old = store.put_decision_entry(conn, row, ctx.actor)
        if outcome == "added":
            added.append(e.id)
        elif outcome == "changed" and old is not None:
            changed[e.id] = "\n".join(difflib.unified_diff(old["body"].split("\n"), e.body.split("\n"),
                                                           lineterm="", n=0))
        elif outcome == "restated" and old is not None:
            restated[e.id] = [k for k in store.DECISION_DERIVED_FIELDS if old.get(k) != row.get(k)]
        elif outcome == "moved":
            moved += 1
    removed = {i: store.delete_decision_entry(conn, i)["body"] for i in gone}
    no_status = [e.id for e in entries if not e.has_status]   # visible without editing DECISIONS.md
    result = {"file": src, "sha256": sha, "entries": len(entries), "added": added, "changed": list(changed),
              "restated": restated, "removed": list(removed), "moved": moved, "no_status": no_status,
              "unchanged": len(entries) - len(added) - len(changed) - len(restated) - moved}
    after = {"sha256": sha, "entries": len(entries), "added": added, "changed": changed,
             "restated": restated, "removed": list(removed), "moved": moved}
    return Result(result, os.path.basename(src), {"removed_text": removed}, after,
                  changed=bool(added or changed or restated or removed or moved))


def _keyword(value: Any) -> str:
    k = " ".join(store._text(value, "keyword", required=True).split())
    if len(k) > KEYWORD_MAX or "," in k:
        raise ValueError(f"a keyword is at most {KEYWORD_MAX} characters, without commas: {value!r}")
    return k


@action("decision.keywords", "decision")
def decision_keywords(conn: sqlite3.Connection, ctx: Ctx, decision: Any, op: Any, keywords: Any,
                      version: Any = None) -> Result:
    """Add or remove keywords of one decision (D-242: the task-manager keeps them current, e.g.
    when a lookup missed or a topic was renamed). Stored apart from the source's Keywords line
    (store.decision_keywords), so a re-import keeps them. Removing a keyword the decision doesn't
    have is an error; adding one it has is a no-op."""
    store._enum(op, ("add", "remove"), "op")
    words = [_keyword(k) for k in (keywords if isinstance(keywords, list) else [keywords])]
    if not words:
        raise ValueError("keywords: at least one")
    cur = store.get_decision_entry(conn, decision)
    if cur is None:
        raise store.NotFound(f"no decision {decision}")
    _check_version(cur, version, f"decision {cur['id']}")
    source = {k.lower() for k in cur["keywords"]}
    added = list(cur["keywords_added"])
    removed = list(cur["keywords_removed"])
    for k in words:
        kl = k.lower()
        in_added = [a for a in added if a.lower() == kl]
        if op == "add":
            if kl in removed:
                removed.remove(kl)
            elif kl not in source and not in_added:
                added.append(k)
        elif in_added:
            added = [a for a in added if a.lower() != kl]
        elif kl in source and kl not in removed:
            removed.append(kl)
        else:
            raise ValueError(f"{cur['id']} has no keyword {k!r} (its keywords: {', '.join(cur['effective_keywords'])})")
    before = {"keywords": cur["effective_keywords"]}
    if (added, sorted(removed)) == (cur["keywords_added"], sorted(cur["keywords_removed"])):
        return Result({"id": cur["id"], "keywords": cur["effective_keywords"], "version": cur["version"]},
                      cur["id"], before, before, changed=False)
    after = store.set_decision_keywords(conn, cur["id"], added, removed, ctx.actor)
    return Result({"id": after["id"], "keywords": after["effective_keywords"], "version": after["version"]},
                  after["id"], before, {"keywords": after["effective_keywords"]})


# ---------------------------------------------------------------- dashboard browser sessions (phase 6a W5)
#
# docs/phase6a_plan.md §3.5, design §9.5 (D-162, D-179). dashboard/sessions.py holds the cookie and
# makes the ids; this is where a logged-in session row is written. Login, rotation (step-up, factor
# change), logout and revocation are audited actions (auth.session_*; the audit row names row ids,
# never a hash). The last_seen touch is the row's own record, with no audit row (as telemetry, §7.3;
# an interpretation gate 8a checks), at most once a minute per session (auth_session_touch). A login
# also ends the user's sessions past a lifetime ('idle' / 'expired'), at auth_max_sessions_per_user the
# oldest ones ('evicted'), and opportunistically prunes sessions past their end plus a short retention
# (finding 2 of the W5 review; SESSION_PRUNE_RETENTION, as IDEMPOTENCY_TTL is pruned on a write rather
# than from a periodic job). A pre-session (an anonymous browser before login) never reaches this
# table: finding 1 of the same review found that a row per cookieless GET /me let a flood hold write
# locks (D-235), so it is now a stateless, server-key-signed, expiring token (dashboard.sessions).

# the login factors dashboard.auth.factors knows (test_dashboard_sessions keeps the two equal; actions.py
# doesn't import the dashboard package, which the live processes never load)
AUTH_FACTORS = ("password", "totp", "recovery_code", "passkey_hw", "passkey_synced", "oidc")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
USER_AGENT_MAX = 200
TOUCH_EVERY = timedelta(minutes=1)
SESSION_PRUNE_RETENTION = timedelta(days=7)


class AuthLifetimes:
    """The session settings in force (auth_*), read together. A saved value that fails its check is
    an error here, never the code default in its place (D-235): the session would otherwise live
    longer or shorter than the owner set."""
    def __init__(self, conn: sqlite3.Connection) -> None:
        def get(key: str) -> int:
            value, source = effective_setting(conn, key)
            if source.startswith("invalid"):
                raise ValueError(f"setting {key} is {source}: fix or reset it")
            return int(value)
        self.idle = timedelta(minutes=get("auth_idle_timeout_min"))
        self.absolute = timedelta(hours=get("auth_absolute_lifetime_h"))
        self.max_sessions = get("auth_max_sessions_per_user")
        self.reauth = timedelta(minutes=get("auth_reauth_window_min"))
        self.remember_device = timedelta(days=get("auth_remember_device_days"))


def auth_session_state(row: Mapping[str, Any], lifetimes: AuthLifetimes, now: datetime | None = None) -> str:
    """'live', 'ended' (ended_at set), 'idle' (no request within auth_idle_timeout_min; last_seen is
    written once a minute, so up to a minute early) or 'expired' (older than auth_absolute_lifetime_h,
    counted from the login)."""
    if row["ended_at"] is not None:
        return "ended"
    t = now or store._utcnow()
    if t - _when(row["created_at"]) > lifetimes.absolute:
        return "expired"
    if t - _when(row["last_seen_at"]) > lifetimes.idle:
        return "idle"
    return "live"


def _when(ts: str) -> datetime:
    dt = store.parse_iso(ts)
    if dt is None:
        raise ValueError("a session row without a time")
    return dt


def _sha256_hex(v: Any, what: str) -> str:
    if not isinstance(v, str) or not SHA256_RE.match(v):
        raise ValueError(f"{what} must be a sha256 hex digest")
    return v


def _user_id(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ValueError(f"user_id must be a positive integer, got {v!r}")
    return v


def _factor_list(v: Any) -> list[str]:
    if not isinstance(v, list) or not v or len(set(v)) != len(v) or any(f not in AUTH_FACTORS for f in v):
        raise ValueError(f"factors must be a non-empty list of distinct names from {', '.join(AUTH_FACTORS)}")
    return list(v)


def _user_agent(v: Any) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("user_agent must be a string")
    return v[:USER_AGENT_MAX]


def _acting_user(ctx: Ctx, user_id: int) -> None:
    """A session write about the actor's own session: the actor is that user."""
    if ctx.actor != f"user:{user_id}":
        raise Forbidden(f"only user:{user_id} acts on this session (actor {ctx.actor})")


def _open_session(conn: sqlite3.Connection, id_sha256: Any) -> store.Row:
    row = store.get_auth_session(conn, _sha256_hex(id_sha256, "id_sha256"))
    if row is None:
        raise store.NotFound("no such session")
    if row["ended_at"] is not None:
        raise store.InvalidTransition(f"session {row['id']} has ended ({row['end_reason']})")
    return row


@action("auth.session_login", "auth_session")
def auth_session_login(conn: sqlite3.Connection, ctx: Ctx, user_id: Any, id_sha256: Any, csrf_sha256: Any,
                       factors: Any, replaces: Any = None, user_agent: Any = None) -> Result:
    """A completed login (the factor rule is the caller's, dashboard.auth.factors): a new full session
    with a fresh id. `replaces`: the id hash of the session this browser had (its pre-session), ended
    'rotated'. -> {session_id, ended: {row id: reason}}."""
    uid = _user_id(user_id)
    _acting_user(ctx, uid)
    new_hash, csrf = _sha256_hex(id_sha256, "id_sha256"), _sha256_hex(csrf_sha256, "csrf_sha256")
    done, agent = _factor_list(factors), _user_agent(user_agent)
    lifetimes = AuthLifetimes(conn)
    now = store._utcnow()
    store.prune_ended_sessions(conn, store.iso_ms(now - SESSION_PRUNE_RETENTION),
                               store.iso_ms(now - lifetimes.absolute - SESSION_PRUNE_RETENTION))
    ended: dict[str, str] = {}
    if replaces is not None:
        old = store.get_auth_session(conn, _sha256_hex(replaces, "replaces"))
        if old is not None and store.end_auth_session(conn, old["id"], "rotated"):
            ended[str(old["id"])] = "rotated"
    still_open = []
    for row in store.open_auth_sessions(conn, uid):
        state = auth_session_state(row, lifetimes)
        if state in ("idle", "expired"):
            store.end_auth_session(conn, row["id"], state)
            ended[str(row["id"])] = state
        else:
            still_open.append(row)
    for row in still_open[:max(0, len(still_open) + 1 - lifetimes.max_sessions)]:
        store.end_auth_session(conn, row["id"], "evicted")
        ended[str(row["id"])] = "evicted"
    sid = store.add_auth_session(conn, new_hash, uid, csrf, factors=done, user_agent=agent,
                                 authenticated_at=store.now_iso())
    return Result({"session_id": sid, "ended": ended}, sid, None,
                  {"user_id": uid, "factors": done, "ended": ended})


@action("auth.session_rotate", "auth_session")
def auth_session_rotate(conn: sqlite3.Connection, ctx: Ctx, id_sha256: Any, new_id_sha256: Any,
                        new_csrf_sha256: Any, factors: Any = None, authenticated: Any = False) -> Result:
    """A new id for a live full session (a step-up or a factor change, §3.5): the old row ends
    'rotated', the new one keeps the login time (the absolute lifetime goes on counting).
    `factors`: the session's factors from now on (default: unchanged); `authenticated`: this was a
    full authentication (a step-up), which restarts the re-auth window. -> {session_id}."""
    row = _open_session(conn, id_sha256)
    if row["kind"] != "full":
        raise store.InvalidTransition("a pre-session has no login to rotate")
    _acting_user(ctx, row["user_id"])
    if auth_session_state(row, AuthLifetimes(conn)) != "live":
        raise store.InvalidTransition(f"session {row['id']} is past its lifetime")
    if not isinstance(authenticated, bool):
        raise ValueError("authenticated must be true or false")
    done = row["factors"] if factors is None else _factor_list(factors)
    store.end_auth_session(conn, row["id"], "rotated")
    sid = store.add_auth_session(conn, _sha256_hex(new_id_sha256, "new_id_sha256"), row["user_id"],
                                 _sha256_hex(new_csrf_sha256, "new_csrf_sha256"), factors=done,
                                 user_agent=row["user_agent"], created_at=row["created_at"],
                                 authenticated_at=store.now_iso() if authenticated else row["authenticated_at"])
    return Result({"session_id": sid}, sid, {"session_id": row["id"], "factors": row["factors"]},
                  {"session_id": sid, "factors": done, "step_up": authenticated})


@action("auth.session_logout", "auth_session")
def auth_session_logout(conn: sqlite3.Connection, ctx: Ctx, id_sha256: Any) -> Result:
    """The user signs out of this session. -> {session_id}."""
    row = _open_session(conn, id_sha256)
    if row["kind"] != "full":
        raise store.InvalidTransition("a pre-session has no login to sign out of")
    _acting_user(ctx, row["user_id"])
    store.end_auth_session(conn, row["id"], "logout")
    return Result({"session_id": row["id"]}, row["id"], {"ended": None}, {"ended": "logout"})


@action("auth.session_revoke", "auth_session")
def auth_session_revoke(conn: sqlite3.Connection, ctx: Ctx, session_id: Any = None, user_id: Any = None) -> Result:
    """End one session (F11) or every open session of a user (reset-auth, a removed user). Who may is
    5a's permission matrix (an owner, or the user for their own). -> {ended: [row ids]}."""
    if (session_id is None) == (user_id is None):
        raise ValueError("give exactly one of session_id and user_id")
    if session_id is not None:
        if isinstance(session_id, bool) or not isinstance(session_id, int):
            raise ValueError("session_id must be an integer")
        row = store.get_auth_session_by_id(conn, session_id)
        if row is None or row["kind"] != "full":
            raise store.NotFound(f"no session {session_id}")
        rows = [row] if row["ended_at"] is None else []
        target: object = session_id
    else:
        rows = store.open_auth_sessions(conn, _user_id(user_id))
        target = f"user:{user_id}"
    ended = [r["id"] for r in rows if store.end_auth_session(conn, r["id"], "revoked")]
    return Result({"ended": ended}, target, {"open": ended}, {"open": []}, changed=bool(ended))


def auth_session_touch(conn: sqlite3.Connection, session_id: int) -> bool:
    """last_seen_at = now when it is a minute old or more (no audit row). -> whether it wrote."""
    if isinstance(session_id, bool) or not isinstance(session_id, int):
        raise ValueError("session_id must be an integer")

    def run() -> bool:
        with store.transaction(conn):
            return store.touch_auth_session(conn, session_id, store.iso_ms(store._utcnow() - TOUCH_EVERY))
    return store.retrying(run, conn)


# ---------------------------------------------------------------- setup codes (phase 6a W7)
#
# docs/phase6a_plan.md §3.7, design §9.3 / §10.4. dashboard/setup.py generates the raw code, hashes it
# and (for 'first_owner') races to claim it and create the first owner in one transaction; this module
# never sees a raw code, only its sha256 (mint_code's caller hashes it) and a row id (claim_code's
# caller already matched the submission against the open row in constant time, store.open_setup_code).
# The actual 'first_owner' -> a user row is 5a's (store.setup_codes has no users FK yet, §7.1): until
# then dashboard.setup's OwnerRegistry hook raises, same as sessions.py's no_roles.

SETUP_CODE_TTL_MAX_H = 24 * 30


def _setup_kind(v: Any) -> str:
    return store._enum(v, store.SETUP_CODE_KINDS, "kind")


def _setup_code_id(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ValueError(f"id must be a positive integer, got {v!r}")
    return v


@action("setup.mint_code", "setup_code")
def setup_mint_code(conn: sqlite3.Connection, ctx: Ctx, kind: Any, code_hash: Any, ttl_hours: Any,
                    user_id: Any = None) -> Result:
    """A fresh one-time code, already hashed by the caller: dashboard/setup.py generates the raw
    code and keeps it only long enough to print or show it once, never passing it to this module or
    into the audit log. 'first_owner' (user_id None, minted while the install has no owner yet --
    this module can't check that itself without 5a's users table, so the caller decides when) or
    'reset' (user_id set, afclaude admin reset-auth). Supersedes any other open code of the same
    (kind, user_id) first, so at most one is ever live. -> {id, kind, user_id, expires_at}."""
    k, h = _setup_kind(kind), _sha256_hex(code_hash, "code_hash")
    uid = None if user_id is None else _user_id(user_id)
    if isinstance(ttl_hours, bool) or not isinstance(ttl_hours, (int, float)) or not 0 < ttl_hours <= SETUP_CODE_TTL_MAX_H:
        raise ValueError(f"ttl_hours must be a number in (0, {SETUP_CODE_TTL_MAX_H}]")
    expires_at = store.iso_ms(store._utcnow() + timedelta(hours=ttl_hours))
    store.supersede_open_setup_codes(conn, k, uid)
    cid = store.add_setup_code(conn, k, uid, h, expires_at)
    return Result({"id": cid, "kind": k, "user_id": uid, "expires_at": expires_at}, cid, None,
                  {"kind": k, "user_id": uid, "expires_at": expires_at})


@action("setup.claim_code", "setup_code")
def setup_claim_code(conn: sqlite3.Connection, ctx: Ctx, id: Any, kind: Any, user_id: Any = None) -> Result:
    """Spend one setup code by row id: the caller already matched the submitted code against this
    row in constant time (dashboard.setup.SetupFlow, store.open_setup_code) and names the (kind,
    user_id) it expects, so a claim can't be replayed against a different code than the one it was
    checked against. Single use and not expired; store.claim_setup_code's UPDATE ... WHERE used_at IS
    NULL is the race guard (two browsers claiming the same code: one winner). -> {id, kind, user_id}."""
    cid, k = _setup_code_id(id), _setup_kind(kind)
    uid = None if user_id is None else _user_id(user_id)
    row = store.get_setup_code_by_id(conn, cid)
    if row is None or row["kind"] != k or row["user_id"] != uid:
        raise store.NotFound("no such setup code")
    if row["used_at"] is not None:
        raise store.InvalidTransition("this code has already been used")
    now = store.now_iso()
    if store._utcnow() >= _when(row["expires_at"]):
        raise store.InvalidTransition("this code has expired")
    if not store.claim_setup_code(conn, cid, now):
        raise store.InvalidTransition("this code has already been used")   # lost the race
    return Result({"id": cid, "kind": k, "user_id": uid}, cid, {"used_at": None}, {"used_at": now})


# ---------------------------------------------------------------- telemetry appenders (§7.3, phase 2c, D-161)
#
# Append-only telemetry rows (usage samples, the weekly series, session windows, the forecast
# log, Haiku judgements, usage reports, the watcher's run readings, the stage-ETA log) are
# written through these registered appenders, not perform(): each row carries ts, actor, via
# and recorded_at and is immutable by trigger (store.TELEMETRY_SCHEMA), so the row is its own
# audit record and no audit_log row is written for it. The derived records that are recomputed
# in place (a run's fill-time row until it is final, a weekly cycle, the user model) go through
# put_record: an upsert carrying the same fields plus updated_at and a trigger-bumped version,
# also without an audit row -- they are recomputable from the append-only rows (an
# interpretation of §7.3 that gate 8a checks). The user model, which the usage review writes,
# is the exception: user_model.set is an audited action (its history); so is cost_model.set.
#
# Every row is the JSON line its producer used to write to its (now retired, D-161) file,
# stored as given; the importer of the legacy files dedupes on its sha256.
# Errors: ValueError (unknown kind, bad actor/via/account, not a JSON object, too big);
# database errors take the DB error path's retry (store.retrying) and then raise a DBError;
# the producers' door, telemetry.py, records the failed rows in the fallback file (§7.8).

TELEMETRY_LINE_MAX = 1024 * 1024


def _ts_of(field: str) -> Callable[[Mapping[str, Any]], str]:
    def get(row: Mapping[str, Any]) -> str:
        return store.telemetry_ts(row.get(field))
    return get


def _flag_field(field: str) -> Callable[[Mapping[str, Any]], bool]:
    def get(row: Mapping[str, Any]) -> bool:
        return row.get(field) is True
    return get


def _never(row: Mapping[str, Any]) -> bool:
    return False


def _sample_stale(row: Mapping[str, Any]) -> bool:
    """usage_stale.row_stale: the usage meters of this sampler row must not be used."""
    import usage_stale
    row_stale: Callable[[dict[str, Any]], object] = getattr(usage_stale, "row_stale")   # untyped module
    return bool(row_stale(dict(row)))


class Appender:
    """A registered telemetry kind: its table, and how a row's ts, stale flag (append-only
    kinds) or record key and final flag (record kinds) are derived from the row."""
    def __init__(self, kind: str, table: str, ts: Callable[[Mapping[str, Any]], str],
                 stale: Callable[[Mapping[str, Any]], bool] = _never,
                 key: Callable[[Mapping[str, Any]], str] | None = None,
                 final: Callable[[Mapping[str, Any]], bool] = _never) -> None:
        self.kind, self.table, self.ts, self.stale, self.key, self.final = kind, table, ts, stale, key, final
        self.record = key is not None
        if table not in (store.TELEMETRY_DOCS if self.record else store.TELEMETRY_APPEND):
            raise ValueError(f"{kind}: {table} is not a {'record' if self.record else 'append-only'} table")


APPENDERS: dict[str, Appender] = {}


def register_appender(a: Appender) -> Appender:
    APPENDERS[a.kind] = a
    return a


def _field_key(field: str) -> Callable[[Mapping[str, Any]], str]:
    def get(row: Mapping[str, Any]) -> str:
        v = row.get(field)
        if not isinstance(v, str) or not v:
            raise ValueError(f"the record has no {field}")
        return v
    return get


def _model_key(row: Mapping[str, Any]) -> str:
    return "model"


def _fitted_ts(row: Mapping[str, Any]) -> str:
    """The user model's time: when it was fitted (else its data end, else now)."""
    for f in ("fitted_at", "user_data_to"):
        if row.get(f):
            return store.telemetry_ts(row.get(f))
    return store.now_iso()


for _a in (Appender("usage.sample", "usage_samples", _ts_of("at"), _sample_stale),
           Appender("usage.series", "usage_weekly_series", _ts_of("at"), _flag_field("stale")),
           Appender("usage.session_window", "usage_session_windows", _ts_of("window_end")),
           Appender("usage.forecast", "forecast_log", _ts_of("at")),
           Appender("usage.haiku", "haiku_judgements", _ts_of("at")),
           Appender("usage.report", "usage_reports", _ts_of("at")),
           Appender("usage.run_reading", "usage_run_readings", _ts_of("at"), _flag_field("stale")),
           Appender("stage_eta.prediction", "stage_eta_log", _ts_of("at")),
           Appender("usage.weekly_cycle", "weekly_cycles", _ts_of("reset_at"), key=_field_key("reset_at")),
           Appender("usage.run", "usage_runs", _ts_of("start"), key=_field_key("run_id"),
                    final=_flag_field("final")),
           Appender("usage.user_model", "user_model", _fitted_ts, key=_model_key),
           # usage_reviews (§7.2): one record per review launch (usage_review.py), final once done
           Appender("usage.review", "usage_reviews", _ts_of("started_at"), key=_field_key("review_id"),
                    final=_flag_field("final")),
           # cost_model: the context cost model of compact_timing.py (D-237/D-238), one record
           Appender("usage.cost_model", "cost_model", _fitted_ts, key=_model_key)):
    register_appender(_a)


def _appender(kind: object, record: bool) -> Appender:
    a = APPENDERS.get(kind) if isinstance(kind, str) else None
    if a is None:
        raise ValueError(f"unknown telemetry kind {kind!r}")
    if a.record != record:
        raise ValueError(f"{a.kind} is {'a record' if a.record else 'an append-only'} kind: use "
                         f"{'put_record' if a.record else 'append'}")
    return a


def _check_writer(actor: object, via: object, account_id: object) -> None:
    if via not in VIAS:
        raise ValueError(f"via must be one of {'|'.join(VIAS)}, got {via!r}")
    if not isinstance(actor, str) or not ACTOR_RE.match(actor):
        raise ValueError(f"bad actor {actor!r}")
    if not isinstance(account_id, str) or not account_id:
        raise ValueError(f"bad account_id {account_id!r}")


def _parse_line(line: object) -> tuple[str, dict[str, Any]]:
    """-> (the line without its trailing newline, the parsed object)."""
    if not isinstance(line, str):
        raise ValueError("a telemetry row is a JSON text line")
    text = line.rstrip("\n")
    if not text.strip() or "\n" in text:
        raise ValueError("a telemetry row is one non-empty line")
    if len(text) > TELEMETRY_LINE_MAX:
        raise ValueError(f"a telemetry row is too big ({len(text)} > {TELEMETRY_LINE_MAX} characters)")
    try:
        row = json.loads(text)
    except ValueError as e:
        raise ValueError(f"a telemetry row is not JSON ({e})") from e
    if not isinstance(row, dict):
        raise ValueError("a telemetry row is a JSON object")
    return text, row


def _check_account(conn: sqlite3.Connection, account_id: str) -> None:
    if conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is None:
        raise ValueError(f"unknown account {account_id!r}")


def append_many(conn: sqlite3.Connection, kind: str, lines: Iterable[object], *, actor: str, via: str,
                account_id: str = store.DEFAULT_ACCOUNT) -> dict[str, int]:
    """Append rows of an append-only kind in one transaction; rows already stored are skipped,
    lines that aren't a JSON object are counted (never stored, never raised: the importer
    reports them). -> {"inserted", "duplicate", "invalid"}."""
    a = _appender(kind, record=False)
    _check_writer(actor, via, account_id)
    parsed: list[tuple[str, str, bool]] = []
    invalid = 0
    for line in lines:
        try:
            text, row = _parse_line(line)
            parsed.append((text, a.ts(row), a.stale(row)))
        except ValueError:
            invalid += 1

    def run() -> dict[str, int]:
        with store.transaction(conn):
            _check_account(conn, account_id)
            n = sum(store.insert_telemetry(conn, a.table, text, ts, stale, actor, via, account_id)
                    for text, ts, stale in parsed)
        return {"inserted": n, "duplicate": len(parsed) - n, "invalid": invalid}
    return store.retrying(run, conn)


def append(conn: sqlite3.Connection, kind: str, line: object, *, actor: str, via: str,
           account_id: str = store.DEFAULT_ACCOUNT) -> bool:
    """Append one row (a JSON object line) of an append-only telemetry kind. -> stored (False:
    the same row was stored before). A line that isn't a JSON object is a ValueError."""
    _parse_line(line)
    return append_many(conn, kind, [line], actor=actor, via=via, account_id=account_id)["inserted"] == 1


def put_records(conn: sqlite3.Connection, kind: str, lines: Iterable[object], *, actor: str, via: str,
                account_id: str = store.DEFAULT_ACCOUNT) -> dict[str, int]:
    """Insert or update records of a record kind in one transaction (each line one record's
    JSON object). -> {"inserted", "updated", "same", "invalid"}."""
    a = _appender(kind, record=True)
    _check_writer(actor, via, account_id)
    keyfn = a.key
    if keyfn is None:   # a record kind always has a key (Appender.record)
        raise ValueError(f"{a.kind} has no record key")
    parsed: list[tuple[str, str, str, bool]] = []
    invalid = 0
    for line in lines:
        try:
            text, row = _parse_line(line)
            parsed.append((keyfn(row), text, a.ts(row), a.final(row)))
        except ValueError:
            invalid += 1

    def run() -> dict[str, int]:
        out = {"inserted": 0, "updated": 0, "same": 0, "invalid": invalid}
        with store.transaction(conn):
            _check_account(conn, account_id)
            for key, text, ts, final in parsed:
                out[store.put_telemetry_record(conn, a.table, key, text, ts, final, actor, via, account_id)] += 1
        return out
    return store.retrying(run, conn)


def put_record(conn: sqlite3.Connection, kind: str, line: object, *, actor: str, via: str,
               account_id: str = store.DEFAULT_ACCOUNT) -> str:
    """Insert or update one record. -> 'inserted' | 'updated' | 'same'. A line that isn't a
    JSON object, or a record without its key, is a ValueError."""
    keyfn = _appender(kind, record=True).key
    _, row = _parse_line(line)
    if keyfn is not None:
        keyfn(row)
    r = put_records(conn, kind, [line], actor=actor, via=via, account_id=account_id)
    return next(k for k in ("inserted", "updated", "same") if r[k])


@action("user_model.set", "user_model")
def user_model_set(conn: sqlite3.Connection, ctx: Ctx, doc: Any) -> Result:
    """Replace the user model (the usage review's user-specific predictor parameters; the DB is
    its only copy, D-161: `python3 telemetry.py user-model set`). Unlike the runners' records it
    is audited, with the old and the new document: the audit log is its history."""
    if not isinstance(doc, Mapping):
        raise ValueError("the user model is a JSON object")
    a = _appender("usage.user_model", record=True)
    old = conn.execute(f"SELECT payload FROM {a.table} WHERE account_id=? AND doc_key='model'",
                       (store.DEFAULT_ACCOUNT,)).fetchone()
    before = json.loads(old[0]) if old is not None else None
    line = json.dumps(dict(doc), ensure_ascii=False)
    r = put_record(conn, "usage.user_model", line, actor=ctx.actor, via=ctx.via)
    return Result({"status": r, "doc": dict(doc)}, "model", {"doc": before}, {"doc": dict(doc)}, changed=r != "same")


@action("cost_model.set", "cost_model")
def cost_model_set(conn: sqlite3.Connection, ctx: Ctx, doc: Any) -> Result:
    """Replace the task-manager's context cost model (compact_timing.py, D-237/D-238: the
    session-% weights per token kind that the compaction break-even and the quickview's re-read
    costs come from; `python3 compact_timing.py fit --store`, run by the monthly usage review).
    Checked by compact_timing.validate; audited with the old and the new document (the audit log
    is its history), like user_model.set."""
    import compact_timing
    new = compact_timing.validate(doc)
    a = _appender("usage.cost_model", record=True)
    old = conn.execute(f"SELECT payload FROM {a.table} WHERE account_id=? AND doc_key='model'",
                       (store.DEFAULT_ACCOUNT,)).fetchone()
    before = json.loads(old[0]) if old is not None else None
    r = put_record(conn, "usage.cost_model", json.dumps(new, ensure_ascii=False), actor=ctx.actor, via=ctx.via)
    return Result({"status": r, "doc": new}, "model", {"doc": before}, {"doc": new}, changed=r != "same")


# ---------------------------------------------------------------- runner state (§7.2/§7.3, phase 2d, D-161)
#
# The runners' state files (runner_state.py maps them) are written through state_save: one
# transaction per save, one audit_log row naming the changed paths (actor runner:<component>;
# not the values: the sampler's offsets change every 15 min, and the rows carry actor, via,
# updated_at and a trigger-bumped version). A state is stored as one row per top-level key, or
# per entry of its "split" parts (the maps updated entry by entry), so two writers that loaded
# the state, changed different entries and saved one after the other both keep their changes:
# with the `base` the writer loaded (merge), only the paths it changed or removed since are
# written; without one (the writer read the file or built a new state) its top-level values
# replace the DB's but split-part entries are only upserted ('file' mode, see state_save). A
# path is deleted only where the runner removed it from its state (or the importer replaces the
# DB copy with the file), and only in a component whose file semantics delete (StateComponent.deletes).
# The log lines go to app_log (append-only, rows immutable by trigger), the at-shim's jobs to
# scheduled_jobs (audited), own_sessions.txt to driven_sessions (kind 'own', audited).

class StateComponent:
    """A runner state file: its actor, the parts stored entry by entry, and whether the runner
    deletes from it (consumed deferrals, pruned dedup keys, a popped test entry)."""
    def __init__(self, name: str, actor: str, split: tuple[str, ...] = (), deletes: bool = True) -> None:
        if not ACTOR_RE.match(actor):
            raise ValueError(f"bad actor {actor!r}")
        self.name, self.actor, self.split, self.deletes = name, actor, split, deletes


STATE_COMPONENTS: dict[str, StateComponent] = {c.name: c for c in (
    StateComponent("keepalive", "runner:keepalive", ("handled", "fires", "limit_hits", "run_stops", "compactions",
                                                      "window_pings")),
    StateComponent("keepalive.deferred", "runner:keepalive"),        # one row per postponed window start
    StateComponent("keepalive.fillup", "runner:keepalive"),          # plan, test
    StateComponent("keepalive.usage_refresh", "runner:keepalive", ("window_start",)),
    StateComponent("dispatcher", "runner:dispatcher", ("sessions", "handled", "starts", "alerted", "rc_held")),
    StateComponent("sampler", "runner:sampler", ("offsets", "sessions")),
    StateComponent("usage_review", "runner:usage_review", deletes=False),
    StateComponent("nightly_tasks", "runner:nightly_tasks", ("tasks",), deletes=False),   # D-254/D-261: one row per task
    StateComponent("runner", "runner"),       # the resident runner: boot, exit, drain (runner.py)
)}
# "db": store.TimedConnection's warnings about write transactions held too long, from any process
LOG_COMPONENTS = ("keepalive", "dispatcher", "sampler", "watchdog", "host_exec", "at_shim", "runner", "db",
                  "runner_shadow")   # runner_shadow: phase 3a S2-S7 (runner.py shadow_log)
LOG_LEVELS = ("info", "warn", "error")
STATE_MARK = "state_file:"          # meta: the state file as the last DB save saw it (mtime_ns:size)
STATE_AUDIT_PATHS = 20              # changed paths named in a save's audit row


def state_component(name: object) -> StateComponent:
    c = STATE_COMPONENTS.get(name) if isinstance(name, str) else None
    if c is None:
        raise ValueError(f"unknown runner state component {name!r}")
    return c


def _state_json(value: object) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def state_flatten(component: str, doc: object) -> dict[str, str]:
    """A state (a JSON object) -> {path: payload}: path = JSON [key] or [split part, entry]."""
    c = state_component(component)
    if not isinstance(doc, Mapping):
        raise ValueError(f"{component}: a state is a JSON object")
    out: dict[str, str] = {}
    for k, v in doc.items():
        if not isinstance(k, str):
            raise ValueError(f"{component}: state keys are text, got {k!r}")
        if k in c.split and isinstance(v, Mapping):
            for e, ev in v.items():
                out[_state_json([k, str(e)])] = _state_json(ev)
        else:
            out[_state_json([k])] = _state_json(v)
    return out


def state_unflatten(component: str, rows: Mapping[str, str]) -> dict[str, Any]:
    """{path: payload} (in row order) -> the state; the split parts are always there ({})."""
    c = state_component(component)
    doc: dict[str, Any] = {p: {} for p in c.split}
    for path, payload in rows.items():
        keys = json.loads(path)
        value = json.loads(payload)
        if len(keys) == 1:
            doc[keys[0]] = value
        else:
            part = doc.get(keys[0])
            if not isinstance(part, dict):
                part = doc[keys[0]] = {}
            part[keys[1]] = value
    return doc


STATE_MODES = ("merge", "file", "replace")
STATE_MARKS_KEPT = 8                # recent file marks of DB saves (a writer's commit can land after the next one's)


def state_save(conn: sqlite3.Connection, component: str, doc: object, *, base: Optional[Mapping[str, str]] = None,
               mode: str = "merge", actor: str, via: str, account_id: str = store.DEFAULT_ACCOUNT,
               file_mark: Optional[str] = None) -> dict[str, int]:
    """Save a runner state in one transaction. mode:
      merge   (a state loaded from the DB, `base` = the rows it was loaded from): write only the
              paths this writer changed or removed since; other writers' changes stay;
      file    (a state read from the file or built anew): the top-level values become the
              given ones (removed ones deleted), the entries of the split parts are upserted
              but never deleted (other writers' entries stay; a pruned entry goes with the next
              merge save of a writer that removes it);
      replace (the importer): the DB copy becomes exactly the given state.
    file_mark: the state file's mark right after the runner wrote it (kept with the last few:
    the readers use the DB copy only while the file carries one of them). A path removed in a
    component that never deletes is a ValueError (nothing written).
    -> {"inserted", "updated", "same", "deleted"}."""
    c = state_component(component)
    _check_writer(actor, via, account_id)
    if mode not in STATE_MODES or (mode == "merge") != (base is not None):
        raise ValueError(f"bad state save mode {mode!r} (merge needs the base, the others none)")
    rows = state_flatten(component, doc)

    def run() -> dict[str, int]:
        out = {"inserted": 0, "updated": 0, "same": 0, "deleted": 0}
        changed: list[str] = []
        with store.transaction(conn):
            _check_account(conn, account_id)
            current = store.state_rows(conn, component, account_id)
            if base is not None:
                todo = {p: v for p, v in rows.items() if base.get(p) != v}
                gone = [p for p in base if p not in rows and p in current]
            else:
                todo = {p: v for p, v in rows.items() if current.get(p) != v}
                gone = [p for p in current if p not in rows and (mode == "replace" or len(json.loads(p)) == 1)]
            if gone and not c.deletes:
                raise ValueError(f"{component}: its runner never deletes, but {len(gone)} path(s) are gone "
                                 f"({', '.join(gone[:3])})")
            for p, v in todo.items():
                r = store.state_set(conn, component, p, v, actor, via, account_id)
                out[r] += 1
                if r != "same":
                    changed.append(p)
            for p in gone:
                if store.state_delete(conn, component, p, account_id):
                    out["deleted"] += 1
                    changed.append(p)
            out["same"] += len(rows) - len(todo)
            if changed:
                store.add_audit(conn, actor, via, "runner_state.save", "runner_state", component, None,
                                {"set": out["inserted"] + out["updated"], "deleted": out["deleted"],
                                 "paths": changed[:STATE_AUDIT_PATHS]})
            if file_mark is not None:
                marks = [] if mode == "replace" else state_marks(conn, component)
                store.set_meta(conn, STATE_MARK + component,
                               json.dumps(([file_mark] + [m for m in marks if m != file_mark])[:STATE_MARKS_KEPT]))
        return out
    return store.retrying(run, conn)


def state_marks(conn: sqlite3.Connection, component: str) -> list[str]:
    """The file marks of the component's last DB saves, newest first."""
    raw = store.get_meta(conn, STATE_MARK + component)
    try:
        marks = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(m) for m in marks] if isinstance(marks, list) else []


def _log_line(component: object, ts: object, level: object, line: object) -> tuple[str, str, str]:
    if component not in LOG_COMPONENTS:
        raise ValueError(f"unknown log component {component!r}")
    if level not in LOG_LEVELS:
        raise ValueError(f"bad log level {level!r}")
    if not isinstance(line, str) or "\n" in line or len(line) > TELEMETRY_LINE_MAX:
        raise ValueError("a log row is one line of text")
    return store.telemetry_ts(ts) or store.now_iso(), str(level), line


def app_log_append(conn: sqlite3.Connection, component: str, entries: Iterable[tuple[object, object, object]], *,
                   actor: str, via: str) -> int:
    """Append log lines (ts, level, line) of one component in one transaction. -> rows added."""
    _check_writer(actor, via, store.DEFAULT_ACCOUNT)
    rows = [_log_line(component, ts, level, line) for ts, level, line in entries]

    def run() -> int:
        with store.transaction(conn):
            for ts, level, line in rows:
                store.insert_app_log(conn, ts, component, level, line, actor, via)
        return len(rows)
    return store.retrying(run, conn) if rows else 0


def app_log_sync(conn: sqlite3.Connection, component: str, entries: Iterable[tuple[object, object, object]], *,
                 actor: str, via: str) -> dict[str, int]:
    """The importer's append: a log file's lines (in file order) whose copies the DB doesn't
    hold yet. A line that occurs n times in the file and m < n times in the DB gets its last
    n - m copies added, so running it again adds nothing and a line the dual-write stored is
    not stored twice. -> {"inserted", "present"}."""
    _check_writer(actor, via, store.DEFAULT_ACCOUNT)
    rows = [_log_line(component, ts, level, line) for ts, level, line in entries]

    def run() -> dict[str, int]:
        n = 0
        with store.transaction(conn):
            have = store.app_log_hash_counts(conn, component)
            seen: dict[str, int] = {}
            for ts, level, line in rows:
                h = hashlib.sha256(line.encode("utf-8")).hexdigest()
                seen[h] = seen.get(h, 0) + 1
                if seen[h] > have.get(h, 0):
                    store.insert_app_log(conn, ts, component, level, line, actor, via)
                    n += 1
        return {"inserted": n, "present": len(rows) - n}
    return store.retrying(run, conn)


def _iso(dt: datetime) -> str:
    s = store.iso(dt)
    assert s is not None      # store.iso only returns None for a None input; dt here never is
    return s


def _log_cutoffs(conn: sqlite3.Connection) -> dict[str, str]:
    """Each log component's cutoff (ISO ts; rows with ts before this are due for deletion):
    log_retention_days, or log_retention_overrides[component] when that component is listed
    (D-130: this is purely app_log's own retention, independent of telemetry/usage data)."""
    default_days = get_setting(conn, "log_retention_days")
    overrides = get_setting(conn, "log_retention_overrides")
    now = store._utcnow()
    return {c: _iso(now - timedelta(days=overrides.get(c, default_days))) for c in LOG_COMPONENTS}


def log_prune_preview(conn: sqlite3.Connection) -> dict[str, Any]:
    """The dry-run: what a `log.prune` action would delete right now, per log_retention_days /
    log_retention_overrides. Read-only (no gate, no transaction, not an audited action)."""
    cutoffs = _log_cutoffs(conn)
    counts = store.app_log_prune_counts(conn, cutoffs)
    return {"cutoffs": cutoffs, "would_delete": counts, "total": sum(counts.values())}


def _check_batch_size(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 100_000:
        raise ValueError(f"batch_size must be an integer in 1..100000, got {v!r}")
    return v


@action("log.prune", "app_log")
def log_prune(conn: sqlite3.Connection, ctx: Ctx, batch_size: Any = 1000) -> Result:
    """The daily retention prune (D-161/D-130; cron, `python3 store.py prune-logs`): delete
    app_log rows past log_retention_days / log_retention_overrides, in batches, through the one
    gated path (store.prune_app_log). Always writes one audit row with the counts, even when
    nothing was due (so the daily run leaves a trail either way)."""
    cutoffs = _log_cutoffs(conn)
    deleted = store.prune_app_log(conn, cutoffs, batch_size=_check_batch_size(batch_size))
    after = {"deleted": deleted, "total": sum(deleted.values()), "cutoffs": cutoffs, "batch_size": batch_size}
    return Result(after, "app_log", None, after, changed=True)


JOB_ON_INTERRUPT = ("rerun", "fail")   # an at-job's payload on_interrupt (phase 3a S4; absent: the runner's default)
JOB_TIMEOUT_MAX = 720.0                # minutes: an at-job's payload timeout (as the runner_timeout_* settings)


def job_put(conn: sqlite3.Connection, job: object, *, actor: str, via: str) -> str:
    """Store an at-shim job (its spool file's object: id, at, cmd, cwd; optional on_interrupt
    (JOB_ON_INTERRUPT: what the runner does with a run of it a restart cut off) and timeout (minutes
    the run may take), phase 3a S4). Audited. -> 'inserted' | 'updated' | 'same'."""
    _check_writer(actor, via, store.DEFAULT_ACCOUNT)
    if not isinstance(job, Mapping):
        raise ValueError("a job is a JSON object")
    jid, at, cmd, cwd = job.get("id"), job.get("at"), job.get("cmd"), job.get("cwd")
    if not isinstance(jid, str) or not re.match(r"^[A-Za-z0-9_-]{1,64}$", jid):
        raise ValueError(f"bad job id {jid!r}")
    if not isinstance(cmd, str) or not cmd or not isinstance(at, str):
        raise ValueError(f"job {jid}: needs at and cmd")
    if job.get("on_interrupt") is not None and job.get("on_interrupt") not in JOB_ON_INTERRUPT:
        raise ValueError(f"job {jid}: on_interrupt is one of {'|'.join(JOB_ON_INTERRUPT)}, got {job.get('on_interrupt')!r}")
    t = job.get("timeout")
    if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= JOB_TIMEOUT_MAX):
        raise ValueError(f"job {jid}: timeout is minutes in (0, {JOB_TIMEOUT_MAX:g}], got {t!r}")
    job_id, run_at, command = jid, store.telemetry_ts(at), cmd
    workdir = cwd if isinstance(cwd, str) else None
    payload = _state_json(dict(job))

    def run() -> str:
        with store.transaction(conn):
            before = store.get_job(conn, job_id)
            r = store.put_job(conn, job_id, run_at, command, workdir, payload, actor, via)
            if r != "same":
                store.add_audit(conn, actor, via, "job.put", "scheduled_job", job_id,
                                None if before is None else {"run_at": before["run_at"], "cmd": before["cmd"]},
                                {"run_at": run_at, "cmd": command})
        return r
    return store.retrying(run, conn)


def job_started(conn: sqlite3.Connection, job_id: str, *, actor: str, via: str, at: Optional[str] = None) -> bool:
    """Mark a pending job started (the at-shim claimed and ran it). Audited. -> changed."""
    _check_writer(actor, via, store.DEFAULT_ACCOUNT)

    def run() -> bool:
        with store.transaction(conn):
            ok = store.mark_job_started(conn, job_id, actor, via, store.telemetry_ts(at) if at else None)
            if ok:
                store.add_audit(conn, actor, via, "job.started", "scheduled_job", job_id,
                                {"status": "pending"}, {"status": "started"})
        return ok
    return store.retrying(run, conn)


def own_session_add(conn: sqlite3.Connection, session_id: object, *, actor: str, via: str,
                    started_at: Optional[str] = None) -> bool:
    """A session id of data/own_sessions.txt into driven_sessions (kind 'own'), if it isn't
    there yet. Audited. -> added."""
    _check_writer(actor, via, store.DEFAULT_ACCOUNT)
    if not isinstance(session_id, str) or not re.match(r"^[A-Za-z0-9-]{8,64}$", session_id):
        raise ValueError(f"bad session id {session_id!r}")
    sid = session_id

    def run() -> bool:
        with store.transaction(conn):
            if store.get_driven_session(conn, sid) is not None:
                return False
            row = store.upsert_driven_session(conn, sid, kind="own", started_at=started_at or store.now_iso())
            store.add_audit(conn, actor, via, "driven_session.add", "driven_session", sid, None, row)
        return True
    return store.retrying(run, conn)


# ---------------------------------------------------------------- the resident runner (phase 3a)
#
# runner.py's writes (docs/phase3a_plan.md §3.3): its job rows (runner_jobs), its run records
# (runner_runs: run_begin -> run_pid -> run_end), its own state (runner_state 'runner': boot,
# clean exit, drain; runner_state_put) and the retention prune (runner.prune). The run writes are
# plain functions like job_put (no perform(): one per run, not an owner's action), with the writer
# check, store.retrying and the §7.8 DB-error path (report_db_error, runner=True); the runner then
# fails (failstop) and exits. D-256: a run row carries its actor (runner:<job>) and via, and gets no
# audit_log row of its own (about 3,000 runs a day); what a run changes in work or state is audited
# by the handler's own actions.

RUN_STATUSES = ("running", "ok", "failed", "timeout", "interrupted", "skipped")
RUN_END_STATUSES = ("ok", "failed", "timeout", "interrupted")
RUN_ERROR_MAX = 2048            # characters of a run's error (a failed child's stderr: its tail)
RUN_RESULT_MAX = 16 * 1024      # characters of the handler's JSON result line
JOB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RUN_KEY_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}$")
BOOT_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
RUNNER_ACTOR = "runner"         # the runner's own writes (job rows, its state); a run's actor is runner:<job>
RUNNER_ROW = "runner"           # the runner_jobs row of the loop itself (its heartbeat), not a job


def _runner_write(conn: sqlite3.Connection, action_name: str, actor: str, params: Mapping[str, Any],
                  fn: Callable[[], _T]) -> _T:
    """fn() on the DB error path: retried when transient; a persistent failure is recorded in the
    fallback file (§7.8) and raised (store.DBError)."""
    try:
        return store.retrying(fn, conn)
    except store.DBError as e:
        if e.escalation is None:
            store.report_db_error(e, actor=actor, action=action_name, params=params, db_path=_db_path(conn),
                                  runner=True)
        raise


def _job_name(job: object) -> str:
    if not isinstance(job, str) or not JOB_NAME_RE.match(job):
        raise ValueError(f"bad runner job name {job!r}")
    return job


def _run_key(key: object) -> Optional[str]:
    if key is None:
        return None
    if not isinstance(key, str) or not RUN_KEY_RE.match(key):
        raise ValueError(f"bad run key {key!r}")
    return key


def runner_state_put(conn: sqlite3.Connection, entries: Mapping[str, Any], *, actor: str, via: str) -> None:
    """Set top-level entries of the runner's own state (runner_state 'runner'), keeping the others
    (a merge save on the rows as they are now; value None deletes the entry). Audited like every
    state save (runner_state.save)."""
    base = store.state_rows(conn, "runner")
    doc = state_unflatten("runner", base)
    for k, v in entries.items():
        if v is None:
            doc.pop(k, None)
        else:
            doc[k] = v
    state_save(conn, "runner", doc, base=base, actor=actor, via=via)


def runner_state_get(conn: sqlite3.Connection) -> dict[str, Any]:
    return state_unflatten("runner", store.state_rows(conn, "runner"))


def runner_drain(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    """The drain in force (`runner.py drain`: no new run that acts on a session), else None:
    the runner state's "drain" entry while its `until` is in the future."""
    d = runner_state_get(conn).get("drain")
    if not isinstance(d, dict) or not isinstance(d.get("until"), str):
        return None
    until = store.parse_iso(d["until"])
    return dict(d) if until is not None and until > store._utcnow() else None


def runner_job_set(conn: sqlite3.Connection, name: str, *, actor: str, via: str, **fields: Any) -> store.Row:
    """Create the job's runner_jobs row or update its fields (store.RUNNER_JOB_FIELDS). -> the row."""
    _check_writer(actor, via, store.DEFAULT_ACCOUNT)
    job = RUNNER_ROW if name == RUNNER_ROW else _job_name(name)

    def run() -> store.Row:
        with store.transaction(conn):
            return store.put_runner_job(conn, job, fields)
    return _runner_write(conn, "runner_job.set", actor, dict(fields, name=job), run)


def run_begin(conn: sqlite3.Connection, *, boot_id: str, job: str, key: Optional[str], timeout_s: Optional[float],
              actor: str, via: str, session: bool = False, rerun: bool = True,
              skipped: Optional[str] = None, claim: Optional[str] = None) -> tuple[Optional[int], Optional[str]]:
    """Record a run start ('running'), or with `skipped` (the reason) a run that was not started
    ('skipped', finished). In one transaction with the checks: a keyed run that ran before is
    refused (UNIQUE (job, key): any row that was not interrupted; with rerun=False, for a job whose
    interrupted run must never run again, any row at all), and so is a run that acts on a session
    (`session`) while a drain is in force. claim: the id of the pending scheduled job (an at-job,
    phase 3a S4) this run claims in the same transaction (marked started, audited as job.started,
    as the at-shim's claim); one claimed elsewhere already (`at --run-due`) is refused, so a job
    never runs twice and a refused run never leaves its job claimed but not run.
    -> (run id, None) or (None, why not)."""
    _check_writer(actor, via, store.DEFAULT_ACCOUNT)
    name, k = _job_name(job), _run_key(key)
    if not isinstance(boot_id, str) or not BOOT_ID_RE.match(boot_id):
        raise ValueError(f"bad boot id {boot_id!r}")
    why = None if skipped is None else _short(skipped, "skip reason", RUN_ERROR_MAX)
    if claim is not None and (why is not None or not isinstance(claim, str)):
        raise ValueError("a run claims a scheduled job (its id) only when it starts")

    def run() -> tuple[Optional[int], Optional[str]]:
        with store.transaction(conn):
            if k is not None:
                done = [r for r in store.key_runs(conn, name, k) if not rerun or r["status"] != "interrupted"]
                if done:
                    return None, f"{name} {k} ran before (run {done[0]['id']}, {done[0]['status']})"
            if session and why is None:
                d = runner_drain(conn)
                if d is not None:
                    return None, f"drained until {d['until']}"
            if claim is not None:
                if not store.mark_job_started(conn, claim, actor, via):
                    j = store.get_job(conn, claim)
                    return None, (f"scheduled job {claim} is unknown" if j is None else
                                  f"scheduled job {claim} was claimed already ({j['status']} {j['started_at']})")
                store.add_audit(conn, actor, via, "job.started", "scheduled_job", claim,
                                {"status": "pending"}, {"status": "started"})
            status = "running" if why is None else "skipped"
            return store.insert_run(conn, boot_id, name, k, status, timeout_s, actor, via, why), None
    return _runner_write(conn, "run.begin", actor, {"job": name, "key": k, "boot_id": boot_id, "claim": claim}, run)


def run_pid(conn: sqlite3.Connection, run_id: int, pid: int, *, actor: str, via: str) -> bool:
    """The running run's child pid (its process group). -> False: it is not running."""
    _check_writer(actor, via, store.DEFAULT_ACCOUNT)

    def run() -> bool:
        with store.transaction(conn):
            return store.set_run_pid(conn, int(run_id), int(pid))
    return _runner_write(conn, "run.pid", actor, {"run_id": run_id, "pid": pid}, run)


def run_end(conn: sqlite3.Connection, run_id: int, status: str, *, actor: str, via: str,
            result: Optional[str] = None, error: Optional[str] = None) -> bool:
    """running -> status (RUN_END_STATUSES), with the handler's JSON result line and the error
    (its tail is kept: RUN_ERROR_MAX). -> False: the run was not running (nothing changed)."""
    _check_writer(actor, via, store.DEFAULT_ACCOUNT)
    if status not in RUN_END_STATUSES:
        raise ValueError(f"a run ends as one of {'|'.join(RUN_END_STATUSES)}, got {status!r}")
    if result is not None:
        if not isinstance(result, str) or len(result) > RUN_RESULT_MAX:
            raise ValueError(f"a run result is a JSON text of at most {RUN_RESULT_MAX} characters")
        json.loads(result)
    err = None if error is None else str(error)[-RUN_ERROR_MAX:]

    def run() -> bool:
        with store.transaction(conn):
            return store.finish_run(conn, int(run_id), status, result, err)
    return _runner_write(conn, "run.end", actor, {"run_id": run_id, "status": status}, run)


def runner_runs_cutoff(conn: sqlite3.Connection) -> str:
    """Runs started before this (ISO) are due for deletion (runner_runs_retention_days)."""
    return _iso(store._utcnow() - timedelta(days=get_setting(conn, "runner_runs_retention_days")))


@action("runner.prune", "runner_runs")
def runner_prune(conn: sqlite3.Connection, ctx: Ctx, batch_size: Any = 1000) -> Result:
    """The runner's daily retention prune of its run records (runner_runs_retention_days; the
    runner's prune_runs job), through the one gated path (store.prune_runner_runs). Always one
    audit row with the count, as log.prune (a daily trail either way)."""
    _runner_only(ctx, "runner.prune")
    cutoff = runner_runs_cutoff(conn)
    deleted = store.prune_runner_runs(conn, cutoff, batch_size=_check_batch_size(batch_size))
    after = {"deleted": deleted, "cutoff": cutoff}
    return Result(after, "runner_runs", None, after, changed=True)
