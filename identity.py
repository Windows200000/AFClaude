"""Who is acting, resolved once at an entry point (phase 5a A2, docs/phase5a_plan.md §3.1 / §3.3).

`resolve()` turns what a process can show (its env, the hook's stamp on an MCP call, a machine token) into
one `Identity`: the actor string the audit log records, plus the session behind it. The entry points (the
CLIs, the MCP server, hand-run runner paths) call it instead of writing a literal actor.

  host owner    cli                      no Claude session in the env (a shell)
  machine user  machine:<id>             a valid machine token and no AFClaude-driven session
  task-manager  task-manager:<session>   the session is a project's manager_session, no subagent id
  agent         agent:<session>[:<id>]   a driven session, a task-manager's subagent, CLAUDE_GUARD_DISABLE=1,
                                         or nothing identifies the caller (the safe side)
  runner        runner:<component>       a runner process (no Claude session in its env)

A2 only records who acts: nothing here refuses (the old owner_only rule stays in force, `autonomous` feeds it).

Session ids follow the /clear chain (D-328/D-329): a pre-/clear id of a known chain is never "unknown"
(store.session_family), the id a session goes on as is the chain's newest (actions.current_session), and an
id from outside the chain is never accepted: a hook stamp naming another session than the process's own
chain is ignored when the process's session is AFClaude's, and refused (IdentityConflict) when it names
another AFClaude-driven session.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Optional

import actions
import store

SESSION_VAR = "CLAUDE_CODE_SESSION_ID"
CLAUDE_VAR = "CLAUDECODE"
AGENT_VAR = "AFCLAUDE_AGENT_ID"
GUARD_VAR = "CLAUDE_GUARD_DISABLE"
TOKEN_VAR = "AFCLAUDE_MACHINE_TOKEN"
TOKEN_FILE_VAR = "AFCLAUDE_MACHINE_FILE"
DEFAULT_TOKEN_FILE = "~/.config/afclaude/machine.json"
STAMP_KEY = "_afc"                        # the hook's field in an MCP call's arguments (plan §3.3)

CLI = "cli"
KEEPALIVE = "runner:keepalive"
DISPATCHER = "runner:dispatcher"

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")       # the session / agent part of an actor (actions.ACTOR_RE)
COMPONENT_RE = re.compile(r"^runner:[a-z0-9_-]{1,32}$")

Kind = Literal["host", "machine", "task-manager", "agent", "runner"]
TokenState = Literal["none", "valid", "invalid", "insecure-file"]


class IdentityConflict(ValueError):
    """The caller's claim contradicts the process's own chain (a stamp naming another AFClaude session)."""


@dataclass(frozen=True)
class Identity:
    """actor: the audit actor; kind: the principal kind; session: the (current) Claude session id behind
    a machine, task-manager or agent write; agent_id: the subagent id when one is known; user_id: the machine
    user; autonomous: the old rule's input (a session AFClaude launched or drives); token: what the token
    check found; source: where the session id came from (env | stamp | none)."""
    actor: str
    kind: Kind
    session: Optional[str] = None
    agent_id: Optional[str] = None
    user_id: Optional[int] = None
    autonomous: bool = False
    token: TokenState = "none"
    source: str = "none"

    def as_dict(self) -> dict[str, Any]:
        return {"actor": self.actor, "kind": self.kind, "session": self.session, "agent_id": self.agent_id,
                "machine_user": self.user_id, "autonomous": self.autonomous, "token": self.token,
                "session_source": self.source}


# ---------------------------------------------------------------- the pieces

def in_claude_session(env: Mapping[str, str]) -> bool:
    """A Claude Code process (or a child of one) is the caller: the Bash tool and the MCP server's env
    carry CLAUDECODE and the session id; a plain shell or a runner has neither. The guard bypass alone
    (CLAUDE_GUARD_DISABLE=1, only ka_resume.sh sets it) marks a session AFClaude launched too."""
    return bool(env.get(SESSION_VAR)) or bool(env.get(CLAUDE_VAR)) or env.get(GUARD_VAR) == "1"


def family(conn: sqlite3.Connection, sid: str) -> list[str]:
    """`sid` and the rest of its /clear chain, oldest first (store.session_family)."""
    return store.session_family(conn, sid)


def _is_manager(conn: sqlite3.Connection, fam: list[str]) -> bool:
    marks = ",".join("?" * len(fam))
    return conn.execute(f"SELECT 1 FROM projects WHERE manager_session IN ({marks}) LIMIT 1",
                        fam).fetchone() is not None


def _known(conn: sqlite3.Connection, fam: list[str]) -> bool:
    """The chain is a session AFClaude drives or a project's task-manager."""
    return bool(actions.autonomous_sessions(conn).intersection(fam)) or _is_manager(conn, fam)


def clean_stamp(stamp: Mapping[str, Any] | None) -> tuple[Optional[str], Optional[str]]:
    """The hook's stamp -> (session, agent id), shape-checked; a malformed stamp is refused, not guessed at."""
    if stamp is None:
        return None, None
    if not isinstance(stamp, Mapping):
        raise IdentityConflict(f"{STAMP_KEY} must be an object")
    sid, agent = stamp.get("session"), stamp.get("agent")
    if sid is not None and not (isinstance(sid, str) and ID_RE.match(sid)):
        raise IdentityConflict(f"{STAMP_KEY}.session is not a session id")
    if agent is not None and not (isinstance(agent, str) and ID_RE.match(agent)):
        raise IdentityConflict(f"{STAMP_KEY}.agent is not an agent id")
    return sid, agent


def resolve_session(conn: sqlite3.Connection, env_sid: str | None,
                    stamp_sid: str | None) -> tuple[Optional[str], str]:
    """The calling session's current id and where it came from. The server's own env keeps the pre-/clear
    id (a /clear goes on in the same process), the hook's stamp is what Claude Code says now. The stamp
    counts when it is in the env id's chain (-> the chain's newest id) or when neither side is a session
    AFClaude knows (an interactive session's /clear, which the store does not follow). A stamp outside the
    chain of a session AFClaude drives is ignored (the process's own chain wins); a stamp naming another
    driven session than the process's own is refused."""
    if env_sid is None:
        return (None, "none") if stamp_sid is None else (family(conn, stamp_sid)[-1], "stamp")
    fam = family(conn, env_sid)
    if stamp_sid is None:
        return fam[-1], "env"
    if stamp_sid in fam:
        return fam[-1], "stamp"
    if _known(conn, fam):
        return fam[-1], "env"
    other = family(conn, stamp_sid)
    if _known(conn, other):
        raise IdentityConflict("the call names an AFClaude session outside the caller's own session chain")
    return other[-1], "stamp"


def read_token(env: Mapping[str, str], path: str | None = None) -> tuple[Optional[str], TokenState]:
    """The machine token: $AFCLAUDE_MACHINE_TOKEN (5b's container form), else the token file (mode
    0600, owned by the caller; JSON with a "token" key, written by `tasks.py machine register`).
    -> (the token, "valid" meaning "present, to be checked") | (None, "none" | "insecure-file")."""
    tok = env.get(TOKEN_VAR)
    if tok:
        return tok.strip(), "valid"
    p = path or env.get(TOKEN_FILE_VAR) or os.path.expanduser(DEFAULT_TOKEN_FILE)
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return None, "none"
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        return None, "insecure-file"
    with open(p) as fh:
        doc = json.load(fh)
    t = doc.get("token") if isinstance(doc, dict) else None
    if not isinstance(t, str) or not t:
        raise ValueError(f"{p} holds no token")
    return t, "valid"


def machine_identity(conn: sqlite3.Connection, env: Mapping[str, str], via: str, transport: str,
                     token_file: str | None = None) -> tuple[Optional[dict[str, Any]], TokenState]:
    """-> (the authenticated machine {user_id, token_id, label} or None, the token state). A token that
    is unknown, revoked or malformed is "invalid": no machine identity (the caller falls to agent)."""
    token, state = read_token(env, token_file)
    if token is None:
        return None, state
    who = actions.authenticate_machine(conn, token, socket.gethostname(), transport, via=via)
    return who, ("valid" if who else "invalid")


def _guard(env: Mapping[str, str]) -> bool:
    return env.get(GUARD_VAR) == "1"


def _agent_actor(sid: str | None, agent_id: str | None) -> str:
    """agent:<session>[:<agent id>]; agent:unknown when nothing names the session."""
    if not sid:
        return "agent:unknown"
    return f"agent:{sid}:{agent_id}" if agent_id else f"agent:{sid}"


# ---------------------------------------------------------------- the resolver

def resolve(conn: sqlite3.Connection, env: Mapping[str, str] | None = None, *,
            stamp: Mapping[str, Any] | None = None, claim: str | None = None,
            transport: Literal["cli", "mcp"] = "cli", token_file: str | None = None) -> Identity:
    """Who acts. `env`: the process's environment (default os.environ); `stamp`: the PreToolUse hook's
    `_afc` object of an MCP call; `claim`: the runner actor a runner path wants (runner:<component>),
    honoured only when no Claude session is in the env; `transport`: "mcp" for the stdio server (always
    inside a session, whatever its env shows)."""
    e = os.environ if env is None else env
    if transport == "cli" and not in_claude_session(e):
        return Identity(resolve_plain(claim), "host" if claim is None else "runner")
    stamp_sid, stamp_agent = clean_stamp(stamp)
    agent_id = stamp_agent if transport == "mcp" else (e.get(AGENT_VAR) or None)
    if agent_id is not None and not ID_RE.match(agent_id):
        raise IdentityConflict(f"{AGENT_VAR} is not an agent id")
    sid, source = resolve_session(conn, e.get(SESSION_VAR) or None, stamp_sid if transport == "mcp" else None)
    fam = family(conn, sid) if sid else []
    manager = bool(fam) and _is_manager(conn, fam)
    driven = bool(fam) and bool(actions.autonomous_sessions(conn).intersection(fam))
    if manager and agent_id is None:
        return Identity(f"task-manager:{sid}", "task-manager", sid, None, None, True, "none", source)
    if driven or manager or _guard(e):
        return Identity(_agent_actor(sid, agent_id), "agent", sid, agent_id, None, True, "none", source)
    who, token = machine_identity(conn, e, transport, "stdio", token_file)
    if who is not None:
        return Identity(f"machine:{who['user_id']}", "machine", sid, agent_id, int(who["user_id"]), False,
                        token, source)
    return Identity(_agent_actor(sid, agent_id), "agent", sid, agent_id, None, False, token, source)


# ---------------------------------------------------------------- conveniences for the entry points

def cli_identity(conn: sqlite3.Connection, claim: str | None = None) -> Identity:
    """The CLI's principal for this process (os.environ)."""
    return resolve(conn, os.environ, claim=claim)


def resolve_plain(claim: str | None = None) -> str:
    """The actor outside any Claude session (needs no database): a runner claim, else `cli`."""
    if claim is None:
        return CLI
    if not COMPONENT_RE.match(claim):
        raise ValueError(f"bad runner actor {claim!r}")
    return claim


def cli_actor(db: str | None = None, claim: str | None = None) -> str:
    """The actor of a library function that has no connection of its own: a shell stays `cli` (or the
    runner claim) without opening the database; inside a Claude session the database classifies it."""
    if not in_claude_session(os.environ):
        return resolve_plain(claim)
    conn = store.connect(db or os.environ.get("AFCLAUDE_DB") or store.DB_PATH)
    try:
        return resolve(conn, os.environ, claim=claim).actor
    finally:
        conn.close()


def runner_actor(component: str, conn: sqlite3.Connection | None = None) -> str:
    """runner:<component> for a runner process; the same code run by hand inside a Claude session
    (keepalive.py --decide, the task-manager's deploy steps) acts as that session's principal instead:
    a runner actor claimed from inside a session is never accepted."""
    claim = f"runner:{component}"
    if conn is not None:
        return resolve(conn, os.environ, claim=claim).actor
    return cli_actor(claim=claim)


def fallback_actor(env: Mapping[str, str] | None = None) -> str:
    """A label for an error report when the database itself failed (no classification possible):
    `cli` outside a session, else agent:<session> (the safe side)."""
    e = os.environ if env is None else env
    if not in_claude_session(e):
        return CLI
    sid = e.get(SESSION_VAR) or None
    return _agent_actor(sid if sid and ID_RE.match(sid) else None, None)
