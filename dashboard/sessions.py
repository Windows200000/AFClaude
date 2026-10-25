"""Browser sessions (phase 6a W5, docs/phase6a_plan.md §3.5; design §9.5, §9.8; D-162, D-179).

Logged-in sessions are server-side rows (store.py's auth_sessions, written through actions.py), so a
session is revocable at once and listable (F11); the browser holds only the cookie:

  __Host-afc_session=<id>; Path=/; Max-Age=<absolute lifetime>; Secure; HttpOnly; SameSite=Lax

The id is 256 random bits (secrets.token_urlsafe(32)); only its sha256 is stored. It rotates at
login, at step-up and at every factor change (a new row; the old one ends 'rotated'). The CSRF
token is derived from the id (HMAC-SHA256 keyed by it), so `me` can hand it out again after a
reload without the token being stored; the row keeps its sha256, which each write's X-CSRF-Token
is checked against through the session. A token from another session never matches.

A pre-session (an anonymous browser's cookie before login) is NOT a row: a W5 review found that a
row per cookieless GET /me let an unauthenticated flood hold write locks (BEGIN IMMEDIATE) with no
limit, which the runners' fail-stop on DB errors (D-235) turns into an outage. Instead it is a
stateless token -- `<payload>.<mac>`, payload = a random nonce plus an expiry, mac = HMAC-SHA256 of
the payload keyed by a server-only key (_presession_key_bytes) -- verified on read with no DB access
at all; nothing is stored, nothing to prune. Its CSRF token is derived exactly as a full session's
(HMAC-SHA256 keyed by the raw cookie value), so the binding is the same: unguessable without having
seen `me`'s response, and specific to this one token. The caller (routes.me) only starts one when
Sec-Fetch-Site says the request is same-origin, none, or absent (dashboard.security.pre_session_ok),
so a cross-site flood never even reaches the signing step.

SessionStore is the app's SessionBackend (routes.py): each request's cookie is looked up (an
unknown, ended, idle, expired or tampered/expired-pre-session is no session: refused at once, no
grace), last_seen is written at most once a minute, and the user is resolved through the `roles`
hook (5a's users and grants: person or machine, the highest role; until 5a no_roles fails, never a
guessed role, D-235). login / rotate / logout are what W6's endpoints call. Lifetimes are the auth_*
settings (actions.AuthLifetimes); a login also prunes sessions past their end plus a short retention
(actions.SESSION_PRUNE_RETENTION, finding 2 of the same review).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta, timezone

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

import actions
import store
from dashboard.auth import factors as auth_factors
from dashboard.routes import Principal, Session

COOKIE = "__Host-afc_session"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")      # token_urlsafe(32): 43 characters, no padding
# a pre-session token: 24 bytes (16-byte nonce + 8-byte expiry, ms since epoch) then a full
# HMAC-SHA256 tag, both base64url without padding (32 and 43 characters); "." never appears in
# base64url, so the two shapes can't collide and a lookup can tell them apart without the DB.
PRE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32}\.[A-Za-z0-9_-]{43}$")
CSRF_CONTEXT = b"afclaude csrf token v1"
PRE_CONTEXT = b"afclaude pre-session token v1"
PRESESSION_KEY_ENV = "AFCLAUDE_PRESESSION_KEY"   # optional override (base64, 32 bytes); see below

_presession_key_lock = threading.Lock()
_presession_key: bytes | None = None

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
    if not (ID_RE.match(raw) or PRE_TOKEN_RE.match(raw)):
        raise ValueError("not a session id")
    return f"{COOKIE}={raw}; Path=/; Max-Age={max_age_s}; Secure; HttpOnly; SameSite=Lax"


def clear_cookie_header() -> str:
    return f"{COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax"


def cookie_id(request: Request) -> str | None:
    """The request's logged-in session id, when it carries a well-formed one (never a pre-session
    token: login's `replaces` only ever ends a stored row)."""
    raw = request.cookies.get(COOKIE)
    return raw if raw is not None and ID_RE.match(raw) else None


def _presession_key_bytes() -> bytes:
    """The HMAC key that signs and checks pre-session tokens. AFCLAUDE_PRESESSION_KEY (base64, 32
    bytes) if set, else a key generated once for this process (not persisted: that would duplicate
    5b's master-key loader, §10.2, not built yet). A process restart invalidates pre-sessions in
    flight, which costs nothing -- an anonymous browser gets a fresh one from its next `me` call."""
    global _presession_key
    if _presession_key is not None:
        return _presession_key
    with _presession_key_lock:
        if _presession_key is None:
            env = os.environ.get(PRESESSION_KEY_ENV)
            if env:
                key = base64.b64decode(env)
                if len(key) != 32:
                    raise ValueError(f"{PRESESSION_KEY_ENV} must decode to 32 bytes of base64")
                _presession_key = key
            else:
                _presession_key = secrets.token_bytes(32)
    return _presession_key


def _pre_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _pre_b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _pre_mac(payload: bytes) -> bytes:
    return hmac.new(_presession_key_bytes(), PRE_CONTEXT + payload, hashlib.sha256).digest()


def new_pre_token(expires_at: datetime) -> str:
    """A fresh pre-session token good until `expires_at` (no DB row: see the module docstring)."""
    payload = secrets.token_bytes(16) + int(expires_at.timestamp() * 1000).to_bytes(8, "big")
    return f"{_pre_b64(payload)}.{_pre_b64(_pre_mac(payload))}"


def pre_token_live(raw: str, now: datetime) -> bool:
    """Whether `raw` (already matched against PRE_TOKEN_RE) is a correctly signed pre-session token
    not yet past its expiry. A bad signature and a stale token are refused alike: no session, no
    grace (as an unknown or expired full session row)."""
    payload_b64, mac_b64 = raw.split(".")
    payload = _pre_b64d(payload_b64)
    if not hmac.compare_digest(_pre_mac(payload), _pre_b64d(mac_b64)):
        return False
    expires_at = datetime.fromtimestamp(int.from_bytes(payload[16:24], "big") / 1000, tz=timezone.utc)
    return now < expires_at


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
        raw = request.cookies.get(COOKIE)
        if raw is None:
            return None
        if PRE_TOKEN_RE.match(raw):
            # stateless: no DB row, no roles hook (a pre-session never has a user)
            if not pre_token_live(raw, store._utcnow()):
                return None
            return Session(id_sha256=id_sha256(raw), csrf_token=csrf_token(raw), principal=None)
        if not ID_RE.match(raw):
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
        """A fresh pre-session for an anonymous browser: no row (see the module docstring), good for
        auth_idle_timeout_min (the setting is still read on every call, as a login's lifetimes are --
        D-162: a changed setting takes effect at once, not from the next restart)."""
        idle = await run_in_threadpool(self._pre_idle)
        raw = new_pre_token(store._utcnow() + idle)
        return Session(id_sha256=id_sha256(raw), csrf_token=csrf_token(raw), principal=None), \
            cookie_header(raw, int(idle.total_seconds()))

    def _pre_idle(self) -> timedelta:
        conn = self._connect()
        try:
            return actions.AuthLifetimes(conn).idle
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
