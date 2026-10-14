#!/usr/bin/env python3
"""The inbox's worker (D-241; inbox.py has the overview): triage each new message with Haiku and
type the "now" ones into the task-manager's session. Runs as a thread of inbox_api.py, woken by
each post and every POLL seconds (a held message is tried again).

Triage: one `claude -p --model haiku` call (keepalive.POKE_ARGV: no tools, no session file; the
shape docker/host_exec.py whitelists, so inside the container it runs on the host through the
bridge, where the Claude CLI's login is), prompts/inbox_triage.md. Its cost and usage are kept on
the message.

Delivery of a "now" message (D-186: no window, no budget, not held by automation_paused: the owner
is asking, that is no automation) goes through the keep-alive's own send path and checks, never around them:
  - keepalive.session_lock (non-blocking): a compaction or a start typing into the session holds it;
  - the session at a usage limit (its last entry is the limit notice) holds it until the reset;
  - keepalive.preflight: the owner holding the session (Remote Control, a terminal, a busy process:
    owner_holds) holds it; any other refusal is an error;
  - plan send-keys: keepalive.owner_typing (keystrokes in an attached terminal, a draft in the input
    box, a dialog) holds it; an unreadable pane is an error (D-235);
  - keepalive.fire (ka_resume.sh: send-keys into the live tmux session, else a resume into one).
A hold is shown on the message and tried again every POLL while it lasts, at most until the next
session-window start after it began (D-244, inbox.hold_deadline; the dashboard shows "held until"):
that start's message takes the message along (keepalive._fire_start, inbox.held_insert: with its
marker and how to answer) and marks it delivered. Still pending START_GRACE after that start (the
start was skipped, held, failed or postponed; the session still held or at its limit) is an error;
so is a hold with no session-window start ahead at all. After the send the message must show up in
the session's transcript within VERIFY_TIMEOUT, else error.

Every failure (the triage call, a refused preflight, a failed send, no trace in the transcript, a
missed hold deadline, a DB failure, a failed pass) is an error on the message where there is one
(inbox.fail: shown on the dashboard until the owner retries it), an ERROR log line, an alert and
automation paused (D-244: failstop.fail with the pause, until the owner clears it with `tasks.py
automation resume`). The pause is install-wide (automation_paused has no project scope; today
AFClaude is the only project). It does not stop the inbox itself: "now" messages are still
delivered while paused (the owner's own message is no automation), but no start runs, so a held one
reaches its deadline without a start to take it along and turns into an error.
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
import schedule  # noqa: E402
import store  # noqa: E402

UTC = timezone.utc
ACTOR = "runner:inbox"
DATA = os.path.join(HERE, "data")
POLL = 30                                 # seconds between passes (a held message is tried again)
TRIAGE_TIMEOUT = 120                      # seconds for the Haiku call
# a held "now" message is an error this long after its deadline (the next session-window start) if
# that start did not take it along: the cron checks a start within CRON_STEP after it, the start
# waits up to COMPACT_LOCK_WAIT for a compaction, then sends and marks it delivered
START_GRACE = schedule.CRON_STEP + ka.COMPACT_LOCK_WAIT + timedelta(minutes=5)
VERIFY_TIMEOUT = timedelta(minutes=10)    # the sent message must be in the transcript by then
VERIFY_POLL = 5                           # seconds
HOLD_LOCKED = "a compaction or a start is typing into the session"


def log(msg: str) -> None:
    print(f"[{datetime.now(inbox.BERLIN).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}", flush=True)


def _act(conn: Any, name: str, **params: Any) -> Any:
    return actions.perform(conn, name, params, actor=ACTOR, via="runner")


def stop(subject: str, detail: str) -> None:
    """An inbox error (D-235/D-244): an ERROR log line, an alert and automation paused (failstop)."""
    failstop.fail("inbox", subject, detail, log=log)


def failed(conn: Any, m: dict[str, Any], subject: str, detail: str) -> None:
    """The message failed (D-235/D-244): error on it, then stop(). Never raises."""
    try:
        _act(conn, "inbox.fail", message_id=m["id"], error=f"{subject}: {detail}")
    except Exception as e:   # noqa: BLE001 - reported with the alert below
        detail += f" (and the error could not be stored on the message: {type(e).__name__}: {e})"
    stop(subject, f"message #{m['id']}: {detail}")


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


def hold(conn: Any, m: dict[str, Any], why: str, now: datetime) -> str:
    """The message can't go in now: held (shown on it, tried again by the next pass) until its
    deadline (the next session-window start after the hold began, D-244) + START_GRACE, then an
    error. -> "held" | "failed"."""
    since = (store.parse_iso(m["hold_since"]) if m["hold_since"] else None) or now
    deadline = inbox.hold_deadline(since)
    if deadline is None:
        failed(conn, m, "inbox message held without a window start to wait for",
               f"no session-window start is scheduled (window_days); the reason: {why}")
        return "failed"
    if now >= deadline + START_GRACE:
        failed(conn, m, "inbox message held past its window start",
               f"held since {ka.berlin(since)} until the window start {ka.berlin(deadline)}, but that start "
               f"did not take it along (skipped, held, failed or postponed; see the progress notes); "
               f"the last reason: {why}")
        return "failed"
    if m["hold"] != why:
        log(f"inbox #{m['id']}: held: {why}")
    try:
        _act(conn, "inbox.hold", message_id=m["id"], reason=why)
    except store.InvalidTransition as e:      # answered or changed meanwhile: nothing to hold
        log(f"inbox #{m['id']}: hold not stored ({e})")
    return "held"


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
    """Try to type one "now" message into its session. -> "delivered" | "held" | "failed" |
    "gone" (a start took it along meanwhile, or it changed: nothing to do)."""
    sid = m["session_id"]
    path = ka.transcript_path(sid)
    if not path:
        failed(conn, m, "inbox message not delivered", f"no transcript of the session {sid[:8]}")
        return "failed"
    lock = ka.session_lock(sid)
    if lock is None:
        return hold(conn, m, HOLD_LOCKED, now)
    try:
        cur = store.get_inbox(conn, m["id"])     # under the lock: a start may have taken it along (D-244)
        if cur is None or cur["status"] != "pending":
            log(f"inbox #{m['id']}: no longer pending ({cur['status'] if cur else 'gone'}), not sent")
            return "gone"
        m = cur
        try:
            why, plan = delivery_hold(sid, path, now)
        except Exception as e:   # noqa: BLE001 - an error on the message (D-235)
            failed(conn, m, "inbox message not delivered", f"{type(e).__name__}: {e}")
            return "failed"
        if why:
            return hold(conn, m, why, now)
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
        try:
            _act(conn, "inbox.deliver", message_id=m["id"], plan=plan)
        except Exception as e:   # noqa: BLE001 - it went out; an error on it keeps it from going out again
            failed(conn, m, "inbox message sent but not marked delivered", f"{type(e).__name__}: {e}")
            return "failed"
        log(f"inbox #{m['id']}: delivered to {sid[:8]} ({plan}" + (f", with notes {ids}" if ids else "") + ")")
        if ids:
            try:
                _act(conn, "inbox.include", message_ids=ids, by=f"inbox #{m['id']}")
            except Exception as e:   # noqa: BLE001 - they went out; not marked, they go out again later
                stop("noted inbox messages not marked included",
                     f"{ids} went out with #{m['id']}: {type(e).__name__}: {e}")
    finally:
        ka.release_lock(lock)
    (verify or start_verify)(m["id"], path, offset)
    return "delivered"


def verify(msg_id: int, path: str, offset: int, timeout: timedelta | None = None) -> bool:
    """The delivered message must reach the session's transcript within VERIFY_TIMEOUT (its marker
    in what was written after the send); otherwise it is an error. -> seen."""
    mark = inbox.marker(msg_id)
    timeout = VERIFY_TIMEOUT if timeout is None else timeout
    deadline = time.time() + timeout.total_seconds()
    while True:
        if inbox.seen(path, offset, mark):
            return True
        if time.time() >= deadline:
            break
        time.sleep(VERIFY_POLL)
    try:
        conn = store.connect()
        try:
            m = store.get_inbox(conn, msg_id)
            if m is not None and m["status"] == "delivered":
                failed(conn, m, "inbox message not delivered",
                       f"typed into the session, but not in its transcript after {timeout}")
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 - the verify thread's own error (D-235): reported, not lost
        stop("inbox delivery check failed", f"message #{msg_id}: {type(e).__name__}: {e}")
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
    A failed pass (a DB failure, ...) is an error (stop: alert + automation paused); the next pass
    still runs (the owner's messages are no automation)."""
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
        except Exception as e:   # noqa: BLE001 - an error (D-235/D-244), reported; the next pass runs
            stop(f"inbox worker pass failed: {type(e).__name__}", f"{e}\n{traceback.format_exc()[-1200:]}")
        wake.wait(POLL)
