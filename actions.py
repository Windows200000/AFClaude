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

The autonomous-writer rule (§6.2): AFClaude's own sessions (driven_sessions,
data/own_sessions.txt, the manager session, the manager_session of a managed
project, or a caller that says so, e.g. CLAUDE_GUARD_DISABLE=1 in the MCP server)
may add tasks and projects through MCP like anyone (owner decision Q3: no approval
step), but never decide sessions, change rules, settings, prompts or request runs.

Settings (§4.1, §4.2.1) are typed here (SETTINGS): a key without a row (or reset
to null) is its code default, so an empty table reproduces today's behaviour. The
runners start reading them in phase 2; nothing reads them yet.
"""
import copy
import hashlib
import inspect
import json
import os
import re
import string
import sys
from datetime import timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402
import afclaude_config  # noqa: E402

VIAS = ("cli", "mcp", "dashboard", "runner")
ACTOR_RE = re.compile(r"^(owner|cli|dispatcher|keepalive|mcp(:[A-Za-z0-9._-]{1,64})?)$")
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
    def __init__(self, msg, current=None):
        super().__init__(msg)
        self.current = current


class Forbidden(ValueError):
    """The actor may not do this (an autonomous session writing rules/decisions via MCP)."""


class Ctx:
    """Who acts: actor (owner | cli | mcp[:<session>] | dispatcher | keepalive), via
    (cli | mcp | dashboard | runner), the idempotency key, and whether the caller
    knows it is an autonomous AFClaude session."""
    def __init__(self, actor, via, key=None, autonomous=False):
        self.actor, self.via, self.key, self.autonomous = actor, via, key, bool(autonomous)

    @property
    def session(self):
        return self.actor.split(":", 1)[1] if self.actor.startswith("mcp:") else None


class Result:
    """What an action returns to perform(): the caller's result plus the audit data.
    before/after are the target's state (rows or small dicts); only the keys that
    changed go into the audit row. changed=False (or before == after) = no-op."""
    NOISE = ("version", "updated_at")

    def __init__(self, result, target_id=None, before=None, after=None, changed=None):
        self.result, self.target_id, self.before, self.after = result, target_id, before, after
        self.changed = changed

    def audit_pair(self):
        b, a = self.before, self.after
        if isinstance(b, dict) and isinstance(a, dict):
            keys = [k for k in dict.fromkeys(list(b) + list(a)) if k not in self.NOISE and b.get(k) != a.get(k)]
            return {k: b.get(k) for k in keys}, {k: a.get(k) for k in keys}
        return b, a

    def is_noop(self):
        if self.changed is not None:
            return not self.changed
        b, a = self.audit_pair()
        return b == a


class Spec:
    def __init__(self, name, fn, target_type, owner_only):
        self.name, self.fn, self.target_type, self.owner_only = name, fn, target_type, owner_only
        sig = inspect.signature(fn)
        ps = list(sig.parameters.values())[2:]                      # after (conn, ctx)
        self.params = {p.name for p in ps if p.kind is p.POSITIONAL_OR_KEYWORD}
        self.required = {p.name for p in ps if p.kind is p.POSITIONAL_OR_KEYWORD and p.default is p.empty}
        self.extra = next((p.name for p in ps if p.kind is p.VAR_KEYWORD), None)


ACTIONS = {}


def action(name, target_type, owner_only=False, extra=None):
    """Register fn(conn, ctx, **params) -> Result as action `name`. extra: the names
    a **fields parameter accepts."""
    def deco(fn):
        spec = Spec(name, fn, target_type, owner_only)
        spec.extra_names = tuple(extra or ())
        ACTIONS[name] = spec
        return fn
    return deco


# ---------------------------------------------------------------- the write path

def perform(conn, name, params=None, *, actor, via, key=None, autonomous=False):
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


def _check_params(spec, params):
    unknown = set(params) - spec.params - (set(spec.extra_names) if spec.extra else set())
    if unknown:
        raise ValueError(f"{spec.name}: unknown parameter(s) {sorted(unknown)}")
    missing = spec.required - set(params)
    if missing:
        raise ValueError(f"{spec.name}: missing parameter(s) {sorted(missing)}")


def _check_limits(params):
    for k, v in params.items():
        if isinstance(v, dict):
            _check_limits(v)
        elif isinstance(v, str) and k in DASHBOARD_LIMITS and len(v) > DASHBOARD_LIMITS[k]:
            raise ValueError(f"{k} is too long ({len(v)} > {DASHBOARD_LIMITS[k]} characters)")


def _check_version(row, version, what):
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

def _own_list():
    try:
        with open(OWN_LIST) as fh:
            return {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    except OSError:
        return set()


def autonomous_sessions(conn):
    """Session ids AFClaude drives: driven_sessions, data/own_sessions.txt, the
    manager session (data/afclaude.json) and every managed project's manager_session."""
    ids = {r[0] for r in conn.execute("SELECT session_id FROM driven_sessions")}
    ids |= {r[0] for r in conn.execute("SELECT manager_session FROM projects WHERE manager_session IS NOT NULL")}
    ids |= _own_list()
    ids.add(afclaude_config.manager_session())
    return ids


def is_autonomous(conn, ctx):
    """Only MCP writes are checked (the CLI and the dashboard are the owner's)."""
    if ctx.via != "mcp":
        return False
    return ctx.autonomous or (ctx.session is not None and ctx.session in autonomous_sessions(conn))


# ---------------------------------------------------------------- tasks

def _task(conn, task_id):
    return store._task_row(conn, store._position(task_id, "task id"))


def _task_change(conn, task_id, version, fn):
    before = _task(conn, task_id)
    _check_version(before, version, f"task #{task_id}")
    after = fn()
    return Result(after, task_id, before, after)


@action("task.add", "task")
def task_add(conn, ctx, title, description=None, project=None, priority=store.DEFAULT_PRIORITY, kind="task",
             created_by_session=None):
    t = store.add_task(conn, title, description, project, priority, kind, created_by_session)
    return Result(t, t["id"], None, t)


@action("task.ask", "task")
def task_ask(conn, ctx, question, project=None, created_by_session=None, title=None):
    """A manager question: a task of kind 'question', born blocked (§4.6)."""
    t = store.ask_question(conn, question, project, created_by_session or ctx.session, title)
    return Result(t, t["id"], None, t)


@action("task.edit", "task", extra=store.TASK_EDITABLE)
def task_edit(conn, ctx, task_id, version=None, **fields):
    """title / description / project (None = no project) / priority / kind."""
    return _task_change(conn, task_id, version, lambda: store.update_task(conn, task_id, **fields))


@action("task.priority", "task")
def task_priority(conn, ctx, task_id, priority, version=None):
    return _task_change(conn, task_id, version, lambda: store.set_stage_priority(conn, task_id, priority))


@action("task.move", "task")
def task_move(conn, ctx, task_id, stage, version=None):
    return _task_change(conn, task_id, version, lambda: store.move_stage(conn, task_id, stage))


@action("task.block", "task")
def task_block(conn, ctx, task_id, question, version=None):
    return _task_change(conn, task_id, version, lambda: store.block_task(conn, task_id, question))


@action("task.answer", "task")
def task_answer(conn, ctx, task_id, answer, version=None):
    """blocked -> pending (a question: -> done). From the dashboard, the same answer
    again right after it was given is a no-op success (a double submit), not an error."""
    t = _task(conn, task_id)
    if (ctx.via == "dashboard" and t["status"] != "blocked" and t["answer"] is not None
            and isinstance(answer, str) and answer.strip() == t["answer"]
            and store.task_events(conn, task_id)[-1]["event"] == "answered"):
        return Result(t, task_id, t, t, changed=False)
    return _task_change(conn, task_id, version, lambda: store.answer_task(conn, task_id, answer))


@action("task.start", "task")
def task_start(conn, ctx, task_id, session=None, version=None):
    return _task_change(conn, task_id, version, lambda: store.start_task(conn, task_id, session))


@action("task.finish", "task")
def task_finish(conn, ctx, task_id, summary=None, version=None):
    return _task_change(conn, task_id, version, lambda: store.finish_task(conn, task_id, summary))


@action("task.cancel", "task")
def task_cancel(conn, ctx, task_id, reason=None, version=None):
    return _task_change(conn, task_id, version, lambda: store.cancel_task(conn, task_id, reason))


@action("task.reopen", "task")
def task_reopen(conn, ctx, task_id, reason=None, version=None):
    return _task_change(conn, task_id, version, lambda: store.reopen_task(conn, task_id, reason))


@action("task.update", "task")
def task_update(conn, ctx, task_id, fields=None, stage=None, status=None, note=None, session=None, version=None):
    """Several changes to one task at once (the MCP tool afclaude_update_task): fields
    (as task.edit), then stage (position in its project), then status: done (note =
    summary), cancelled (note = reason), blocked (note = the question), in_progress
    (session), pending (reopen; a blocked task must be answered instead)."""
    if fields is not None and not isinstance(fields, dict):
        raise ValueError("fields must be an object")

    def apply():
        if fields:
            store.update_task(conn, task_id, **fields)
        if stage is not None:
            store.move_stage(conn, task_id, stage)
        if status is not None:
            cur = store.get_task(conn, task_id)
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

def _project(conn, project):
    return store._project_row(conn, project)


@action("project.add", "project")
def project_add(conn, ctx, name, description=None, path=None, rank=None):
    p = store.add_project(conn, name, description, path, rank)
    return Result(p, p["id"], None, p)


@action("project.edit", "project", extra=store.PROJECT_EDITABLE)
def project_edit(conn, ctx, project, version=None, **fields):
    """name / description / path / manager_session (None = unmanaged)."""
    before = _project(conn, project)
    _check_version(before, version, f"project {before['name']!r}")
    after = store.update_project(conn, before["id"], **fields)
    return Result(after, after["id"], before, after)


@action("project.move", "project")
def project_move(conn, ctx, project, rank, version=None):
    before = _project(conn, project)
    _check_version(before, version, f"project {before['name']!r}")
    after = store.move_project(conn, before["id"], rank)
    return Result(after, after["id"], {"rank": before["rank"]}, {"rank": after["rank"]})


@action("project.priority", "project")
def project_priority(conn, ctx, project, priority):
    """Every open stage of the project -> priority (naturally idempotent)."""
    p = _project(conn, project)
    r = store.set_project_priority(conn, p["id"], priority)
    return Result(r, p["id"], {"changed": []}, {"priority": r["priority"], "changed": r["changed"]},
                  changed=bool(r["changed"]))


# ---------------------------------------------------------------- stalled sessions and rules

@action("session.decide", "session", owner_only=True)
def session_decide(conn, ctx, session_id, decision, note=None, stall_ref=None, version=None):
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
def session_clear(conn, ctx, session_id):
    before = store.get_decision(conn, session_id)
    cleared = store.clear_decision(conn, session_id)
    return Result({"session_id": session_id, "cleared": cleared}, session_id, before, None, changed=cleared)


@action("rule.add", "rule", owner_only=True)
def rule_add(conn, ctx, scope, match, decision, note=None):
    """Standing rule; the same scope+match again replaces the old one (store.add_rule)."""
    old = None
    if scope in store.RULE_SCOPES and isinstance(match, str) and match.strip():
        m = store._norm_match(scope, match)
        r = conn.execute("SELECT * FROM standing_rules WHERE scope=? AND match=?", (scope, m)).fetchone()
        old = dict(r) if r else None
    r = store.add_rule(conn, scope, match, decision, note)
    changed = old is None or (old["decision"], old["note"]) != (r["decision"], r["note"])
    return Result(r, r["id"], old, r, changed=changed)


@action("rule.remove", "rule", owner_only=True)
def rule_remove(conn, ctx, rule_id, version=None):
    r = conn.execute("SELECT * FROM standing_rules WHERE id=?", (store._position(rule_id, "rule id"),)).fetchone()
    if r is None:
        raise store.NotFound(f"no rule #{rule_id}")
    _check_version(dict(r), version, f"rule #{rule_id}")
    store.remove_rule(conn, rule_id)
    return Result({"removed": rule_id}, rule_id, dict(r), None, changed=True)


# ---------------------------------------------------------------- settings (§4.1, §4.2.1)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEK_MIN = 7 * 24 * 60
GROUP_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
WINDOW_MODES = ("linked", "individual", "week", "link")


def _number(lo, hi):
    def check(v, conn=None):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"must be a number in {lo}..{hi}, got {v!r}")
        return float(v)
    return check


def _bool(v, conn=None):
    if not isinstance(v, bool):
        raise ValueError(f"must be true or false, got {v!r}")
    return v


def _tz(v, conn=None):
    try:
        ZoneInfo(v)
    except Exception:
        raise ValueError(f"unknown time zone {v!r}") from None
    return v


def _hhmm(v):
    m = re.match(r"^(\d{1,2}):(\d{2})$", v) if isinstance(v, str) else None
    if not m or int(m[1]) > 23 or m[2] not in ("00", "30"):
        raise ValueError(f"start must be HH:MM on the 30-minute grid (e.g. 23:00, 23:30), got {v!r}")
    return f"{int(m[1]):02d}:{m[2]}"


def _default_window_days():
    """Owner decision 29.09.2026: every day 23:00 + 2 session windows, one weekly link
    group. Taken from the local window_start / window_hours (afclaude_config, branch
    `window`) when set, so both describe the same window."""
    sh = _default_session_hours()
    start = afclaude_config.get("window_start", "23:00")
    try:
        start = _hhmm(start)
        n = max(1, int(round(float(afclaude_config.get("window_hours", 2 * sh)) / sh)))
    except (TypeError, ValueError):
        start, n = "23:00", 2
    return {d: {"start": start, "n": n, "group": "weekly"} for d in DAYS}


def _default_session_hours():
    return afclaude_config.SESSION_LENGTH.total_seconds() / 3600


def _default_last_mile_hours():
    return afclaude_config.last_mile_setting()


def _hours_or_auto(lo, hi):
    """last_mile_hours: "auto" (the budget.py formula) or a number of hours in lo..hi."""
    num = _number(lo, hi)

    def check(v, conn=None):
        if isinstance(v, str) and v.strip().lower() == "auto":
            return "auto"
        try:
            return num(v, conn)
        except ValueError:
            raise ValueError(f'must be "auto" or a number in {lo}..{hi}, got {v!r}') from None
    return check


def _fmt_min(m):
    m %= WEEK_MIN
    return f"{DAYS[m // 1440]} {m % 1440 // 60:02d}:{m % 60:02d}"


def _window_days(v, conn=None):
    """Validate + normalize {mon..sun: {start, n, group} | null}: start on the 30-min
    grid, 1 <= n with n x session_hours <= 24 h, identical windows within a link group,
    no overlap between any two days' windows (the week wraps around)."""
    if not isinstance(v, dict) or set(v) != set(DAYS):
        raise ValueError(f"window_days needs exactly the keys {', '.join(DAYS)}")
    sh = get_setting(conn, "session_hours") if conn is not None else _default_session_hours()
    out, groups = {}, {}
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
    spans = []
    for i, d in enumerate(DAYS):
        if out[d]:
            h, m = map(int, out[d]["start"].split(":"))
            s = i * 1440 + h * 60 + m
            spans.append((d, s, s + int(round(out[d]["n"] * sh * 60))))
    for i, (a, sa, ea) in enumerate(spans):
        for b, sb, eb in spans[i + 1:]:
            for k in (-WEEK_MIN, 0, WEEK_MIN):
                if max(sa, sb + k) < min(ea, eb + k):
                    first, second = ((a, sa, ea), (b, sb + k, eb + k)) if sa <= sb + k else ((b, sb + k, eb + k), (a, sa, ea))
                    raise ValueError(f"{first[0]} {out[first[0]]['start']} x {out[first[0]]['n']} ends "
                                     f"{_fmt_min(first[2])}, so {second[0]} can't start at "
                                     f"{out[second[0]]['start']} (overlap)")
    return out


class Setting:
    def __init__(self, default, check, doc):
        self.default, self.check, self.doc = default, check, doc


SETTINGS = {
    "window_days": Setting(_default_window_days, _window_days,
                           "per-weekday automation windows {mon..sun: {start, n, group} | null} (§4.2.1)"),
    "window_tz": Setting(lambda: "Europe/Berlin", _tz, "time zone of the window starts"),
    "session_hours": Setting(_default_session_hours, _number(1, 24), "length of one session-limit window"),
    "last_mile_hours": Setting(_default_last_mile_hours, _hours_or_auto(0, 168),
                               'end-of-week period with no HOLD before the weekly reset: "auto" = '
                               'ceil(session windows of quota left) x session_hours, or hours (0 = off)'),
    "projection_threshold": Setting(lambda: 90.0, _number(1, 100), "budget rule: projected weekly %"),
    "cutoff_after_window_hours": Setting(lambda: 2.0, _number(0, 24),
                                         "budget rule: a weekly reset this long after the window end still "
                                         "continues (11:00 after a 09:00 end)"),
    "session_usage_stop": Setting(lambda: 85.0, _number(1, 100), "dispatcher: no new starts at/above this "
                                                                  "session % (dispatcher.json's default)"),
    "automation_paused": Setting(lambda: False, _bool, "every runner holds new starts/continues"),
}


def _setting_spec(key):
    s = SETTINGS.get(key)
    if s is None:
        raise ValueError(f"unknown setting {key!r} (known: {', '.join(SETTINGS)})")
    return s


def get_setting(conn, key):
    """The effective value: the saved one, else the code default."""
    spec = _setting_spec(key)
    row = store.get_setting_row(conn, key) if conn is not None else None
    return copy.deepcopy(row["value"]) if row and row["value"] is not None else spec.default()


def settings(conn):
    """{key: {value, default, source: db|default, version, updated_at, updated_by, doc}}."""
    rows = store.setting_rows(conn)
    out = {}
    for k, spec in SETTINGS.items():
        r = rows.get(k)
        saved = r is not None and r["value"] is not None
        out[k] = {"value": r["value"] if saved else spec.default(), "default": spec.default(),
                  "source": "db" if saved else "default", "version": r["version"] if r else 0,
                  "updated_at": r["updated_at"] if r else None, "updated_by": r["updated_by"] if r else None,
                  "doc": spec.doc}
    return out


def _setting_view(conn, key):
    r = store.get_setting_row(conn, key)
    return {"key": key, "value": get_setting(conn, key), "version": r["version"] if r else 0,
            "source": "db" if r and r["value"] is not None else "default"}


def _write_setting(conn, ctx, key, value, version):
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
def setting_set(conn, ctx, key, value, version=None):
    if value is None:
        raise ValueError("value must not be null (use setting.reset)")
    return _write_setting(conn, ctx, key, value, version)


@action("setting.reset", "setting", owner_only=True)
def setting_reset(conn, ctx, key, version=None):
    return _write_setting(conn, ctx, key, None, version)


@action("automation.set", "setting", owner_only=True)
def automation_set(conn, ctx, paused):
    """Pause / resume every runner (idempotent). Running sessions keep running."""
    return _write_setting(conn, ctx, "automation_paused", _bool(paused), None)


def _fresh_group(days):
    used = {w["group"] for w in days.values() if w}
    i = 1
    while f"g{i}" in used:
        i += 1
    return f"g{i}"


@action("window.set", "setting", owner_only=True)
def window_set(conn, ctx, day, start, n=None, mode="linked", group=None, version=None):
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


def prompt_default(name):
    """The default text of prompts/<name> (name = file name, e.g. continue.md)."""
    if not isinstance(name, str) or not PROMPT_NAME_RE.match(name) or name == "README.md":
        raise ValueError(f"bad prompt name {name!r} (a file name under prompts/, e.g. continue.md)")
    path = os.path.join(PROMPTS_DIR, name)
    try:
        with open(path) as fh:
            return fh.read()
    except FileNotFoundError:
        raise store.NotFound(f"no prompt {name}") from None


def placeholders(text):
    """The {placeholder} names of a str.format template; None if it isn't one (unbalanced braces)."""
    try:
        return {f.split(".")[0].split("[")[0] for _, f, _, _ in string.Formatter().parse(text) if f is not None}
    except ValueError:
        return None


def check_prompt(name, body):
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


def prompt_text(conn, name):
    """What a sender should use: the override if there is one, else the file (phase 3 wires it in)."""
    o = store.get_prompt_override(conn, name)
    return o["body"] if o and o["body"] is not None else prompt_default(name)


@action("prompt.set", "prompt", owner_only=True)
def prompt_set(conn, ctx, name, body, version=None):
    default = check_prompt(name, body)
    row = store.get_prompt_override(conn, name)
    _check_version(row, version, f"prompt {name}")
    old = row["body"] if row else None
    if old == body:
        return Result(row, name, {"body": old}, {"body": body}, changed=False)
    r = store.put_prompt_override(conn, name, body, hashlib.sha256(default.encode()).hexdigest(), ctx.actor)
    return Result(r, name, {"body": old}, {"body": body})


@action("prompt.reset", "prompt", owner_only=True)
def prompt_reset(conn, ctx, name, version=None):
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
def request_add(conn, ctx, kind, target=None):
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
