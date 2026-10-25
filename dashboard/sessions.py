"""Browser sessions (phase 6a W5, docs/phase6a_plan.md §3.5; design §9.5, §9.8; D-162, D-179).

Server-side rows (store.py's auth_sessions, written through actions.py), so a session is revocable
at once and listable (F11); the browser holds only the cookie:

  __Host-afc_session=<id>; Path=/; Max-Age=<absolute lifetime>; Secure; HttpOnly; SameSite=Lax

The id is 256 random bits (secrets.token_urlsafe(32)); only its sha256 is stored. It rotates at
login, at step-up and at every factor change (a new row; the old one ends 'rotated'). The CSRF
token is derived from the id (HMAC-SHA256 keyed by it), so `me` can hand it out again after a
reload without the token being stored; the row keeps its sha256, which each write's X-CSRF-Token
is checked against through the session. A token from another session never matches.

SessionStore is the app's SessionBackend (routes.py): each request's cookie is looked up (an
unknown, ended, idle or expired session is no session: refused at once, no grace), last_seen is
written at most once a minute, and the user is resolved through the `roles` hook (5a's users and
grants: person or machine, the highest role; until 5a no_roles fails, never a guessed role, D-235).
login / rotate / logout are what W6's endpoints call. Lifetimes are the auth_* settings
(actions.AuthLifetimes).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

import actions
import store
from dashboard.auth import factors as auth_factors
from dashboard.routes import Principal, Session

COOKIE = "__Host-afc_session"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")      # token_urlsafe(32): 43 characters, no padding
CSRF_CONTEXT = b"afclaude csrf token v1"

Roles = Callable[[int], Awaitable[Principal]]


async def no_roles(user_id: int) -> Principal:
    """The roles hook until 5a's users and grants exist: there is nothing to decide a role from."""
    raise NotImplementedError("a signed-in user's role needs 5a's users and grants (roles.principal)")


def new_id() -> str:
    return secrets.token_urlsafe(32)


def id_sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def csrf_token(raw: str) -> str:
    mac = hmac.new(raw.encode("ascii"), CSRF_CONTEXT, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def csrf_sha256(raw: str) -> str:
    return hashlib.sha256(csrf_token(raw).encode("ascii")).hexdigest()


def cookie_header(raw: str, max_age_s: int) -> str:
    if not ID_RE.match(raw):
        raise ValueError("not a session id")
    return f"{COOKIE}={raw}; Path=/; Max-Age={max_age_s}; Secure; HttpOnly; SameSite=Lax"


def clear_cookie_header() -> str:
    return f"{COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax"


def cookie_id(request: Request) -> str | None:
    """The request's session id, when it carries a well-formed one."""
    raw = request.cookies.get(COOKIE)
    return raw if raw is not None and ID_RE.match(raw) else None


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _factor_names(done: Sequence[str]) -> list[str]:
    names = list(done)
    unknown = set(names) - auth_factors.FACTORS
    if unknown or not names or len(set(names)) != len(names):
        raise ValueError(f"bad login factors {names!r}")
    return names


class SessionStore:
    """The sessions of one database (`db_path`, None = store.DB_PATH)."""

    def __init__(self, db_path: str | None, roles: Roles = no_roles) -> None:
        self.db_path = db_path
        self.roles = roles

    def _connect(self) -> sqlite3.Connection:
        return store.connect(self.db_path)

    # --- the SessionBackend of routes.py ----------------------------------------------------------

    async def authenticate(self, request: Request) -> Session | None:
        raw = cookie_id(request)
        if raw is None:
            return None
        row = await run_in_threadpool(self._lookup, raw)
        if row is None:
            return None
        principal: Principal | None = None
        if row["user_id"] is not None:
            principal = await self.roles(row["user_id"])
            if principal.user_id != row["user_id"]:
                raise RuntimeError(f"the roles hook answered for user {principal.user_id}, not {row['user_id']}")
        return Session(id_sha256=row["id_sha256"], csrf_token=csrf_token(raw), principal=principal,
                       factors=tuple(row["factors"]), authenticated_at=row["authenticated_at"])

    def _lookup(self, raw: str) -> store.Row | None:
        conn = self._connect()
        try:
            row = store.get_auth_session(conn, id_sha256(raw))
            if row is None:
                return None
            if not hmac.compare_digest(row["csrf_sha256"], csrf_sha256(raw)):
                raise RuntimeError(f"session {row['id']}: its CSRF hash doesn't belong to its id")
            if actions.auth_session_state(row, actions.AuthLifetimes(conn)) != "live":
                return None
            if store._utcnow() - _seen(row) >= actions.TOUCH_EVERY:
                actions.auth_session_touch(conn, row["id"])
            return row
        finally:
            conn.close()

    async def start_pre(self, request: Request) -> tuple[Session, str]:
        raw = new_id()
        max_age = await run_in_threadpool(self._start_pre, raw, _user_agent(request))
        return Session(id_sha256=id_sha256(raw), csrf_token=csrf_token(raw), principal=None), \
            cookie_header(raw, max_age)

    def _start_pre(self, raw: str, user_agent: str | None) -> int:
        conn = self._connect()
        try:
            actions.auth_session_start_pre(conn, id_sha256(raw), csrf_sha256(raw), user_agent)
            return _max_age(conn)
        finally:
            conn.close()

    # --- what the login, step-up and logout endpoints call (W6, 6b) ---------------------------------

    async def login(self, request: Request, user_id: int, done: Sequence[str]) -> tuple[Session, str]:
        """A completed login (the caller applied the factor rule): a new full session replacing the
        browser's current one. -> the session (its user unresolved: principal None until the next
        request) and its Set-Cookie value."""
        names, old, raw = _factor_names(done), cookie_id(request), new_id()

        def run() -> int:
            conn = self._connect()
            try:
                actions.perform(conn, "auth.session_login",
                                {"user_id": user_id, "id_sha256": id_sha256(raw), "csrf_sha256": csrf_sha256(raw),
                                 "factors": names, "replaces": id_sha256(old) if old is not None else None,
                                 "user_agent": _user_agent(request)}, actor=f"user:{user_id}", via="dashboard")
                return _max_age(conn)
            finally:
                conn.close()
        max_age = await run_in_threadpool(run)
        return Session(id_sha256(raw), csrf_token(raw), None, tuple(names)), cookie_header(raw, max_age)

    async def rotate(self, session: Session, done: Sequence[str] | None = None, *,
                     step_up: bool = False) -> tuple[Session, str]:
        """A new id for the session (a step-up, `step_up=True`, or a factor change, `done` = the
        factors from now on). -> the new session and its Set-Cookie value."""
        if session.principal is None:
            raise ValueError("a pre-session has no login to rotate")
        user_id, raw = session.principal.user_id, new_id()
        names = _factor_names(done) if done is not None else None

        def run() -> int:
            conn = self._connect()
            try:
                actions.perform(conn, "auth.session_rotate",
                                {"id_sha256": session.id_sha256, "new_id_sha256": id_sha256(raw),
                                 "new_csrf_sha256": csrf_sha256(raw), "factors": names, "authenticated": step_up},
                                actor=f"user:{user_id}", via="dashboard")
                return _max_age(conn)
            finally:
                conn.close()
        max_age = await run_in_threadpool(run)
        return (Session(id_sha256(raw), csrf_token(raw), session.principal,
                        tuple(names) if names is not None else session.factors), cookie_header(raw, max_age))

    async def logout(self, session: Session) -> str:
        """End the session. -> the Set-Cookie value that clears the cookie."""
        if session.principal is None:
            raise ValueError("a pre-session has no login to sign out of")
        user_id = session.principal.user_id

        def run() -> None:
            conn = self._connect()
            try:
                actions.perform(conn, "auth.session_logout", {"id_sha256": session.id_sha256},
                                actor=f"user:{user_id}", via="dashboard")
            finally:
                conn.close()
        await run_in_threadpool(run)
        return clear_cookie_header()


def _seen(row: store.Row) -> datetime:
    seen = store.parse_iso(row["last_seen_at"])
    if seen is None:
        raise ValueError(f"session {row['id']} has no last_seen_at")
    return seen


def _max_age(conn: sqlite3.Connection) -> int:
    """The cookie's Max-Age: the absolute lifetime (the server decides validity on every request)."""
    return int(actions.AuthLifetimes(conn).absolute.total_seconds())
