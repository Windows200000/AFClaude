#!/usr/bin/env python3
"""The owner's message box to the task-manager (D-178, D-241; dashboard design §3).

The owner types a message on the temp dashboard (the quickview page); inbox_api.py stores it
(inbox.post: the row in store.inbox_messages is the only copy of the text, D-241) and its worker
(inbox_worker.py) asks Haiku whether it needs the task-manager now:

  now   it is typed into the task-manager's tmux session at once (D-186: no window, no budget),
        through the keep-alive's own send path and checks (the session lock, preflight's
        owner_holds, owner_typing), with the message id and how to answer
        (`tasks.py inbox reply <id> "..."`); the answer shows on the dashboard: in the box's chat for
        ui_chat_reset_hours, and in the questions section until the owner dismisses it (D-304).
  note  it waits in the DB; the next message the keep-alive sends into the session (a run's
        continue, a fill-up, the message after a compaction, or a "now" message) lists the
        noted ones verbatim and marks them included, so the session never wakes (and re-reads
        its whole context after the cache expired, D-237/D-238) just for a note.

No triage while the task-manager is active (D-252): a keep-alive run going, a turn or subagent
running or a recent request makes a message "now" without the Haiku call. The keep-alive's
compaction flow (save prompt, hand-off, /compact, compact_after, READY) is no activity (D-262):
from the save prompt on and after the compaction messages are triaged again, and a "now" verdict
made during the flow goes in only after its READY (inbox_worker.manager_active, delivery_hold).

A "now" message that can't be typed in (the owner holds the session or is typing, a compaction, a
usage limit) is held and tried again while the hold lasts, at most until the next session-window
start (D-244, hold_deadline): that start's message takes it along (held_insert, keepalive's
_fire_start), marked as needing an answer; still not out by then is an error. A start whose gate
passed fires late in its session window (D-274): the hold then lasts until that planned start.

A triage call refused at the account's usage limit (session or weekly) is no error yet (D-244): the
message waits, untriaged and shown as held, until the limit's reset (retry_after), then it is
triaged again once; failing again then is an error.

A failed triage or delivery is an error on the message (D-235/D-244: shown on the dashboard,
logged, alerted, and automation paused until the owner clears it; never delivered silently, never
dropped; the owner retries it).

This module is the pure part (prompts, the verdict parser, the note list, the dashboard view);
the Haiku call and the delivery are inbox_worker.py's, the HTTP endpoint is inbox_api.py's.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import actions  # noqa: E402
import prompts  # noqa: E402
import runner_state  # noqa: E402
import schedule  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
NOTE_ITEM_MAX = 1500        # characters of one noted message in a message into the session ...
NOTES_MAX = 3000            # ... and of the whole list (the rest waits for the next message, D-225)
HELD_MAX = 3000             # characters of the held "now" messages in a start's message (the rest: id only)
MESSAGE_MAX_BYTES = 12000   # a message typed with tmux send-keys (its command limit is 16 KB)
VIEW_MAX = 10               # messages on the dashboard (the current thread, D-178)
RATE_MAX = 20               # messages per hour the endpoint takes (each costs a Haiku call)
NEWLINE = " ⏎ "             # a line break of the owner's text in a one-line session message


class TriageError(Exception):
    """The triage call failed or gave no usable verdict."""


class TriageLimit(TriageError):
    """The triage call was refused at the account's usage limit (D-244): `kind` (session, weekly,
    or "" for a plain "hit your limit") and the limit notice it printed (with its reset time, if any)."""
    def __init__(self, kind: str, notice: str) -> None:
        super().__init__(f"the account is at its {kind + ' ' if kind else ''}usage limit: {notice.strip()[:200]}")
        self.kind = kind
        self.notice = notice


def prompt(name: str) -> str:
    """prompts/<name>.md, the default (D-169), through the registry (prompts.py)."""
    return prompts.default(name)


def one_line(text: str) -> str:
    """The text on one line (tmux send-keys would submit at the first newline)."""
    return NEWLINE.join(x.strip() for x in text.strip().splitlines() if x.strip())


def berlin(iso: str | None, fmt: str = "%d.%m. %H:%M") -> str | None:
    dt = store.parse_iso(iso)
    return dt.astimezone(BERLIN).strftime(fmt) if dt else None


# ---------------------------------------------------------------- the triage (Haiku)

def triage_prompt(text: str) -> str:
    return prompt("inbox_triage").format(text=text)


def parse_triage(returncode: int, stdout: str, stderr: str = "") -> dict[str, Any]:
    """`claude -p --output-format json` output -> {verdict, reason, cost_usd, usage}. Anything
    else (a failed call, an error result, no JSON object, an unknown verdict) is a TriageError."""
    if returncode != 0:
        raise TriageError(f"claude -p exited {returncode}: {(stderr or stdout or '').strip()[:300]}")
    try:
        out = json.loads(stdout)
    except ValueError:
        raise TriageError(f"claude -p printed no JSON: {stdout.strip()[:300]!r}") from None
    if not isinstance(out, dict):
        raise TriageError(f"claude -p printed {type(out).__name__}, not an object")
    res = out.get("result")
    if out.get("is_error") or not isinstance(res, str):
        raise TriageError(f"the triage call returned an error: {str(res or out.get('subtype'))[:300]}")
    m = re.search(r"\{.*\}", res, re.S)
    try:
        ans = json.loads(m.group(0)) if m else None
    except ValueError:
        ans = None
    if not isinstance(ans, dict):
        raise TriageError(f"no JSON verdict in the answer: {res.strip()[:300]!r}")
    verdict = str(ans.get("verdict", "")).strip().lower()
    if verdict not in store.INBOX_VERDICTS:
        raise TriageError(f"unknown verdict {ans.get('verdict')!r} (want now|note)")
    reason = " ".join(str(ans.get("reason") or "").split()) or "(no reason given)"
    cost = out.get("total_cost_usd")
    usage = out.get("usage")
    return {"verdict": verdict, "reason": reason,
            "cost_usd": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            "usage": usage if isinstance(usage, dict) else None}


# ---------------------------------------------------------------- messages into the session

def _note_item(m: dict[str, Any]) -> str:
    text = one_line(m["text"])
    if len(text) > NOTE_ITEM_MAX:
        text = text[:NOTE_ITEM_MAX] + f"... (cut; the full text: python3 tasks.py inbox show {m['id']})"
    return f"#{m['id']} ({berlin(m['created_at'])}): \"{text}\""


def notes_insert(conn: sqlite3.Connection, session_id: str) -> tuple[str | None, list[int]]:
    """The noted messages for the session as one line for a message into it (inbox_notes.md),
    oldest first, at most NOTES_MAX characters (the rest waits for the next message), and their
    ids (mark them included once the message went out). Nothing noted: (None, [])."""
    items: list[str] = []
    ids: list[int] = []
    size = 0
    for m in reversed(store.list_inbox(conn, "noted", session_id, limit=200)):
        item = _note_item(m)
        if items and size + len(item) > NOTES_MAX:
            break
        items.append(item)
        ids.append(m["id"])
        size += len(item)
    if not items:
        return None, []
    left = len(store.list_inbox(conn, "noted", session_id, limit=1000)) - len(ids)
    more = f" ({left} more: python3 tasks.py inbox list)" if left > 0 else ""
    return prompt("inbox_notes").format(notes=" ".join(items) + more).strip(), ids


def held_insert(conn: sqlite3.Connection, session_id: str) -> tuple[str | None, list[int]]:
    """D-244: the "now" messages for the session still waiting for delivery (held: the owner holds
    the session or types, a compaction, a usage limit; or not tried yet), as one line for a start's
    message (inbox_held.md), oldest first, each with its marker and how to answer. All of them go
    (each one owes an answer): a text is cut at NOTE_ITEM_MAX, and past HELD_MAX characters only the
    marker and a pointer to `tasks.py inbox show` remain. -> (text, ids: mark them delivered once
    the message went out), or (None, [])."""
    items: list[str] = []
    ids: list[int] = []
    size = 0
    for m in reversed(store.list_inbox(conn, "pending", session_id, limit=200)):
        if m["verdict"] != "now":
            continue
        text = one_line(m["text"])
        if len(text) > NOTE_ITEM_MAX:
            text = text[:NOTE_ITEM_MAX] + f"... (cut; the full text: python3 tasks.py inbox show {m['id']})"
        item = f"{marker(m['id'])} ({berlin(m['created_at'])}): \"{text}\""
        if size + len(item) > HELD_MAX:
            item = f"{marker(m['id'])} ({berlin(m['created_at'])}): (its text: python3 tasks.py inbox show {m['id']})"
        items.append(item)
        ids.append(m["id"])
        size += len(item)
    if not items:
        return None, []
    return prompt("inbox_held").format(held=" ".join(items)).strip(), ids


def hold_deadline(since: datetime, cfg: schedule.Config | None = None) -> datetime | None:
    """D-244: a "now" message held since `since` waits at most until the first session-window start
    after it (schedule.next_session_start): that start's message takes it along, else it is an
    error (inbox_worker.hold). None: no window at all (nothing to wait for: an error too).
    D-274: when the keep-alive planned that start for later in its session window (the gate
    passed, the run fires late), the planned time instead (planned_start)."""
    start = schedule.next_session_start(since, cfg)
    if start is None:
        return None
    return planned_start(start, cfg) or start


def planned_start(start: datetime, cfg: schedule.Config | None = None) -> datetime | None:
    """D-274: the time the keep-alive planned the session-window start `start` for (its entry in
    the runner state 'keepalive.deferred' marked planned: keepalive.defer_window_start), or None."""
    ent = runner_state.load("keepalive.deferred").get(schedule.start_key(start, cfg))
    if not isinstance(ent, dict) or not ent.get("planned"):
        return None
    t = _ts(ent.get("recheck_at"))
    return t if t is not None and t > start else None


def hold_until(m: dict[str, Any], cfg: schedule.Config | None = None) -> datetime | None:
    """The deadline of a held message (hold_deadline of its hold_since; a held triage: its
    retry_after, the usage limit's reset), else None."""
    if m.get("hold") and m.get("retry_after"):
        return _ts(m["retry_after"])
    since = _ts(m.get("hold_since")) if m.get("hold") else None
    return hold_deadline(since, cfg) if since is not None else None


def seen(path: str, offset: int, mark: str) -> bool:
    """`mark` is in what the transcript `path` got after `offset` (an unreadable file: not seen)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return mark.encode() in fh.read()
    except OSError:
        return False


def now_fields(m: dict[str, Any], notes: str | None) -> dict[str, Any]:
    """The placeholders of inbox_now.md for message m (+ the noted ones going along)."""
    return {"id": m["id"], "at": berlin(m["created_at"]), "text": one_line(m["text"]), "notes": notes or ""}


def now_message(m: dict[str, Any], notes: str | None) -> str:
    """The "now" message as typed into a live session: inbox_now.md on one line."""
    return " ".join(prompt("inbox_now").format(**now_fields(m, notes)).split())


def fits(message: str) -> bool:
    return len(message.encode()) <= MESSAGE_MAX_BYTES


def marker(msg_id: int) -> str:
    """What the delivered message carries (inbox_now.md), searched for in the transcript."""
    return f"[AFClaude inbox #{msg_id}]"


# ---------------------------------------------------------------- the dashboard view (D-178, D-246, D-248)

def _ts(iso: str | None) -> datetime | None:
    return store.parse_iso(iso) if iso else None


def activity(m: dict[str, Any]) -> datetime:
    """The message's last activity (posted, triaged, delivered, included, answered, failed)."""
    times = [_ts(m.get(k)) for k in ("created_at", "triaged_at", "delivered_at", "included_at",
                                     "answered_at", "error_at")]
    return max(t for t in times if t is not None)


CHAT_STATUSES = ("pending", "delivered", "answered", "error")   # the chat; noted ones are lines, included ones gone
BUSY_STATES = ("sorting", "sending", "held", "delivered")         # the box shows a loading animation
VIEW_FMT = "%a %d.%m. %H:%M"


def thread(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict[str, Any]]:
    """The box's chat, oldest first (D-178, D-246): the messages passed on to the task-manager or on
    their way (not the noted or included ones), newest back to the first gap longer than
    ui_chat_reset_hours (none in the last ui_chat_reset_hours: the chat is empty again), at most
    VIEW_MAX; a message still waiting or failed always stays in view. Walked by last activity, not
    id: an old message answered now (a noted one, included, then answered) is the newest."""
    now = now or datetime.now(UTC)
    reset = timedelta(hours=float(actions.get_setting(conn, "ui_chat_reset_hours")))
    rows = store.list_inbox(conn, CHAT_STATUSES, limit=200)
    out: list[dict[str, Any]] = []
    edge = now
    for m in sorted(rows, key=activity, reverse=True):
        if edge - activity(m) > reset:
            break
        out.append(m)
        edge = min(edge, _ts(m["created_at"]) or edge)
    out = out[:VIEW_MAX]
    seen = {m["id"] for m in out}
    out += [m for m in rows if m["id"] not in seen and m["status"] in ("pending", "error")]
    return sorted(out, key=lambda m: m["id"])


def state(m: dict[str, Any]) -> str:
    """What the box shows for the message (D-246): sorting (Haiku has not answered yet), sending
    ("now", being typed in), held (also a triage waiting for a usage limit's reset, D-244),
    delivered (waiting for the answer): the loading animation;
    noted: a checkmark; answered: the chat; error: a red mark and Retry; included: gone (D-248)."""
    if m["status"] != "pending":
        return str(m["status"])
    if m["hold"]:
        return "held"
    return "sending" if m["verdict"] else "sorting"


def tip(m: dict[str, Any], until: datetime | None = None) -> str:
    """The tooltip of the message's mark in the box (D-230: no labels on the page itself)."""
    st = state(m)
    if st == "sorting":
        return f"Sent {berlin(m['created_at'], VIEW_FMT)} · sorting"
    if st == "sending":
        return f"Passing it on to the task-manager · sent {berlin(m['created_at'], VIEW_FMT)}"
    if st == "held" and not m["verdict"]:
        return f"Held: {m['hold']}" + (f" · sorted after {until.astimezone(BERLIN).strftime(VIEW_FMT)}" if until else "")
    if st == "held":
        return f"Held: {m['hold']}" + (f" · goes out with the window start {until.astimezone(BERLIN).strftime(VIEW_FMT)} "
                                       "at the latest" if until else "")
    if st == "delivered":
        return f"Delivered {berlin(m['delivered_at'], VIEW_FMT)} · waiting for the answer"
    if st == "noted":
        return f"Saved for the next session · {berlin(m['triaged_at'] or m['created_at'], VIEW_FMT)}"
    if st == "answered":
        return f"Answered {berlin(m['answered_at'], VIEW_FMT)}"
    if st == "error":
        return f"Failed {berlin(m['error_at'], VIEW_FMT)}: {m['error']}"
    return f"Read by the task-manager {berlin(m['included_at'], VIEW_FMT)}"


def message_view(m: dict[str, Any], cfg: schedule.Config | None = None) -> dict[str, Any]:
    until = hold_until(m, cfg) if m["status"] == "pending" else None
    return {"id": m["id"], "at": m["created_at"], "at_berlin": berlin(m["created_at"], VIEW_FMT),
            "text": m["text"], "status": m["status"], "state": state(m), "tip": tip(m, until),
            "verdict": m["verdict"], "verdict_reason": m["verdict_reason"], "hold": m["hold"],
            "hold_since_berlin": berlin(m["hold_since"]),
            "hold_until_berlin": until.astimezone(BERLIN).strftime(VIEW_FMT) if until else None,
            "delivered_berlin": berlin(m["delivered_at"]),
            "included_berlin": berlin(m["included_at"]), "answer": m["answer"],
            "answered_berlin": berlin(m["answered_at"]), "error": m["error"]}


def notice_view(n: dict[str, Any]) -> dict[str, Any]:
    """A notice for the owner (D-306) as the questions section shows it."""
    return {"id": n["id"], "title": n["title"], "text": n["text"], "project": n["project_name"],
            "source": n["source"], "posted_berlin": berlin(n["created_at"], VIEW_FMT),
            "updated_berlin": berlin(n["updated_at"], VIEW_FMT) if n["updated_at"] else None}


def view(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """What the box shows (D-246/D-248), oldest first: the chat (thread) and the noted messages
    still waiting for the next message into the session (an included one is gone; a noted one
    stays until then, whatever the chat's reset). `chat`: an answer is in view (the box shows it
    as a chat); `busy`: a message is still on its way (the page polls faster). `threads`: every
    answered message the owner has not dismissed, whatever its age, for the questions section (D-304);
    `notices`: every notice the owner has not dismissed, oldest first, for the same section (D-306)."""
    ms = sorted(thread(conn, now) + store.list_inbox(conn, "noted", limit=200), key=lambda m: m["id"])
    cfg = schedule.load() if any(m["hold"] and m["status"] == "pending" for m in ms) else None   # one DB read
    out = [message_view(m, cfg) for m in ms]
    return {"messages": out, "threads": [message_view(m) for m in store.inbox_threads(conn)],
            "notices": [notice_view(n) for n in store.list_notices(conn)],
            "chat": any(m["answer"] for m in ms),
            "busy": any(m["state"] in BUSY_STATES for m in out),
            "reset_hours": float(actions.get_setting(conn, "ui_chat_reset_hours")), "max_chars": store.INBOX_TEXT_MAX}


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """The counts for status.json (export_quickview.py; no texts: they stay in the DB, D-241)."""
    counts = {s: 0 for s in store.INBOX_STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) FROM inbox_messages GROUP BY status"):
        counts[r[0]] = int(r[1])
    return {"counts": counts, "errors": counts["error"], "waiting": counts["pending"],
            "unanswered": counts["delivered"]}


def rate_ok(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    """At most RATE_MAX messages in the last hour (each one costs a Haiku call)."""
    since = ((now or datetime.now(UTC)) - timedelta(hours=1)).astimezone(UTC)
    stamp = since.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (since.microsecond // 1000)   # as now_iso()
    return store.inbox_posted_since(conn, stamp) < RATE_MAX
