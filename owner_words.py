#!/usr/bin/env python3
"""owner_words.py: proof that the owner said something (phase 4b Q2, docs/phase4_plan.md §3.3; D-104, D-285).

The owner's role is never inferred from the owner's presence: a session AFClaude started counts as
task-manager or agent whatever it carries (D-198), and the task-manager's tmux session is also where
the owner chats. What a decision write needs from an autonomous caller is therefore a quote that
occurs in one stored owner message, found here:
  - an `inbox_messages` row with created_by = owner for that session (D-241: the text as typed is
    kept only there), cited as `inbox:<id>`; or
  - an owner prompt in that session's transcript (keepalive.transcript_path's lookup, read-only),
    cited as `chat:<session>:<uuid>`: a non-sidechain user entry with origin kind "human" that is
    not meta, not a tool result and not a keep-alive message (KEEPALIVE_PREFIXES: AFClaude's own
    texts are typed into tmux too, so they must never pass for the owner's; a long one is stored as a
    `<pasted_content>` block, which is unwrapped before the prefix is looked at: a prompt whose leading
    block carries an AFClaude prefix is AFClaude's, a paste without one stays the owner's). An inbox delivery
    arrives in the transcript behind the "[AFClaude inbox #N]" prefix: it is cited by its inbox id,
    not found twice.
Quotes are matched after whitespace normalisation, on word boundaries, and must be at least
MIN_QUOTE_WORDS words or MIN_QUOTE_CHARS characters long; case and typos must match exactly. No match, or
several messages that match, is an OwnerWordsError (D-235): the caller names one with `ref`.

The prefix list and the owner-prompt rule are compact_timing's (D-237: an owner prompt is what a
re-read is counted against), moved here so both use one definition; compact_timing imports them.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
INBOX_PREFIX = "[AFClaude inbox #"           # the owner's dashboard message delivered into the session
KEEPALIVE_PREFIXES = ("[keep-alive]", "This session was compacted", "You are the long-running", "[AFClaude",
                      "/compact", "<command-name>/compact")
INTERRUPT_PREFIX = "[Request interrupted"
# Claude Code stores a long text typed with `tmux send-keys -l` as a bracketed paste: the prompt text is
# "\n\n<pasted_content id=\"..\">\n<text>\n</pasted_content id=\"..\">\n", origin human, promptSource "typed".
PASTE_OPEN = re.compile(r"\s*<pasted_content\b[^>]*>\s*")
PASTE_CLOSE = re.compile(r"\s*</pasted_content\b[^>]*>\s*")
MIN_QUOTE_WORDS = 3                          # a quote of an owner message: at least this many words ...
MIN_QUOTE_CHARS = 12                         # ... or this many characters (a decision id alone is exempt: word=True)
EXCERPT = 90                                 # characters of a candidate message shown when several match
SHOWN = 5                                    # candidates listed in that refusal


class OwnerWordsError(ValueError):
    """No stored owner message proves the quote, or the proof is ambiguous (the message says what to do)."""


@dataclass(frozen=True)
class Evidence:
    """One stored owner message: ref (`inbox:<id>` | `chat:<session>:<uuid>`), its time and its text."""
    ref: str
    at: datetime
    text: str

    @property
    def label(self) -> str:
        """What a decision's source and the audit row say: `inbox #N <time>` / `chat <session> <time>`."""
        when = self.at.astimezone(BERLIN).strftime("%d.%m. %H:%M")
        kind, _, rest = self.ref.partition(":")
        if kind == "inbox":
            return f"inbox #{rest} {when}"
        return f"chat {rest.split(':', 1)[0]} {when}"


# ---------------------------------------------------------------- the transcript's owner prompts

def prompt_text(e: Mapping[str, Any]) -> Optional[str]:
    """A user entry's prompt text, None for a tool result or no text."""
    msg = e.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None
        parts = [str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return " ".join(parts) if parts else None
    return None


def unwrap_paste(text: str) -> str:
    """The prompt text without the `<pasted_content ...>` tags Claude Code puts around a pasted (or
    tmux-typed) long text: the leading open tag and the first closing tag, whitespace trimmed around them."""
    m = PASTE_OPEN.match(text)
    if m is None:
        return text
    return PASTE_CLOSE.sub("\n", text[m.end():], count=1).strip()


def prompt_kind(e: Mapping[str, Any], text: str) -> str:
    """owner | inbox (the owner's dashboard message, delivered) | keepalive | summary | other."""
    if e.get("isCompactSummary"):
        return "summary"
    text = unwrap_paste(text)
    if text.lstrip().startswith(INBOX_PREFIX):
        return "inbox"
    if text.lstrip().startswith(KEEPALIVE_PREFIXES):
        return "keepalive"
    origin = e.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind == "human" and not e.get("isMeta"):
        return "owner"
    return "other"


def owner_prompt(e: Mapping[str, Any]) -> Optional[str]:
    """The text of a transcript entry that is an owner prompt, else None (a sidechain, a tool result, a
    keep-alive or inbox message, a peer message and an interrupt marker are not)."""
    if e.get("type") != "user" or e.get("isSidechain"):
        return None
    text = prompt_text(e)
    if text is None or text.startswith(INTERRUPT_PREFIX):
        return None
    return unwrap_paste(text) if prompt_kind(e, text) == "owner" else None


def _entries(path: str) -> Iterator[dict[str, Any]]:
    """keepalive.iter_entries (the keep-alive's transcript reader; keepalive.py is untyped)."""
    import keepalive
    fn: Callable[[str], Iterable[dict[str, Any]]] = getattr(keepalive, "iter_entries")
    for e in fn(path):
        if isinstance(e, dict):
            yield e


def _transcript_path(session: str) -> Optional[str]:
    import keepalive
    fn: Callable[[str], Optional[str]] = getattr(keepalive, "transcript_path")
    return fn(session)


def _ts(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def transcript_messages(session: str) -> list[Evidence]:
    """The owner prompts of the session's transcript, oldest first; none if it has no transcript yet.
    A transcript that exists but can't be read raises (OwnerWordsError, D-235)."""
    path = _transcript_path(session)
    if path is None:
        return []
    out: list[Evidence] = []
    try:
        for e in _entries(path):
            text = owner_prompt(e)
            at, uuid = _ts(e.get("timestamp")), e.get("uuid")
            if text is not None and at is not None and isinstance(uuid, str) and uuid:
                out.append(Evidence(f"chat:{session}:{uuid}", at, text))
    except OSError as err:
        raise OwnerWordsError(f"can't read the transcript of session {session}: {err}") from err
    return out


def inbox_messages(conn: sqlite3.Connection, session: str) -> list[Evidence]:
    """The owner's inbox messages for the session, oldest first (every status: the owner wrote them)."""
    out: list[Evidence] = []
    for r in conn.execute("SELECT id, created_at, text FROM inbox_messages WHERE session_id = ? "
                          "AND created_by = 'owner' ORDER BY id", (session,)):
        at = store.parse_iso(r["created_at"])
        if at is not None:
            out.append(Evidence(f"inbox:{r['id']}", at, r["text"]))
    return out


# ---------------------------------------------------------------- the lookup

def normalise(text: str) -> str:
    """Whitespace runs become one space; nothing else changes (case and typos must match)."""
    return " ".join(text.split())


def _has(message: str, quote: str) -> bool:
    """The quote occurs in the message on word boundaries (a decision id `D-12` is not in `D-123`)."""
    return re.search(r"(?<![A-Za-z0-9-])" + re.escape(quote) + r"(?![A-Za-z0-9])", message) is not None


def _excerpt(ev: Evidence, quote: str) -> str:
    text = normalise(ev.text)
    i = max(text.find(quote), 0)
    a = max(0, i - 20)
    cut = text[a:a + EXCERPT]
    return ("..." if a else "") + cut + ("..." if a + EXCERPT < len(text) else "")


def check_ref(ref: str, session: str) -> None:
    """`inbox:<id>` or `chat:<this session>:<uuid>`, else an OwnerWordsError."""
    m = re.fullmatch(r"inbox:([1-9][0-9]*)|chat:([^:\s]+):(\S+)", ref)
    if m is None:
        raise OwnerWordsError(f"--from {ref!r}: write inbox:<id> or chat:<session>:<uuid> (as listed when several "
                              "owner messages match)")
    if m.group(2) is not None and m.group(2) != session:
        raise OwnerWordsError(f"--from {ref!r} names another session's chat; only this session's ({session}) "
                              "owner messages count")


def find(conn: sqlite3.Connection, quotes: Sequence[str] | str, session: Optional[str], ref: Optional[str] = None,
         *, word: bool = False, since: Optional[datetime] = None) -> Evidence:
    """The one stored owner message of `session` (inbox rows, then its transcript) that contains every quote.
    ref picks one when several match. word: the quote is a decision id (exempt from the minimum length;
    matching is always on word boundaries: `D-12` is not in `D-123`). since: a message before this time
    doesn't count (an entry can't be confirmed before it existed). -> Evidence(ref, at, text); an OwnerWordsError when there is none or it is ambiguous."""
    qs = [normalise(q) for q in ([quotes] if isinstance(quotes, str) else quotes)]
    if not qs or any(not q for q in qs):
        raise OwnerWordsError("a quote of the owner's words is needed (the exact words, as the owner wrote them)")
    if not word:
        short = [q for q in qs if len(q.split()) < MIN_QUOTE_WORDS and len(q) < MIN_QUOTE_CHARS]
        if short:
            raise OwnerWordsError(f"the quote {short[0]!r} is too short to prove anything: quote at least "
                                  f"{MIN_QUOTE_WORDS} words or {MIN_QUOTE_CHARS} characters of the owner's words")
    if not session:
        raise OwnerWordsError("no session id to look for the owner's message in (CLAUDE_CODE_SESSION_ID is "
                              "unset): an autonomous session records what the owner said only against a "
                              "message of its own session")
    if ref is not None:
        check_ref(ref, session)
    pool = inbox_messages(conn, session) + transcript_messages(session)
    if since is not None:
        pool = [m for m in pool if m.at >= since]
    if ref is not None:
        pool = [m for m in pool if m.ref == ref]
        if not pool:
            raise OwnerWordsError(f"--from {ref}: no such owner message in session {session} (an inbox message "
                                  "must be the owner's, a chat message an owner prompt, not a keep-alive text)")
    hits = [m for m in pool if all(_has(normalise(m.text), q) for q in qs)]
    shown = " / ".join(f'"{q}"' for q in qs)
    if not hits:
        raise OwnerWordsError(f"{shown} is in none of the owner's messages of session {session} "
                              f"({len(pool)} checked: its inbox messages and owner prompts; whitespace is "
                              "ignored, case and typos must match exactly): quote the owner's words verbatim")
    if len(hits) > 1:
        lines = [f"  {m.ref} ({m.at.astimezone(BERLIN).strftime('%d.%m. %H:%M')}): {_excerpt(m, qs[0])}"
                 for m in hits[-SHOWN:]]
        raise OwnerWordsError(f"{shown} is in {len(hits)} owner messages; name the one it is from with "
                              "--from (MCP: ref):\n" + "\n".join(lines)
                              + (f"\n  (the {SHOWN} newest of {len(hits)})" if len(hits) > SHOWN else ""))
    return hits[0]
