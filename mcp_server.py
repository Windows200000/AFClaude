#!/usr/bin/env python3
"""
AFClaude MCP server (stdio): the task store for any Claude Code session on
this host. Sessions add, query, prioritize and answer tasks, and decide
stalled sessions, by calling these tools only when the user explicitly asks
for AFClaude ("add an AFClaude task to do X", "what's waiting for me in
AFClaude?"), or when an AFClaude task prompt tells them to report. A task
added here is the same as one added in the UI/CLI: no approval step.

    .venv/bin/python mcp_server.py        (Claude Code starts it; see mcp_register.md)

Reads through store.py (schema v4, same DB as tasks.py: $AFCLAUDE_DB or data/afclaude.db);
every write goes through actions.py (validation, one transaction, an audit row as
actor mcp:<session>; an autonomous AFClaude session, CLAUDE_GUARD_DISABLE=1 or a
session AFClaude drives, may not decide sessions or change rules)
and the official MCP Python SDK (mcp 2.x, MCPServer), installed in .venv/
(Python 3.12; the system python3 is 3.9, too old for the SDK).

The calling session's directory ("project" default, and "." in arguments):
  1. the client's MCP roots (first file:// root), if it declares roots
  2. $CLAUDE_PROJECT_DIR, if set in the server's environment
  3. the server's cwd: Claude Code starts a stdio server per session, in
     that session's working directory
"/" and $HOME count as "no project". Results name the source (roots/env/cwd).
created_by_session is $CLAUDE_CODE_SESSION_ID, when the client passes it on.

Env: AFCLAUDE_DB (database), AFCLAUDE_PROJECTS_DIR (transcripts root for the
stalled-session scan, default ~/.claude/projects). Results are compact JSON;
times are Europe/Berlin. Expected failures (unknown id, wrong state, bad
value) come back as tool errors with a one-line message; a database error
(docs/dashboard_design/07_data_model.md §7.8) as a tool error holding a JSON object
{"error": {error_class, kind, message, not_saved, escalation, next_step}}.
"""
import functools
import json
import os
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, Literal, Optional, ParamSpec
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402  (reads)
import actions  # noqa: E402  (every write)
import prompts  # noqa: E402  (the prompt registry: read-only view, phase 4a P4)
import tasks as cli  # noqa: E402  (berlin(), resolve_session(), session_json())

from mcp.server.mcpserver import Context, MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

Priority = Literal["high", "medium", "low"]
P = ParamSpec("P")
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

# Owner decision (29.09.2026, dashboard design Q3): tasks added through MCP are treated
# exactly like tasks created in the UI (no approval step), so the tools may only be used
# when the user explicitly asks for AFClaude. Said in the server instructions and, short,
# at the start of every tool description (clients may drop the instructions).
USE_ONLY = "Only when the user explicitly asks for AFClaude (or an AFClaude task prompt says so). "
INSTRUCTIONS = (
    "AFClaude task store on this host. Use these tools ONLY when the user explicitly asks for AFClaude "
    "(adding, listing, reprioritizing or answering AFClaude tasks, its projects, what is waiting for them, "
    "stalled-session decisions or rules), or when the AFClaude task prompt this session was started with tells "
    "you to report through them. Never on your own initiative, e.g. not as your own todo list. A task added here "
    "counts exactly like one the user added in the UI: there is no approval step, and the project's task-manager "
    "works it on its own in its next run (runs begin at the nightly session-window starts). Tasks are stages of ranked projects; execution order is all "
    "high stages (by project rank, then stage), then medium, then low. Times are Europe/Berlin.")

server = MCPServer(
    name="afclaude",
    instructions=INSTRUCTIONS,
    log_level="WARNING",
)


# ---------------------------------------------------------------- helpers

def db() -> sqlite3.Connection:
    return store.connect(os.environ.get("AFCLAUDE_DB") or store.DB_PATH)


def out(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def bt(s: str | None) -> str | None:
    """UTC ISO -> '2026-09-29 12:00 CEST' (None stays None)."""
    return cli.berlin(s, "%Y-%m-%d %H:%M %Z") if s else None


def compact(d: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != ""}


def task_brief(t: Mapping[str, Any]) -> dict[str, Any]:
    return compact({"id": t["id"], "title": t["title"], "status": t["status"], "priority": t["priority"],
                    "project": t["project"], "stage": t["stage_seq"],
                    "kind": t["kind"] if t["kind"] != "task" else None,
                    "estimated_sessions": t.get("estimated_sessions"),
                    "options": t.get("options"), "decision": t.get("decision_id"),
                    "question": t["blocked_question"] if t["status"] == "blocked" else None,
                    "updated": bt(t["updated_at"])})


def task_full(t: Mapping[str, Any], events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    d = task_brief(t)
    d.update(compact({
        "description": t["description"], "project_rank": t["project_rank"], "created": bt(t["created_at"]),
        "created_by_session": t["created_by_session"], "assigned_session": t["assigned_session"],
        "question": t["blocked_question"], "asked": bt(t["blocked_at"]),
        "answer": t["answer"], "answered": bt(t["answered_at"]),
        "result": t["result_summary"], "done": bt(t["done_at"])}))
    d["history"] = [compact({"at": bt(e["ts"]), "event": e["event"], "detail": e["detail"]}) for e in events]
    return d


def project_brief(p: Mapping[str, Any]) -> dict[str, Any]:
    return compact({"rank": p["rank"], "name": p["name"], "open": p.get("open"), "ready": p.get("ready"),
                    "path": p["path"], "description": p["description"],
                    "manager_session": p.get("manager_session"), "manager_model": p.get("manager_model"),
                    "manager_effort": p.get("manager_effort")})


def session_brief(s: Mapping[str, Any]) -> dict[str, Any]:
    d = cli.session_json(s)
    return compact({"session_id": d["session_id"], "title": d["title"], "cwd": d["cwd"],
                    "limit": d["stall_kind"], "stalled_since": bt(d["stalled_since"]),
                    "resets": bt(d["stall_reset_at"]), "hits": d["hits"],
                    "decision": d["decision"], "decision_source": d["decision_source"]})


def session_id_env() -> str | None:
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def actor() -> str:
    """mcp:<calling session> for the audit log (plain mcp if the client didn't pass its id)."""
    sid = session_id_env()
    return f"mcp:{sid}" if sid and actions.ACTOR_RE.match(f"mcp:{sid}") else "mcp"


def act(conn: sqlite3.Connection, action: str, /, **params: Any) -> Any:
    """One write through the shared write path. The guard-hook bypass marks a session
    AFClaude launched (autonomous), whatever its id."""
    return actions.perform(conn, action, params, actor=actor(), via="mcp",
                           autonomous=os.environ.get("CLAUDE_GUARD_DISABLE") == "1")


def _no_project_dir(path: str) -> bool:
    home = os.path.expanduser("~")
    return path in ("/", os.path.normpath(home)) if home else path == "/"


async def caller_dir(ctx: Optional[Context]) -> tuple[str | None, str]:
    """(directory, source) of the calling session; directory None if unknown."""
    try:
        caps = ctx.client_capabilities if ctx is not None else None
        if ctx is not None and caps is not None and caps.roots is not None:
            import anyio
            import warnings
            with warnings.catch_warnings(), anyio.fail_after(3):
                warnings.simplefilter("ignore")
                res = await ctx.session.list_roots()
            for r in res.roots:
                u = urlparse(str(r.uri))
                if u.scheme == "file" and u.path:
                    p = os.path.normpath(unquote(u.path))
                    return (None if _no_project_dir(p) else p), "roots"
    except Exception:           # no request context, no back-channel, timeout, bad reply: fall through
        pass
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and os.path.isabs(env):
        p = os.path.normpath(env)
        return (None if _no_project_dir(p) else p), "env"
    p = os.getcwd()
    return (None if _no_project_dir(p) else p), "cwd"


async def resolve_dir_arg(ctx: Optional[Context], value: str | None) -> str | None:
    """'.' -> the caller's directory; './x', '../x' relative to it; '~/x' expanded; else unchanged."""
    if value is None:
        return None
    v = value.strip()
    if v == "." or v == ".." or v.startswith(("./", "../")):
        base, _ = await caller_dir(ctx)
        if base is None:
            raise ValueError("'.' needs the calling session's directory, which is unknown here "
                             "(pass a project name or absolute path)")
        return os.path.normpath(os.path.join(base, v))
    if v.startswith("~"):
        return os.path.expanduser(v)
    return v


# What a schema mismatch tells the calling session. A server process that stays up across an update
# keeps its old code: when the database moved to a newer schema (the explicit migrate step ran), that
# old code is refused here (SchemaTooNew) and can't be fixed in place: a reconnect starts the new one.
SCHEMA_MESSAGES = {
    True: "AFClaude was updated: reconnect the afclaude MCP server (/mcp) or restart the session",
    False: "AFClaude's database is waiting for its migration (the container's start step runs it): "
           "retry in a minute; if it stays, tell the owner (the owner was alerted by the migrate step)",
}


def db_error(e: BaseException, tool_name: str, params: Mapping[str, Any]) -> ToolError:
    """A database error -> the structured hand-back (§7.8, D-171) as the tool error: JSON
    {"error": {error_class, kind, message, not_saved, escalation, next_step}}. A write that
    failed was already recorded and escalated by actions.perform; any other DB error (a read,
    opening the DB) is reported here."""
    err = store.as_db_error(e)
    if err.escalation is None:
        store.report_db_error(err, actor=actor(), action=f"mcp:{tool_name}", params=params, write=False,
                              db_path=os.environ.get("AFCLAUDE_DB") or store.DB_PATH)
    h = store.handback(err)
    if isinstance(err, store.SchemaMismatch):    # not a fault to retry: say what the caller does
        h["message"] = h["next_step"] = SCHEMA_MESSAGES[isinstance(err, store.SchemaTooNew)]
    return ToolError(json.dumps({"error": h}, ensure_ascii=False))


def tool_errors(fn: Callable[P, Awaitable[str]]) -> Callable[P, Awaitable[str]]:
    """Caller errors -> ToolError with a one-line message (no traceback); database errors ->
    the structured hand-back (db_error)."""
    @functools.wraps(fn)
    async def wrapper(*a: P.args, **kw: P.kwargs) -> str:
        try:
            return await fn(*a, **kw)
        except ToolError:
            raise
        except LookupError as e:                         # store.NotFound
            raise ToolError(e.args[0] if e.args else str(e)) from None
        except store.InvalidTransition as e:
            raise ToolError(f"invalid transition: {e}") from None
        except ValueError as e:
            raise ToolError(str(e)) from None
        except (store.DBError, sqlite3.Error) as e:      # incl. the schema guard (SchemaMismatch)
            raise db_error(e, getattr(fn, "__name__", "tool"), {k: v for k, v in kw.items() if k != "ctx"}) from None
    return wrapper


def tool(description: str, read_only: bool = False
         ) -> Callable[[Callable[P, Awaitable[str]]], Callable[P, Awaitable[str]]]:
    def deco(fn: Callable[P, Awaitable[str]]) -> Callable[P, Awaitable[str]]:
        wrapped = tool_errors(fn)
        server.add_tool(wrapped, name=fn.__name__, description=USE_ONLY + description, structured_output=False,
                        annotations=READ_ONLY if read_only else None)
        return wrapped
    return deco


# ---------------------------------------------------------------- tools

@tool("Add an AFClaude task (a stage appended to a project). project: name or directory; default = the calling "
      "session's directory (its project, created if new). priority default high. Returns the task.")
async def afclaude_add_task(title: str, description: str = "", priority: Priority = "high",
                            project: Optional[str] = None, ctx: Optional[Context] = None) -> str:
    source = "explicit"
    if project is None or not project.strip():
        project, source = await caller_dir(ctx)
        if project is None:
            source = "none"
    else:
        project = await resolve_dir_arg(ctx, project)
    conn = db()
    try:
        t = act(conn, "task.add", title=title, description=description or None, project=project,
                priority=priority, created_by_session=session_id_env())
    finally:
        conn.close()
    return out(dict(task_brief(t), project_source=source))


@tool("List AFClaude tasks in execution order. status: open (default: pending/in_progress/blocked), ready (the "
      "run queue), all, or one status. project: name, directory, or '.' (caller's project).", read_only=True)
async def afclaude_list_tasks(status: str = "open", project: Optional[str] = None, limit: int = 20,
                              ctx: Optional[Context] = None) -> str:
    st = {"open": list(store.OPEN_STATUSES), "ready": "pending", "all": None}.get(status, status)
    project = await resolve_dir_arg(ctx, project)
    conn = db()
    try:
        rows = store.list_tasks(conn, status=st, project=project)
    finally:
        conn.close()
    limit = max(1, min(int(limit), 200))
    return out({"total": len(rows), "tasks": [task_brief(t) for t in rows[:limit]]})


@tool("Get one AFClaude task with description, Q&A and full event history.", read_only=True)
async def afclaude_get_task(id: int) -> str:
    conn = db()
    try:
        t = store.get_task(conn, id)
        if t is None:
            raise store.NotFound(f"no task #{id}")
        return out(task_full(t, store.task_events(conn, id)))
    finally:
        conn.close()


@tool("Change an AFClaude task. Fields: title, description, priority, project (moves it to the end of that "
      "project), stage (position in its project, 1 = first). status: done (note = summary), cancelled (note = "
      "reason), pending (reopen), in_progress, blocked (note = the question for the user). estimated_sessions: "
      "how many full session windows the stage still needs (the stage ETA, D-213). One call is atomic.")
async def afclaude_update_task(id: int, title: Optional[str] = None, description: Optional[str] = None,
                               priority: Optional[Priority] = None, project: Optional[str] = None,
                               stage: Optional[int] = None,
                               status: Optional[Literal["pending", "in_progress", "blocked", "done",
                                                        "cancelled"]] = None,
                               note: Optional[str] = None, estimated_sessions: Optional[float] = None,
                               ctx: Optional[Context] = None) -> str:
    fields: dict[str, str | float | None] = {k: v for k, v in (("title", title), ("description", description),
                                                               ("priority", priority)) if v is not None}
    if estimated_sessions is not None:
        fields["estimated_sessions"] = estimated_sessions
    if project is not None:
        fields["project"] = await resolve_dir_arg(ctx, project) if project.strip() else None
    if not fields and stage is None and status is None:
        raise ValueError("nothing to change (give title/description/priority/project/stage/status/"
                         "estimated_sessions)")
    conn = db()
    try:
        t = act(conn, "task.update", task_id=id, fields=fields or None, stage=stage, status=status, note=note,
                session=session_id_env())
        return out(task_brief(t))
    finally:
        conn.close()


@tool("Answer a blocked AFClaude task's question; it becomes pending (ready for the next run) again. Not for a "
      "question about a decision entry (the owner decides those).")
async def afclaude_answer_task(id: int, answer: str) -> str:
    conn = db()
    try:
        t = act(conn, "task.answer", task_id=id, answer=answer)
    finally:
        conn.close()
    return out(dict(task_brief(t), question=t["blocked_question"], answer=t["answer"]))


@tool("Ask the user a question you can't decide yourself: it becomes a blocked AFClaude task (kind question) in "
      "their inbox; their answer closes it (read it with afclaude_get_task). project: name or directory; default = "
      "the calling session's directory. options: short answer choices (the owner can always type their own). "
      "Returns the task.")
async def afclaude_ask(question: str, project: Optional[str] = None, options: Optional[list[str]] = None,
                       ctx: Optional[Context] = None) -> str:
    if project is None or not project.strip():
        project, _ = await caller_dir(ctx)
    else:
        project = await resolve_dir_arg(ctx, project)
    conn = db()
    try:
        t = act(conn, "task.ask", question=question, project=project, options=options,
                created_by_session=session_id_env())
    finally:
        conn.close()
    return out(task_brief(t))


@tool("AFClaude projects, a ranked list (1 = top). action: list; add (name, optional rank, path, description); "
      "move (name, rank); prio (name, priority: sets every open stage of the project); edit (name, new_name/"
      "description/path/manager_session). name may be '.' = the caller's project; path '.' = the caller's "
      "directory. manager_session (session id or unique prefix; '' = none) makes the project managed: that "
      "session (the project's task-manager) works its stages itself. launch (name, model and/or effort; "
      "owner-only, D-260): the model / effort of the project's task-manager session from its next start or "
      "resume; 'default' clears one.")
async def afclaude_project(action: Literal["list", "add", "move", "prio", "edit", "launch"], name: Optional[str] = None,
                           rank: Optional[int] = None, priority: Optional[Priority] = None,
                           description: Optional[str] = None, new_name: Optional[str] = None,
                           path: Optional[str] = None, manager_session: Optional[str] = None,
                           model: Optional[str] = None, effort: Optional[str] = None,
                           ctx: Optional[Context] = None) -> str:
    conn = db()
    try:
        if action == "list":
            return out({"projects": [project_brief(p) for p in store.list_projects(conn)]})
        if not name or not name.strip():
            raise ValueError(f"{action} needs name")
        path = await resolve_dir_arg(ctx, path)
        if action == "add":
            p = act(conn, "project.add", name=name, description=description, path=path, rank=rank)
            return out(project_brief(p))
        ref = await resolve_dir_arg(ctx, name)
        if action == "move":
            if rank is None:
                raise ValueError("move needs rank")
            p = act(conn, "project.move", project=ref, rank=rank)
        elif action == "prio":
            if priority is None:
                raise ValueError("prio needs priority")
            r = act(conn, "project.priority", project=ref, priority=priority)
            return out({"project": r["project"], "priority": r["priority"], "changed_tasks": r["changed"]})
        elif action == "launch":
            p = act(conn, "project.manager_launch", project=ref, model=model, effort=effort)
        else:
            f: dict[str, str | None] = {k: v for k, v in (("name", new_name), ("description", description),
                                                          ("path", path)) if v is not None}
            if manager_session is not None:
                f["manager_session"] = (cli.resolve_session(conn, manager_session.strip())
                                        if manager_session.strip() else None)
            if not f:
                raise ValueError("edit needs new_name, description, path or manager_session")
            p = act(conn, "project.edit", project=ref, **f)
        return out(project_brief(p))
    finally:
        conn.close()


def _scan() -> str | None:
    """Incremental stalled-session scan (stalled.py) in its own connection; error text or None."""
    try:
        import stalled
        conn = db()
        try:
            stalled.scan(conn, os.environ.get("AFCLAUDE_PROJECTS_DIR") or None)
        finally:
            conn.close()
        return None
    except Exception as e:      # a broken transcript must not hide the blocked tasks
        return f"{type(e).__name__}: {e}"


async def _scan_async() -> str | None:
    import anyio
    return await anyio.to_thread.run_sync(_scan)


@tool("What is waiting for the user: blocked AFClaude tasks (answer with afclaude_answer_task) and stalled "
      "Claude Code sessions with no continue/ignore decision (afclaude_decide_session). Scans transcripts first.",
      read_only=True)
async def afclaude_inbox() -> str:
    err = await _scan_async()
    conn = db()
    try:
        box = store.pending_user_input(conn)
    finally:
        conn.close()
    res: dict[str, Any] = {"blocked_tasks": [dict(task_brief(t), asked=bt(t["blocked_at"]))
                                             for t in box["blocked_tasks"]],
           "undecided_sessions": [session_brief(s) for s in box["undecided_sessions"]]}
    if err:
        res["scan_error"] = err
    return out(res)


@tool("Decide a stalled Claude Code session (id or unique prefix): continue (resume it in the nightly window), "
      "ignore, or clear the decision. Applies to its current stall only; use afclaude_rule for standing rules.")
async def afclaude_decide_session(session: str, decision: Literal["continue", "ignore", "clear"],
                                  note: Optional[str] = None) -> str:
    err = await _scan_async()
    conn = db()
    try:
        sid = cli.resolve_session(conn, session.strip())
        if decision == "clear":
            act(conn, "session.clear", session_id=sid)
        else:
            act(conn, "session.decide", session_id=sid, decision=decision, note=note)
        dec, src = store.effective_decision(conn, sid)
    finally:
        conn.close()
    return out(compact({"session_id": sid, "decision": dec or "undecided", "source": src, "scan_error": err}))


@tool("Standing continue/ignore rules for stalled sessions. action: list; add (scope session|project, match, "
      "decision); rm (id). match: session id/prefix, or for project a directory ('.' = caller's; covers "
      "subdirs) or a ~/.claude/projects dir name.")
async def afclaude_rule(action: Literal["add", "list", "rm"], scope: Optional[Literal["session", "project"]] = None,
                        match: Optional[str] = None, decision: Optional[Literal["continue", "ignore"]] = None,
                        id: Optional[int] = None, note: Optional[str] = None,
                        ctx: Optional[Context] = None) -> str:
    conn = db()
    try:
        if action == "list":
            return out({"rules": [compact(dict(r, created_at=bt(r["created_at"]))) for r in store.list_rules(conn)]})
        if action == "rm":
            if id is None:
                raise ValueError("rm needs id")
            act(conn, "rule.remove", rule_id=id)
            return out({"removed": id})
        if scope is None or not match or decision is None:
            raise ValueError("add needs scope, match and decision")
        m: str | None = match.strip()
        if scope == "session":
            m = cli.resolve_session(conn, match.strip())
        else:
            m = await resolve_dir_arg(ctx, m)
        r = act(conn, "rule.add", scope=scope, match=m, decision=decision, note=note)
        return out(compact(dict(r, created_at=bt(r["created_at"]))))
    finally:
        conn.close()


@tool("Write your compaction hand-off (D-144): when the AFClaude keep-alive asks you to save your work before "
      "a compaction, record a SHORT structured note for your compacted self; it is injected into the continue "
      "message after the compaction. summary (2-3 sentences, required); done, in_progress, next_steps (lists of "
      "short items); owner_messages (verbatim); in_flight (exact branches, worktrees, commits, paths); lessons; "
      "working_notes (exact commands, gotchas); open_question_ids (AFClaude task ids of open questions for the "
      "user); notes. session: your "
      "full session id (default: this session's, when the client passes it). A newer hand-off replaces the "
      "older one. Returns the stored hand-off's id.")
async def afclaude_handoff(summary: str, done: Optional[list[str]] = None, in_progress: Optional[list[str]] = None,
                           next_steps: Optional[list[str]] = None, owner_messages: Optional[list[str]] = None,
                           in_flight: Optional[list[str]] = None, lessons: Optional[list[str]] = None,
                           working_notes: Optional[list[str]] = None, open_question_ids: Optional[list[int]] = None,
                           notes: Optional[str] = None, session: Optional[str] = None,
                           project: Optional[str] = None, ctx: Optional[Context] = None) -> str:
    body: dict[str, Any] = {"summary": summary, "done": done or [], "in_progress": in_progress or [],
                            "next_steps": next_steps or [], "owner_messages": owner_messages or [],
                            "in_flight": in_flight or [], "lessons": lessons or [],
                            "working_notes": working_notes or [], "open_question_ids": open_question_ids or [],
                            "notes": notes}
    project = await resolve_dir_arg(ctx, project)
    conn = db()
    try:
        sid = (session or "").strip() or session_id_env()
        if not sid:
            raise ValueError("session is unknown here: pass your full session id as session")
        if len(sid) != 36:
            sid = cli.resolve_session(conn, sid)
        h = act(conn, "handoff.write", session_id=sid, body=body, project=project)
    finally:
        conn.close()
    return out(compact({"id": h["id"], "session_id": h["session_id"], "project_id": h["project_id"],
                        "created": bt(h["created_at"]), "saved": "HANDOFF SAVED"}))


@tool("A project's docs in the AFClaude DB (D-161): progress, exceptions, alerts (logs: entries are appended), "
      "goals and review:<name> (whole documents, each change a new version). action: list (the project's docs); "
      "show (name; a log shows its newest `entries` entries, default 10, a document its whole text); append "
      "(name, text: one entry for a log, a newline is added if missing); set (name, text: the whole new text, "
      "optional version = the one you read). project: name or directory, default the calling session's. "
      "The DB is the source: the files (PROGRESS.md, GOALS.md, EXCEPTIONS.md) are imported every 10 min "
      "(hand edits) and regenerated by `tasks.py doc export`; alerts and reviews exist only here.")
async def afclaude_doc(action: Literal["list", "show", "append", "set"], name: Optional[str] = None,
                       text: Optional[str] = None, project: Optional[str] = None, entries: int = 10,
                       version: Optional[int] = None, ctx: Optional[Context] = None) -> str:
    if project is None or not project.strip():
        project, _ = await caller_dir(ctx)
        if project is None:
            raise ValueError("the calling session's project is unknown here: pass project")
    else:
        project = await resolve_dir_arg(ctx, project)
    conn = db()
    try:
        p = store.get_project(conn, project)
        if p is None:
            raise store.NotFound(f"no project {project!r}")
        if action == "list":
            return out({"project": p["name"], "docs": [
                compact({"name": d["name"], "kind": d["kind"], "entries": d["entries"], "version": d["version"],
                         "updated": bt(d["updated_at"]), "updated_by": d["updated_by"]})
                for d in store.list_docs(conn, p["id"])]})
        if not name:
            raise ValueError(f"{action} needs name")
        if action == "show":
            doc = store.get_doc(conn, p["id"], name)
            if doc is None:
                raise store.NotFound(f"no doc {name!r} for {p['name']}")
            base = {"project": p["name"], "name": name, "kind": doc["kind"], "version": doc["version"]}
            if doc["kind"] == "log":
                rows = store.doc_entries(conn, doc["id"], max(1, min(int(entries), 200)))
                return out(dict(base, entries=[compact({"id": e["id"], "op": e["op"], "at": bt(e["ts"]),
                                                        "by": e["author"], "text": e["body"]}) for e in rows]))
            return out(dict(base, text=store.doc_render(conn, doc["id"])))
        if text is None:
            raise ValueError(f"{action} needs text")
        if action == "append":
            r = act(conn, "doc.append", project=p["name"], name=name, text=text if text.endswith("\n") else text + "\n")
        else:
            r = act(conn, "doc.set", project=p["name"], name=name, text=text, version=version)
        return out(compact(r))
    finally:
        conn.close()


DECISION_HITS = 10           # entries a find returns (the rest: their ids under "more")
EXPAND_NOTICE = ("Titles and topics name the subject only: expand an entry (full=true, or action show) and read it "
                 "before acting on it or passing it on (D-176).")
FULL_NOTICE = ("This is the entry to act on; a title, list line or summary names the topic only, and an "
               "interpretation is not binding (D-176).")


def decision_line(d: Mapping[str, Any]) -> dict[str, Any]:
    """What a find lists without full: id, title, state, topics and links (D-284); no rule text."""
    return compact({"id": d["id"], "title": d["title"], "state": d["state"], "topics": d["topics"] or None,
                    "links": [store.link_text(x) for x in d["links"]] or None})


def decision_full(d: Mapping[str, Any]) -> dict[str, Any]:
    return compact({"id": d["id"], "title": d["title"], "state": d["state"], "status": d["status"],
                    "topics": d["topics"] or None, "keywords": d["effective_keywords"],
                    "links": [store.link_text(x) + " " + x["title"] for x in d["links"]] or None, "entry": d["body"]})


@tool("The owner's decisions (D-242). find (keywords/topics; all: also done, superseded, proposed): id, title, "
      "topics only, NOT enough to act on: expand (full=true, or show) before acting. show (id D-NNN): the full "
      "entry. propose (title, topics 1+, optional words/scope/interpretation/keywords/project/supersedes ids): your "
      "own decision, waits for the owner; topics are few free keywords ('all' if general), reuse existing ones. "
      "record (title, topics, words = the owner's verbatim quote(s) from one owner message of this session, inbox "
      "or chat; ref only if several match): the owner's decision, active at once.")
async def afclaude_decisions(action: Literal["find", "show", "propose", "record"],
                             keywords: Optional[list[str]] = None,
                             id: Optional[str] = None, full: bool = False, all: bool = False,
                             title: Optional[str] = None, topics: Optional[list[str]] = None,
                             words: Optional[str | list[str]] = None, scope: Optional[str] = None,
                             interpretation: Optional[str] = None, project: Optional[str] = None,
                             supersedes: Optional[list[str]] = None, ref: Optional[str] = None) -> str:
    conn = db()
    try:
        if action == "propose":
            if not title:
                raise ValueError("propose needs title")
            r = act(conn, "decision.add", title=title, topics=topics, author="task-manager", words=words,
                    scope=scope, interpretation=interpretation, keywords=keywords, project=project,
                    supersedes=supersedes)
            return out(compact({"id": r["id"], "title": r["title"], "state": r["state"], "topics": r["topics"],
                                "supersedes": r["supersedes"], "new_topics": r.get("new_topics"),
                                "topic_note": r.get("topic_note"),
                                "note": "Proposed: it waits for the owner's confirmation."}))
        if action == "record":
            if not title or not words:
                raise ValueError("record needs title and words (the owner's exact quote)")
            r = act(conn, "decision.add", title=title, topics=topics, author="owner", words=words,
                    scope=scope, interpretation=interpretation, keywords=keywords, project=project, ref=ref)
            return out(compact({"id": r["id"], "title": r["title"], "state": r["state"], "date": r["date"],
                                "source": r["source"], "topics": r["topics"], "new_topics": r.get("new_topics"),
                                "topic_note": r.get("topic_note"),
                                "note": "Recorded as the owner's decision, active, against their message."}))
        if action == "show":
            if not id:
                raise ValueError("show needs id")
            d = store.get_decision_entry(conn, id)
            if d is None:
                raise store.NotFound(f"no decision {id}")
            return out(dict(decision_full(d), note=FULL_NOTICE))
        hits = store.find_decision_entries(conn, keywords or [],
                                           None if all else ("active", "partial"))
        shown = hits[:DECISION_HITS]
        res: dict[str, Any] = {"hits": [decision_full(d) if full else decision_line(d) for d in shown]}
        if len(hits) > len(shown):
            res["more"] = [d["id"] for d in hits[len(shown):]]
        res["note"] = FULL_NOTICE if full else EXPAND_NOTICE
        return out(res)
    finally:
        conn.close()


def prompt_preview(conn: sqlite3.Connection, project: Optional[str], declared: set[str],
                   values: Optional[Mapping[str, str]]) -> tuple[Optional[dict[str, str]], dict[str, Any]]:
    """(the project's variable values, the system variables) for a preview: the project's own, its
    tool paths, the session_stop_pct setting, `values`; any other system variable shows as <name>."""
    pvals, path = None, None
    if project is not None and project.strip():
        p = store.get_project(conn, int(project) if project.isdigit() else project)
        if p is None:
            raise store.NotFound(f"no project {project!r}")
        pvals, path = prompts.project_values(conn, p["id"]), p["path"] or None
    system = prompts.preview_system(path, dict(values or {}), declared)
    return pvals, {v: system.get(v, f"<{v}>") for v in declared}


@tool("The prompts AFClaude sends (read-only; an owner override replaces the default system-wide). list: prompts "
      "and bundles, with each override's flags. bundle (name = kind) / render (name = prompt): the text a sender "
      "sends, for a project's variable values; values: system variables, the rest show as <name>. bundle: manager=false "
      "(a neutral session), hooked=true (a session the hook gave the rules) change the rules tail.", read_only=True)
async def afclaude_prompts(action: Literal["list", "bundle", "render"], name: Optional[str] = None,
                           project: Optional[str] = None, values: Optional[dict[str, str]] = None,
                           manager: bool = True, hooked: bool = False, ctx: Optional[Context] = None) -> str:
    conn = db()
    try:
        try:
            if action == "list":
                status = {r["name"]: r for r in prompts.override_status(conn)}
                return out({"prompts": [compact({
                    "name": n, "system": sorted(sp.system), "project": sorted(sp.project), "used_by": sp.used_by,
                    "bundles": prompts.bundles_using(n),
                    "override": compact({k: status[n][k] for k in ("version", "updated_at", "updated_by", "base_outdated",
                                                                    "default_changed", "fits")}) if n in status else None})
                    for n, sp in sorted(prompts.PROMPTS.items())],
                    "bundles": [{"kind": k, "parts": [part.prompt or ("insert" if part.insert else "rules")
                                                      for part in b.parts], "flat": b.flat, "used_by": b.used_by}
                                for k, b in sorted(prompts.BUNDLES.items())]})
            if not name:
                raise ValueError(f"{action} needs name")
            if project is not None:
                project = await resolve_dir_arg(ctx, project)
            if action == "bundle":
                kind = prompts.check_bundle(name)
                pvals, system = prompt_preview(conn, project, prompts.bundle_system(kind), values)
                b = prompts.BUNDLES[kind]
                return out({"kind": kind, "flat": b.flat,
                            "parts": [part.prompt or ("insert: " + part.insert if part.insert else "rules")
                                      for part in b.parts],
                            "text": prompts.bundle_in(conn, kind, pvals, system, hooked=hooked,
                                                       manager=manager)})
            prompt_name = prompts.check_name(name)
            pvals, system = prompt_preview(conn, project, set(prompts.PROMPTS[prompt_name].system), values)
            return out({"name": prompt_name, "text": prompts.render_in(conn, prompt_name, pvals, system)})
        except prompts.UnknownPrompt as e:
            raise store.NotFound(str(e)) from None
        except prompts.UnknownBundle as e:
            raise store.NotFound(str(e)) from None
        except prompts.StaleOverride as e:
            raise ValueError(str(e)) from None
    finally:
        conn.close()


def notice_brief(n: Mapping[str, Any]) -> dict[str, Any]:
    return compact({"id": n["id"], "title": n["title"], "text": n["text"], "project": n["project_name"],
                    "posted": bt(n["created_at"]), "by": n["created_by"], "source": n["source"],
                    "dismissed": bt(n["dismissed_at"])})


@tool("Notices for the owner on the AFClaude dashboard (D-306), shown until dismissed. post: title (one line), "
      "text (the full content, no file pointers), optional project. list (all: also dismissed).")
async def afclaude_notice(action: Literal["post", "list"], title: Optional[str] = None, text: Optional[str] = None,
                          project: Optional[str] = None, all: bool = False, ctx: Optional[Context] = None) -> str:
    conn = db()
    try:
        if action == "list":
            return out({"notices": [notice_brief(n) for n in store.list_notices(conn, include_dismissed=all)]})
        params: dict[str, Any] = {"title": title, "text": text}
        if project is not None and project.strip():
            ref = await resolve_dir_arg(ctx, project)
            p = store.get_project(conn, ref)
            if p is None:
                raise store.NotFound(f"no project {project!r}")
            params["project"] = p["id"]
        return out(notice_brief(act(conn, "notice.post", **params)))
    finally:
        conn.close()



@tool("Run now, no window/budget gate: work (target: project), continue (target: stalled session) or review "
      "(usage review).")
async def afclaude_request(kind: Literal["work", "continue", "review"], target: Optional[str] = None,
                           ctx: Optional[Context] = None) -> str:
    import runner
    conn = db()
    try:
        if kind == "continue":
            target = cli.resolve_session(conn, (target or "").strip())
        elif kind == "work" and target is not None:
            target = await resolve_dir_arg(ctx, target)
        usage = cli.run_now_usage()       # phase 3b S5 risk: it spends budget outside the plan
        r = act(conn, "request.add", kind=cli.RUN_NOW_KINDS[kind], target=target)
    finally:
        conn.close()
    runner.wake()
    return out(dict(request_brief(r), usage=usage))


def request_brief(r: Mapping[str, Any]) -> dict[str, Any]:
    res = json.loads(r["result"]) if r.get("result") else {}
    return compact({"id": r["id"], "kind": r["kind"], "target": r["target"], "status": r["status"],
                    "at": bt(r["ts"]), "by": r["actor"], "tmux": res.get("tmux"), "reason": res.get("reason")})

if __name__ == "__main__":
    server.run("stdio")
