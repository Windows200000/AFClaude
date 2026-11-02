"""Roles, grants and machine credentials, the pure part (docs/phase5a_plan.md §3.1, §3.2, §3.5, §3.6;
D-149, D-172, D-183, D-184, D-190, D-192, D-195, D-198).

  principals     parse_actor(): an actor string -> the kind of principal it names (§3.1)
  roles          effective_role(): one user's grants -> the role on a project (owner > manager > editor >
                 viewer); visible_projects(); at_least()
  grant rules    check_grant_change(): who may set or remove which grant (owners anything, a project's
                 managers editor and viewer on that project only, D-192)
  last owner     manual_person_owner_ids() / check_last_owner(): D-183 (a person whose owner grant is manual;
                 a group-derived owner and a machine owner never count)
  group sync     plan_group_sync(): the `oidc:<group>` grants a login's groups give vs the ones stored,
                 on top of dashboard.auth.oidc_groups (the map and the diff are 6b's, pure too)
  credentials    registration codes and machine tokens: generation, normalising, hashing, the constant-time
                 compare (only a hash is ever stored: store.machine_tokens / machine_registrations)

No database, no I/O: actions.py (grant.*, user.*, machine.*) reads the rows into the dataclasses below and
writes the result. Nothing here is a permission check on an action yet (the matrix is step A3); the
D-192 rule in check_grant_change() is the one rule about who may grant.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Iterable, Literal, Optional

from dashboard.auth import oidc_groups as og

Level = og.Level
LEVELS = og.LEVELS                                  # ("viewer", "editor", "manager"), lowest first
Role = Literal["owner", "manager", "editor", "viewer"]
ROLE_RANK: dict[str, int] = {"viewer": 1, "editor": 2, "manager": 3, "owner": 4}
Scope = Literal["owner", "project"]
SCOPES: tuple[str, ...] = ("owner", "project")
UserKind = Literal["person", "machine"]
USER_KINDS: tuple[str, ...] = ("person", "machine")
MANUAL = "manual"
OIDC_PREFIX = "oidc:"


class Denied(Exception):
    """A role rule says no (actions.py turns it into Forbidden)."""


# ---------------------------------------------------------------- principals (§3.1)

PrincipalKind = Literal["host", "user", "machine", "task-manager", "agent", "runner", "other"]
_ACTOR_FORMS: tuple[tuple[PrincipalKind, "re.Pattern[str]"], ...] = (
    ("host", re.compile(r"^(cli|owner)$")),
    ("user", re.compile(r"^user:(?P<id>[1-9][0-9]{0,17})$")),
    ("machine", re.compile(r"^machine:(?P<id>[1-9][0-9]{0,17})$")),
    ("task-manager", re.compile(r"^task-manager:(?P<session>[A-Za-z0-9._-]{1,64})$")),
    ("agent", re.compile(r"^agent:(?P<session>[A-Za-z0-9._-]{1,64})(:(?P<agent>[A-Za-z0-9._-]{1,64}))?$")),
    ("runner", re.compile(r"^runner(:(?P<component>[a-z0-9_-]{1,32}))?$")),
)


@dataclass(frozen=True)
class Principal:
    """Who an actor string names. `user_id` is set for a person or machine user (the users.id the grants
    belong to); `session` for a task-manager or agent. `other` = a legacy actor with no role of its own
    yet (dispatcher, keepalive, setup, mcp[:<session>])."""
    kind: PrincipalKind
    actor: str
    user_id: Optional[int] = None
    session: Optional[str] = None

    @property
    def has_grants(self) -> bool:
        return self.user_id is not None


def parse_actor(actor: str) -> Principal:
    for kind, rx in _ACTOR_FORMS:
        m = rx.match(actor)
        if m is None:
            continue
        groups = m.groupdict()
        uid = int(groups["id"]) if groups.get("id") else None
        return Principal(kind, actor, uid, groups.get("session"))
    return Principal("other", actor)


# ---------------------------------------------------------------- grants and effective roles

@dataclass(frozen=True)
class GrantRow:
    """One grants row as the rules see it."""
    user_id: int
    scope: Scope
    project_id: Optional[int]
    level: Optional[Level]
    source: str = MANUAL

    @property
    def is_manual(self) -> bool:
        return self.source == MANUAL


@dataclass(frozen=True)
class UserRow:
    id: int
    kind: UserKind
    revoked: bool = False


def check_grant_shape(scope: str, project_id: Optional[int], level: Optional[str], source: str) -> None:
    """The stored form: `owner` has no project and no level; `project` has both; the source is `manual`
    or `oidc:<group>`."""
    if scope == "owner":
        if project_id is not None or level is not None:
            raise ValueError("an owner grant has no project and no level")
    elif scope == "project":
        if project_id is None or level not in LEVELS:
            raise ValueError(f"a project grant needs a project and a level ({'|'.join(LEVELS)})")
    else:
        raise ValueError(f"scope must be one of {'|'.join(SCOPES)}, got {scope!r}")
    if source != MANUAL and not (source.startswith(OIDC_PREFIX) and len(source) > len(OIDC_PREFIX)):
        raise ValueError(f"source must be 'manual' or 'oidc:<group>', got {source!r}")


def is_owner(grants: Iterable[GrantRow]) -> bool:
    return any(g.scope == "owner" for g in grants)


def effective_role(grants: Iterable[GrantRow], project_id: Optional[int]) -> Optional[Role]:
    """The role one (unrevoked) user's grants give on a project: owner on any project (and for global
    things, project_id None); else the highest level granted on that project; None = no access (D-149,
    D-195: a new user has none)."""
    best: Optional[Role] = None
    for g in grants:
        if g.scope == "owner":
            return "owner"
        if project_id is not None and g.project_id == project_id and g.level is not None:
            if best is None or ROLE_RANK[g.level] > ROLE_RANK[best]:
                best = g.level
    return best


def at_least(role: Optional[str], needed: str) -> bool:
    """Is `role` (None = no access) at least `needed`?"""
    return role is not None and ROLE_RANK[role] >= ROLE_RANK[needed]


def visible_projects(grants: Iterable[GrantRow], all_project_ids: Iterable[int]) -> frozenset[int]:
    """The projects a user sees: all for an owner, else the ones with any grant."""
    grants = list(grants)
    if is_owner(grants):
        return frozenset(all_project_ids)
    return frozenset(g.project_id for g in grants if g.project_id is not None)


def check_grant_change(grantor: Iterable[GrantRow], scope: str, project_id: Optional[int],
                       new_level: Optional[str], old_level: Optional[str] = None) -> None:
    """May a user holding `grantor`'s grants set (new_level) or remove (new_level None) this grant, which
    is `old_level` today (None = it doesn't exist yet)? Owners: anything (D-172: the owner role is
    grantable). A project's manager: `editor` and `viewer` on that project only, and may not touch
    another manager's or an owner's grant there (D-192); a manager can't make an owner or a manager.
    Anyone else: nothing."""
    grantor = list(grantor)
    if is_owner(grantor):
        return
    if scope != "project" or project_id is None:
        raise Denied("only an owner grants or removes the owner role")
    if effective_role(grantor, project_id) != "manager":
        raise Denied("only an owner or a manager of the project grants access to it")
    for lv in (new_level, old_level):
        if lv is not None and ROLE_RANK[lv] > ROLE_RANK["editor"]:
            raise Denied("a manager grants and revokes editor and viewer only (D-192)")


# ---------------------------------------------------------------- the last owner (D-183)

def manual_person_owner_ids(users: Iterable[UserRow], grants: Iterable[GrantRow]) -> frozenset[int]:
    """The unrevoked person users with a manual owner grant: the ones D-183 protects. A group-derived
    owner can lose the role at the next login and a machine owner has no dashboard login, so neither
    stands in for the install's last owner."""
    persons = {u.id for u in users if u.kind == "person" and not u.revoked}
    return frozenset(g.user_id for g in grants
                     if g.scope == "owner" and g.is_manual and g.user_id in persons)


def check_last_owner(before: frozenset[int], after: frozenset[int]) -> None:
    """A change that takes the last manual person owner away is refused. With none before (no person
    users yet, before phase 6a) there is nothing to protect."""
    if before and not after:
        raise Denied("this would remove the last owner who was not granted through an OIDC group "
                     "(D-183): make another owner first")


# ---------------------------------------------------------------- group sync (§3.5)

def plan_group_sync(current: Iterable[og.Grant], groups: Iterable[str],
                    entries: Iterable[og.MapEntry]) -> og.GrantDiff:
    """What a login's groups change in the user's `oidc:` grants (`current`: only those, project by name)."""
    return og.diff(current, og.desired_grants(groups, entries))


# ---------------------------------------------------------------- credentials (§3.6, D-195, D-198)

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"    # no 0/O/1/I: read aloud or typed from a screen
CODE_GROUPS, CODE_GROUP_LEN = 4, 5                   # 20 characters = 100 bits
CODE_TTL_MINUTES = 15
TOKEN_PREFIX = "afcm_"
TOKEN_SECRET_BYTES = 32
_TOKEN_RE = re.compile(r"^afcm_(?P<id>[1-9][0-9]{0,17})_(?P<secret>[0-9a-f]{64})$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_registration_code() -> str:
    """A one-time registration code, `ABCDE-FGHJK-LMNPQ-RSTUV`: the caller shows it once and passes only
    its code_sha256() on."""
    n = CODE_GROUPS * CODE_GROUP_LEN
    chars = "".join(secrets.choice(CODE_ALPHABET) for _ in range(n))
    return "-".join(chars[i:i + CODE_GROUP_LEN] for i in range(0, n, CODE_GROUP_LEN))


def normalise_code(code: str) -> str:
    """What is hashed: upper case, without spaces and dashes (typing it back is forgiving)."""
    return re.sub(r"[\s-]", "", code).upper()


def code_sha256(code: str) -> str:
    return sha256_hex(normalise_code(code))


def new_token_secret() -> str:
    """The random part of a machine token (32 bytes, hex)."""
    return secrets.token_hex(TOKEN_SECRET_BYTES)


def secret_sha256(secret: str) -> str:
    """What machine_tokens.token_sha256 holds: the hash of the token's random part (the id in front is
    the row id the register action returns, unknown before the insert)."""
    return sha256_hex(secret)


def format_token(token_id: int, secret: str) -> str:
    return f"{TOKEN_PREFIX}{token_id}_{secret}"


def parse_token(token: str) -> tuple[int, str]:
    """-> (machine_tokens.id, secret) of a well-formed token; ValueError for anything else (the message
    never echoes the text)."""
    m = _TOKEN_RE.match(token.strip()) if isinstance(token, str) else None
    if m is None:
        raise ValueError("not a machine token (afcm_<id>_<64 hex>)")
    return int(m["id"]), m["secret"]


def secret_matches(stored_sha256: str, secret: str) -> bool:
    """Constant-time compare of a presented secret against the stored hash."""
    return bool(_SHA_RE.match(stored_sha256)) and hmac.compare_digest(stored_sha256, secret_sha256(secret))

