#!/usr/bin/env python3
"""
decisions.py: the owner's decision store in the DB (D-242; phase 4b of docs/dashboard_design/07_data_model.md
§7.6, D-107/D-151/D-158/D-176). The table is in store.py (DECISIONS_SCHEMA: `decisions`, plus the
FTS5 index `decisions_fts` when the SQLite build has FTS5, else lookups use LIKE), the writes in
actions.py (decision.import, decision.keywords, audited). This module parses the Markdown source,
imports it, and builds what the task-manager reads:

  - DECISIONS.md (local only, gitignored) was the source until the seeding below (phase 4b Q2); since then the
    DB is the only source and the file is neither read nor written (`import` below reports "retired"; a file
    whose sha differs from the seeded one alerts once per episode). Until then the importer read it (never
    wrote it) and mirrored it into the table: one row per "### D-NNN" or
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
    It ran every 10 min with the docs import
    (project_docs.py import --cron), at every SessionStart hook below, and by hand:
        python3 decisions.py import [--file PATH]        (= python3 tasks.py decision import)
  - the seeding (Q2, the cut-over, run once by the task-manager; it re-keys what the import already holds):
        python3 decisions.py seed --topics F --merges F [--file PATH] [--dry-run]     action decision.seed
        python3 decisions.py seed --undo
    F = two JSON files (see seed_plan): topics {"D-001": ["topic", ...]} for every surviving entry (1-3 each,
    D-284), and merges {"merges": [{title, summary, topics, supersedes, scope?, interpretation?}], "keep": [ids]}.
    The classes: active and partly superseded entries become the DB's own (origin db), unchanged, with their
    topics; superseded, done and expired ones are deleted with the full text in the audit log (D-158, D-286);
    the file's DR-NN entries and non-entry sections get one audit row each; a merge becomes a proposed
    task-manager entry carrying the originals' owner words with their dates, the originals staying in force
    until the owner confirms it (D-104, D-285). Refused before any write: a DB that does not mirror the file,
    a surviving entry without 1-3 topics, a done entry whose Status names a part that stays and that is in no
    merge or `keep`. A backup <db>.preseed-<utc>.bak first, one transaction, the counts per class against the
    file's in the result; meta decisions_source=db, decisions_seed_sha, decision_id_floor, decisions_seeded.
    --undo is refused if a seeded row changed since or a seeded proposal was confirmed.
  - keywords: the file's Keywords line, plus the task-manager's own additions and removals
    (`tasks.py decision keywords D-NNN add|remove KW...`, action decision.keywords, D-242). Those are
    kept apart from the file's list, so a re-import never undoes them.
  - the TLDR (D-242): every current decision (not done, expired or superseded) as one line, id + title
    (+ the status when it isn't plain "active"). The repo's SessionStart hook injects it after a
    compaction and in a brand-new conversation (sources compact, startup, clear; not resume, and so
    not a run's continue), from the DB only (before the seeding it imported the file first):
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
  - decision questions (phase 4b Q3): an open proposal and an unconfirmed task-manager entry each have one
    question for the owner (actions.ask_decision_question); question_kind / question_text say which kind it
    is and render it in plain words and in full (D-263, D-279); confirmed entries are protected (D-104).

    python3 decisions.py tldr | import [--file PATH] | seed ... | hook --part N --of M
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


SOURCE_META = "decisions_source"                     # 'db' once the seeding ran (Q2); absent = the file
SEED_SHA_META = "decisions_seed_sha"                 # the file's sha256 at the seeding
SEEDED_META = "decisions_seeded"                     # JSON: what the seeding turned into db rows (for --undo)
CHANGED_MARK = "decisions_file_changed_alerted"      # meta: the file-changed alert's episode (the file's sha)
TOPICS_MAX = 3                                       # a seeded entry's topics: 1-3 (D-284: a low number)


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


ID_RE = re.compile(r"\b[DM]-\d{3,}\b")


def supersedes_ids(text: Optional[str]) -> list[str]:
    """The ids a native proposal replaces (its `supersedes` column: "D-012, D-015"; the ids in the text, in
    order, once each). A file entry's free-text Supersedes line is not read through this."""
    return list(dict.fromkeys(ID_RE.findall(text or "")))


def render_body(did: str, title: str, *, type: Optional[str], date: Optional[str], status: Optional[str],
                summary: Optional[str], words: Optional[str], source: Optional[str], scope: Optional[str],
                interpretation: Optional[str], supersedes: Optional[str] = None) -> str:
    """The entry text of a native decision, in the source's shape ("### D-NNN title", one
    "- **Name:** value" line per field that is set), so show, find and the full-text index treat
    it like an imported one. Topics and links are kept apart from the text."""
    lines = [f"### {did} {title}"]
    for name, value in (("Type", type), ("Date", date), ("Summary", summary),
                        ("Owner's words", f'"{words}"' if words else None), ("Source", source),
                        ("Scope", scope), ("Status", status), ("Supersedes", supersedes),
                        ("Interpretation (task-manager, not binding)", interpretation)):
        if value:
            lines.append(f"- **{name}:** {value}")
    return "\n".join(lines)



# ---------------------------------------------------------------- decision questions (Q3)

PROPOSAL_CHOICES = ("approve", "edit", "reject")      # a proposed entry
CONFIRM_CHOICES = ("confirm", "drop")                 # an active, unconfirmed task-manager entry
QUESTION_TITLE_MAX = 120


def question_kind(d: dict[str, Any]) -> Optional[str]:
    """What the owner is asked about entry `d`: "proposal" (state proposed), "confirm" (an active
    task-manager entry nobody confirmed), None (nothing: an owner's entry, a confirmed one, any other
    state). An entry that mirrors the file (origin 'file') is never asked."""
    if d.get("origin") != "db":
        return None
    if d["state"] == "proposed":
        return "proposal"
    if d["state"] == "active" and d["author"] == "task-manager" and not d.get("confirmed_at"):
        return "confirm"
    return None


def question_choices(kind: str) -> tuple[str, ...]:
    return PROPOSAL_CHOICES if kind == "proposal" else CONFIRM_CHOICES


def _entry_lines(d: dict[str, Any]) -> list[str]:
    """One entry in plain words: id, title and date, its summary, the owner's words and the source."""
    out = [f"{d['id']}: {d['title']}" + (f" ({d['date']})" if d.get("date") else "")]
    if d.get("summary"):
        out.append(d["summary"])
    if d.get("words"):
        out.append(f"The owner's words: \"{d['words']}\"")
    if d.get("source"):
        out.append(f"Source: {d['source']}")
    return out


def question_text(d: dict[str, Any], kind: str, replaced: list[dict[str, Any]]) -> tuple[str, str]:
    """(title, text) of the question about entry `d` (plain words, nothing cut: D-263, D-279). `replaced`:
    the entries a proposal replaces, each with its own title, date and the owner's words."""
    head = d["title"] if len(d["title"]) <= QUESTION_TITLE_MAX else d["title"][:QUESTION_TITLE_MAX - 1] + "…"
    if kind == "proposal":
        title = f"Approve {d['id']}: {head}"
        lines = ["Approve this decision the task-manager proposes?", "", *_entry_lines(d)]
        if replaced:
            lines += ["", "It replaces these entries, which are deleted when you approve it:"]
            for o in replaced:
                lines += [""] + _entry_lines(o)
        lines += ["", "approve: it becomes your decision" + (" and the entries above are deleted" if replaced else "")
                  + ". edit: you rewrite its summary, then it is approved. reject: it is deleted and "
                  + ("the entries above stay." if replaced else "nothing else changes.")]
    else:
        title = f"Confirm {d['id']}: {head}"
        lines = ["Confirm this decision the task-manager recorded on its own?", "", *_entry_lines(d), "",
                 "confirm: it becomes yours and the task-manager can no longer change or delete it. "
                 "drop: it is deleted."]
    return title, "\n".join(lines)


# ---------------------------------------------------------------- the seeding (Q2)

class SeedRefused(ValueError):
    """The seeding's preconditions aren't met (every problem listed): nothing was written."""


DRIFT_RE = re.compile(r"^### (DR-\d+)\b[ \t]*(.*?)\s*$")
PREAMBLE = "(before the first heading)"
# a done entry whose Status still names a part that stays in force ("done (the guard stays active)"); an
# "active until <date>" one-off is not that
ACTIVE_PART_RE = re.compile(r"\b(active|stays?|remains?|still|continues?|later)\b", re.IGNORECASE)
ACTIVE_UNTIL_RE = re.compile(r"active until \d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2})?", re.IGNORECASE)


@dataclass
class Block:
    """A part of DECISIONS.md that is not a D-/M- entry: a DR-NN drift entry, or a section (a "##" heading or
    another "###" with its text; the text before the first heading)."""
    kind: str                                # drift | section
    id: str                                  # DR-NN, or the heading
    text: str                                # heading line included
    empty: bool                              # nothing but the heading (the structure the entries' section keeps)


def split_blocks(text: str) -> list[Block]:
    """Everything of the source that parse() does not return as an entry, in file order: each "##" heading,
    each "###" that is no D-/M- entry, and the text before the first heading, with the text up to the next
    heading. A trailing "---" rule is not part of it."""
    out: list[Block] = []
    cur: Optional[tuple[str, str, list[str]]] = ("section", PREAMBLE, [])

    def close() -> None:
        if cur is None:
            return
        kind, bid, lines = cur
        while lines and lines[-1].strip() in ("", "---"):
            lines.pop()
        if lines:
            out.append(Block(kind, bid, "\n".join(lines), empty=bid != PREAMBLE and len(lines) == 1))

    for line in text.split("\n"):
        h = HEADING_RE.match(line)
        if h:
            close()
            if h.group(1) == "###" and ENTRY_RE.match(line):
                cur = None
                continue
            d = DRIFT_RE.match(line)
            cur = ("drift", d.group(1), [line.rstrip()]) if d else ("section", h.group(2), [line.rstrip()])
            continue
        if cur is not None:
            cur[2].append(line.rstrip())
    close()
    return out


@dataclass
class Merge:
    """A merge of the seeding: the task-manager's rewording of several entries, a proposal the owner
    confirms (D-285); confirming it deletes the originals (`supersedes`)."""
    title: str
    summary: str
    topics: list[str]
    supersedes: list[str]
    words: Optional[str]                     # the originals' owner words, verbatim, with id and date
    scope: Optional[str] = None
    interpretation: Optional[str] = None


@dataclass
class SeedPlan:
    file: str
    sha: str
    entries: list[Entry]
    classes: dict[str, str]                  # entry id -> active | partial | done | expired | superseded
    survive: list[str]                       # become db rows, in file order
    delete: list[str]                        # deleted with their full text in the audit log
    keep: list[str]
    topics: dict[str, list[str]]             # every surviving entry's topics
    merges: list[Merge]
    blocks: list[Block]
    floor: int                               # the file's highest D-number

    def counts(self) -> dict[str, Any]:
        """The classes against the file's own numbers; balanced = every entry is accounted for once."""
        by = {c: sum(1 for v in self.classes.values() if v == c)
              for c in ("active", "partial", "done", "expired", "superseded")}
        merged = {i for m in self.merges for i in m.supersedes}
        return {"file": {"entries": len(self.entries), "drift_entries": sum(b.kind == "drift" for b in self.blocks),
                         "sections": sum(b.kind == "section" and not b.empty for b in self.blocks),
                         "headings_only": sum(b.empty for b in self.blocks)},
                "classes": by,
                "become_db_rows": len(self.survive),
                "of_which_kept": len(self.keep), "of_which_merge_originals": len(merged),
                "deleted": len(self.delete), "proposals": len(self.merges),
                "audit_only": sum(not b.empty for b in self.blocks),
                "balanced": len(self.survive) + len(self.delete) == len(self.entries)}


def expired_status(status: Optional[str]) -> bool:
    """A one-off whose moment passed: "expired ..." or an "active until <date>" that is past (D-278)."""
    s = (status or "").strip().lower()
    return s.startswith("expired") or until_of(s) is not None


def merge_words(originals: list[Entry]) -> Optional[str]:
    """The originals' owner words as written, each with its id and date: what a merge's proposal carries."""
    parts = [f"{e.id} ({e.date or 'undated'}): {e.words}" for e in originals if e.words]
    return " | ".join(parts) or None


def load_seed_files(topics_path: str, merges_path: str) -> tuple[Any, list[Any], Any]:
    """The two JSON files of the seeding: topics {"D-001": ["topic", ...], ...} and merges
    {"merges": [{"title", "summary", "topics", "supersedes", "scope"?, "interpretation"?}, ...],
    "keep": ["D-094", ...]} -> (topics, merges, keep)."""
    def load(path: str, what: str) -> Any:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError) as e:
            raise SeedRefused(f"can't read the {what} file {path}: {e}") from e
    topics = load(topics_path, "topics")
    merges = load(merges_path, "merges")
    if not isinstance(merges, dict) or set(merges) - {"merges", "keep"}:
        raise SeedRefused(f"the merges file {merges_path} must be a JSON object with only 'merges' and 'keep'")
    ms = merges.get("merges", [])
    if not isinstance(ms, list):
        raise SeedRefused(f"the merges file {merges_path}: 'merges' must be a list")
    return topics, ms, merges.get("keep", [])


def _topic_list(value: Any, who: str, problems: list[str]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        problems.append(f"{who}: topics must be a list of 1-{TOPICS_MAX} strings")
        return []
    try:
        out = list(dict.fromkeys(store.decision_topic(t) for t in value))
    except ValueError as e:
        problems.append(f"{who}: {e}")
        return []
    if not 1 <= len(out) <= TOPICS_MAX:
        problems.append(f"{who}: {len(out)} topics, needs 1-{TOPICS_MAX} (D-284)")
        return []
    return out


def seed_plan(conn: sqlite3.Connection, path: str, topics: Any, merges: list[Any], keep: Any) -> SeedPlan:
    """Check every precondition of the seeding and work out what it does; reads only. Refused
    (SeedRefused, all problems listed) when: the DB doesn't mirror the file (run the import first); a
    surviving entry has no topics or more than TOPICS_MAX; a done entry whose Status names a part that
    stays in force is in no merge and not in `keep`; a merge or keep names an id that isn't there, is
    superseded, or is used twice."""
    try:
        text, sha = read_source(path)
    except (OSError, UnicodeDecodeError) as err:
        raise SeedRefused(f"can't read {path}: {err}") from err
    entries = parse(text)
    check_entries(entries, path)
    by_id = {e.id: e for e in entries}
    rows = {r["id"]: r for r in store.list_decision_entries(conn) if r["origin"] == "file"}
    clash = sorted(r[0] for r in conn.execute("SELECT id FROM decisions WHERE origin = 'db'") if r[0] in by_id)
    if clash:
        raise IdCollision(f"{path}: {', '.join(clash)} is in the file and is also a native entry")
    new = [i for i in by_id if i not in rows]
    gone = [i for i in rows if i not in by_id]
    stale = [i for i, e in by_id.items() if i in rows and (
        rows[i]["body_sha256"] != store.sha256_text(e.body) or rows[i]["state"] != e.row()["state"]
        or rows[i]["keywords"] != e.keywords)]
    if new or gone or stale:
        raise SeedRefused("the DB does not mirror the file: run `python3 tasks.py decision import` first "
                          f"(in the file only: {', '.join(new[:10]) or '-'}; in the DB only: "
                          f"{', '.join(gone[:10]) or '-'}; differing: {', '.join(stale[:10]) or '-'})")
    problems: list[str] = []
    classes: dict[str, str] = {}
    for e in entries:
        st = e.row()["state"]
        classes[e.id] = ("expired" if expired_status(e.status) else "done") if st == "done" else st
    used: set[str] = set()
    ms: list[Merge] = []
    allowed = {"title", "summary", "topics", "supersedes", "scope", "interpretation"}
    for n, m in enumerate(merges, 1):
        who = f"merge {n}"
        if not isinstance(m, dict) or set(m) - allowed or not {"title", "summary", "topics", "supersedes"} <= set(m):
            problems.append(f"{who}: an object with title, summary, topics, supersedes (and scope, "
                            "interpretation at most)")
            continue
        who = f"merge {n} ({str(m['title'])[:40]})"
        sup = m["supersedes"]
        if not isinstance(sup, list) or not sup or not all(isinstance(i, str) for i in sup):
            problems.append(f"{who}: supersedes must be a list of entry ids")
            continue
        for i in sup:
            if i not in by_id:
                problems.append(f"{who}: {i} is not in the file")
            elif classes[i] == "superseded":
                problems.append(f"{who}: {i} is superseded already")
            elif i in used:
                problems.append(f"{who}: {i} is in another merge")
        used.update(sup)
        try:
            title = store._text(m["title"], "title", required=True)
            summary = store._text(m["summary"], "summary", required=True)
            scope = store._text(m.get("scope"), "scope")
            interp = store._text(m.get("interpretation"), "interpretation")
        except ValueError as err:
            problems.append(f"{who}: {err}")
            continue
        tops = _topic_list(m["topics"], who, problems)
        ms.append(Merge(title, summary, tops, [i for i in sup if i in by_id],
                        merge_words([by_id[i] for i in sup if i in by_id]), scope, interp))
    kept: list[str] = []
    if not isinstance(keep, list) or not all(isinstance(i, str) for i in keep):
        problems.append("keep: a list of entry ids")
    else:
        for i in dict.fromkeys(keep):
            if i not in by_id:
                problems.append(f"keep {i}: not in the file")
            elif classes[i] in ("active", "partial"):
                problems.append(f"keep {i}: it is {classes[i]}, it stays anyway")
            elif i in used:
                problems.append(f"keep {i}: it is in a merge")
            else:
                kept.append(i)
    covered = used | set(kept)
    for e in entries:
        if classes[e.id] == "done" and e.id not in covered and ACTIVE_PART_RE.search(
                ACTIVE_UNTIL_RE.sub("", e.status or "")):
            problems.append(f"{e.id} is done but its Status names a part that stays ({e.status!r}): cover it "
                            "with a merge or list it under keep")
    survive = [e.id for e in entries if classes[e.id] in ("active", "partial") or e.id in covered]
    if not isinstance(topics, dict):
        problems.append("topics: an object {id: [topics]}")
        topics = {}
    tmap: dict[str, list[str]] = {}
    for i in survive:
        if i not in topics:
            problems.append(f"{i} has no topics (D-284: 1-{TOPICS_MAX} per surviving entry)")
        else:
            tmap[i] = _topic_list(topics[i], i, problems)
    for i in topics:
        if i not in survive:
            problems.append(f"topics for {i}, which does not survive the seeding")
    if problems:
        raise SeedRefused(f"the seeding is refused, nothing was written ({len(problems)} problems):\n  - "
                          + "\n  - ".join(problems))
    top = max((int(e.id[2:]) for e in entries if e.id.startswith("D-")), default=0)
    return SeedPlan(path, sha, entries, classes, survive, [e.id for e in entries if e.id not in survive],
                    kept, tmap, ms, split_blocks(text), top)


def source_of(conn: sqlite3.Connection) -> str:
    """'db' once the seeding ran (the DB is the only source, the file is no longer imported), else 'file'."""
    return "db" if store.get_meta(conn, SOURCE_META) == "db" else "file"


def seed(topics_path: Optional[str], merges_path: Optional[str], *, db_path: Optional[str] = None,
         path: Optional[str] = None, dry_run: bool = False, undo: bool = False) -> dict[str, Any]:
    """The seeding (action decision.seed, CLI only): checks everything first (a dry run of the action), then
    backs the database up (<db>.preseed-<utc>.bak, store._backup_copy) and runs it in one transaction.
    undo: restores (decision.seed with undo). -> the action's result (counts per class against the file's)."""
    import actions
    params: dict[str, Any] = {"dry_run": True}
    if undo:
        params["undo"] = True
    else:
        if not topics_path or not merges_path:
            raise SeedRefused("seed needs --topics FILE and --merges FILE")
        topics, merges, keep = load_seed_files(topics_path, merges_path)
        params.update(path=os.path.abspath(path or DEFAULT_SOURCE), topics=topics, merges=merges, keep=keep)
    conn = store.connect(db_path)
    try:
        report: dict[str, Any] = actions.perform(conn, "decision.seed", params, actor="cli", via="cli")
        if dry_run:
            return report
        params["dry_run"] = False
        if not undo:
            db_file = store._db_file(conn)
            if db_file is None:
                raise SeedRefused("the database is not a file: no backup can be made")
            params["backup"] = store._backup_copy(conn, db_file, "preseed")
        result: dict[str, Any] = actions.perform(conn, "decision.seed", params, actor="cli", via="cli")
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------- import

def import_file(path: Optional[str] = None, db_path: Optional[str] = None, actor: str = IMPORT_ACTOR,
                via: str = "runner", force: bool = False, conn: Optional[sqlite3.Connection] = None,
                alert: Optional[Callable[[str, str], None]] = None) -> dict[str, Any]:
    """Mirror the source file into the decisions table (actions.decision_import, audited).
    -> {"file", "sha256", "entries", "added", "changed", "removed", "unchanged"}. After the seeding (Q2) it
    imports nothing: -> {"retired": True, "file", "sha256", "seed_sha256", "differs"}, and a file that is not the
    one the DB was seeded from alerts the owner once per episode (_file_changed)."""
    import actions
    own = conn is None
    c = conn if conn is not None else store.connect(db_path)
    try:
        r: dict[str, Any] = actions.perform(c, "decision.import",
                                            {"path": os.path.abspath(path or DEFAULT_SOURCE), "force": force},
                                            actor=actor, via=via)
        if r.get("retired"):
            _file_changed(c, r, alert)
        return r
    finally:
        if own:
            c.close()


def _file_changed(conn: sqlite3.Connection, r: dict[str, Any], alert: Optional[Callable[[str, str], None]]) -> None:
    """After the cut-over the file is not read any more; one whose sha differs from the seeding's means
    somebody wrote a decision into it. Alert once per episode (CHANGED_MARK; it ends when the file is the
    seeded one again): the DB has the decisions, record it with `tasks.py decision add`."""
    mark = store.get_meta(conn, CHANGED_MARK)
    if not r["differs"]:
        if mark:
            with store.transaction(conn):
                store.set_meta(conn, CHANGED_MARK, "")
        return
    if not mark:
        (alert or _alert)("DECISIONS.md changed after the cut-over",
                          f"{r['file']} is not the file the decision store was seeded from (the DB is the only "
                          "source now, the file is not imported any more). DECISIONS.md changed after the "
                          "cut-over; record it with `python3 tasks.py decision add`.")
        with store.transaction(conn):
            store.set_meta(conn, CHANGED_MARK, r["sha256"])


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
    if source_of(conn) == "db":
        return None                                  # after the seeding the DB is the only source (Q2)
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
        what = f"[AFClaude decisions TLDR (D-242): could NOT be built ({type(e).__name__}: {e}). Tell the owner. "
        if _source_kind(db_path) == "file":
            text = what + (f"Until it is fixed, read {source_path or DEFAULT_SOURCE} directly before design or "
                           "planning work.]")
        else:        # the DB is the only source (after the seeding; or it can't even be read): no file to read
            text = what + ("Until it is fixed, look decisions up with `python3 tasks.py decision find "
                           "KEYWORD...` before design or planning work (the DB holds them; DECISIONS.md is not "
                           "read any more).]")
        sys.stderr.write(f"decisions hook: {type(e).__name__}: {e}\n")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}


def _source_kind(db_path: Optional[str]) -> Optional[str]:
    """The decision source ('file' | 'db'), or None when the DB can't be read (the hook's failure text)."""
    try:
        conn = store.connect(db_path)
        try:
            return source_of(conn)
        finally:
            conn.close()
    except (store.DBError, sqlite3.Error, OSError):
        return None


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
        r = import_file(path, db_path, alert=alert)
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
    sd = sub.add_parser("seed", help="the one-time cut-over: the DB becomes the only source (Q2); see docs/phase4_plan.md")
    sd.add_argument("--topics", default=None, help="JSON file: {\"D-001\": [\"topic\", ...], ...} for every surviving entry")
    sd.add_argument("--merges", default=None, help="JSON file: {\"merges\": [...], \"keep\": [...]}")
    sd.add_argument("--file", default=None, help=f"the source (default {DEFAULT_SOURCE})")
    sd.add_argument("--dry-run", action="store_true", help="check and count, write nothing")
    sd.add_argument("--undo", action="store_true", help="restore the file as the source (refused after an edit)")
    args = ap.parse_args(argv)
    if args.cmd == "hook":
        if not 1 <= args.part <= args.of:
            ap.error("--part must be 1..--of")
        return print_hook(args.part, args.of, args.file)
    try:
        if args.cmd == "seed":
            print(json.dumps(seed(args.topics, args.merges, db_path=args.db, path=args.file,
                                  dry_run=args.dry_run, undo=args.undo), indent=1, ensure_ascii=False))
            return 0
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
