#!/usr/bin/env python3
"""An expired or invalid Claude login, told apart from every other failure of a `claude -p` call (D-320).

Background (02.11.2026 14:15): the usage read failed and AFClaude paused with only "`claude -p /usage`
did not refresh ~/.claude.json". The cause was an expired login: the token-refresh request (a tiny Haiku
`claude -p`) had printed {"is_error": true, "result": "Failed to authenticate: OAuth session expired and
could not be refreshed"} with exit code 1, which the log kept cut to 60 characters. `claude -p /usage`
itself is a local command: it exits 0 with only the cost summary, so it never shows the cause.

This module is the one detector every `claude -p` caller uses (the usage read and its token-refresh
request, the inbox triage, the Haiku usage judgement, the rc-continue sender, the push notification, the
window-start ping), plus the state the dashboard shows:

  detect(returncode, stdout, stderr)   -> the CLI's own message when the call failed to authenticate, else None
  record_expired(source, found)        the state "login expired" (data: runner state 'keepalive.usage_refresh',
                                       key "auth"; the quickview's banner reads it)
  record_ok(source)                    a later successful call clears it (also: record_ok is cheap, it writes
                                       only while the state says expired)

A caller that finds an auth failure still fails fast and pauses (D-235/D-282), with SUBJECT as the failure's
subject: the pause reason and the alert then say plainly what to do. It is never a retry or a fallback.

What counts (the strings the Claude CLI 2.1.x prints, found in its binary): "Failed to authenticate: OAuth
session expired and could not be refreshed" (`-p`), "Not logged in · Please run /login", "API Error: 401
Invalid API key · Please run /login", "Your session has expired. Please run /login to sign in again.",
"Authentication required", and an assistant/result event with error "authentication_failed". Not a login
problem, so not matched: "Failed to refresh OAuth token: another Claude Code process is refreshing it"
(transient), a full disk, a usage limit.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# The fix, as true for this setup: the credentials are the host user's (the container's calls run
# `claude` on the host through the bridge), and /login is an interactive command of a terminal session.
FIX = "run `claude` in a terminal on the host, then type /login and sign in again (not possible over Remote Control)"
SHORT = "Claude login expired"
SUBJECT = f"{SHORT}: {FIX}"   # failstop's subject: stable, and the pause reason says what to do
COMPONENT = "keepalive.usage_refresh"   # the runner state the "auth" key lives in (no new component)
KEY = "auth"
FOUND_MAX = 300

_AUTH = re.compile(
    r"please run /login|not logged in|failed to authenticate|authentication[_ ](?:failed|required)"
    r"|oauth (?:session|(?:access )?token) (?:has )?(?:expired|revoked)|invalid api key"
    r"|api error: 401|session (?:has )?expired", re.I)


def _events(stdout: str) -> list[dict[str, Any]]:
    """The JSON objects `claude -p` printed: one object (--output-format json), a list of events, or
    one event per line (stream-json). Lines that are no JSON object are skipped."""
    text = (stdout or "").strip()
    if not text:
        return []
    try:
        whole = json.loads(text)
        if isinstance(whole, dict):
            return [whole]
        if isinstance(whole, list):
            return [e for e in whole if isinstance(e, dict)]
    except ValueError:
        pass
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _message_text(ev: dict[str, Any]) -> str:
    c = (ev.get("message") or {}).get("content") if isinstance(ev.get("message"), dict) else None
    if isinstance(c, list):
        return " ".join(str(b.get("text") or "") for b in c if isinstance(b, dict))
    return c if isinstance(c, str) else ""


def detect(returncode: int, stdout: str = "", stderr: str = "", local: bool = False) -> Optional[str]:
    """The Claude CLI's own words when this `claude -p` call failed to authenticate (an expired or invalid
    login), else None. Looks only where an error is: an error result (is_error), an assistant/result event
    whose error is "authentication_failed", or, for a failed call (exit code != 0), stderr and stdout.
    A successful call's model answer is never searched (it may mention /login). local=True: the call is a
    local command with no model reply (`-p /usage`), so its whole output counts, whatever the exit code."""
    texts: list[str] = []
    failed = returncode != 0
    events = _events(stdout)
    for ev in events:
        is_auth_event = ev.get("error") == "authentication_failed" or ev.get("api_error_status") == 401
        if ev.get("is_error") or is_auth_event:
            failed = True
            texts.append(str(ev.get("result") or "") + " " + _message_text(ev))
            if is_auth_event:
                texts.append("authentication_failed")   # the event's error code, when its text names nothing
    if failed or local:
        texts.append(stderr or "")
        if not events or local:
            texts.append(stdout or "")
    for t in texts:
        m = _AUTH.search(t)
        if m:
            return " ".join(t.split())[:FOUND_MAX]
    return None


def failure_detail(found: str) -> str:
    """The alert text for an auth failure `found` (detect's result)."""
    return (f"The Claude CLI answered: {found!r}. The login of the host user's Claude account has expired (or is "
            f"invalid), so no `claude -p` call works until you log in again: {FIX}. AFClaude clears this by itself "
            "with its next successful Claude call, then `python3 tasks.py automation resume` lifts the pause.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def state(path: Optional[str] = None) -> dict[str, Any]:
    """The stored auth state ({expired, since, last, source, found, cleared_at}) or {} (none yet). `path`:
    a side file (tests); None = the live DB state. Never raises (runner_state.load)."""
    import runner_state
    got = runner_state.load(COMPONENT, path).get(KEY)
    return dict(got) if isinstance(got, dict) else {}


def is_expired(path: Optional[str] = None) -> bool:
    return bool(state(path).get("expired"))


def record_expired(source: str, found: str, path: Optional[str] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """Store "login expired" (kept `since` while it stays expired). -> the state. A state that can't be
    stored is logged by runner_state (the failure itself is reported by the caller's fail-stop)."""
    import runner_state
    now = now or _now()
    doc = runner_state.load(COMPONENT, path)
    got = doc.get(KEY)
    old: dict[str, Any] = got if isinstance(got, dict) else {}
    st = {"expired": True, "since": old.get("since") if old.get("expired") else now.isoformat(),
          "last": now.isoformat(), "source": source, "found": found[:FOUND_MAX]}
    doc[KEY] = st
    runner_state.save(COMPONENT, doc, path)
    return st


def record_ok(source: str, path: Optional[str] = None, now: Optional[datetime] = None) -> bool:
    """A Claude call authenticated: clear the state if it said expired. -> True when it was cleared."""
    import runner_state
    if not is_expired(path):
        return False
    now = now or _now()
    doc = runner_state.load(COMPONENT, path)
    doc[KEY] = {"expired": False, "cleared_at": now.isoformat(), "source": source}
    runner_state.save(COMPONENT, doc, path)
    return True
