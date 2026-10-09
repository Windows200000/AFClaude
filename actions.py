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
import hashlib
import inspect
import json
import os
import re
import sqlite3
import string
import sys
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any, Callable, Optional, TypeVar
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402
import afclaude_config  # noqa: E402
import schedule  # noqa: E402

VIAS = ("cli", "mcp", "dashboard", "runner")
ACTOR_RE = re.compile(r"^(owner|cli|dispatcher|keepalive|runner(:[a-z0-9_-]{1,32})?|mcp(:[A-Za-z0-9._-]{1,64})?)$")
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
IDEMPOTENCY_TTL = timedelta(days=7)
OWN_LIST = os.environ.get("AFCLAUDE_OWN_LIST", os.path.join(HERE, "data", "own_sessions.txt"))
PROMPTS_DIR = os.path.join(HERE, "prompts")
PROMPT_MAX = 16 * 1024
# §6.3 size limits, for writes from the dashboard (the CLI and MCP keep their old behaviour)
DASHBOARD_LIMITS = {"title": 200, "name": 200, "answer": 8192, "question": 8192, "description": 16384,
                    "note": 2000, "reason": 2000, "summary": 8192, "path": 1024, "match": 1024}


class Conflict(store.InvalidTransition):
    """Stale version, reused idempotency key, or the target changed since the caller
    looked (HTTP 409). `current` is the current row, when there is one."""
    def __init__(self, msg: str, current: Any = None) -> None:
        super().__init__(msg)
        self.current = current


class Forbidden(ValueError):
    """The actor may not do this (an autonomous session writing rules/decisions via MCP)."""


class Ctx:
    """Who acts: actor (owner | cli | mcp[:<session>] | dispatcher | keepalive | runner[:<job>]), via
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
    try:
        with open(OWN_LIST) as fh:
            return {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    except OSError:
        return set()


def autonomous_sessions(conn: sqlite3.Connection) -> set[str]:
    """Session ids AFClaude drives: driven_sessions, data/own_sessions.txt, the
    manager session (data/afclaude.json) and every managed project's manager_session."""
    ids = {r[0] for r in conn.execute("SELECT session_id FROM driven_sessions")}
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
SECTIONS = ("schedule", "budget", "automation")


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


def _flag(default: bool, doc: str, section: str, important: bool = False) -> Setting:
    return Setting(default, _bool, doc, section=section, kind="bool", important=important)


USAGE_MODELS = ("pacing", "linear")

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
    "pacing_idle_min": _num(60, 0, 1440, "Yield to the user: after their last activity AFClaude waits this "
                            "many minutes before it starts (a hold postpones to then)", "budget"),
    "pacing_min_gap": _num(1, 0, 50, "Night gate: no run if the budget for it is at most this many weekly %",
                           "budget"),
    "pacing_session_cap": _num(85, 1, 100, "Session guard: no night start while the current session window "
                               "is at or above this % (the last stretch uses 100%)", "budget"),
    "pacing_last_mile_yield": _flag(True, "Also yield to an active user in the last stretch before the "
                                    "weekly reset", "budget"),
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
    # --- automation
    "automation_paused": _flag(False, "Every runner holds new starts and continues (running sessions go "
                               "on); read by the resident runner (phase 3a)", "automation", important=True),
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
}


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
def automation_set(conn: sqlite3.Connection, ctx: Ctx, paused: Any) -> Result:
    """Pause / resume every runner (idempotent). Running sessions keep running."""
    return _write_setting(conn, ctx, "automation_paused", _bool(paused), None)


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


def placeholders(text: str) -> set[str] | None:
    """The {placeholder} names of a str.format template; None if it isn't one (unbalanced braces)."""
    try:
        return {f.split(".")[0].split("[")[0]
                for _literal, f, _spec, _conv in string.Formatter().parse(text) if f is not None}
    except ValueError:
        return None


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
    """What a sender should use: the override if there is one, else the file (phase 3 wires it in)."""
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


# ---------------------------------------------------------------- execution requests (§4.6)

REQUEST_KINDS = ("continue_now", "review_now")


@action("request.add", "request", owner_only=True)
def request_add(conn: sqlite3.Connection, ctx: Ctx, kind: Any, target: Any = None) -> Result:
    """Queue continue_now (target = a stalled session id) or review_now (no target).
    One open request per (kind, target): asking again returns the open one."""
    store._enum(kind, REQUEST_KINDS, "kind")
    if kind == "continue_now":
        target = store._text(target, "target", required=True)
        if store.get_session(conn, target) is None:
            raise store.NotFound(f"unknown session {target}")
    elif target is not None:
        raise ValueError("review_now takes no target")
    open_ = store.open_request(conn, kind, target)
    if open_:
        return Result(open_, open_["id"], open_, open_, changed=False)
    r = store.add_request(conn, ctx.actor, kind, target)
    return Result(r, r["id"], None, r)


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
# interpretation of §7.3 that gate 8a checks).
#
# Every row is the JSON line its producer writes to its file (the dual-write period: the
# producers keep writing the files until they are retired, telemetry.py), stored as given, so
# a reader gets byte-identical rows from either and the importer dedupes on its sha256.
# Errors: ValueError (unknown kind, bad actor/via/account, not a JSON object, too big);
# database errors take the DB error path's retry (store.retrying) and then raise a DBError
# WITHOUT a fallback record: during the dual-write the file holds the row and
# `python3 store.py import-telemetry` (idempotent) brings the DB up to date.

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
           Appender("usage.user_model", "user_model", _fitted_ts, key=_model_key)):
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
