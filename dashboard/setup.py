"""Setup, the first-registration race guard and the first-startup wizard's own piece (phase 6a W7,
docs/phase6a_plan.md §3.7; design §9.3, §10.4; D-149 (part), D-172, D-229, D-235).

**Superseded in part by D-293 / docs/phase6b_plan.md §3.12 (P10):** the owner later asked for a URL
carrying a one-time token instead of a typed code, so whoever opens the link is the first owner, not
whoever wins the browser race. This module is still built as the first cut (P10 isn't merged yet) and
is exactly the mechanism P10 replaces; its single-transaction claim is the race guard P10 keeps.

Two things need 5a's users/grants table, not built yet (as dashboard/sessions.py's `no_roles` and
dashboard/routes.py's `no_grants`): actually writing the first owner's row, and asking whether one
already exists. Both are the `OwnerRegistry` hook below (`no_owner_registry` raises NotImplementedError
until 5a wires a real one, D-235: never a guessed "no owner yet"). The setup code's own table
(store.setup_codes), its single-use claim and the one-transaction race guard are real now and fully
tested with a fake registry; `admin reset-auth`'s session and code handling is real the same way, its
`clear_factors` call (the `credentials` / `recovery_codes` tables, W6's plan, not built either) is
the same kind of hook.

Flow: `status()` (open while no owner exists, public, no code needed) -> `begin()` (the setup code,
a username, a password: normalises and checks the code and the password rule, generates a TOTP seed
and keeps it -- with the password's hash, never the raw password -- only in this process's memory
against the caller's pre-session, expiring; a bad code or password spends nothing) -> `confirm()` (the
6-digit TOTP code): one transaction (BEGIN IMMEDIATE) re-checks no owner exists, claims the setup code
and calls `OwnerRegistry.create_owner`, all three or none (SQLite's write lock serialises two racing
confirms, so the loser's own re-check already sees the winner's committed owner, or loses the code
claim outright); any failure leaves the code unspent so the browser can simply retry. The 10 recovery
codes are generated here and returned once in the result; hashing and storing them (the pepper, the
master key they need, §7.1, isn't built either) is the registry's job, same as the TOTP seed.

Pending registration state is this process's memory, not a table (as sessions.py's pre-session key):
there is only ever one install-wide registration happening while there is no owner, and a process
restart simply loses an unfinished one -- the browser starts over, same as a pre-session expiring.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from starlette.concurrency import run_in_threadpool

import actions
import store
from dashboard.auth import passwords, recovery, totp
from dashboard.routes import Session

# §3.7: "8 groups of 4 base32 characters" = 32 characters = 160 bit, RFC 4648 base32 (no padding).
# Same alphabet and confusable mapping as recovery.py (0/1 read as O/I; case, spaces and hyphens
# don't matter on input), a different length: one single code, not ten, so no HMAC pepper (plain
# sha256 of 160 random bits is already unbruteforceable; recovery.py's pepper defends its ten
# *shorter*, 80-bit codes together).
CODE_GROUPS = 8
CODE_GROUP_LEN = 4
CODE_CHARS = CODE_GROUPS * CODE_GROUP_LEN       # 32
CODE_BYTES = CODE_CHARS * 5 // 8                # 20 bytes = 160 bit
_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
_CONFUSABLE = str.maketrans({"0": "O", "1": "I", "-": None, " ": None})

CODE_TTL_HOURS = 24.0            # not in the plan or a decision: *interpretation*, 8a checks it
PENDING_TTL = timedelta(minutes=10)   # time to scan the QR code and type the confirmation code


def generate_code() -> str:
    """A fresh setup/reset code in display form (groups of four, shown once, never stored in clear)."""
    raw = base64.b32encode(secrets.token_bytes(CODE_BYTES)).decode("ascii")
    return "-".join(raw[i:i + CODE_GROUP_LEN] for i in range(0, len(raw), CODE_GROUP_LEN))


def normalize_code(code: str) -> str | None:
    """`code` as hashed (32 alphabet characters, upper case), or None if it can't be one."""
    c = code.upper().translate(_CONFUSABLE)
    return c if len(c) == CODE_CHARS and set(c) <= _ALPHABET else None


def hash_code(code: str) -> str:
    c = normalize_code(code)
    if c is None:
        raise ValueError("not a setup code")
    return hashlib.sha256(c.encode("ascii")).hexdigest()


def code_matches(row: store.Row, code: str) -> bool:
    """Whether `code` is the one `row` (store.open_setup_code) holds, compared in constant time."""
    c = normalize_code(code)
    if c is None:
        return False
    return hmac.compare_digest(hash_code(c).encode("ascii"), row["code_hash"].encode("ascii"))


def _expired(row: store.Row) -> bool:
    expires_at = store.parse_iso(row["expires_at"])
    if expires_at is None:
        raise ValueError(f"setup code {row['id']} has no expires_at")
    return store._utcnow() >= expires_at


class SetupError(ValueError):
    """A setup/registration step refused; `code` is a short, stable reason word (never the raw
    code or password)."""
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BadCode(SetupError):
    def __init__(self, message: str = "that code is wrong, used or has expired") -> None:
        super().__init__("bad_code", message)


class SetupClosed(SetupError):
    def __init__(self, message: str = "setup is already complete") -> None:
        super().__init__("setup_closed", message)


class BadTotpCode(SetupError):
    def __init__(self, message: str = "that code doesn't match") -> None:
        super().__init__("bad_totp_code", message)


class NoPendingRegistration(SetupError):
    def __init__(self, message: str = "start over: this registration has expired or was never begun") -> None:
        super().__init__("no_pending_registration", message)


class OwnerRegistry(Protocol):
    """5a's users and grants (not built yet): whether an owner already exists, writing the first
    owner's row (inside the caller's open transaction, so the setup code's claim and the owner's
    creation are one write) and clearing an existing user's login factors (admin reset-auth; W6's
    credentials/recovery_codes tables, 6b's passkeys -- also not built). Sync: always called from
    inside a transaction a caller already opened (run_in_threadpool is the async boundary)."""
    def has_owner(self, conn: sqlite3.Connection) -> bool: ...

    def create_owner(self, conn: sqlite3.Connection, username: str, password_phc: str,
                     totp_seed: bytes, recovery_codes: list[str]) -> int: ...

    def clear_factors(self, conn: sqlite3.Connection, user_id: int) -> None: ...


class NoOwnerRegistry:
    """The registry until 5a's users and grants exist: there is nothing to decide any of this from
    (sessions.py's no_roles, routes.py's no_grants: fail loudly, D-235, never a guessed answer)."""
    def has_owner(self, conn: sqlite3.Connection) -> bool:
        raise NotImplementedError("whether an owner exists needs 5a's users table")

    def create_owner(self, conn: sqlite3.Connection, username: str, password_phc: str,
                     totp_seed: bytes, recovery_codes: list[str]) -> int:
        raise NotImplementedError("creating the first owner needs 5a's users table")

    def clear_factors(self, conn: sqlite3.Connection, user_id: int) -> None:
        raise NotImplementedError("clearing login factors needs W6's credentials/recovery_codes tables")


no_owner_registry = NoOwnerRegistry()


@dataclass(frozen=True)
class Provisioning:
    """What `begin()` hands the browser to enrol an authenticator app: never stored anywhere."""
    provisioning_uri: str
    seed_base32: str          # for "can't scan it" manual entry


@dataclass
class _Pending:
    code: str
    username: str
    password_phc: str
    totp_seed: bytes
    expires_at: datetime


USERNAME_RE_MAX = 64


def check_username(username: str) -> str:
    """A plain, display-safe username (5a will own the real rule; this is the first cut so `begin()`
    can fail before anything is kept in memory). -> the trimmed username."""
    u = username.strip()
    if not (1 <= len(u) <= USERNAME_RE_MAX):
        raise SetupError("bad_username", f"the username needs 1-{USERNAME_RE_MAX} characters")
    return u


def mint_first_owner_code(conn: sqlite3.Connection, *, ttl_hours: float = CODE_TTL_HOURS) -> str:
    """A fresh 'first_owner' code (the web process's bootstrap, once per start, only while no owner
    exists yet -- that check is the caller's: this module can't make it without 5a's users table).
    Printed to stdout (D-173), never logged or stored except as its sha256. -> the raw code."""
    code = generate_code()
    actions.perform(conn, "setup.mint_code",
                    {"kind": "first_owner", "code_hash": hash_code(code), "ttl_hours": ttl_hours},
                    actor="cli", via="cli")
    return code


def reset_auth(conn: sqlite3.Connection, user_id: int, owners: OwnerRegistry = no_owner_registry,
               ttl_hours: float = CODE_TTL_HOURS) -> str:
    """Lockout recovery (`afclaude admin reset-auth`, host access only, §9.3): ends every open
    session of `user_id`, clears its login factors and mints a fresh 'reset' code for it, in one
    transaction (a crash never leaves the user without sessions or factors but also without a code
    back in). -> the raw code (print it; never stored or logged elsewhere)."""
    code = generate_code()
    with store.transaction(conn):
        actions.perform(conn, "auth.session_revoke", {"user_id": user_id}, actor="cli", via="cli")
        owners.clear_factors(conn, user_id)
        actions.perform(conn, "setup.mint_code",
                        {"kind": "reset", "user_id": user_id, "code_hash": hash_code(code),
                         "ttl_hours": ttl_hours}, actor="cli", via="cli")
    return code


class SetupFlow:
    """The web app's setup/first-registration endpoints call this (dashboard.sessions.SessionStore
    is its sibling: both are per-app instances holding what a request needs). `owners`: 5a's real
    registry once it exists, else `no_owner_registry`."""

    def __init__(self, db_path: str | None, owners: OwnerRegistry = no_owner_registry) -> None:
        self.db_path = db_path
        self.owners = owners
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        return store.connect(self.db_path)

    def _prune_pending(self, now: datetime) -> None:
        for key in [k for k, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[key]

    async def status(self) -> str:
        """'open' (no owner yet, `/setup` is reachable) or 'closed'."""

        def run() -> str:
            conn = self._connect()
            try:
                return "closed" if self.owners.has_owner(conn) else "open"
            finally:
                conn.close()
        return await run_in_threadpool(run)

    async def begin(self, session: Session, code: str, username: str, password: str) -> Provisioning:
        """The setup code, a chosen username and password: checked and, if they pass, a fresh TOTP
        seed is generated and kept (with the password's hash, never the password itself) against
        this browser's pre-session until `confirm()` or PENDING_TTL. Nothing is written yet -- a
        wrong code or password here spends nothing, and two browsers may both `begin()` with the
        same code (only one can `confirm()` it, §3.7)."""
        if session.principal is not None:
            raise SetupError("already_signed_in", "sign out first")
        clean_user = check_username(username)
        passwords.check_rule(password)   # PasswordRuleError (a SetupError) before anything else

        def run() -> None:
            conn = self._connect()
            try:
                if self.owners.has_owner(conn):
                    raise SetupClosed()
                row = store.open_setup_code(conn, "first_owner", None)
                if row is None or _expired(row) or not code_matches(row, code):
                    raise BadCode()
            finally:
                conn.close()
        await run_in_threadpool(run)

        seed = totp.new_seed()
        phc = passwords.hash_password(password)
        now = store._utcnow()
        with self._lock:
            self._prune_pending(now)
            self._pending[session.id_sha256] = _Pending(code, clean_user, phc, seed, now + PENDING_TTL)
        return Provisioning(totp.provisioning_uri(seed, clean_user),
                            base64.b32encode(seed).decode("ascii"))

    async def confirm(self, session: Session, totp_code: str) -> tuple[int, list[str]]:
        """The 6-digit confirmation code: on a match, one transaction claims the setup code and
        creates the owner (both or neither). -> (user_id, the 10 recovery codes, shown once)."""
        with self._lock:
            self._prune_pending(store._utcnow())
            pending = self._pending.get(session.id_sha256)
        if pending is None:
            raise NoPendingRegistration()
        if totp.verify(pending.totp_seed, totp_code, time.time(), None) is None:
            raise BadTotpCode()
        codes = recovery.generate()

        def run() -> int:
            conn = self._connect()
            try:
                with store.transaction(conn):
                    if self.owners.has_owner(conn):
                        raise SetupClosed()
                    row = store.open_setup_code(conn, "first_owner", None)
                    if row is None or _expired(row) or not code_matches(row, pending.code):
                        raise BadCode()
                    actions.perform(conn, "setup.claim_code", {"id": row["id"], "kind": "first_owner"},
                                    actor="setup", via="dashboard")
                    return self.owners.create_owner(conn, pending.username, pending.password_phc,
                                                    pending.totp_seed, codes)
            finally:
                conn.close()
        user_id = await run_in_threadpool(run)
        with self._lock:
            self._pending.pop(session.id_sha256, None)
        return user_id, codes
