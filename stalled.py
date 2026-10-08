#!/usr/bin/env python3
"""
Host-wide detector for Claude Code sessions that are stopped by a usage limit.

Walks every TOP-LEVEL session transcript (~/.claude/projects/<proj>/<uuid>.jsonl;
subagent transcripts live in subdirectories and are skipped) and keeps the
result in the SQLite store (store.py, data/afclaude.db):

  - every synthetic "You've hit your ... limit" notice -> limit_hits (history)
  - per session: title, cwd, own flag, last user/assistant entry, and whether
    that entry is a limit notice (= currently stalled), with kind and reset time

Detection is keepalive.py's: stall_info() / parse_reset_text() / message_text()
on the last user/assistant entry, where "user/assistant entry" is the
predicate of keepalive.last_message() (trailing system / last-prompt /
cost-state / atis-latch lines don't count). A later user/assistant entry
un-stalls the session.

Incremental: per file it remembers the byte offset of the last complete line
and only reads what was appended since; a file that shrank is rescanned from 0.
Only lines that can matter are JSON-decoded: lines mentioning <synthetic> or a
title entry type, plus a short backwards walk from the end of each new chunk to
find its last user/assistant entry.

    stalled.py scan                       one-line summary
    stalled.py list [--all] [--json]      currently stalled (or: ever hit a limit)
    stalled.py history <session-prefix>   one session's limit hits

list/history scan first (it is incremental and cheap); --no-scan skips that.
Everything under ~/.claude/projects is only read, never written.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keepalive as ka  # noqa: E402  (detection)
import store  # noqa: E402
import usage_sampler as us  # noqa: E402  (own-session markers)

UTC = timezone.utc
OWN_CWD_MARKERS = tuple(dict.fromkeys(("/work/keepalive",) + tuple(us.OWN_CWD_MARKERS)))
OWN_NAME_RE = us.OWN_NAME_RE
OWN_LIST = os.environ.get("AFCLAUDE_OWN_LIST", us.OWN_LIST)

TITLE_TYPES = {"custom-title": (3, "customTitle"), "agent-name": (2, "agentName"), "ai-title": (1, "aiTitle")}
FORWARD_MARKERS = (b"<synthetic>", b'"custom-title"', b'"agent-name"', b'"ai-title"')
RESET_ON_RESCAN = dict(title=None, title_rank=None, first_seen=None, last_activity=None,
                       last_msg_uuid=None, last_msg_type=None, last_msg_ts=None,
                       stalled=0, stalled_since=None, stall_kind=None, stall_reset_at=None,
                       stall_text=None, stall_uuid=None)
CLEAR_STALL = dict(stalled=0, stalled_since=None, stall_kind=None, stall_reset_at=None,
                   stall_text=None, stall_uuid=None)


def is_message_entry(e):
    """Same predicate as keepalive.last_message()."""
    return e.get("type") in ("user", "assistant") and not e.get("isSidechain")


def _loads(line):
    try:
        return json.loads(line)
    except UnicodeDecodeError:
        try:
            return json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            return None
    except ValueError:
        return None


def transcripts(projects_dir):
    """Top-level session transcripts only: <projects>/<proj>/<uuid>.jsonl."""
    return sorted(glob.glob(os.path.join(projects_dir, "*", "*.jsonl")))


def is_own(sid, cwd, title, own_extra):
    return (sid in own_extra
            or any(mk in (cwd or "") for mk in OWN_CWD_MARKERS)
            or bool(OWN_NAME_RE.match(title or "")))


def process_chunk(conn, sid, chunk, row):
    """Apply the complete lines in `chunk` (bytes) to session `sid`.
    `row` is the session's current DB state (dict). Returns (#new hits, updates)."""
    lines = [ln for ln in chunk.split(b"\n") if ln.strip()]
    upd = {}
    new_hits = 0

    # forward pass: hits (in order) and titles
    rank = row.get("title_rank") or 0
    for ln in lines:
        if not any(mk in ln for mk in FORWARD_MARKERS):
            continue
        e = _loads(ln)
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        if t in TITLE_TYPES:
            r, key = TITLE_TYPES[t]
            val = e.get(key)
            if val and r >= rank:
                upd["title"], upd["title_rank"], rank = val, r, r
            continue
        si = ka.stall_info(e)
        if si:
            if store.add_hit(conn, sid, si["uuid"] or f"{sid}:{e.get('timestamp')}", si["timestamp"],
                             si["kind"], si["reset_from_text"], si["text"]):
                new_hits += 1

    # first entry timestamp (only once per session)
    if not row.get("first_seen"):
        for ln in lines[:200]:
            e = _loads(ln)
            if isinstance(e, dict) and e.get("timestamp"):
                upd["first_seen"] = e["timestamp"]
                break

    # backward walk: last user/assistant entry, latest timestamp, latest cwd
    last_msg = last_ts = cwd = None
    extra = 0
    for ln in reversed(lines):
        e = _loads(ln)
        if not isinstance(e, dict):
            continue
        if last_ts is None and e.get("timestamp"):
            last_ts = e["timestamp"]
        if cwd is None and e.get("cwd"):
            cwd = e["cwd"]
        if last_msg is None and is_message_entry(e):
            last_msg = e
        if last_msg is not None:
            if cwd is not None and last_ts is not None:
                break
            extra += 1
            if extra > 200:     # give up on cwd/timestamp; keep the old value
                break
    if last_ts and last_ts > (row.get("last_activity") or ""):
        upd["last_activity"] = last_ts
    if cwd:
        upd["cwd"] = cwd
    if last_msg is not None:
        upd.update(last_msg_uuid=last_msg.get("uuid"), last_msg_type=last_msg.get("type"),
                   last_msg_ts=last_msg.get("timestamp"))
        si = ka.stall_info(last_msg)
        if si:
            upd.update(stalled=1, stalled_since=store.iso(si["timestamp"]), stall_kind=si["kind"],
                       stall_reset_at=store.iso(si["reset_from_text"]), stall_text=si["text"],
                       stall_uuid=si["uuid"])
        else:
            upd.update(CLEAR_STALL)
    return new_hits, upd


def scan(conn: sqlite3.Connection | None = None, projects_dir: str | None = None,
         own_list: str | None = None) -> dict[str, Any]:
    """Incremental scan of all top-level transcripts. Returns a stats dict."""
    t0 = time.monotonic()
    own_conn = conn is None
    conn = conn or store.connect()
    projects_dir = projects_dir or ka.PROJECTS_DIR
    own_list = own_list or OWN_LIST
    st = {"files": 0, "changed": 0, "rescanned": 0, "bytes": 0, "new_hits": 0, "new_sessions": 0}
    known = {r["session_id"]: dict(r) for r in conn.execute("SELECT * FROM sessions")}
    now = store.iso(datetime.now(UTC))
    for path in transcripts(projects_dir):
        st["files"] += 1
        sid = os.path.basename(path)[:-len(".jsonl")]
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        row = known.get(sid)
        if row and row["path"] == path and size == row["last_scanned_size"]:
            continue
        upd = {"path": path, "project_dir": os.path.basename(os.path.dirname(path)),
               "last_scanned_size": size, "updated_at": now}
        if row is None:
            row = {}
            st["new_sessions"] += 1
        off = row.get("last_scanned_offset") or 0
        if size < off or (row.get("path") and row["path"] != path):
            off = 0
            row = dict(row, **RESET_ON_RESCAN)
            upd.update(RESET_ON_RESCAN)
            st["rescanned"] += 1
        with open(path, "rb") as fh:
            fh.seek(off)
            chunk = fh.read(size - off)
        last_nl = chunk.rfind(b"\n")
        if not row.get("session_id"):
            store.upsert_session(conn, sid, path=path)   # hits reference the row (FK)
        if last_nl >= 0:
            st["changed"] += 1
            st["bytes"] += last_nl + 1
            hits, u = process_chunk(conn, sid, chunk[:last_nl + 1], row)
            st["new_hits"] += hits
            upd.update(u)
            upd["last_scanned_offset"] = off + last_nl + 1
        else:
            upd["last_scanned_offset"] = off
        store.upsert_session(conn, sid, **upd)
        known[sid] = dict(row, **upd, session_id=sid)
    # own flag for every session (own_sessions.txt can change without any transcript changing)
    own_extra = set()
    try:
        with open(own_list) as fh:
            own_extra = {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    except OSError:
        pass
    for sid, r in known.items():
        own = int(is_own(sid, r.get("cwd"), r.get("title"), own_extra))
        if own != r.get("own"):
            conn.execute("UPDATE sessions SET own=? WHERE session_id=?", (own, sid))
    st["seconds"] = round(time.monotonic() - t0, 3)
    store.set_meta(conn, "last_scan_at", now)
    store.set_meta(conn, "last_scan_seconds", st["seconds"])
    conn.commit()
    st.update(store.counts(conn))
    if own_conn:
        conn.close()
    return st


# ---------------------------------------------------------------- CLI

def berlin_short(s):
    dt = store.parse_iso(s)
    return dt.astimezone(ka.BERLIN).strftime("%Y-%m-%d %H:%M %Z") if dt else "-"


def summary_line(st):
    return (f"scan: {st['files']} transcripts, {st['changed']} changed ({st['rescanned']} rescanned), "
            f"{st['bytes'] / 1e6:.1f} MB read, +{st['new_hits']} hits, +{st['new_sessions']} sessions | "
            f"total {st['sessions']} sessions, {st['hits']} hits in {st['sessions_with_hits']} sessions, "
            f"{st['stalled']} stalled | {st['seconds']:.2f}s")


def cut(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


def row_json(r, now):
    reset = store.parse_iso(r.get("stall_reset_at"))
    return {"session_id": r["session_id"], "title": r.get("title"), "cwd": r.get("cwd"),
            "own": bool(r.get("own")), "stalled": bool(r.get("stalled")),
            "stalled_since": r.get("stalled_since"), "kind": r.get("stall_kind"),
            "reset_at": r.get("stall_reset_at"),
            "reset_at_berlin": berlin_short(r.get("stall_reset_at")) if reset else None,
            "reset_passed": bool(reset and reset <= now), "hits": r.get("hits"),
            "last_hit": r.get("last_hit"), "text": r.get("stall_text"), "path": r.get("path")}


def cmd_list(conn, args):
    now = datetime.now(UTC)
    rows = store.stalled_sessions(conn, include_resolved=args.all)
    if args.json:
        print(json.dumps([row_json(r, now) for r in rows], indent=1))
        return
    if not rows:
        print("no sessions ever hit a limit" if args.all else "no session is currently stalled")
        return
    print(f"{'session':8}  {'st':2}  {'kind':7}  {'resets (Berlin)':22}  {'hits':>4}  {'own':3}  "
          f"{'title':30}  cwd")
    for r in rows:
        reset = store.parse_iso(r.get("stall_reset_at"))
        rs = berlin_short(r.get("stall_reset_at")) + ("*" if reset and reset <= now else "") \
            if r.get("stalled") else "-"
        print(f"{r['session_id'][:8]:8}  {'S' if r.get('stalled') else '-':2}  "
              f"{(r.get('stall_kind') or '-') if r.get('stalled') else '-':7}  {rs:22}  "
              f"{r.get('hits') or 0:>4}  {'yes' if r.get('own') else '':3}  "
              f"{cut(r.get('title') or '(untitled)', 30):30}  {r.get('cwd') or '-'}")
    if any(r.get("stalled") and (store.parse_iso(r.get("stall_reset_at")) or now) <= now for r in rows):
        print("* reset time already passed; the session is still waiting for a continue")


def cmd_history(conn, args):
    matches = store.find_sessions(conn, args.session)
    if not matches:
        print(f"no session matches {args.session!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"{len(matches)} sessions match {args.session!r}: "
              + ", ".join(m["session_id"][:13] for m in matches[:10]), file=sys.stderr)
        return 1
    s = matches[0]
    hits = store.session_history(conn, s["session_id"])
    if args.json:
        print(json.dumps({"session": s, "hits": hits}, indent=1))
        return 0
    print(f"{s['session_id']}  {s.get('title') or '(untitled)'}  own={bool(s.get('own'))}")
    print(f"  cwd {s.get('cwd') or '-'}\n  path {s.get('path')}")
    print(f"  first {berlin_short(s.get('first_seen'))}, last activity {berlin_short(s.get('last_activity'))}")
    if s.get("stalled"):
        print(f"  STALLED since {berlin_short(s.get('stalled_since'))}: {s.get('stall_kind')} limit, "
              f"resets {berlin_short(s.get('stall_reset_at'))}")
    else:
        print(f"  not stalled (last {s.get('last_msg_type')} entry {berlin_short(s.get('last_msg_ts'))})")
    print(f"  {len(hits)} limit hit(s):")
    for h in hits:
        print(f"    {berlin_short(h['ts'])}  {h.get('kind') or '?':8} resets {berlin_short(h.get('reset_at'))}"
              f"  | {cut(h.get('text'), 70)}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help=f"SQLite file (default {store.DB_PATH})")
    ap.add_argument("--projects-dir", help=f"transcripts root (default {ka.PROJECTS_DIR})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="incremental scan, one-line summary")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("list", help="currently stalled sessions")
    p.add_argument("--all", action="store_true", help="every session that ever hit a limit")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-scan", action="store_true")
    p = sub.add_parser("history", help="limit hits of one session")
    p.add_argument("session", help="session id or prefix")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-scan", action="store_true")
    args = ap.parse_args(argv)
    conn = store.connect(args.db)
    try:
        if args.cmd == "scan":
            st = scan(conn, args.projects_dir)
            print(json.dumps(st) if args.json else summary_line(st))
            return 0
        if not args.no_scan:
            scan(conn, args.projects_dir)
        if args.cmd == "list":
            cmd_list(conn, args)
            return 0
        return cmd_history(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
