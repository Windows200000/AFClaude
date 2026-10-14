#!/usr/bin/env python3
"""The inbox's worker (D-241; inbox.py has the overview): triage each new message with Haiku and
type the "now" ones into the task-manager's session. Runs as a thread of inbox_api.py, woken by
each post and every POLL seconds (a held message is tried again).

Triage: one `claude -p --model haiku` call (keepalive.POKE_ARGV: no tools, no session file; the
shape docker/host_exec.py whitelists, so inside the container it runs on the host through the
bridge, where the Claude CLI's login is), prompts/inbox_triage.md. Its cost and usage are kept on
the message.

Delivery of a "now" message (D-186: no window, no budget, not held by automation_paused: the owner
is asking) goes through the keep-alive's own send path and checks, never around them:
  - keepalive.session_lock (non-blocking): a compaction or a start typing into the session holds it;
  - the session at a usage limit (its last entry is the limit notice) holds it until the reset;
  - keepalive.preflight: the owner holding the session (Remote Control, a terminal, a busy process:
    owner_holds) holds it; any other refusal is an error;
  - plan send-keys: keepalive.owner_typing (keystrokes in an attached terminal, a draft in the input
    box, a dialog) holds it; an unreadable pane is an error (D-235);
  - keepalive.fire (ka_resume.sh: send-keys into the live tmux session, else a resume into one).
A hold is shown on the message and tried again every POLL; after HOLD_MAX it is an error. After
the send the message must show up in the session's transcript within VERIFY_TIMEOUT, else error.

Every failure (the triage call, a refused preflight, a failed send, no trace in the transcript) is
an error on the message (inbox.fail: shown on the dashboard until the owner retries it), a log
line and an alert (failstop.fail). It does not pause automation (pause=False): the inbox starts no
autonomous work, and the error is the owner's to see on the message they just sent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import actions  # noqa: E402
import failstop  # noqa: E402
import host  # noqa: E402
import inbox  # noqa: E402
import keepalive as ka  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
ACTOR = "runner:inbox"
DATA = os.path.join(HERE, "data")
POLL = 30                                 # seconds between passes (a held message is tried again)
TRIAGE_TIMEOUT = 120                      # seconds for the Haiku call
HOLD_MAX = timedelta(hours=2)             # a "now" message held this long: an error
VERIFY_TIMEOUT = timedelta(minutes=10)    # the sent message must be in the transcript by then
VERIFY_POLL = 5                           # seconds
HOLD_LOCKED = "a compaction or a start is typing into the session"


def log(msg: str) -> None:
    print(f"[{datetime.now(inbox.BERLIN).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}", flush=True)


def _act(conn: Any, name: str, **params: Any) -> Any:
    return actions.perform(conn, name, params, actor=ACTOR, via="runner")


def failed(conn: Any, m: dict[str, Any], subject: str, detail: str) -> None:
    """The message failed (D-235): error on it, a log line, an alert. Never raises."""
    try:
        _act(conn, "inbox.fail", message_id=m["id"], error=f"{subject}: {detail}")
    except Exception as e:   # noqa: BLE001 - reported with the alert below
        detail += f" (and the error could not be stored on the message: {type(e).__name__}: {e})"
    failstop.fail("inbox", subject, f"message #{m['id']}: {detail}", log=log, pause=False)


# ---------------------------------------------------------------- triage

def triage_call(text: str) -> dict[str, Any]:
    """One Haiku call -> inbox.parse_triage's dict; a failed call raises inbox.TriageError."""
    try:
        r = host.run_on_host(ka.POKE_ARGV, input=inbox.triage_prompt(text), cwd=DATA, env=ka.SCRUBBED_ENV,
                             timeout=TRIAGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise inbox.TriageError(f"claude -p timed out after {TRIAGE_TIMEOUT} s") from None
    except (OSError, ValueError) as e:
        raise inbox.TriageError(f"claude -p could not run: {type(e).__name__}: {e}") from None
    return inbox.parse_triage(r.returncode, r.stdout or "", r.stderr or "")


def triage(conn: Any, m: dict[str, Any]) -> str | None:
    """Sort one message. -> its verdict, or None (failed: the message shows the error)."""
    try:
        t = triage_call(m["text"])
    except inbox.TriageError as e:
        failed(conn, m, "inbox message triage failed", str(e))
        return None
    try:
        _act(conn, "inbox.triage", message_id=m["id"], verdict=t["verdict"], reason=t["reason"],
             cost_usd=t["cost_usd"], usage=t["usage"])
    except store.InvalidTransition as e:      # answered or changed meanwhile: nothing to do
        log(f"inbox #{m['id']}: triage result not stored ({e})")
        return None
    log(f"inbox #{m['id']}: {t['verdict']} ({t['reason']})")
    return str(t["verdict"])


# ---------------------------------------------------------------- delivery

def delivery_hold(sid: str, path: str, now: datetime) -> tuple[str | None, str]:
    """Why the message can't be typed in now (or None) and the send plan. A failed check raises."""
    stall = ka.stall_info(ka.last_message(path))
    if stall:
        reset = stall.get("reset_from_text")
        if reset is None or reset > now:
            return (f"the session is at its {stall.get('kind')} usage limit until "
                    f"{ka.berlin(reset) if reset else 'its reset'}"), ""
    ok, problems, plan = ka.preflight(sid)
    if not ok:
        if ka.owner_holds(problems):
            return f"the owner holds the session ({problems[0]})", ""
        raise RuntimeError("preflight refused: " + "; ".join(problems))
    if plan == "send-keys":
        why = ka.owner_typing(sid, now)
        if why:
            return why, plan
    return None, plan


def hold(conn: Any, m: dict[str, Any], why: str, now: datetime) -> None:
    since = store.parse_iso(m["hold_since"]) if m["hold_since"] else None
    if since is not None and now - since > HOLD_MAX:
        failed(conn, m, "inbox message not delivered",
               f"held for more than {HOLD_MAX.total_seconds() / 3600:g} h; the last reason: {why}")
        return
    if m["hold"] != why:
        log(f"inbox #{m['id']}: held: {why}")
    try:
        _act(conn, "inbox.hold", message_id=m["id"], reason=why)
    except store.InvalidTransition as e:      # answered or changed meanwhile: nothing to hold
        log(f"inbox #{m['id']}: hold not stored ({e})")


def build(conn: Any, m: dict[str, Any], plan: str, cwd: str) -> tuple[str, list[int]]:
    """The message to type in and the noted messages going along with it (left out if the message
    would get too long for one send). A live session gets inbox_now.md alone; a resumed one also
    the standing rules (or the hook's pointer), like every message that starts a session."""
    notes, ids = inbox.notes_insert(conn, m["session_id"])
    for n, i in ((notes, ids), (None, [])):
        if plan == "send-keys":
            msg = inbox.now_message(m, n)
        else:
            msg = ka.session_message("inbox_now", hooked=ka.rules_hooked(cwd), **inbox.now_fields(m, n))
        if inbox.fits(msg):
            return msg, i
    raise RuntimeError(f"the message is too long to type in ({len(msg.encode())} bytes > {inbox.MESSAGE_MAX_BYTES})")


def deliver(conn: Any, m: dict[str, Any], now: datetime, verify: Callable[..., Any] | None = None) -> str:
    """Try to type one "now" message into its session. -> "delivered" | "held" | "failed"."""
    sid = m["session_id"]
    path = ka.transcript_path(sid)
    if not path:
        failed(conn, m, "inbox message not delivered", f"no transcript of the session {sid[:8]}")
        return "failed"
    lock = ka.session_lock(sid)
    if lock is None:
        hold(conn, m, HOLD_LOCKED, now)
        return "held"
    try:
        try:
            why, plan = delivery_hold(sid, path, now)
        except Exception as e:   # noqa: BLE001 - an error on the message (D-235)
            failed(conn, m, "inbox message not delivered", f"{type(e).__name__}: {e}")
            return "failed"
        if why:
            hold(conn, m, why, now)
            return "held"
        cwd = ka.session_cwd(path)
        try:
            msg, ids = build(conn, m, plan, cwd)
            offset = os.path.getsize(path)
            rc, out, err = ka.fire(sid, cwd, msg, plan)
        except Exception as e:   # noqa: BLE001 - an error on the message (D-235)
            failed(conn, m, "inbox message not delivered", f"{type(e).__name__}: {e}")
            return "failed"
        if rc != 0:
            failed(conn, m, "inbox message not delivered",
                   f"plan={plan} rc={rc} {(out or '').strip()[:200]} {(err or '').strip()[:300]}".strip())
            return "failed"
        _act(conn, "inbox.deliver", message_id=m["id"], plan=plan)
        log(f"inbox #{m['id']}: delivered to {sid[:8]} ({plan}" + (f", with notes {ids}" if ids else "") + ")")
        if ids:
            try:
                _act(conn, "inbox.include", message_ids=ids, by=f"inbox #{m['id']}")
            except Exception as e:   # noqa: BLE001 - they went out; not marked, they go out again later
                failstop.fail("inbox", "noted inbox messages not marked included",
                              f"{ids} went out with #{m['id']}: {type(e).__name__}: {e}", log=log, pause=False)
    finally:
        ka.release_lock(lock)
    (verify or start_verify)(m["id"], path, offset)
    return "delivered"


def seen(path: str, offset: int, mark: str) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return mark.encode() in fh.read()
    except OSError:
        return False


def verify(msg_id: int, path: str, offset: int, timeout: timedelta | None = None) -> bool:
    """The delivered message must reach the session's transcript within VERIFY_TIMEOUT (its marker
    in what was written after the send); otherwise it is an error. -> seen."""
    mark = inbox.marker(msg_id)
    timeout = VERIFY_TIMEOUT if timeout is None else timeout
    deadline = time.time() + timeout.total_seconds()
    while True:
        if seen(path, offset, mark):
            return True
        if time.time() >= deadline:
            break
        time.sleep(VERIFY_POLL)
    conn = store.connect()
    try:
        m = store.get_inbox(conn, msg_id)
        if m is not None and m["status"] == "delivered":
            failed(conn, m, "inbox message not delivered",
                   f"typed into the session, but not in its transcript after {timeout}")
    finally:
        conn.close()
    return False


def start_verify(msg_id: int, path: str, offset: int) -> None:
    threading.Thread(target=verify, args=(msg_id, path, offset), name=f"inbox-verify-{msg_id}", daemon=True).start()


# ---------------------------------------------------------------- the passes

def process(conn: Any, now: datetime | None = None, verify: Callable[..., Any] | None = None) -> dict[str, int]:
    """One pass: triage the new messages, then try to deliver every "now" one (oldest first).
    -> counts per outcome."""
    out: dict[str, int] = {}
    for m in reversed(store.list_inbox(conn, "pending", limit=200)):
        if m["verdict"] is None:
            v = triage(conn, m)
            out[v or "failed"] = out.get(v or "failed", 0) + 1
    for m in reversed(store.list_inbox(conn, "pending", limit=200)):
        if m["verdict"] == "now":
            r = deliver(conn, m, now or datetime.now(UTC), verify)
            out[r] = out.get(r, 0) + 1
    return out


def run_forever(wake: threading.Event, stop: threading.Event | None = None) -> None:
    """The worker thread: a pass on every wake-up (a new message, a retry) and every POLL seconds.
    A failed pass is logged and alerted (failstop, no pause) and the next one runs."""
    stop = stop or threading.Event()
    log("inbox worker started")
    while not stop.is_set():
        wake.clear()
        try:
            conn = store.connect()
            try:
                process(conn)
            finally:
                conn.close()
        except Exception as e:   # noqa: BLE001 - an error (D-235), reported; the next pass runs
            failstop.fail("inbox", f"inbox worker pass failed: {type(e).__name__}",
                          f"{e}\n{traceback.format_exc()[-1200:]}", log=log, pause=False)
        wake.wait(POLL)
