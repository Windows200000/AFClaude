#!/usr/bin/env python3
"""The one-time import of OPEN_QUESTIONS.md into the DB (phase 4b Q3 W3, docs/phase4_plan.md "Q3: questions and
proposals"; D-102, D-151, D-181, D-235, D-263, D-279), mypy --strict.

    python3 questions.py import [--file P] [--dry-run] [--project NAME]

After it the DB is the only question list (meta questions_source = db): each item of the file is a question
for the checkout's project (title = the heading without its `Q-...:` label, text = the item verbatim), and
every proposal and every unconfirmed task-manager entry has its decision question (actions.
ensure_decision_questions). The file is read, never written (D-181), and only here: since phase 4b Q5 nothing
else reads it (the quickview, the 10-min file check and the undo are gone; the file stays on disk for the owner
to archive). This module parses and checks (plan_import) and drives the CLI (backup, dry run); the writes are
the action `question.import` (actions.py: CLI only, audited, one transaction). The item parser
(parse_open_questions) is here: `## ` sections and top-level `- ` items, fence-aware."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import store  # noqa: E402

SOURCE_META = "questions_source"                       # 'db' once the import ran; absent or 'file' before
FILE_SHA_META = "questions_file_sha"                   # the file's sha256 at the import
IMPORTED_META = "questions_imported"                   # JSON: the question ids the import created
DEFAULT_SOURCE = os.path.join(HERE, "OPEN_QUESTIONS.md")
IMPORT_VIA = "cli"
TITLE_MAX = 120                                        # as store.ask_question cuts a title it makes itself


class ImportRefused(ValueError):
    """The import can't be done; nothing was written. The message lists every reason."""


@dataclass
class Item:
    style: str          # "-" (a top-level '- ' item) or "##" (a '## ' section)
    title: str          # the heading without its Q-...: label (D-263)
    text: str           # the item verbatim (D-279)

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class Plan:
    file: str
    sha: str
    project: str
    items: list[Item]


def source_of(conn: sqlite3.Connection) -> str:
    """Where the quickview and the questions come from: "db" once the import ran, else "file"."""
    return "db" if store.get_meta(conn, SOURCE_META) == "db" else "file"


def read_source(path: str) -> tuple[str, str]:
    """(text, sha256) of the file; a missing or unreadable one raises (OSError, UnicodeDecodeError)."""
    with open(path, "rb") as fh:
        data = fh.read()
    return data.decode("utf-8"), hashlib.sha256(data).hexdigest()


_LABEL_RE = re.compile(r"^\s*Q-[\w.-]*\s*:\s*")


def item_title(first: str) -> str:
    """The heading of an item as a question title: its markdown bold and its `Q-...:` label dropped, cut at
    TITLE_MAX characters."""
    t = first.strip()
    if t.startswith("**"):
        t = t[2:]
        if t.rstrip().endswith("**"):
            t = t.rstrip()[:-2]
    t = _LABEL_RE.sub("", t).strip()
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX - 1] + "…"


def _preamble_problems(lines: list[str]) -> list[str]:
    """The text before the first item is no question, but only a title ('# ...'), comments and blank lines
    may stand there: any other text would be lost by the import."""
    out: list[str] = []
    comment = False
    for n, line in enumerate(lines, 1):
        s = line.strip()
        if comment:
            comment = "-->" not in s
        elif not s or re.match(r"^#\s+\S", s):
            continue
        elif s.startswith("<!--"):
            comment = "-->" not in s
        else:
            out.append(f"line {n} stands before the first item and would be lost: {s[:60]!r}")
    return out


# the item parser (moved here from export_quickview, which no longer reads the file, phase 4b Q5)

_MD_FILE_RE = re.compile(r"\b[\w./-]*\.md\b")
_MD_POINTER_WORDS_RE = re.compile(r"\b(see|cf\.?|details?( in)?|refer to|in)\b", re.IGNORECASE)


def parse_open_questions(oq_md: str) -> list[tuple[str, list[str]]]:
    """The items of OPEN_QUESTIONS.md as (style, lines): style "-" for a top-level '- ' item, "##" for a
    '## ' section (today's format; the heading is the first line). `lines` is the list of raw lines, the first
    with its '- ' / '## ' stripped. A line is part of the current item unless it starts a new one: a '-'
    with no leading indentation (not inside a '## ' section, whose own '- ' lines are its body) or a '## '
    heading. Indented continuation lines and nested numbered/bulleted sub-items (D-279: a multi-line item
    must not collapse to its first line) stay with the item, and so does a fenced code block (``` or ~~~).
    Text before the first item (title, blank, comments) is no question."""
    items: list[tuple[str, list[str]]] = []
    cur: Optional[list[str]] = None
    style = ""
    fence: Optional[str] = None
    for line in oq_md.splitlines():
        m = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence is not None or m:     # a fenced code block is body text: its '## ' and '- ' lines start no item
            if fence is None:
                assert m is not None
                fence = m.group(1)[0] * 3
            elif re.match(r"^\s*" + re.escape(fence[0]) + "{3,}\\s*$", line):
                fence = None
            if cur is not None:
                cur.append(line.rstrip())
            continue
        if re.match(r"^##\s+\S", line):
            if cur is not None:
                items.append((style, cur))
            cur, style = [re.sub(r"^##\s+", "", line).rstrip()], "##"
        elif style != "##" and re.match(r"^-\s+\S", line):
            if cur is not None:
                items.append((style, cur))
            cur, style = [re.sub(r"^-\s+", "", line).rstrip()], "-"
        elif cur is not None:
            cur.append(line.rstrip())
    if cur is not None:
        items.append((style, cur))
    return items


def _open_question_warning(lines: list[str]) -> Optional[str]:
    """D-279 safety net: flag an item whose only real content is a pointer to a .md file (the
    original bug - a header naming a doc with the actual questions left out of OPEN_QUESTIONS.md
    entirely) so it's caught instead of an empty-looking question being imported."""
    full = " ".join(l.strip() for l in lines if l.strip())
    if not _MD_FILE_RE.search(full):
        return None
    rest = _MD_FILE_RE.sub("", full)
    rest = _MD_POINTER_WORDS_RE.sub("", rest)
    rest = re.sub(r"[\s:.,()*_\-]+", "", rest)
    if len(rest) < 20:
        return "points mainly to a file instead of stating the question here"
    return None


def parse_items(text: str) -> tuple[list[Item], list[str]]:
    """(items, problems) of the file's text. problems: text before the first item, an item without a
    title, and an item that only points to a .md file (D-279). No item at all is a problem too."""
    raw = parse_open_questions(text)
    lines = text.splitlines()
    kept = sum(len(part) for _style, part in raw)
    problems = _preamble_problems(lines[:len(lines) - kept])
    items: list[Item] = []
    for n, (style, part) in enumerate(raw, 1):
        body = list(part)
        while body and not body[-1].strip():
            body.pop()
        item = Item(style, item_title(body[0]), "\n".join(body))
        if not item.title:
            problems.append(f"item {n} ({body[0][:40]!r}) has no title after its label")
        warn = _open_question_warning(part)         # D-279's pointer check
        if warn:
            problems.append(f"item {n} ({item.title[:40]!r}) {warn}")
        items.append(item)
    if not raw:
        problems.append("the file has no item (a top-level '- ' item or a '## ' section)")
    return items, problems


def plan_import(conn: sqlite3.Connection, path: str, project: Optional[str] = None) -> Plan:
    """The checks before any write. ImportRefused with every problem when: the source is not the file any
    more, the file can't be read, text would be left over, an item is empty, the project is unknown.
    -> the items, the file's sha and the project the questions belong to."""
    import actions
    problems: list[str] = []
    if source_of(conn) == "db":
        raise ImportRefused("the questions are imported already (meta questions_source = db)")
    p = store.get_project(conn, project, nearest=False) if project is not None \
        else store.get_project(conn, actions.HERE, nearest=False)
    if p is None:
        problems.append(f"no project {project!r}" if project is not None else
                        "this checkout is no project (`tasks.py project add`, or name one with --project)")
    items: list[Item] = []
    sha = ""
    try:
        text, sha = read_source(path)
        items, found = parse_items(text)
        problems += found
    except (OSError, UnicodeDecodeError) as e:
        problems.append(f"can't read {path}: {e}")
    if problems:
        raise ImportRefused(f"the import is refused, nothing was written ({len(problems)} problems):\n  - "
                            + "\n  - ".join(problems))
    assert p is not None
    return Plan(path, sha, str(p["name"]), items)


def import_questions(db_path: Optional[str] = None, path: Optional[str] = None, dry_run: bool = False,
                     project: Optional[str] = None) -> dict[str, Any]:
    """The CLI's import: the checks and counts as a dry run (always, first), then the backup
    (<db>.preqimport-<utc>.bak) and the action question.import in one transaction. -> the action's result."""
    import actions
    params: dict[str, Any] = {"dry_run": True, "path": os.path.abspath(path or DEFAULT_SOURCE), "project": project}
    conn = store.connect(db_path)
    try:
        report: dict[str, Any] = actions.perform(conn, "question.import", params, actor="cli", via=IMPORT_VIA)
        if dry_run:
            return report
        params["dry_run"] = False
        db_file = store._db_file(conn)
        if db_file is None:
            raise ImportRefused("the database is not a file: no backup can be made")
        params["backup"] = store._backup_copy(conn, db_file, "preqimport")
        result: dict[str, Any] = actions.perform(conn, "question.import", params, actor="cli", via=IMPORT_VIA)
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AFClaude question import (phase 4b Q3; docs/phase4_plan.md).")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("import", help="the one-time import of OPEN_QUESTIONS.md; the DB becomes the question list")
    i.add_argument("--file", default=None, help=f"the source (default {DEFAULT_SOURCE})")
    i.add_argument("--project", default=None, help="the project the questions belong to (default: this checkout's)")
    i.add_argument("--dry-run", action="store_true", help="check and count, write nothing")
    args = ap.parse_args(argv)
    try:
        print(json.dumps(import_questions(args.db, args.file, args.dry_run, args.project),
                         indent=1, ensure_ascii=False))
        return 0
    except (store.DBError, sqlite3.Error) as err:
        print(f"error: database: {err}", file=sys.stderr)
        return 1
    except (OSError, ValueError, LookupError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
