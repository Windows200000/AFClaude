"""OIDC group claims → grants, the pure part (docs/phase6b_plan.md §3.6; design §2, §9.4;
docs/phase5a_plan.md §3.2, §3.5; D-149, D-172, D-183).

  parse_group_map()  the `auth_oidc_group_map` setting, `[{group, grant}]` with a grant `owner` or
                     `project:<name>:<viewer|editor|manager>`; checked when it is saved and read
  desired_grants()   the group grants a login's groups give: one per matching map entry, source
                     `oidc:<group>` (the grants table's form); no matching group = none (D-149)
  diff()             the user's current `oidc:` grants against the desired ones: what to add, what to
                     remove, and whether `owner` is newly granted (an alert to the owners, §3.6)
  require_groups()   the groups to map, or a refusal when the map needs them and the provider sent none

No DB here: `grant.sync_groups` (5a, through actions.py) resolves project names to ids, writes the
diff, never touches manual grants, and keeps the last-owner guard (D-183: group-derived owners
don't count for it). Group names match exactly (case-sensitive), as the provider sends them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Optional, get_args

Level = Literal["viewer", "editor", "manager"]
LEVELS: tuple[str, ...] = get_args(Level)
MAX_ENTRIES = 200


class GroupMapError(ValueError):
    """The group map setting is malformed (refused at save; at login it refuses the login)."""


class GroupsMissing(Exception):
    """The group map has entries but neither the ID token nor userinfo carried the groups claim:
    the grants can't be computed, so the login is refused (D-235), not treated as no groups."""


@dataclass(frozen=True)
class Grant:
    """One grant row as 5a stores it: scope `owner` (no project, no level) or `project`."""
    scope: Literal["owner", "project"]
    project: Optional[str]          # the project's name as in the map; sync_groups resolves the id
    level: Optional[Level]
    source: str                     # "oidc:<group>"

    @property
    def is_owner(self) -> bool:
        return self.scope == "owner"


@dataclass(frozen=True)
class MapEntry:
    group: str
    grant: str                      # as written: "owner" or "project:<name>:<level>"
    scope: Literal["owner", "project"]
    project: Optional[str]
    level: Optional[Level]


def _parse_grant(text: str) -> tuple[Literal["owner", "project"], Optional[str], Optional[Level]]:
    if text == "owner":
        return "owner", None, None
    if not text.startswith("project:"):
        raise GroupMapError(f"grant {text!r}: 'owner' or 'project:<name>:<level>'")
    name, sep, level = text[len("project:"):].rpartition(":")
    if not sep or not name.strip() or name != name.strip():
        raise GroupMapError(f"grant {text!r}: no project name (or spaces around it)")
    for lv in get_args(Level):
        if level == lv:
            return "project", name, lv
    raise GroupMapError(f"grant {text!r}: level {level!r} is not one of {', '.join(LEVELS)}")


def parse_group_map(value: Any) -> tuple[MapEntry, ...]:
    """The setting's value (already JSON-decoded): a list of `{group, grant}` objects, no other
    keys, non-empty group names, valid grants, no entry twice."""
    if not isinstance(value, list):
        raise GroupMapError("the group map is a list of {group, grant}")
    if len(value) > MAX_ENTRIES:
        raise GroupMapError(f"more than {MAX_ENTRIES} entries")
    out: list[MapEntry] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"group", "grant"}:
            raise GroupMapError(f"entry {item!r}: exactly the keys 'group' and 'grant'")
        group, grant = item["group"], item["grant"]
        if not isinstance(group, str) or not group or not isinstance(grant, str):
            raise GroupMapError(f"entry {item!r}: group and grant are non-empty strings")
        if (group, grant) in seen:
            raise GroupMapError(f"entry {item!r} appears twice")
        seen.add((group, grant))
        scope, project, level = _parse_grant(grant)
        out.append(MapEntry(group=group, grant=grant, scope=scope, project=project, level=level))
    return tuple(out)


def maps_owner(entries: Iterable[MapEntry]) -> bool:
    """A group is mapped to owner: saving such a map is high-impact, with the warning of §9.4."""
    return any(e.scope == "owner" for e in entries)


def require_groups(entries: tuple[MapEntry, ...], from_id_token: Optional[frozenset[str]],
                   from_userinfo: Optional[frozenset[str]]) -> frozenset[str]:
    """The groups to map: the ID token's claim, else userinfo's. With an empty map none are needed
    (empty set). With a non-empty map and the claim in neither: GroupsMissing."""
    if from_id_token is not None:
        return from_id_token
    if from_userinfo is not None:
        return from_userinfo
    if entries:
        raise GroupsMissing("the groups claim is in neither the ID token nor userinfo")
    return frozenset()


def desired_grants(groups: Iterable[str], entries: Iterable[MapEntry]) -> frozenset[Grant]:
    """The grants these groups give: one per map entry whose group the user is in."""
    have = frozenset(groups)
    return frozenset(Grant(scope=e.scope, project=e.project, level=e.level, source=f"oidc:{e.group}")
                     for e in entries if e.group in have)


def gives_owner(grants: Iterable[Grant]) -> bool:
    return any(g.is_owner for g in grants)


@dataclass(frozen=True)
class GrantDiff:
    add: frozenset[Grant]
    remove: frozenset[Grant]
    owner_granted: bool             # owner is among the added and the user had no group owner grant


def diff(current: Iterable[Grant], desired: frozenset[Grant]) -> GrantDiff:
    """`current`: the user's grants with an `oidc:` source (manual ones are not passed and never
    removed). Grants of groups the user left, or of map entries removed since, are removed."""
    cur = frozenset(current)
    manual = [g for g in cur if not g.source.startswith("oidc:")]
    if manual:
        raise ValueError(f"manual grants are not group grants: {sorted(map(repr, manual))}")
    add, remove = desired - cur, cur - desired
    return GrantDiff(add=add, remove=remove, owner_granted=gives_owner(add) and not gives_owner(cur))
