#!/usr/bin/env python3
"""The dispatcher's continue of a `claude rc` server thread (docs/rc_server_mode.md §4.1, §8; D-288).

An approved stalled session (D-041, D-206) held by a live rc-server thread child (preflight:
keepalive.RC_LIVE) can't be resumed or taken over: that forks the owner's Remote Control thread.
Instead the neutral continue text goes into the thread's cross-session inbox, which starts a turn
in an idle thread:

  1. the thread's CURRENT registry entry (~/.claude/sessions/<pid>.json, read now: its `name` is
     derived, dir + 2 hex, and changes on every server restart; session ids and ListAgents' short
     refs are no addresses): one live sdk-cli child with --sdk-url (or an rc-server parent), idle;
  2. a one-shot Haiku `claude -p` on the host, allowed SendMessage + ListAgents only, sends the
     text to that name (host.rc_send; in the container the whitelisted `rc-send` bridge shape,
     D-164). The documented sender: the socket protocol is never written by hand;
  3. its stream-json must show exactly one SendMessage call to that name carrying the intended text
     (whitespace-normalised), whose result is no error and no "No agent named … is reachable";
  4. the thread's transcript must get a new user entry with a <cross-session-message> followed by
     a real model reply (not <synthetic>, no API error) within VERIFY_SECONDS. That entry, not the
     tool result's wording, is the proof of delivery.

Every failure raises RcError (D-234/D-235/D-239: the dispatcher records it, alerts and pauses);
nothing is skipped silently. A busy thread raises Busy: the owner is using it, so no message is
sent and the next pass looks again (the preflight never touches a busy session either).
A thread whose child has exited (keepalive.RC_EXITED) is not handled here: only a message from the
app re-serves it, so the dispatcher leaves it to the owner ("continue it from the app").
"""
import glob
import json
import os
import re
import subprocess
import time

import host
import keepalive as ka

SEND_TIMEOUT = 120          # seconds for the Haiku sender (about 3 s in the live test, §8); the host side
                            # stops it sooner (host.RC_HOST_TIMEOUT), so it never delivers after this
VERIFY_SECONDS = 180        # the delivered message's user entry plus a real model reply within this
POLL_SECONDS = 2
MESSAGE_TAG = "<cross-session-message"
NOT_REACHABLE = re.compile(r"no agent named|not reachable", re.I)


class RcError(Exception):
    """The rc continue failed: an error the owner sees (D-235)."""


class Busy(Exception):
    """The rc thread is busy (the owner is using it): no message now."""


def thread_entry(session_id):
    """The registry entry (+ "pid", "argv") of the live rc-server thread child holding
    `session_id`, read now. RcError: no entry, no live child, several, or not an rc thread."""
    found = []
    for path in glob.glob(os.path.join(ka.SESSIONS_DIR, "*.json")):
        try:
            with open(path) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue                       # another session's file being rewritten: not ours to judge
        if isinstance(d, dict) and d.get("sessionId") == session_id:
            found.append((path, d))
    if not found:
        raise RcError(f"no ~/.claude/sessions registry entry for {session_id[:8]}")
    live = []
    for path, d in found:
        try:
            pid = int(d.get("pid") or os.path.basename(path)[:-5])
        except (TypeError, ValueError):
            continue
        start = d.get("procStart")
        if ka.pid_alive(pid) and (start is None or ka.proc_starttime(pid) in (None, str(start))):
            live.append((pid, d))
    if not live:
        raise RcError(f"no live child: the registry entry of {session_id[:8]} names no live process")
    if len(live) > 1:
        raise RcError(f"several live processes hold {session_id[:8]}: {sorted(p for p, _ in live)}")
    pid, d = live[0]
    argv = ka.proc_argv(pid)
    ppid = ka.proc_ppid(pid)
    rc_child = ka.is_sdk_child(argv) or bool(ppid and ka.is_rc_server(ka.proc_argv(ppid)))
    if d.get("entrypoint") != "sdk-cli" or not rc_child:
        raise RcError(f"pid {pid} holding {session_id[:8]} is no rc-server thread child "
                      f"(entrypoint {d.get('entrypoint')!r}, rc child: {rc_child})")
    return dict(d, pid=pid, argv=argv)


def _strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def _content(ev):
    c = (ev.get("message") or {}).get("content")
    return [x for x in c if isinstance(x, dict)] if isinstance(c, list) else []


def _norm(s):
    return " ".join(s.split())


def check_sender(r, name, text):
    """The sender's CompletedProcess (stream-json on stdout): one SendMessage to `name` carrying
    `text`, not failed. -> the tool result's text. RcError otherwise."""
    if r.returncode != 0:
        why = {126: " (the host bridge denied the rc-send shape)",
               124: " (the host stopped the sender at its timeout)"}.get(r.returncode, "")
        raise RcError(f"the Haiku sender exited rc={r.returncode}{why}: {(r.stderr or '').strip()[-300:]}")
    events = []
    for line in (r.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    uses = [c for ev in events if ev.get("type") == "assistant" for c in _content(ev)
            if c.get("type") == "tool_use" and c.get("name") == "SendMessage"]
    if len(uses) != 1:
        raise RcError(f"the Haiku sender made {len(uses)} SendMessage calls, not one")
    use = uses[0]
    if name not in set(_strings(use.get("input"))):
        raise RcError(f"the Haiku sender's SendMessage did not address {name!r}: {json.dumps(use.get('input'))[:300]}")
    message = (use.get("input") or {}).get("message")
    if not isinstance(message, str) or _norm(message) != _norm(text):
        raise RcError(f"the Haiku sender's SendMessage text differs from the intended one: "
                      f"{json.dumps(message)[:300]}")
    results = [c for ev in events if ev.get("type") == "user" for c in _content(ev)
               if c.get("type") == "tool_result" and c.get("tool_use_id") == use.get("id")]
    if not results:
        raise RcError("the Haiku sender's SendMessage has no result")
    got = " ".join(_strings(results[0].get("content")))[:500]
    if NOT_REACHABLE.search(got):
        raise RcError(f"No agent named {name!r} is reachable (child gone or name changed): {got}")
    if results[0].get("is_error"):
        raise RcError(f"the SendMessage to {name!r} failed: {got}")
    final = [ev for ev in events if ev.get("type") == "result"]
    if not final or final[-1].get("is_error"):
        raise RcError(f"the Haiku sender's run ended in an error: {json.dumps(final[-1:])[:300]}")
    return got


def scan_after(path, offset):
    """(message entry seen, real model reply after it) in `path` after byte `offset`. A synthetic
    notice or an API error entry is no reply (keepalive.is_real_reply)."""
    seen = answered = False
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read().decode(errors="replace")
    for line in data.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue                       # a line still being written
        if not isinstance(e, dict) or e.get("isSidechain"):
            continue
        if e.get("type") == "user" and MESSAGE_TAG in ka.message_text(e):
            seen = True
        elif seen and ka.is_real_reply(e):
            answered = True
    return seen, answered


def send(session_id, text, runner=None, sleep=None, clock=time.monotonic, verify_seconds=None,
         before_send=None):
    """Deliver `text` into the live rc thread holding `session_id` and verify it started a turn.
    `before_send(name)` runs once all checks passed, right before the sender is started: the
    caller records that a send may now be under way (a killed pass must never send twice).
    -> {"name", "pid", "result", "seconds"}. Busy: the thread is busy. RcError: anything else."""
    verify_seconds = VERIFY_SECONDS if verify_seconds is None else verify_seconds
    e = thread_entry(session_id)
    name, status = e.get("name") or "", e.get("status")
    if status == "busy":
        raise Busy(f"rc thread {name!r} (pid {e['pid']}) is busy")
    if status != "idle":
        raise RcError(f"rc thread {name!r} (pid {e['pid']}) has registry status {status!r}, not idle/busy")
    if not host.RC_NAME.match(name):
        raise RcError(f"rc thread of {session_id[:8]} has no usable registry name: {name!r}")
    if any(a == "--dangerously-skip-permissions" or "bypassPermissions" in a for a in e["argv"]):
        raise RcError(f"rc thread {name!r} runs with bypassPermissions: it would hold the message")
    path = ka.transcript_path(session_id)
    if not path:
        raise RcError(f"no transcript of {session_id[:8]} to verify the message in")
    offset = os.path.getsize(path)
    if before_send:
        before_send(name)
    t0 = clock()
    try:
        r = (runner or host.rc_send)(name, text, SEND_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RcError(f"the Haiku sender to {name!r} timed out after {SEND_TIMEOUT} s") from None
    except (OSError, ValueError) as err:
        raise RcError(f"the Haiku sender to {name!r} failed: {type(err).__name__}: {err}"[:500]) from None
    result = check_sender(r, name, text)
    deadline = t0 + verify_seconds
    while True:
        seen, answered = scan_after(path, offset)
        if answered:
            return {"name": name, "pid": e["pid"], "result": result[:200], "seconds": round(clock() - t0, 1)}
        if clock() >= deadline:
            raise RcError(f"rc thread {name!r}: " + ("the message arrived but no real model reply followed"
                                                     if seen else "no <cross-session-message> entry")
                          + f" within {verify_seconds} s (sender said: {result[:200]})")
        (sleep or time.sleep)(POLL_SECONDS)
