#!/usr/bin/env python3
"""The inbox's worker (D-241; inbox.py has the overview): triage each new message with Haiku and
type the "now" ones into the task-manager's session. Phase 3a S7 (docs/phase3a_plan.md §5 S7, §3.2):
the resident runner calls process() as its `inbox` job (woken by inbox_api's wake, plus a 30-s tick
while a message is pending or held) and verify_tick() as its `inbox_verify` job (5 s while a
verification is pending). AFCLAUDE_SCHEDULER=legacy still runs both from inbox_api.py serve's
worker thread (run_forever), woken by each post and every POLL seconds.

A delivered message's post-send check is a stored runner_state "inbox" entry ({msg, path, offset,
deadline}, store_verify/verify_tick), not a thread: a restart resumes it from the same offset and
deadline and ends it seen or as an error at its deadline (§3.4).

Triage: one `claude -p --model haiku` call (keepalive.POKE_ARGV: no tools, no session file; the
shape docker/host_exec.py whitelists, so inside the container it runs on the host through the
bridge, where the Claude CLI's login is), prompts/inbox_triage.md. Its cost and usage are kept on
the message.

A triage call refused at the account's usage limit (D-244; the notice `claude -p` prints as its
result or on stderr, matched like keepalive.stall_info does a session's: "You've hit your session
limit · resets 2pm (Europe/Berlin)"; session, weekly or a plain "hit your limit") is held, not an
error: the message stays pending and untriaged, shown as held, with retry_after = the limit's reset
(the notice's reset time, else for a session or weekly limit the usage cache's resets_at; none: an
error). No call is made before retry_after + TRIAGE_RETRY_MARGIN; then the triage runs once more
(the activity check first, as for every triage), and any failure then, the limit again included, is
an error. Every other triage failure is an error at once.

No triage while the task-manager is active (D-252): a message whose session has a keep-alive run
going (keepalive.run_active, D-224), a turn or a subagent running (keepalive.compaction_busy_reason)
or a request in its transcript within ACTIVE_WINDOW (compact_timing.scan) is "now" without the
Haiku call (its reason says so): the task-manager is working anyway, so the message goes in now,
through the same delivery and its checks, instead of waiting as a note for the next message into
the session. The keep-alive's compaction flow is no activity (D-262; compaction_flow reads its
record in the keepalive state's "compactions"): from its start (right before the save prompt) until
its end the message is triaged, and after it only a prompt that came after its end (and that turn's
requests) counts, never the flow's own turns (save prompt, hand-off, /compact, compact_after,
READY) nor what came before it. A failed activity check (the keepalive state unreadable, the
transcript unreadable, ...) is an error on the message, never a triage or a delivery in its place
(D-235/D-244).

Delivery of a "now" message (D-186: no window, no budget, not held by automation_paused: the owner
is asking, that is no automation) goes through the keep-alive's own send path and checks, never around them:
  - keepalive.session_lock (non-blocking): a compaction or a start typing into the session holds it;
    so does the compaction flow's "running" record (D-262: a "now" verdict made during the flow
    goes in after its READY, through these same checks);
  - the session at a usage limit (its last entry is the limit notice) holds it until the reset;
  - keepalive.preflight: the owner holding the session (Remote Control, a terminal, a busy process:
    owner_holds) holds it; any other refusal is an error;
  - plan send-keys: keepalive.owner_typing (keystrokes in an attached terminal, a draft in the input
    box, a dialog) holds it; an unreadable pane is an error (D-235);
  - keepalive.fire (ka_resume.sh: send-keys into the live tmux session, else a resume into one).
A hold is shown on the message and tried again by the next pass while it lasts (the `inbox` job's
wake-or-30-s-tick, or the legacy thread's POLL), at most until the next session-window start after
it began (D-244, inbox.hold_deadline; the dashboard shows "held until"):
that start's message takes the message along (keepalive._fire_start, inbox.held_insert: with its
marker and how to answer) and marks it delivered. Still pending START_GRACE after that start (the
start was skipped, held, failed or postponed; the session still held or at its limit) is an error;
so is a hold with no session-window start ahead at all. After the send the message must show up in
the session's transcript within VERIFY_TIMEOUT, else error.

Every failure (the triage call, a refused preflight, a failed send, no trace in the transcript, a
missed hold deadline, a DB failure, a failed pass) is an error on the message where there is one
(inbox.fail: shown on the dashboard until the owner retries it), an ERROR log line, an alert and
automation paused (D-244: failstop.fail with the pause, until the owner clears it). An error about
a message pauses only the project the message was for (the project its session is the task-manager
of; `tasks.py automation resume --project NAME` clears it); one that is about no message (a failed
pass, the delivery check failing to read the DB) or whose session manages no project pauses
everything (`tasks.py automation resume`). It does not stop the inbox itself: "now" messages are
still delivered while paused (the owner's own message is no automation), but no start of that
project runs, so a held one reaches its deadline without a start to take it along and turns into
an error.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import actions  # noqa: E402
import compact_timing  # noqa: E402
import failstop  # noqa: E402
import host  # noqa: E402
import inbox  # noqa: E402
import keepalive as ka  # noqa: E402
import runner_state  # noqa: E402
import schedule  # noqa: E402
import store  # noqa: E402
import usage_stale  # noqa: E402

UTC = timezone.utc
ACTOR = "runner:inbox"
DATA = os.path.join(HERE, "data")
POLL = 30                                 # seconds between passes (a held message is tried again)
TRIAGE_TIMEOUT = 120                      # seconds for the Haiku call
TRIAGE_RETRY_MARGIN = timedelta(minutes=2)  # a triage held at a usage limit is tried again this long after its reset
LIMIT_KINDS = ("session", "weekly", "")     # the usage limits a triage waits for ("": a plain "hit your limit")
ACTIVE_WINDOW = timedelta(minutes=10)     # D-252: a request this recent: the task-manager is active, no triage
# a held "now" message is an error this long after its deadline (the next session-window start) if
# that start did not take it along: the cron checks a start within CRON_STEP after it, the start
# waits up to COMPACT_LOCK_WAIT for a compaction, then sends and marks it delivered
START_GRACE = schedule.CRON_STEP + ka.COMPACT_LOCK_WAIT + timedelta(minutes=5)
VERIFY_TIMEOUT = timedelta(minutes=10)    # the sent message must be in the transcript by then
VERIFY_COMPONENT = "inbox"                # runner_state: "entries" {msg, path, offset, deadline} (§3.3)
HOLD_LOCKED = "a compaction or a start is typing into the session"
# D-262: the keep-alive's compaction flow (keepalive._compact_now: save prompt, hand-off, /compact,
# compact_after, READY) is no task-manager activity, and a "now" message waits until it is over.
# Every step has a timeout, so a flow still recorded "running" this long after its start is over
# (the save and its re-send; D-307: each hand-off asked for again, with the turn before it, and the
# turns before and after the last one and before /compact)
COMPACT_FLOW_MAX = (ka.COMPACT_IDLE_WAIT + 2 * ka.SAVE_TIMEOUT
                    + ka.HANDOFF_REFRESHES * (ka.COMPACT_TURN_WAIT + ka.SAVE_TIMEOUT)
                    + 2 * ka.COMPACT_TURN_WAIT + ka.COMPACT_TIMEOUT + ka.VERIFY_TIMEOUT + timedelta(minutes=5))
HOLD_COMPACTING = "the keep-alive's compaction flow is running (it goes in after the READY, D-262)"


def log(msg: str) -> None:
    print(f"[{datetime.now(inbox.BERLIN).strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}", flush=True)


def _act(conn: Any, name: str, **params: Any) -> Any:
    return actions.perform(conn, name, params, actor=ACTOR, via="runner")


def fail_stop(subject: str, detail: str, session: str | None = None) -> None:
    """An inbox error (D-235/D-244): an ERROR log line, an alert and automation paused (failstop):
    with `session` (the message's) only the project that session is the task-manager of, else
    everything."""
    failstop.fail("inbox", subject, detail, log=log, session=session)


def failed(conn: Any, m: dict[str, Any], subject: str, detail: str) -> None:
    """The message failed (D-235/D-244): error on it, then fail_stop() for its project. Never raises."""
    try:
        _act(conn, "inbox.fail", message_id=m["id"], error=f"{subject}: {detail}")
    except Exception as e:   # noqa: BLE001 - reported with the alert below
        detail += f" (and the error could not be stored on the message: {type(e).__name__}: {e})"
    fail_stop(subject, f"message #{m['id']}: {detail}", session=m["session_id"])


# ---------------------------------------------------------------- triage

def limit_notice(returncode: int, stdout: str, stderr: str) -> tuple[str, str] | None:
    """A failed `claude -p` call (exit code != 0 or an error result) refused at a usage limit: its
    result (the JSON) or stderr is the limit notice (keepalive.LIMIT_TEXT_RE, as stall_info reads
    a session's) -> (kind: session | weekly | "" for a plain one, the notice), else None (also
    for a limit the triage can't wait for: a spend limit, ...)."""
    texts = [stderr]
    failed = returncode != 0
    try:
        out = json.loads(stdout)
    except ValueError:
        out = None
    if isinstance(out, dict):
        failed = failed or bool(out.get("is_error"))
        if isinstance(out.get("result"), str):
            texts.insert(0, out["result"])
    if not failed:
        return None
    for t in texts:
        m = ka.LIMIT_TEXT_RE.search(t or "")
        if m:
            word = m.group("kind").strip().lower()
            kind = "weekly" if "week" in word else "session" if "session" in word else word
            return (kind, t.strip()) if kind in LIMIT_KINDS else None
    return None


def triage_call(text: str) -> dict[str, Any]:
    """One Haiku call -> inbox.parse_triage's dict; a failed call raises inbox.TriageError, one
    refused at a usage limit inbox.TriageLimit (limit_notice)."""
    try:
        r = host.run_on_host(ka.POKE_ARGV, input=inbox.triage_prompt(text), cwd=DATA, env=ka.SCRUBBED_ENV,
                             timeout=TRIAGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise inbox.TriageError(f"claude -p timed out after {TRIAGE_TIMEOUT} s") from None
    except (OSError, ValueError) as e:
        raise inbox.TriageError(f"claude -p could not run: {type(e).__name__}: {e}") from None
    hit = limit_notice(r.returncode, r.stdout or "", r.stderr or "")
    if hit:
        raise inbox.TriageLimit(*hit)
    return inbox.parse_triage(r.returncode, r.stdout or "", r.stderr or "")


def limit_reset(e: inbox.TriageLimit, now: datetime) -> datetime | None:
    """When the usage limit `e` resets: the notice's reset time, else (a session or weekly limit)
    the usage cache's resets_at of that limit; None: neither is after `now`. An unreadable usage
    cache raises (D-235)."""
    reset = ka.parse_reset_text(e.notice, now)
    if reset is not None and reset > now:
        return reset
    if e.kind not in ("session", "weekly"):
        return None
    reset = ((ka.usage_cache_checked() or {}).get(e.kind) or {}).get("resets_at")
    return reset if reset is not None and reset > now else None


def triage_hold(conn: Any, m: dict[str, Any], e: inbox.TriageLimit, now: datetime) -> str | None:
    """The triage call hit the account's usage limit (D-244): hold the message until the limit's
    reset (inbox.triage_hold). No reset to wait for (none in the notice nor the usage cache, or
    the cache unreadable) is an error. -> "held", or None (failed: the message shows the error)."""
    try:
        reset = limit_reset(e, now)
    except Exception as x:   # noqa: BLE001 - an error on the message (D-235), never a guessed wait
        failed(conn, m, "inbox message triage failed", f"{e}; and its reset could not be read: {type(x).__name__}: {x}")
        return None
    if reset is None:
        failed(conn, m, "inbox message triage failed", f"{e}; no reset time to wait for (the notice nor the usage cache)")
        return None
    why = (f"the account is at its {e.kind + ' ' if e.kind else ''}usage limit: sorted after its reset "
           f"{reset.astimezone(inbox.BERLIN).strftime('%d.%m. %H:%M')}")
    try:
        _act(conn, "inbox.triage_hold", message_id=m["id"], reason=why, retry_after=store.iso(reset))
    except store.InvalidTransition as x:      # answered or changed meanwhile: nothing to hold
        log(f"inbox #{m['id']}: triage hold not stored ({x})")
        return None
    log(f"inbox #{m['id']}: triage held: {why}")
    return "held"


def load_state() -> Any:
    """The keepalive state (its "handled" fires, "compactions", ...); unreadable raises."""
    st = ka.load_state()
    if getattr(st, "unavailable", False):
        raise RuntimeError("the keepalive state can't be read from the DB: "
                           + (runner_state.load_failed() or "unknown reason"))
    return st


_STALE_FLOWS: set[str] = set()   # "running" records already alerted as stale (once per process)


def compaction_flow(sid: str, st: Any, now: datetime) -> tuple[datetime, datetime | None] | None:
    """D-262: the session's latest compaction flow, from the keepalive state's "compactions"
    (keepalive.record_compaction: "running" from right before the save prompt until the READY reply
    or the failure, then "compacted"/"failed" with done_at): -> (its start, its end; None while it
    runs), or None (no flow). A failed attempt whose save prompt never went out (its save step has
    no sent_at) was no flow. A record still "running" COMPACT_FLOW_MAX after its start can't be
    running any more (every step has a timeout; the keep-alive died in it): it ended there, with an
    alert (once) so the owner sees it."""
    best: tuple[datetime, datetime | None, str] | None = None
    for key, v in (st.get("compactions") or {}).items():
        if not isinstance(v, dict) or v.get("session") not in (None, sid) or v.get("status") not in ka.COMPACT_STARTED:
            continue
        save = (v.get("steps") or {}).get("save")
        if v.get("status") == "failed" and isinstance(save, dict) and not save.get("sent_at"):
            continue
        start = usage_stale.ts(v.get("at"))
        if start is None or start > now or (best and start <= best[0]):
            continue
        end = None if v.get("status") == "running" else (usage_stale.ts(v.get("done_at")) or start)
        best = (start, end, key)
    if best is None:
        return None
    start, end, key = best
    if end is None and now - start > COMPACT_FLOW_MAX:
        end = start + COMPACT_FLOW_MAX
        if key not in _STALE_FLOWS:
            _STALE_FLOWS.add(key)
            ka.alert(f"compaction {key} of {sid[:8]} still recorded as running",
                     f"started {ka.berlin(start)}, longer than its steps' timeouts allow ({COMPACT_FLOW_MAX}): the "
                     "keep-alive likely died in it. The inbox treats it as over (D-262); look at keepalive.log.")
            log(f"inbox: compaction {key} of {sid[:8]} recorded running since {ka.berlin(start)}: treated as over")
    return start, end


def manager_active(sid: str, now: datetime) -> str | None:
    """D-252: why the task-manager session `sid` counts as active (a keep-alive run going, a turn
    or a subagent running, a request within ACTIVE_WINDOW), else None (no transcript: not active).
    D-262: the keep-alive's compaction flow is no activity: while it runs nothing counts (the
    message is triaged; a "now" one waits for the READY, delivery_hold), and after it only what
    came after its end counts (a prompt after it, and the requests of such prompts' turns), so
    neither the flow's own turns nor the activity before it make the task-manager active. A check
    that can't be made raises (the keepalive state or the transcript unreadable)."""
    st = load_state()
    flow = compaction_flow(sid, st, now)
    if flow and flow[1] is None:
        return None
    if ka.run_active(sid, now, st) is not None:   # only a run fired after the flow (run_active)
        return "a keep-alive run is going"
    path = ka.transcript_path(sid)
    if not path:
        return None
    sc = compact_timing.scan_cached(path)
    since = flow[1] if flow else None
    busy = ka.compaction_busy_reason(path, now)
    if busy and (since is None or any(p.ts > since for p in sc.prompts)):
        return busy
    if since is None:
        reqs = sc.requests
    else:
        reqs = [r for p, rs in compact_timing.turns(sc) if p.ts > since for r in rs]
    if reqs and now - reqs[-1].ts <= ACTIVE_WINDOW:
        return f"its last request was {max(0.0, (now - reqs[-1].ts).total_seconds()) / 60:.0f} min ago"
    return None


def triage(conn: Any, m: dict[str, Any], now: datetime | None = None) -> str | None:
    """Sort one message: "now" without a Haiku call while its task-manager is active (D-252),
    else the Haiku verdict. A call refused at the account's usage limit holds the message until
    the limit's reset (triage_hold, D-244); after that reset it is sorted once more, and a failure
    then (the limit again too) is an error. -> its verdict, "held" (waiting for the reset), or None
    (failed: the message shows the error)."""
    now = now or datetime.now(UTC)
    retry = store.parse_iso(m.get("retry_after"))
    if retry is not None and now < retry + TRIAGE_RETRY_MARGIN:
        return "held"
    try:
        active = manager_active(m["session_id"], now)
    except Exception as e:   # noqa: BLE001 - an error on the message (D-235/D-244), never a triage instead
        failed(conn, m, "inbox activity check failed", f"{type(e).__name__}: {e}")
        return None
    if active:
        t: dict[str, Any] = {"verdict": "now", "reason": f"task-manager active ({active}): no triage",
                             "cost_usd": None, "usage": None}
    else:
        try:
            t = triage_call(m["text"])
        except inbox.TriageError as e:
            if retry is not None:
                failed(conn, m, "inbox message triage failed after the usage limit's reset",
                       f"held until {ka.berlin(retry)}, sorted again: {e}")
                return None
            if isinstance(e, inbox.TriageLimit):
                return triage_hold(conn, m, e, now)
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
    """Why the message can't be typed in now (or None) and the send plan. A failed check raises.
    The keep-alive's compaction flow holds it until the READY (D-262): the session lock (deliver)
    and, next to it, the flow's "running" record (compaction_flow), which also covers a keep-alive
    whose lock directory isn't this process's."""
    flow = compaction_flow(sid, load_state(), now)
    if flow and flow[1] is None:
        return f"{HOLD_COMPACTING}, since {ka.berlin(flow[0])}", ""
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
            msg = ka.session_message("inbox_now", repo=cwd or None, hooked=ka.rules_hooked(cwd),
                                   **inbox.now_fields(m, n))
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
                fail_stop("noted inbox messages not marked included",
                          f"{ids} went out with #{m['id']}: {type(e).__name__}: {e}", session=sid)
    finally:
        ka.release_lock(lock)
    (verify or store_verify)(m["id"], path, offset, now)
    return "delivered"


# ---------------------------------------------------------------- post-send verification (S7, §3.3)
#
# A delivered message's check used to be a thread (one per message, blocking in time.sleep until it
# saw the marker or its timeout). Phase 3a S7 stores it instead (runner_state VERIFY_COMPONENT
# "inbox", "entries" {msg, path, offset, deadline}), so a restart resumes the same entry from the
# same offset and deadline rather than losing it: the `inbox_verify` runner job (or, in legacy mode,
# the worker thread's own pass) checks every pending entry each time it runs.

def store_verify(msg_id: int, path: str, offset: int, now: datetime, timeout: timedelta | None = None) -> None:
    """Record message `msg_id`'s post-send check: it must reach the session's transcript (its
    marker, inbox.seen, after `offset`) within `timeout` (default VERIFY_TIMEOUT), else it is an
    error (D-235), as a live verify always was. Wakes the runner: its `inbox_verify` tick is
    otherwise dormant while nothing is pending (§3.7)."""
    deadline = now + (VERIFY_TIMEOUT if timeout is None else timeout)
    doc = runner_state.load(VERIFY_COMPONENT, default={"entries": {}})
    doc.setdefault("entries", {})[str(msg_id)] = {"msg": msg_id, "path": path, "offset": offset,
                                                  "deadline": store.iso(deadline)}
    runner_state.save(VERIFY_COMPONENT, doc)
    import runner      # runner.py never imports inbox_worker; a plain import would still be safe, but this
    runner.wake()      # matches how runner_jobs.py's own functions import their modules late


def check_verify(conn: Any, entry: dict[str, Any], now: datetime) -> str:
    """One stored entry's outcome: "seen" (its marker is in the transcript after its offset:
    settled), "pending" (keep waiting), or "failed" (past its deadline without being seen: an
    error on the message, as a live verify always was) -> "seen" | "pending" | "failed"."""
    if inbox.seen(entry["path"], entry["offset"], inbox.marker(entry["msg"])):
        return "seen"
    deadline = store.parse_iso(entry.get("deadline"))
    if deadline is not None and now < deadline:
        return "pending"
    m = store.get_inbox(conn, entry["msg"])
    if m is not None and m["status"] == "delivered":
        failed(conn, m, "inbox message not delivered",
              f"typed into the session, but not in its transcript after {VERIFY_TIMEOUT}")
    return "failed"


def verify_pending(conn: Any = None, now: datetime | None = None) -> bool:
    """Whether the `inbox_verify` job has anything to do (runner.POLL_PENDING)."""
    return bool(runner_state.load(VERIFY_COMPONENT, default={"entries": {}}).get("entries"))


def verify_tick(conn: Any, now: datetime | None = None) -> dict[str, int]:
    """The `inbox_verify` job (docs/phase3a_plan.md §3.2/§3.3): one check per pending entry; a
    restart resumes each from its stored offset and deadline (§3.4) and ends it seen or as an
    error at its deadline. -> counts per outcome."""
    now = now or datetime.now(UTC)
    doc = runner_state.load(VERIFY_COMPONENT, default={"entries": {}})
    entries = doc.setdefault("entries", {})
    out: dict[str, int] = {}
    for key in list(entries):
        try:
            outcome = check_verify(conn, entries[key], now)
        except Exception as e:   # noqa: BLE001 - the tick's own error (D-235): reported, not lost
            fail_stop("inbox delivery check failed", f"message #{entries[key].get('msg')}: {type(e).__name__}: {e}")
            outcome = "failed"
        out[outcome] = out.get(outcome, 0) + 1
        if outcome != "pending":
            entries.pop(key, None)
    runner_state.save(VERIFY_COMPONENT, doc)
    return out


# ---------------------------------------------------------------- the passes

def pending(conn: Any, now: datetime | None = None) -> bool:
    """Whether the `inbox` job has anything to do (runner.POLL_PENDING): a message waiting to be
    triaged or delivered, or held (D-244: tried again by every pass while a hold lasts)."""
    return bool(store.list_inbox(conn, "pending", limit=1))


def process(conn: Any, now: datetime | None = None, verify: Callable[..., Any] | None = None) -> dict[str, int]:
    """One pass: triage the new messages, then try to deliver every "now" one (oldest first).
    -> counts per outcome."""
    out: dict[str, int] = {}
    for m in reversed(store.list_inbox(conn, "pending", limit=200)):
        if m["verdict"] is None:
            v = triage(conn, m, now)
            out[v or "failed"] = out.get(v or "failed", 0) + 1
    for m in reversed(store.list_inbox(conn, "pending", limit=200)):
        if m["verdict"] == "now":
            r = deliver(conn, m, now or datetime.now(UTC), verify)
            out[r] = out.get(r, 0) + 1
    return out


def run_forever(wake: threading.Event, stop: threading.Event | None = None) -> None:
    """AFCLAUDE_SCHEDULER=legacy only (S7: the runner's `inbox` / `inbox_verify` jobs replace this
    thread otherwise): a pass on every wake-up (a new message, a retry) and every POLL seconds,
    triage/delivery then the verify checks. A failed pass (a DB failure, ...) is an error
    (fail_stop: alert + automation paused, install-wide: it is about no one message); the next pass
    still runs (the owner's messages are no automation)."""
    stop = stop or threading.Event()
    log("inbox worker started")
    while not stop.is_set():
        wake.clear()
        try:
            conn = store.connect()
            try:
                process(conn)
                verify_tick(conn)
            finally:
                conn.close()
        except Exception as e:   # noqa: BLE001 - an error (D-235/D-244), reported; the next pass runs
            fail_stop(f"inbox worker pass failed: {type(e).__name__}", f"{e}\n{traceback.format_exc()[-1200:]}")
        wake.wait(POLL)
