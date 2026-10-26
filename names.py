#!/usr/bin/env python3
"""Readable tmux session names (D-147; docs/dashboard_design/05_runners.md §5.3, docs/phase3b_plan.md §3.2).

Pure: no DB, no tmux. The launchers (keepalive.claim_tmux) pass the names already taken and
store the chosen one in driven_sessions; nothing computes a session's name from its id any more.

  task-manager  afc-<project-slug>-task-manager
  review        afc-review
  stall         afc-stall-<title-slug>-<id4>
  request       afc-work-<title-slug>-<id4>        (a backlog project, keepalive --work-on)

Charset [a-z0-9-], at most 40 characters; the slug is cut to fit. A suffix (-2, -3, ...) only
when the name is taken. TMUX_RE is every name the host bridge accepts (docker/host_exec.py,
host.py, ka_resume.sh): today's ka-<id8> names, until no live session uses one (3b S6), and these.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Collection, Optional

PREFIX = "afc-"
MAX_LEN = 40
TMUX_RE = re.compile(r"^(ka-[0-9a-f]{8}|afc-[a-z0-9-]{1,36})\Z")   # \Z: no trailing newline
KINDS = ("task-manager", "review", "stall", "request")
MAX_SUFFIX = 99
# letters NFKD doesn't split into a base letter + accent
TRANSLIT = {"ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i"}


def slug(text: Optional[str], limit: int = MAX_LEN) -> str:
    """[a-z0-9-] of `text`: accents dropped (é -> e, ß -> ss), anything else one hyphen, at most
    `limit` characters, no hyphen at either end. Nothing left (e.g. only CJK): ""."""
    t = "".join(TRANSLIT.get(c, c) for c in (text or "").lower())
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:max(limit, 0)].strip("-")


def _parts(kind: str, project: Optional[str], title: Optional[str], sid: Optional[str]) -> tuple[str, str, str]:
    """(head, slug source, tail) of a kind's name: head + slug + tail, the empty parts dropped."""
    if kind == "task-manager":
        if not project:
            raise ValueError("a task-manager name needs its project")
        return "", project, "task-manager"
    if kind == "review":
        return "review", "", ""
    if kind in ("stall", "request"):
        if not sid or not re.match(r"^[0-9a-f]{4}", sid):
            raise ValueError(f"a {kind} name needs the session id, got {sid!r}")
        return ("stall" if kind == "stall" else "work"), title or "", sid[:4]
    raise ValueError(f"kind must be one of {'|'.join(KINDS)}, got {kind!r}")


def _join(head: str, s: str, tail: str, suffix: str) -> str:
    return PREFIX + "-".join(p for p in (head, s, tail) if p) + suffix


def tmux_name(kind: str, project: Optional[str] = None, title: Optional[str] = None, sid: Optional[str] = None,
              taken: Collection[str] = ()) -> str:
    """The readable tmux name of a new `kind` session (KINDS): `project` (task-manager), `title`
    (stall, request; may be empty), `sid` (stall, request: its first 4 characters). Not one of
    `taken`: the base name, else the first free one with -2, -3, ... (the slug cut shorter to fit).
    No free name up to -99: ValueError."""
    head, source, tail = _parts(kind, project, title, sid)
    for n in range(1, MAX_SUFFIX + 1):
        suffix = "" if n == 1 else f"-{n}"
        room = MAX_LEN - len(_join(head, "", tail, suffix)) - (1 if source else 0)
        name = _join(head, slug(source, room) if source else "", tail, suffix)
        if not TMUX_RE.match(name):
            raise ValueError(f"tmux name {name!r} is not [a-z0-9-] of at most {MAX_LEN} characters")
        if name not in taken:
            return name
    raise ValueError(f"no free tmux name for {kind} {project or title or ''!r}: up to -{MAX_SUFFIX} all taken")
