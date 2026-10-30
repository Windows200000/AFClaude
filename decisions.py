#!/usr/bin/env python3
"""
decisions.py: the owner's decision store in the DB (D-242; phase 4b of docs/dashboard_design/07_data_model.md
§7.6, D-107/D-151/D-158/D-176). The table is in store.py (DECISIONS_SCHEMA: `decisions`, plus the
FTS5 index `decisions_fts` when the SQLite build has FTS5, else lookups use LIKE), the writes in
actions.py (decision.import, decision.keywords, audited). This module parses the Markdown source,
imports it, and builds what the task-manager reads:

  - DECISIONS.md (local only, gitignored) stays the source the task-manager edits for now. The
    importer reads it (never writes it) and mirrors it into the table: one row per "### D-NNN" or
    "### M-NNN" entry (the drift log's DR-NN entries and other headings are not decisions), with
    the fields of its "**Name:** value" lines (Type, Date, Status, Supersedes, Summary, Keywords,
    Owner's words, Source, Scope, Interpretation) and the full entry text. An entry with no Status
    line defaults to "active" (state_of), unless its title itself names the fate ("... (SUPERSEDED
    by D-NNN: ...)", e.g. the early M-003) -- import_file's "no_status" always lists every entry
    with no Status line, title marker or not, so the rest (plain, no marker) stay visible too, since
    DECISIONS.md is never edited here to add one. Idempotent: an unchanged entry writes nothing, so
    an unchanged file is a no-op (no audit row) -- but an entry whose text is unchanged while a
    derived field (e.g. state) now comes out different, because the derivation logic itself
    changed, is still updated ("restated": store.put_decision_entry / DECISION_DERIVED_FIELDS).
    It runs every 10 min with the docs import
    (project_docs.py import --cron), at every SessionStart hook below, and by hand:
        python3 decisions.py import [--file PATH]        (= python3 tasks.py decision import)
  - keywords: the file's Keywords line, plus the task-manager's own additions and removals
    (`tasks.py decision keywords D-NNN add|remove KW...`, action decision.keywords, D-242). Those are
    kept apart from the file's list, so a re-import never undoes them.
  - the TLDR (D-242): every current decision (not done, expired or superseded) as one line, id + title
    (+ the status when it isn't plain "active"). The repo's SessionStart hook injects it after a
    compaction and in a brand-new conversation (sources compact, startup, clear; not resume, and so
    not a run's continue), importing the file first so it is current:
        python3 decisions.py hook --part N --of M        (.claude/settings.json)
    Claude Code caps a hook's additionalContext at 10,000 characters, so the TLDR is split over M
    hook commands (parts); a TLDR that outgrows them says so in its last part (add a part).
  - lookups: `tasks.py decision find KW...` (full entries, keyword or topic matches first, then
    full-text hits), `tasks.py decision show D-NNN`, MCP afclaude_decisions (find/show/propose; a
    find without full=true lists only id, title and topics, D-176/D-284: expand an entry before
    acting on it).
  - native entries (phase 4b Q1, actions.decision_add|edit|expire|delete|confirm|link|topics): written
    in the DB itself (origin 'db'), their text rendered here (render_body) from the fields; an
    owner's words are active at once, the task-manager's own entries 'proposed' (not in the TLDR)
    until the owner confirms (D-285). The import above leaves them alone.

    python3 decisions.py tldr | import [--file PATH] | hook --part N --of M
(the lookups and keyword changes are in tasks.py: `tasks.py decision tldr|find|show|keywords|import`)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402  (reads)

DEFAULT_SOURCE = os.environ.get("AFCLAUDE_DECISIONS_FILE") or os.path.join(HERE, "DECISIONS.md")
IMPORT_ACTOR = "runner:import"
HOOK_ACTOR = "runner:session-start"
HOOK_SOURCES = ("startup", "compact", "clear")        # D-242: not resume (nor a run's continue)
HOOK_CONTEXT_CAP = 10000                             # Claude Code's cap per hook's additionalContext
HOOK_MARGIN = 200                                    # room for the part header's numbers
FIND_LIMIT = 10                                      # full entries a find prints by default

ENTRY_RE = re.compile(r"^### ((?:D|M)-\d+)\b[ \t]*(.*?)\s*$")
HEADING_RE = re.compile(r"^(#{2,3}) +(.*?)\s*$")
FIELD_RE = re.compile(r"\*\*([^*\n]{1,80}?):\*\*")
TRAIL_RE = re.compile(r"(?:\s*(?:·|\n\s*-|\n\s*---))+\s*$")
# "**Name:**" -> the column (first occurrence wins; addenda and corrections stay in the full text)
FIELDS = {"type": "type", "date": "date", "status": "status", "supersedes": "supersedes",
          "summary": "summary", "keywords": "keywords", "owner's words": "words", "source": "source",
          "scope": "scope"}
STATES = store.DECISION_STATES                       # + proposed: the task-manager's own, waiting for the owner
CURRENT = ("active", "partial")                      # what the TLDR and a default find show (not proposed)
PROPOSED_STATUS = "proposed (the task-manager's own entry, waiting for the owner's confirmation, D-285)"
BERLIN = ZoneInfo("Europe/Berlin")                   # D-031: a Status's "active until" date is Berlin time


class SourceError(ValueError):
    """DECISIONS.md can't be imported as it is (no entries, a duplicate id, a mass removal)."""


class IdCollision(SourceError):
    """The file has an entry whose id a native entry already holds (both would be D-NNN): the import
    stops, nothing is written, the owner is alerted (D-235); the file is never edited by code."""


@dataclass
class Entry:
    id: str
    title: str
    section: str
    body: str                                # the full entry text, heading line included
    type: Optional[str] = None
    date: Optional[str] = None
    status: Optional[str] = None
    supersedes: Optional[str] = None
    summary: Optional[str] = None
    words: Optional[str] = None
    source: Optional[str] = None
    scope: Optional[str] = None
    interpretation: Optional[str] = None
    keywords: list[str] = field(default_factory=list)

    def row(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = state_of(self.status, self.title)
        return d

    @property
    def has_status(self) -> bool:
        return bool((self.status or "").strip())


TITLE_SUPERSEDED_RE = re.compile(r"\bsuperseded\b", re.IGNORECASE)


# "active until 2026-10-22 19:00 (one-off, D-278)": current until then (a date alone: to its end)
UNTIL_RE = re.compile(r"active until (\d)")
UNTIL_DATE_RE = re.compile(r"active until (\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?(?!\d)")


def until_of(status: str) -> Optional[datetime]:
    """The end of an "active until YYYY-MM-DD[ HH:MM]" Status (Berlin time; a date alone ends at
    the next midnight), or None when the Status isn't that. "active until" followed by anything
    but a valid date (e.g. a typo'd one) raises SourceError: the entry's lifetime can't be told."""
    s = status.strip().lower()
    if not UNTIL_RE.match(s):
        return None
    m = UNTIL_DATE_RE.match(s)
    try:
        if m is None:
            raise ValueError("no YYYY-MM-DD[ HH:MM] date")
        if m.group(2):
            return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M").replace(tzinfo=BERLIN)
        return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=BERLIN) + timedelta(days=1)
    except ValueError as e:
        raise SourceError(f"Status {status.strip()!r}: 'active until' needs a valid date "
                          f"(YYYY-MM-DD or YYYY-MM-DD HH:MM, Berlin time): {e}") from e


def state_of(status: Optional[str], title: Optional[str] = None, now: Optional[datetime] = None) -> str:
    """The status line -> active | partial | done | superseded. Anything that isn't plainly done
    or (fully) superseded counts as current: 'active (to build)', 'pending clarification', a
    missing status. 'expired ...' (a one-off whose moment passed, D-278) counts as done, and so
    does 'active until <date>' once that date (Berlin time) is past `now` (default: the current
    time; the stored state follows at the next import, every 10 min and at each session start,
    as a "restated" entry). A handful of early manager entries (M-001..M-010) have no Status line
    at all; when one of those also names its own fate in the title ("... (SUPERSEDED by D-NNN:
    ...)", e.g. M-003) that marker decides instead of defaulting to active (`import_file`'s
    "no_status" list still names every entry with no Status line, marked or not, so the rest stay
    visible too)."""
    s = (status or "").strip().lower()
    if s.startswith("partially superseded"):
        return "partial"
    if s.startswith("superseded"):
        return "superseded"
    if s.startswith("done") or s.startswith("expired"):
        return "done"
    until = until_of(s)
    if until is not None and (now or datetime.now(BERLIN)) >= until:
        return "done"
    if not s and title and TITLE_SUPERSEDED_RE.search(title):
        return "superseded"
    return "active"


def split_keywords(text: str) -> list[str]:
    """'a, b, C' -> ['a', 'b', 'C'] (as written, duplicates by case dropped)."""
    out: list[str] = []
    seen: set[str] = set()
    for k in text.split(","):
        k = " ".join(k.split())
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def _fields(e: Entry, text: str) -> None:
    marks = list(FIELD_RE.finditer(text))
    for i, m in enumerate(marks):
        name = m.group(1).strip().lower()
        col = "interpretation" if name.startswith("interpretation") else FIELDS.get(name)
        if col is None:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        value = TRAIL_RE.sub("", text[m.end():end]).strip()
        if col == "keywords":
            if not e.keywords:
                e.keywords = split_keywords(value)
        elif getattr(e, col) is None:
            setattr(e, col, value)


def parse(text: str) -> list[Entry]:
    """Every D-NNN / M-NNN entry of DECISIONS.md, in file order. An entry runs from its "### "
    heading to the next "##"/"###" heading; a trailing "---" rule is not part of it."""
    entries: list[Entry] = []
    section = ""
    cur: Optional[Entry] = None
    lines: list[str] = []

    def close() -> None:
        if cur is None:
            return
        while lines and lines[-1].strip() in ("", "---"):
            lines.pop()
        cur.body = "\n".join(lines)
        _fields(cur, "\n".join(lines[1:]))
        entries.append(cur)

    for line in text.split("\n"):
        h = HEADING_RE.match(line)
        if h:
            close()
            cur, lines = None, []
            if h.group(1) == "##":
                section = h.group(2)
                continue
            m = ENTRY_RE.match(line)
            if m:
                cur = Entry(id=m.group(1), title=m.group(2), section=section, body="")
                lines = [line.rstrip()]
            continue
        if cur is not None:
            lines.append(line.rstrip())
    close()
    return entries


def read_source(path: str) -> tuple[str, str]:
    """(text, sha256) of the source file; a missing or unreadable file raises (OSError)."""
    with open(path, "rb") as fh:
        data = fh.read()
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


def file_ids(path: Optional[str] = None) -> set[str]:
    """The ids the source file holds (none: an install without a file). A file that exists but can't
    be read or parsed raises SourceError: an id can't be given out safely then (D-235)."""
    src = os.path.abspath(path or DEFAULT_SOURCE)
    if not os.path.exists(src):
        return set()
    try:
        text, _ = read_source(src)
    except (OSError, UnicodeDecodeError) as err:
        raise SourceError(f"can't read {src}: {err}") from err
    return {e.id for e in parse(text)}


def check_entries(entries: list[Entry], path: str) -> None:
    """Refuse a source that can't be right: no entries, a duplicate id, an entry without a title,
    an "active until" Status without a valid date (until_of)."""
    if not entries:
        raise SourceError(f"{path}: no '### D-NNN' / '### M-NNN' entries")
    seen: set[str] = set()
    for e in entries:
        if e.id in seen:
            raise SourceError(f"{path}: {e.id} appears twice")
        seen.add(e.id)
        if not e.title:
            raise SourceError(f"{path}: {e.id} has no title")
        try:
            until_of(e.status or "")
        except SourceError as err:
            raise SourceError(f"{path}: {e.id}: {err}") from err


# ---------------------------------------------------------------- native entries (Q1)

def now_text() -> str:
    """A native entry's default date: Berlin time to the minute, as the source writes them."""
    return datetime.now(BERLIN).strftime("%Y-%m-%d %H:%M")


def render_body(did: str, title: str, *, type: Optional[str], date: Optional[str], status: Optional[str],
                summary: Optional[str], words: Optional[str], source: Optional[str], scope: Optional[str],
                interpretation: Optional[str]) -> str:
    """The entry text of a native decision, in the source's shape ("### D-NNN title", one
    "- **Name:** value" line per field that is set), so show, find and the full-text index treat
    it like an imported one. Topics and links are kept apart from the text."""
    lines = [f"### {did} {title}"]
    for name, value in (("Type", type), ("Date", date), ("Summary", summary),
                        ("Owner's words", f'"{words}"' if words else None), ("Source", source),
                        ("Scope", scope), ("Status", status),
                        ("Interpretation (task-manager, not binding)", interpretation)):
        if value:
            lines.append(f"- **{name}:** {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------- import

def import_file(path: Optional[str] = None, db_path: Optional[str] = None, actor: str = IMPORT_ACTOR,
                via: str = "runner", force: bool = False, conn: Optional[sqlite3.Connection] = None
                ) -> dict[str, Any]:
    """Mirror the source file into the decisions table (actions.decision_import, audited).
    -> {"file", "sha256", "entries", "added", "changed", "removed", "unchanged"}."""
    import actions
    own = conn is None
    c = conn if conn is not None else store.connect(db_path)
    try:
        r: dict[str, Any] = actions.perform(c, "decision.import",
                                            {"path": os.path.abspath(path or DEFAULT_SOURCE), "force": force},
                                            actor=actor, via=via)
        return r
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------- output

STATE_TAGS = {"active": "", "partial": "partly superseded", "done": "done", "superseded": "superseded",
              "proposed": "proposed"}


def tldr_line(row: dict[str, Any]) -> str:
    """'D-NNN title', plus ' [partly superseded]' etc. when the state isn't active (the status
    line's details, e.g. 'active (to build)', are in the full entry; a TLDR line stays short)."""
    tag = STATE_TAGS[row["state"]]
    return f"{row['id']} {row['title']}" + (f" [{tag}]" if tag else "")


def tldr_lines(conn: sqlite3.Connection) -> list[str]:
    """One line per current decision (active or partly superseded), in file order."""
    return [tldr_line(r) for r in store.decision_titles(conn, CURRENT)]


TLDR_HEAD = ("[AFClaude decisions TLDR (D-242){part}: every current decision as its id and title; all "
             "are active unless marked [partly superseded]. A title names the topic only: before acting on a "
             "decision or putting it in a brief, read it in full: `python3 tasks.py decision show D-NNN` "
             "or `tasks.py decision find KEYWORD...` (MCP afclaude_decisions; find also matches topics). Add or "
             "fix keywords when a lookup misses: `tasks.py decision keywords D-NNN add|remove KW`.]")


def split_parts(lines: list[str], parts: int, cap: int = HOOK_CONTEXT_CAP) -> list[list[str]]:
    """The TLDR lines over `parts` hook parts, each with its header under `cap` characters, in
    order. Lines that don't fit the last part are dropped there and named in a closing line."""
    room = cap - len(TLDR_HEAD) - HOOK_MARGIN
    out: list[list[str]] = [[] for _ in range(parts)]
    i, size = 0, 0
    for n, line in enumerate(lines):
        if size + len(line) + 1 > room:
            i, size = i + 1, 0
            if i >= parts:
                rest = lines[n:]
                last = out[parts - 1]
                note = (f"TLDR CUT: {len(rest)} more decisions ({rest[0].split(' ', 1)[0]} .. "
                        f"{rest[-1].split(' ', 1)[0]}) did not fit the {parts} hook parts: run "
                        "`python3 tasks.py decision tldr` now, and add a part in .claude/settings.json.")
                while last and sum(len(x) + 1 for x in last) + len(note) + 1 > room:
                    rest.insert(0, last.pop())
                    note = (f"TLDR CUT: {len(rest)} more decisions ({rest[0].split(' ', 1)[0]} .. "
                            f"{rest[-1].split(' ', 1)[0]}) did not fit the {parts} hook parts: run "
                            "`python3 tasks.py decision tldr` now, and add a part in .claude/settings.json.")
                last.append(note)
                return out
        out[i].append(line)
        size += len(line) + 1
    return out


def tldr_text(lines: list[str], part: Optional[int] = None, of: Optional[int] = None,
              note: Optional[str] = None) -> str:
    head = TLDR_HEAD.format(part=f", part {part} of {of}" if part is not None else "")
    return "\n".join([head] + ([note] if note else []) + lines)


# ---------------------------------------------------------------- the SessionStart hook

def rules_hooked(cwd: str) -> bool:
    """Only a session started in this checkout's root gets the TLDR (as keepalive.rules_hooked)."""
    try:
        return bool(cwd) and os.path.realpath(cwd) == os.path.realpath(HERE)
    except (OSError, ValueError):
        return False


def _hook_import(conn: sqlite3.Connection, source_path: Optional[str], db_path: Optional[str]) -> Optional[str]:
    """The hook's import. A database this code can't write to (a migration still to run, D-235's
    schema guard) or a file that can't be imported because of an id collision doesn't stop the TLDR:
    it is built from what the DB holds, read-only, and the returned note (shown at the top of part 1)
    says what is stale. A collision also alerts the owner (once per episode, as the 10-min import).
    Any other failure raises: the hook then says it could not build the TLDR."""
    problem = store.schema_problem(conn)
    if problem is not None:
        return (f"[NOTE: DECISIONS.md was not imported, the database's schema doesn't fit this code "
                f"({type(problem).__name__}); this list is what the database held and may lack the newest "
                f"entries. Tell the owner: `python3 store.py migrate`, or update the code.]")
    try:
        import_file(source_path, db_path, actor=HOOK_ACTOR, conn=conn)
    except IdCollision as e:
        _import_failed(db_path, source_path or DEFAULT_SOURCE, f"{type(e).__name__}: {e}")
        return f"[NOTE: DECISIONS.md was NOT imported, the owner was alerted: {e}]"
    return None


def hook_output(part: int, of: int, hook_input: dict[str, Any], source_path: Optional[str] = None,
                db_path: Optional[str] = None, project_dir: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The SessionStart hook's JSON for part `part` of `of`, or None (nothing injected): another
    source (resume), a session outside this repo's root, or an empty part. The import runs first,
    in every part (they run in parallel; each sees the file's current state after its own import).
    A failure never fails the session start: the context then says what failed and what to do."""
    if hook_input.get("source") not in HOOK_SOURCES:
        return None
    cwd = hook_input.get("cwd") or project_dir or os.getcwd()
    if not rules_hooked(str(cwd)):
        return None
    try:
        conn = store.connect(db_path)
        try:
            note = _hook_import(conn, source_path, db_path)
            chunk = split_parts(tldr_lines(conn), of, HOOK_CONTEXT_CAP - len(note or ""))[part - 1]
        finally:
            conn.close()
        if not chunk:
            return None
        text = tldr_text(chunk, part, of, note if part == 1 else None)
    except Exception as e:   # noqa: BLE001 - a hook must never fail the session start
        if part != 1:
            return None          # part 1 reports it (once, not per part)
        text = (f"[AFClaude decisions TLDR (D-242): could NOT be built ({type(e).__name__}: {e}). Tell the "
                f"owner. Until it is fixed, read {source_path or DEFAULT_SOURCE} directly before design or "
                "planning work.]")
        sys.stderr.write(f"decisions hook: {type(e).__name__}: {e}\n")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}


def print_hook(part: int, of: int, source_path: Optional[str] = None) -> int:
    """The hook command: reads the hook input (stdin JSON), prints the hook JSON, always exits 0."""
    try:
        raw = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        hook_input = {}
    out = hook_output(part, of, hook_input if isinstance(hook_input, dict) else {}, source_path,
                      project_dir=os.environ.get("CLAUDE_PROJECT_DIR"))
    if out is not None:
        print(json.dumps(out))
    return 0


# ---------------------------------------------------------------- the cron import

CRON_MARK = "decisions_import_alerted"      # meta key: the --cron alert's episode


def cron_import(db_path: Optional[str] = None, repo: Optional[str] = None,
                alert: Optional[Callable[[str, str], None]] = None) -> tuple[int, str]:
    """The scheduled import (project_docs.py import --cron, every 10 min): the repo's DECISIONS.md
    into the DB. No file = nothing to do (an install without one). A failure alerts the owner once
    per episode (it ends when the file imports again) and exits 1. -> (exit code, line or "")."""
    path = os.path.join(os.path.abspath(repo or HERE), "DECISIONS.md")
    if not os.path.exists(path):
        return 0, ""
    mismatch = False
    try:
        conn = store.connect(db_path)
        try:
            problem = store.schema_problem(conn)       # no doomed write: the failure is the alert
        finally:
            conn.close()
        if problem is not None:
            raise problem
        r = import_file(path, db_path)
        err = None
    except (OSError, ValueError, LookupError, store.DBError, sqlite3.Error) as e:
        r, err, mismatch = {}, f"{type(e).__name__}: {e}", isinstance(e, store.SchemaMismatch)
    if err and mismatch:      # the episode's mark can't be written either: the out-of-DB alert, once
        store.alert_once(store.fallback_path(db_path), "AFClaude decisions import failed: the schema",
                         f"{path}: {err}\nNo write to the database until it is migrated "
                         "(python3 store.py migrate) or the code is updated.", "decisions-import")
    elif err:
        _import_failed(db_path, path, err, alert)
    else:
        _import_ok(db_path)
    if err:
        return 1, f"{store.now_iso()} decisions import: {err}"
    if r.get("added") or r.get("changed") or r.get("removed"):
        parts = [f"{k} {', '.join(r[k])}" for k in ("added", "changed", "removed") if r.get(k)]
        return 0, f"{store.now_iso()} decisions import: " + "; ".join(parts)
    return 0, ""


def _import_failed(db_path: Optional[str], path: str, err: str,
                   alert: Optional[Callable[[str, str], None]] = None) -> None:
    """Alert the owner that the import failed, once per episode (CRON_MARK, shared by the 10-min
    import and the hook; the episode ends when an import succeeds, _import_ok)."""
    conn = store.connect(db_path)
    try:
        if not store.get_meta(conn, CRON_MARK):
            (alert or _alert)("decisions import failed",
                              f"{path}: {err}\nThe decision store in the DB stays as it was until the file "
                              "imports again (python3 tasks.py decision import).")
            with store.transaction(conn):
                store.set_meta(conn, CRON_MARK, store.now_iso())
    finally:
        conn.close()


def _import_ok(db_path: Optional[str]) -> None:
    conn = store.connect(db_path)
    try:
        if store.get_meta(conn, CRON_MARK):
            with store.transaction(conn):
                store.set_meta(conn, CRON_MARK, "")
    finally:
        conn.close()


def _alert(subject: str, body: str) -> None:
    import project_docs
    project_docs._alert(subject, body)


# ---------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AFClaude decision store (D-242).")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("import", help="import DECISIONS.md into the DB (idempotent; the file is not changed)")
    i.add_argument("--file", default=None, help=f"the source (default {DEFAULT_SOURCE})")
    i.add_argument("--force", action="store_true", help="allow removing more than half of the entries")
    h = sub.add_parser("hook", help="the SessionStart hook: one part of the TLDR as hook JSON (stdin: hook input)")
    h.add_argument("--part", type=int, required=True)
    h.add_argument("--of", type=int, required=True)
    h.add_argument("--file", default=None, help=f"the source (default {DEFAULT_SOURCE})")
    sub.add_parser("tldr", help="the TLDR as the hook shows it (no import)")
    args = ap.parse_args(argv)
    if args.cmd == "hook":
        if not 1 <= args.part <= args.of:
            ap.error("--part must be 1..--of")
        return print_hook(args.part, args.of, args.file)
    try:
        if args.cmd == "import":
            print(json.dumps(import_file(args.file, args.db, actor="cli", via="cli", force=args.force),
                             indent=1, ensure_ascii=False))
            return 0
        conn = store.connect(args.db)
        try:
            print(tldr_text(tldr_lines(conn)))
        finally:
            conn.close()
        return 0
    except (store.DBError, sqlite3.Error) as err:
        print(f"error: database: {err}", file=sys.stderr)
        return 1
    except (OSError, ValueError, LookupError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
