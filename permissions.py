"""The permission matrix: who may call which action (phase 5a A3, docs/phase5a_plan.md §0, §2, §3.4, §3.7).

One table (`MATRIX`) holds a `Perm` per action; `check()` resolves the caller's principal (roles.parse_actor,
its grants, the project it manages or works in) and the target's project(s) and returns a `Verdict`:
allowed, proposal (an agent's write that becomes a proposal for the task-manager, actions.perform A4) or refused.
`check()` only reads. actions.perform() acts on the verdict according to the `roles_enforce` setting: in
shadow mode (the default, all of A3) it logs a non-allowed verdict (actions.roles_shadow_add, the
`roles_shadow` table, the list `tasks.py roles shadow` prints) and the write goes on under the old
owner_only rule; with `on` a refusal raises Forbidden.

Owner decisions it carries out: D-149/D-172 (global = owner only, several owners), D-184/D-190/D-192 (manager =
editor + forcing a run + granting editor/viewer; editors and managers make project decisions), D-186 (the
message box wakes for owners and managers only: delivery, not a permission), D-196 (viewers read questions, never
answer), D-160/D-182/D-185 (agents: direct writes, proposals, refusals), D-195/D-198 (a machine user is a user
with grants, none until given; a session AFClaude drives is task-manager or agent whatever token it carries),
D-283 (the task-manager clears a pause), D-299 (stalled sessions and rules are global, owner only), D-106 (the
AFClaude task-manager's deploy steps), D-290/D-330 (the host's own sessions are the owner `cli`).

Advisory, not authentication (plan A3): the actor string comes from identity.py, which every caller on this
host can influence; only the machine token is a credential.

No import of actions.py (it imports this module): the scope resolvers and predicates read the tables through
store.py. mypy --strict.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Optional

import host
import roles
import store

Outcome = Literal["allowed", "proposal", "refused", "filtered"]
TmMode = Literal["no", "own_project", "self_project", "if"]
AgentMode = Literal["refuse", "propose", "direct_if"]
Params = Mapping[str, Any]
Scope = tuple[Optional[int], ...]

SCOPE_GLOBAL: Scope = (None,)


class Unresolved(Exception):
    """The target (task, decision, ...) can't be found: the action raises its own NotFound, the check leaves it."""


@dataclass(frozen=True)
class Perm:
    """One action's permission entry (§3.4).
    scope          a key of SCOPES: the project(s) the call touches (None in the result = global)
    human          the least role on every touched project (`owner` for global things; None = an internal step
                   no role-bearing user takes: runners and the host's own hand-run)
    force          a forcing write (a run past usage, D-184/D-190): needs manager, an editor may not
    task_manager   no | own_project (a project it manages) | self_project (AFClaude's own task-manager: deploy
                   steps, the install-wide resume) | if (no project needed, `tm_if` decides)
    tm_if          a PREDICATES name that must also hold for a task-manager
    agent          refuse | propose (a proposal in its own project) | direct_if (`agent_if` holds: a direct
                   write; else a proposal, or a refusal with agent_miss="refuse")
    runner         a runner component (runner:<component>) may call it
    credential     no principal: the credential in the params is the check (machine.register, machine.seen)
    kinds          principal kinds allowed outright (the login flow: user, setup), `kinds_if` narrows them
    """
    scope: str = "global"
    human: Optional[roles.Role] = "owner"
    force: bool = False
    task_manager: TmMode = "no"
    tm_if: Optional[str] = None
    agent: AgentMode = "refuse"
    agent_if: Optional[str] = None
    agent_miss: Literal["propose", "refuse"] = "propose"
    runner: bool = False
    credential: bool = False
    kinds: tuple[str, ...] = ()
    kinds_if: Optional[str] = None
    note: str = ""


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    reason: str
    projects: Scope = ()
    role: Optional[str] = None


@dataclass(frozen=True)
class Who:
    """The caller as the matrix needs it. `kind`: host | user | machine | task-manager | agent | runner | setup |
    other; `family`: the session's /clear chain (task-manager, agent); `managed`: projects the session is the
    task-manager of; `working`: projects an agent works in (driven_sessions.project_id, or its task-manager's);
    `self_tm`: it is AFClaude's own task-manager."""
    kind: str
    actor: str
    session: Optional[str] = None
    user_id: Optional[int] = None
    active: bool = True
    grants: tuple[roles.GrantRow, ...] = ()
    family: tuple[str, ...] = ()
    managed: frozenset[int] = frozenset()
    working: frozenset[int] = frozenset()
    self_tm: bool = False


# ---------------------------------------------------------------- the caller

def afclaude_project_id(conn: sqlite3.Connection) -> Optional[int]:
    """AFClaude's own project: the one whose path is this repository (plan §0, D-283/D-106). That is the
    host checkout (`host.repo_dir()`; in the image the code is at /app, K1), as projects.path holds it."""
    here = os.path.realpath(host.repo_dir())
    for r in conn.execute("SELECT id, path FROM projects WHERE path IS NOT NULL"):
        if os.path.realpath(os.path.expanduser(r["path"])) == here:
            return int(r["id"])
    return None


def _in(sql: str, ids: tuple[str, ...]) -> str:
    return sql.replace("{marks}", ",".join("?" * len(ids)))


def who_is(conn: sqlite3.Connection, actor: str, session: Optional[str] = None) -> Who:
    p = roles.parse_actor(actor)
    kind: str = p.kind
    if p.kind == "other":
        kind = "runner" if actor in ("dispatcher", "keepalive") else "setup" if actor == "setup" else "other"
    if p.has_grants and p.user_id is not None:
        u = store.get_user(conn, p.user_id)
        grants = tuple(roles.GrantRow(g["user_id"], g["scope"], g["project_id"], g["level"], g["source"])
                       for g in store.list_grants(conn, p.user_id))
        return Who(kind, actor, session, p.user_id, u is not None and u["revoked_at"] is None, grants)
    sid = p.session or session
    if kind in ("task-manager", "agent") and sid and sid != "unknown":
        fam = tuple(store.session_family(conn, sid))
        managed = frozenset(int(r[0]) for r in conn.execute(
            _in("SELECT id FROM projects WHERE manager_session IN ({marks})", fam), fam))
        driven = frozenset(int(r[0]) for r in conn.execute(
            _in("SELECT project_id FROM driven_sessions WHERE project_id IS NOT NULL AND session_id IN ({marks})",
                fam), fam))
        own = afclaude_project_id(conn)
        return Who(kind, actor, sid, None, True, (), fam, managed, managed | driven,
                   own is not None and own in managed)
    return Who(kind, actor, session)


def role_on(who: Who, project: Optional[int]) -> Optional[str]:
    return roles.effective_role(who.grants, project)


# ---------------------------------------------------------------- scope resolvers: params -> projects

Resolver = Callable[[sqlite3.Connection, Params], Scope]


def _project_ref(conn: sqlite3.Connection, ref: Any, create_is_global: bool = False) -> Optional[int]:
    """A `project` param (name, id or directory; None = no project) -> its id; None for no project and, with
    create_is_global, for a name that doesn't exist (task.add creates it: creating a project is global)."""
    if ref is None or (isinstance(ref, str) and not ref.strip()):
        return None
    try:
        p = store.get_project(conn, int(ref) if isinstance(ref, str) and ref.isdigit() else ref)
    except ValueError as e:
        raise Unresolved(str(e)) from None
    if p is None:
        if create_is_global:
            return None
        raise Unresolved(f"no project {ref!r}")
    return int(p["id"])


def _r_project(conn: sqlite3.Connection, p: Params) -> Scope:
    return (_project_ref(conn, p.get("project"), create_is_global=True),)


def _r_project_strict(conn: sqlite3.Connection, p: Params) -> Scope:
    return (_project_ref(conn, p.get("project")),)


def _task_project(conn: sqlite3.Connection, task_id: Any) -> Optional[int]:
    if isinstance(task_id, bool) or not isinstance(task_id, int):
        raise Unresolved("task id is not a number")
    t = store.get_task(conn, task_id)
    if t is None:
        raise Unresolved(f"no task #{task_id}")
    return None if t["project_id"] is None else int(t["project_id"])


def _r_task(conn: sqlite3.Connection, p: Params) -> Scope:
    out = [_task_project(conn, p.get("task_id"))]
    if p.get("project") is not None:               # task.edit's project field: the target counts too
        out.append(_project_ref(conn, p["project"], create_is_global=True))
    return tuple(dict.fromkeys(out))


def _r_decision(conn: sqlite3.Connection, p: Params) -> Scope:
    """A decision's project; an install-wide one (project NULL) is global: owner only for humans (D-184 keeps
    global configuration out of project roles, plan §0). check() maps it to AFClaude's own project for a
    task-manager and an agent (`_afclaude_scope`): AFClaude's own task-manager writes install-wide decisions
    and agents propose to it."""
    if "decision" in p:
        d = store.get_decision_entry(conn, p["decision"])
        if d is None:
            raise Unresolved(f"no decision {p['decision']}")
        pid = d.get("project_id")
    else:
        pid = _project_ref(conn, p.get("project"))
    return SCOPE_GLOBAL if pid is None else (int(pid),)


def _afclaude_scope(conn: sqlite3.Connection, perm: Perm, scope: Scope) -> Scope:
    """What a task-manager or agent is held to: an install-wide decision (scope NULL) counts as a thing of
    AFClaude's own project, so only a task-manager that manages it (or an agent working in it) qualifies."""
    if perm.scope != "decision" or None not in scope:
        return scope
    own = afclaude_project_id(conn)
    return tuple(own if p is None else p for p in scope)


def _session_project(conn: sqlite3.Connection, sid: Any) -> Optional[int]:
    if not isinstance(sid, str):
        return None
    fam = tuple(store.session_family(conn, sid))
    r = conn.execute(_in("SELECT id FROM projects WHERE manager_session IN ({marks}) ORDER BY rank, id LIMIT 1",
                         fam), fam).fetchone()
    if r is not None:
        return int(r[0])
    r = conn.execute(_in("SELECT project_id FROM driven_sessions WHERE project_id IS NOT NULL AND session_id "
                         "IN ({marks}) LIMIT 1", fam), fam).fetchone()
    return None if r is None else int(r[0])


def _r_handoff(conn: sqlite3.Connection, p: Params) -> Scope:
    if p.get("project") is not None:
        return (_project_ref(conn, p["project"]),)
    return (_session_project(conn, p.get("session_id")),)


def _r_inbox_post(conn: sqlite3.Connection, p: Params) -> Scope:
    """Where a message goes: its project, else its session's project, else the first task-manager's (D-064)."""
    if p.get("project") is not None:
        return (_project_ref(conn, p["project"]),)
    if p.get("session_id") is not None:
        return (_session_project(conn, p["session_id"]),)
    tms = store.manager_sessions(conn)
    if not tms:
        raise Unresolved("no task-manager session exists")
    return (int(tms[0]["project_id"]),)


def _r_inbox_message(conn: sqlite3.Connection, p: Params) -> Scope:
    mid = p.get("message_id")
    m = store.get_inbox(conn, mid) if isinstance(mid, int) and not isinstance(mid, bool) else None
    if m is None:
        raise Unresolved(f"no inbox message {mid!r}")
    return (_session_project(conn, m["session_id"]),)


def _r_notice(conn: sqlite3.Connection, p: Params) -> Scope:
    if "notice_id" in p:
        nid = p["notice_id"]
        n = store.get_notice(conn, nid) if isinstance(nid, int) and not isinstance(nid, bool) else None
        if n is None:
            raise Unresolved(f"no notice {nid!r}")
        return (None if n["project_id"] is None else int(n["project_id"]),)
    pid = p.get("project")                              # notice.post: a project id (or none: install-wide)
    return (None if pid is None else _project_ref(conn, pid),)


def _r_request(conn: sqlite3.Connection, p: Params) -> Scope:
    """work_on_now targets a project; continue_now (a stalled session, D-299) and review_now are global."""
    if p.get("kind") == "work_on_now" and p.get("target") is not None:
        return (_project_ref(conn, p["target"]),)
    return SCOPE_GLOBAL


def _r_grant(conn: sqlite3.Connection, p: Params) -> Scope:
    """The owner role is global; a level on a project is that project's (its managers grant editor/viewer)."""
    if p.get("scope") == "project" and p.get("project") is not None:
        return (_project_ref(conn, p["project"]),)
    return SCOPE_GLOBAL


def _r_proposal(conn: sqlite3.Connection, p: Params) -> Scope:
    """A proposal belongs to the project it was proposed in (A4); one that can't be found is the action's NotFound."""
    pid = p.get("proposal_id")
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise Unresolved("proposal id is not a number")
    row = store.get_proposal(conn, pid)
    if row is None:
        raise Unresolved(f"no proposal P-{pid}")
    return (row["project_id"],)


SCOPES: dict[str, Resolver] = {
    "global": lambda conn, p: SCOPE_GLOBAL,
    "project": _r_project,
    "project_strict": _r_project_strict,
    "task": _r_task,
    "decision": _r_decision,
    "doc": _r_project_strict,
    "handoff": _r_handoff,
    "inbox_post": _r_inbox_post,
    "inbox_message": _r_inbox_message,
    "notice": _r_notice,
    "request": _r_request,
    "grant": _r_grant,
    "proposal": _r_proposal,
}


# ---------------------------------------------------------------- predicates (agents' direct writes, the exceptions)

Predicate = Callable[[sqlite3.Connection, Who, Params], bool]


def _p_pause_clear(conn: sqlite3.Connection, who: Who, p: Params) -> bool:
    """D-283: a task-manager only clears a pause (after investigating); setting one stays the owner's."""
    return p.get("paused") is False


def _p_own_session(conn: sqlite3.Connection, who: Who, p: Params) -> bool:
    sid = p.get("session_id")
    return isinstance(sid, str) and sid.strip() in who.family


def _task(conn: sqlite3.Connection, p: Params) -> Optional[Mapping[str, Any]]:
    tid = p.get("task_id")
    return store.get_task(conn, tid) if isinstance(tid, int) and not isinstance(tid, bool) else None


def _p_assigned_task(conn: sqlite3.Connection, who: Who, p: Params) -> bool:
    """The task is assigned to the caller's session (task.start sets it): own status and notes (§3.7)."""
    t = _task(conn, p)
    return t is not None and t["assigned_session"] is not None and str(t["assigned_session"]) in (*who.family,
                                                                                                  who.actor)


def _p_start_pending(conn: sqlite3.Connection, who: Who, p: Params) -> bool:
    t = _task(conn, p)
    return t is not None and t["status"] == "pending" and t["project_id"] is not None


def _p_alerts_doc(conn: sqlite3.Connection, who: Who, p: Params) -> bool:
    """A failure escalation (§7.8): an append to the project's own `alerts` doc."""
    return p.get("name") == "alerts"


def _p_review_session(conn: sqlite3.Connection, who: Who, p: Params) -> bool:
    """D-291: the usage review session writes its own prediction numbers directly."""
    if not who.family:
        return False
    return conn.execute(_in("SELECT 1 FROM driven_sessions WHERE kind='review' AND session_id IN ({marks}) LIMIT 1",
                            who.family), who.family).fetchone() is not None


def _p_self_user(conn: sqlite3.Connection, who: Who, p: Params) -> bool:
    """A user acts on their own account (the login's group sync, 6b)."""
    return who.user_id is not None and p.get("user_id") == who.user_id


PREDICATES: dict[str, Predicate] = {
    "pause_clear": _p_pause_clear, "own_session": _p_own_session, "assigned_task": _p_assigned_task,
    "start_pending": _p_start_pending, "alerts_doc": _p_alerts_doc, "review_session": _p_review_session,
    "self_user": _p_self_user,
}
# Predicates that make a direct write only inside the agent's own project (the scope check follows them).
PROJECT_BOUND = ("alerts_doc", "start_pending", "assigned_task")


# ---------------------------------------------------------------- the table

# Project things: an editor writes (D-184/D-190), the project's task-manager writes, an agent proposes (§3.7).
def _proj(scope: str = "project_strict", human: roles.Role = "editor", **kw: Any) -> Perm:
    kw.setdefault("task_manager", "own_project")
    kw.setdefault("agent", "propose")
    return Perm(scope=scope, human=human, **kw)


OWNER = Perm()                                                       # global, owner only: nobody else
DEPLOY = Perm(task_manager="self_project", runner=True, note="a deploy step (D-106)")
INTERNAL = Perm(human=None, runner=True, note="a runner's own step")

MATRIX: dict[str, Perm] = {
    # --- tasks (project of the task; a task with no project is global)
    "task.add": _proj("project"),
    "task.ask": _proj("project"),
    "task.edit": _proj("task"),
    "task.priority": _proj("task"),
    "task.move": _proj("task"),
    "task.cancel": _proj("task"),
    "task.reopen": _proj("task"),
    "task.block": _proj("task", agent="direct_if", agent_if="assigned_task"),
    "task.answer": _proj("task", agent="refuse"),
    "task.start": _proj("task", agent="direct_if", agent_if="start_pending"),
    "task.finish": _proj("task", agent="direct_if", agent_if="assigned_task"),
    "task.update": _proj("task", agent="direct_if", agent_if="assigned_task"),
    # --- questions (D-196: editors and up answer, viewers read)
    "question.answer": _proj("task", agent="refuse", task_manager="no"),
    "question.decide": _proj("task", agent="refuse", task_manager="no"),
    "question.close": _proj("task"),
    "question.import": DEPLOY,
    # --- projects (creation and rank are global, §0)
    "project.add": OWNER,
    "project.move": OWNER,
    "project.delete": OWNER,
    "project.manager_launch": OWNER,
    "project.edit": _proj(human="manager", agent="refuse", runner=True),
    "project.priority": _proj(),
    # --- stalled sessions and standing rules: global, owner only (D-299)
    "session.decide": OWNER,
    "session.clear": OWNER,
    "rule.add": OWNER,
    "rule.remove": OWNER,
    # --- settings, windows, prompts: owner only (D-149, D-193)
    "setting.set": OWNER,
    "setting.reset": OWNER,
    "window.set": OWNER,
    "setting.import": DEPLOY,
    "automation.set": Perm(task_manager="self_project", tm_if="pause_clear", runner=True,
                           note="D-283: the AFClaude task-manager clears the install-wide pause"),
    "automation.set_project": Perm(scope="project_strict", human="manager", task_manager="own_project",
                                   tm_if="pause_clear", runner=True,
                                   note="D-335: the project's manager pauses and resumes it, owners as always; "
                                        "D-283: its task-manager only clears"),
    "install.stamp": DEPLOY,
    "install.restored": DEPLOY,
    "masterkey.check": INTERNAL,
    "prompt.set": OWNER,
    "prompt.reset": OWNER,
    "prompt.defaults_sync": DEPLOY,
    "prompt_var.set": _proj(),
    "prompt_var.reset": _proj(),
    "prompt_var.migrate": DEPLOY,
    # --- run requests: work_on_now is a forced run (manager), the rest is global (continue_now, review_now)
    "request.add": Perm(scope="request", human="manager", force=True),
    "request.start": INTERNAL,
    "request.finish": INTERNAL,
    "request.wake": INTERNAL,             # A5: the inbox job wakes a task-manager for a proposal (D-194)
    # --- the runner's own bookkeeping
    "driven.set": INTERNAL,
    "session.cleared": INTERNAL,
    "handoff.consume": INTERNAL,
    "inbox.triage": INTERNAL,
    "inbox.triage_hold": INTERNAL,
    "inbox.hold": INTERNAL,
    "inbox.deliver": INTERNAL,
    "inbox.include": INTERNAL,
    "inbox.fail": INTERNAL,
    "typed.record": INTERNAL,
    "log.prune": INTERNAL,
    "runner.prune": INTERNAL,
    # --- hand-offs (§3.7: an agent's own, direct)
    "handoff.write": Perm(scope="handoff", human="editor", task_manager="if", tm_if="own_session",
                          agent="direct_if", agent_if="own_session", agent_miss="refuse"),
    # --- the owner's message box
    "inbox.post": Perm(scope="inbox_post", human="editor", note="who wakes the task-manager is delivery, D-186"),
    "inbox.reply": Perm(scope="inbox_message", human="editor", task_manager="own_project"),
    "inbox.retry": Perm(scope="inbox_message", human="editor"),
    "inbox.dismiss": Perm(scope="inbox_message", human="editor"),
    "notice.post": _proj("notice"),
    "notice.dismiss": Perm(scope="notice", human="editor"),
    # --- the task-manager's review of agents' proposals (§3.7; owners, managers and editors from 7a, D-190)
    "run.open": INTERNAL,
    "run.close": INTERNAL,
    "run.deliver": INTERNAL,
    "proposal.alert": INTERNAL,
    "task.forward": _proj("task", agent="refuse"),
    "proposal.review": Perm(scope="proposal", human="editor", task_manager="own_project"),
    # --- docs
    "doc.append": _proj("doc", agent="direct_if", agent_if="alerts_doc", runner=True),
    "doc.set": _proj("doc"),
    "doc.sync": Perm(scope="doc", human="editor", task_manager="own_project", runner=True),
    # --- decisions (editors and managers make project decisions, D-192; the owner confirms changes to its own)
    "decision.add": _proj("decision"),
    "decision.edit": _proj("decision"),
    "decision.delete": _proj("decision"),
    "decision.expire": _proj("decision"),
    "decision.keywords": _proj("decision"),
    "decision.topics": _proj("decision"),
    "decision.link": _proj("decision"),
    "decision.confirm": Perm(scope="decision", human="owner"),
    "decision.import": DEPLOY,
    "decision.seed": DEPLOY,
    "decision.activate_own": DEPLOY,
    # --- the usage models (D-291: the usage review writes its numbers directly)
    "user_model.set": Perm(runner=True, agent="direct_if", agent_if="review_session", agent_miss="refuse"),
    "cost_model.set": Perm(runner=True, agent="direct_if", agent_if="review_session", agent_miss="refuse"),
    # --- users, grants, machines (A1; managers grant editor and viewer, enforced in the action too)
    "grant.set": Perm(scope="grant", human="manager"),
    "grant.remove": Perm(scope="grant", human="manager"),
    "grant.sync_groups": Perm(human=None, kinds=("user",), kinds_if="self_user",
                              note="the OIDC login's own call (6b), for the user logging in"),
    "user.rename": OWNER,
    "user.revoke": OWNER,
    "machine.rename": OWNER,
    "machine.revoke": OWNER,
    "machine.code": OWNER,
    "machine.register": Perm(credential=True, note="no principal: the registration code is the credential"),
    "machine.seen": Perm(credential=True, note="the token check's own write"),
    # --- the dashboard's login flow (6a)
    "auth.session_login": Perm(human=None, kinds=("user",)),
    "auth.session_rotate": Perm(human=None, kinds=("user",)),
    "auth.session_logout": Perm(human=None, kinds=("user",)),
    "auth.session_revoke": OWNER,
    "setup.mint_code": OWNER,
    "setup.claim_code": Perm(human=None, kinds=("setup",)),
}


def mismatch(names: Iterable[str]) -> list[str]:
    """Actions with no entry, entries with no action: deny by default, a test fails on either."""
    have = set(names)
    return sorted(have - set(MATRIX)) + sorted(set(MATRIX) - have)


# ---------------------------------------------------------------- the check

_ORDER = {"viewer": 1, "editor": 2, "manager": 3, "owner": 4}


def _needed(perm: Perm) -> Optional[roles.Role]:
    if perm.human is None:
        return None
    if perm.force and _ORDER[perm.human] < _ORDER["manager"]:
        return "manager"
    return perm.human


def _refuse(reason: str, scope: Scope = ()) -> Verdict:
    return Verdict("refused", reason, scope)


def _all_in(scope: Scope, have: frozenset[int]) -> bool:
    return bool(scope) and all(p is not None and p in have for p in scope)


def _pred(name: Optional[str], conn: sqlite3.Connection, who: Who, params: Params) -> bool:
    return True if name is None else PREDICATES[name](conn, who, params)


def check(conn: sqlite3.Connection, name: str, params: Params, actor: str,
          session: Optional[str] = None) -> Verdict:
    """May `actor` call action `name` with `params`? Reads only. An action with no entry is refused."""
    perm = MATRIX.get(name)
    if perm is None:
        return _refuse(f"{name} has no permission entry (deny by default)")
    who = who_is(conn, actor, session)
    if perm.credential:
        return Verdict("allowed", "no principal: the credential in the request is the check")
    if who.kind == "host":
        return Verdict("allowed", "the host owner")
    try:
        scope = SCOPES[perm.scope](conn, params)
    except Unresolved as e:
        return Verdict("allowed", f"target not found ({e}): the action refuses it itself")
    if who.kind in perm.kinds and who.active and _pred(perm.kinds_if, conn, who, params):
        return Verdict("allowed", f"{who.kind} (login flow)", scope)
    if who.kind == "runner":
        return (Verdict("allowed", "a runner's step", scope) if perm.runner
                else _refuse(f"{name} is not a runner step", scope))
    if who.kind in ("user", "machine"):
        return _human(perm, who, name, scope)
    if who.kind == "task-manager":
        return _task_manager(conn, perm, who, name, params, _afclaude_scope(conn, perm, scope))
    if who.kind == "agent":
        return _agent(conn, perm, who, name, params, _afclaude_scope(conn, perm, scope))
    return _refuse(f"{who.actor} is not a principal with a role", scope)


def _human(perm: Perm, who: Who, name: str, scope: Scope) -> Verdict:
    if not who.active:
        return _refuse(f"{who.actor} is revoked or unknown", scope)
    need = _needed(perm)
    if need is None:
        return _refuse(f"{name} is an internal step: no user role takes it", scope)
    lowest = "owner"
    for pid in scope:
        role = role_on(who, pid)
        here = "owner" if pid is None else need
        if not roles.at_least(role, here):
            what = "global: owner only" if pid is None else f"needs {need}{' (forcing)' if perm.force else ''}"
            return _refuse(f"{name} {what} (this is {role or 'no grant'})", scope)
        if role is not None and _ORDER[role] < _ORDER[lowest]:
            lowest = role
    return Verdict("allowed", f"{lowest} grant", scope, lowest)


def _task_manager(conn: sqlite3.Connection, perm: Perm, who: Who, name: str, params: Params, scope: Scope) -> Verdict:
    mode = perm.task_manager
    if mode == "no":
        return _refuse(f"{name} is not a task-manager's", scope)
    if not _pred(perm.tm_if, conn, who, params):
        return _refuse(f"{name}: a task-manager only does it under {perm.tm_if}", scope)
    if mode == "if":
        return Verdict("allowed", "a task-manager's own", scope)
    if mode == "self_project":
        return (Verdict("allowed", "AFClaude's own task-manager", scope) if who.self_tm
                else _refuse(f"{name} is for AFClaude's own task-manager", scope))
    if _all_in(scope, who.managed):
        return Verdict("allowed", "the task-manager of the project", scope)
    return _refuse(f"{name} is outside the projects this task-manager manages", scope)


def _agent(conn: sqlite3.Connection, perm: Perm, who: Who, name: str, params: Params, scope: Scope) -> Verdict:
    if perm.agent == "refuse":
        return _refuse(f"{name} is not an agent's", scope)
    if perm.agent == "direct_if" and perm.agent_if is not None and _pred(perm.agent_if, conn, who, params):
        if perm.agent_if in PROJECT_BOUND and not _all_in(scope, who.working):
            return _refuse(f"{name}: outside the project this agent works in", scope)
        return Verdict("allowed", f"an agent's direct write ({perm.agent_if})", scope)
    if perm.agent == "direct_if" and perm.agent_miss == "refuse":
        return _refuse(f"{name}: an agent only does it under {perm.agent_if}", scope)
    if _all_in(scope, who.working):
        return Verdict("proposal", "an agent's write in its project: a proposal for the task-manager", scope)
    return _refuse(f"{name}: no project of this agent's to propose to", scope)


# ---------------------------------------------------------------- reads (plan §3.4)

def visible_projects(conn: sqlite3.Connection, actor: str, session: Optional[str] = None) -> Optional[frozenset[int]]:
    """The projects a caller's listings may show: None = all (the host owner, a runner, an owner grant), else the
    set (a user or machine user: any grant; a task-manager: the projects it manages; an agent: the project it
    works in; anything else: none). Viewers see what editors see; the answer action is the write check's."""
    who = who_is(conn, actor, session)
    if who.kind in ("host", "runner"):
        return None
    if who.kind in ("user", "machine"):
        if not who.active:
            return frozenset()
        ids = [int(r[0]) for r in conn.execute("SELECT id FROM projects")]
        return None if roles.is_owner(who.grants) else roles.visible_projects(who.grants, ids)
    if who.kind == "task-manager":
        return who.managed
    if who.kind == "agent":
        return who.working
    return frozenset()
