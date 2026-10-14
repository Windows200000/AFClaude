#!/usr/bin/env python3
"""The change manifest (D-243): an objective overview of what an agent's branch changed, for the
task-manager's review before a merge. Generated mechanically from git, the Python ASTs and the DB,
never from the agent's own description.

    python3 change_manifest.py <base>..<head> [--since ISO [--until ISO]] [--snapshot FILE | --compare FILE]
                               [--mapping FILE] [--decisions PATH] [--db PATH] [--live-hunks] [--budget]
                               [--sections LIST]

Sections (plain text, each headed with its approximate token count):
  1  manifest: per file +/- lines; Python functions/classes/constants added, removed or changed
     (signature, body, docstring; compared on the base and head ASTs, with file:line); imports;
     the interfaces (DB schema and migrations, settings keys, CLI subcommands and options, MCP
     tools, audited actions, cron/compose, .claude settings/hooks/agents), each listed even when
     empty so "none" means checked. Test files are left out (owner, D-243: one count line only);
     counts-only files (COUNTS_ONLY) get their name and counts.
  2  the full diff of docs, prompts and *.md (a word diff), .claude/, docker/ (non-Python), mypy.ini
     and settings files (a line diff)
  3a only with --live-hunks (owner, D-243: off by default): the full code hunks of the files the
     live system imports (LIVE_FILES); without it, live code is listed like any other code (1, 3b)
  3b the changed symbols that match a decision's keywords (or cite its id): which D-ids matched
     which symbol, and that symbol's own diff
  4  non-file changes: the audit-log rows of the run (--since/--until), grouped by action; and,
     with --compare, what changed in the containers, images, host crontab, at jobs and the
     scheduled_jobs table since a --snapshot taken before the run
  5  the worker's symbol -> D-id|none mapping (--mapping): changed symbols missing from it,
     entries naming no changed symbol, unknown or superseded D-ids, and symbols whose changed
     lines cite a D-id the mapping doesn't give

--snapshot FILE only saves the snapshot (cheap, read-only: docker ps/inspect/images, crontab -l,
atq, the DB read-only) and prints nothing else. --compare FILE takes a fresh one and diffs it; the
saved snapshot's time is then the default --since. --budget prints only the token count per
section (≈ chars / 3.5); --sections 1,3b,... picks sections.

Decisions come from one function, load_decisions(): today it parses DECISIONS.md (local only,
never committed; each "### D-NNN" entry's "**Keywords:**" and "**Status:**" lines), later the DB
decision store (D-242) can replace the reader behind it. Superseded decisions are not matched.

Any failure (git, a missing DB or decisions file, docker/crontab/atq, a bad mapping line) aborts
with an error on stderr and exit code 2 (D-235); a Python file that doesn't parse is reported in
its section and the run exits 3 after printing.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import fnmatch
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.environ.get("AFCLAUDE_DB", os.path.join(HERE, "data", "afclaude.db"))
DEFAULT_DECISIONS = os.path.join(HERE, "DECISIONS.md")

# Code the live system imports (the worker rules; D-243 3a): full hunks in section 3a.
LIVE_FILES = ("keepalive.py", "store.py", "stalled.py", "usage_sampler.py", "export_quickview.py",
              "ka_resume.sh", "actions.py", "dispatcher.py", "failstop.py")
# Full diff text in section 2 (docs, prompts, settings, infra); Python files are never in it.
FULL_DIFF = ("prompts/*", "docs/*", "*.md", ".claude/*", "docker/*", "quickview/*.yml", "quickview/*.conf",
             "mypy.ini", "*.json", "*.ini", "*.toml", "requirements*.txt", ".gitignore", ".dockerignore",
             ".githooks/*", "*crontab*")
# Of those, shown as a word diff (D-243): prose, where whole changed lines waste tokens.
WORD_DIFF = ("*.md", "docs/*", "prompts/*")
# Generated or bulk files: name and counts only.
COUNTS_ONLY = ("quickview/*.html", "*.min.js", "*.lock", "*.svg", "*.png", "*.jpg")
# Interfaces under .claude/ and the hook dirs.
CLAUDE_DIRS = (".claude/*", "hooks/*", ".githooks/*")
SECTIONS = ("1", "2", "3a", "3b", "4", "5")
CHARS_PER_TOKEN = 3.5
SIG_MAX = 110            # a longer signature is cut with "…"
TARGETS_MAX = 8          # audit targets listed per action, then "+N more"


class ManifestError(RuntimeError):
    """A failure that aborts the manifest (exit 2)."""


def tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def is_test(path: str) -> bool:
    base = os.path.basename(path)
    return base.startswith("test") or base.endswith("_test.py")


def cut(s: str, n: int = SIG_MAX) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


# ---------------------------------------------------------------- git

def git(*args: str, cwd: str | None = None) -> str:
    try:
        p = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ManifestError(f"git {' '.join(args)}: {e}") from e
    if p.returncode != 0:
        raise ManifestError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def resolve_range(spec: str, cwd: str | None = None) -> tuple[str, str]:
    """'base..head' or 'base...head' (from their merge base) -> two commit ids."""
    if "..." in spec:
        a, b = spec.split("...", 1)
        head = git("rev-parse", "--verify", (b or "HEAD") + "^{commit}", cwd=cwd).strip()
        base = git("merge-base", a, head, cwd=cwd).strip()
        return base, head
    if ".." not in spec:
        raise ManifestError(f"range {spec!r}: expected <base>..<head>")
    a, b = spec.split("..", 1)
    base = git("rev-parse", "--verify", a + "^{commit}", cwd=cwd).strip()
    head = git("rev-parse", "--verify", (b or "HEAD") + "^{commit}", cwd=cwd).strip()
    return base, head


@dataclass
class FileChange:
    status: str          # A | M | D | R
    path: str            # head path (base path for D)
    old: str             # base path (== path unless renamed)
    added: int | None    # None = binary
    removed: int | None


def changed_files(base: str, head: str, cwd: str | None = None) -> list[FileChange]:
    names = git("diff", "--name-status", "-z", "-M", base, head, cwd=cwd).split("\0")
    stats = git("diff", "--numstat", "-z", "-M", base, head, cwd=cwd).split("\0")
    counts: dict[str, tuple[int | None, int | None]] = {}
    i = 0
    while i < len(stats) and stats[i]:
        a, r, p = stats[i].split("\t", 2)
        if p == "":                  # a rename: old and new path follow
            p = stats[i + 2]
            i += 3
        else:
            i += 1
        counts[p] = (None if a == "-" else int(a), None if r == "-" else int(r))
    out: list[FileChange] = []
    i = 0
    while i < len(names) and names[i]:
        st = names[i]
        if st[0] in "RC":
            old, new = names[i + 1], names[i + 2]
            i += 3
        else:
            old = new = names[i + 1]
            i += 2
        plus, minus = counts.get(new, (0, 0))
        out.append(FileChange(st[0], new, old, plus, minus))
    return out


def show(rev: str, path: str, cwd: str | None = None) -> str:
    return git("show", f"{rev}:{path}", cwd=cwd)


WORD_RE = re.compile(r"(\[-.*?-\]|\{\+.*?\+\})")
WORD_KEEP = 8            # word diff: unchanged words kept on each side of a change


def trim_words(line: str, keep: int = WORD_KEEP) -> str:
    """A word-diff line with its long unchanged stretches cut to `keep` words next to each change
    ('…' marks the cut): a one-word edit in a 2 KB README row stays one short line."""
    parts = WORD_RE.split(line)
    if len(parts) == 1:
        return line
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(part)
            continue
        core = part.strip()
        lead, trail = part[:len(part) - len(part.lstrip())], part[len(part.rstrip()):]
        words = core.split()
        first, last = i == 0, i == len(parts) - 1
        if len(words) <= (keep if first or last else 2 * keep):
            out.append(part)
        elif first:
            out.append(lead + "… " + " ".join(words[-keep:]) + trail)
        elif last:
            out.append(lead + " ".join(words[:keep]) + " …" + trail)
        else:
            out.append(lead + " ".join(words[:keep]) + " … " + " ".join(words[-keep:]) + trail)
    return "".join(out)


def file_diff(base: str, head: str, paths: Sequence[str], cwd: str | None = None, words: bool = False) -> str:
    """The unified diff of `paths`, with the git header lines cut down to one '=== path' line.
    words=True (docs, D-243): a word diff without context lines, [-removed-]{+added+} inside the
    changed lines, long unchanged stretches cut (trim_words)."""
    if not paths:
        return ""
    opts = ["-U0", "--word-diff=plain"] if words else ["-U3"]
    raw = git("diff", *opts, "-M", base, head, "--", *paths, cwd=cwd)
    out: list[str] = []
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            out.append("=== " + line.split(" b/", 1)[-1])
        elif line.startswith(("index ", "--- ", "+++ ", "similarity index", "rename from", "rename to",
                              "new file mode", "deleted file mode", "old mode", "new mode")):
            if line.startswith(("rename from", "new file mode", "deleted file mode")):
                out.append("  (" + line + ")")
        elif words and line.startswith("@@"):
            out.append(line.split(" @@", 1)[0] + " @@")
        else:
            out.append(trim_words(line) if words else line)
    return "\n".join(out)


# ---------------------------------------------------------------- Python symbols (AST)

@dataclass
class Sym:
    qual: str
    kind: str            # def | class | const
    start: int
    end: int
    sig: str             # for display
    sig_key: str
    body_key: str
    doc: str | None
    src: list[str]
    node: ast.AST | None = None


@dataclass
class PyFile:
    syms: dict[str, Sym] = field(default_factory=dict)
    imports: set[str] = field(default_factory=set)
    module_key: str = ""         # the other module-level statements
    tree: ast.Module | None = None
    error: str | None = None


def _split_doc(body: list[ast.stmt]) -> tuple[str | None, list[ast.stmt]]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[0].value.value, body[1:]
    return None, body


def _dump(nodes: Iterable[ast.AST | None]) -> str:
    return "\n".join(ast.dump(n) if n is not None else "-" for n in nodes)


def parse_py(text: str) -> PyFile:
    pf = PyFile()
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        pf.error = f"line {e.lineno}: {e.msg}"
        return pf
    pf.tree = tree
    lines = text.splitlines()
    other: list[ast.stmt] = []

    def add(sym: Sym) -> None:
        q, n = sym.qual, 2
        while q in pf.syms:          # @overload stubs, conditional redefinitions
            q = f"{sym.qual}#{n}"
            n += 1
        sym.qual = q
        pf.syms[q] = sym

    def src(node: ast.AST, start: int) -> list[str]:
        end = getattr(node, "end_lineno", None) or start
        return lines[start - 1:end]

    def visit(body: Sequence[ast.stmt], prefix: str, top: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                doc, rest = _split_doc(node.body)
                sig = f"{node.name}({ast.unparse(node.args)})"
                if node.returns is not None:
                    sig += f" -> {ast.unparse(node.returns)}"
                add(Sym(prefix + node.name, "def", start, node.end_lineno or start,
                        ("async " if isinstance(node, ast.AsyncFunctionDef) else "") + sig,
                        type(node).__name__ + _dump([node.args, node.returns, *node.decorator_list]),
                        _dump(rest), doc, src(node, start), node))
            elif isinstance(node, ast.ClassDef):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                doc, rest = _split_doc(node.body)
                bases = ", ".join(ast.unparse(b) for b in [*node.bases, *node.keywords])
                own = [s for s in rest if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                add(Sym(prefix + node.name, "class", start, node.end_lineno or start,
                        f"class {node.name}" + (f"({bases})" if bases else ""),
                        _dump([*node.bases, *node.keywords, *node.decorator_list]), _dump(own), doc,
                        src(node, start), node))
                visit(rest, f"{prefix}{node.name}.", False)
            elif top and isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [_target_name(t) for t in targets]
                if all(n is not None for n in names) and node.value is not None:
                    key = _dump([node.value, getattr(node, "annotation", None)])
                    for n in names:
                        assert n is not None
                        add(Sym(n, "const", node.lineno, node.end_lineno or node.lineno, n, "", key, None,
                                src(node, node.lineno), node))
                else:
                    other.append(node)
            elif top and isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if isinstance(node, ast.ImportFrom):
                        pf.imports.add(f"{'.' * node.level}{node.module or ''}.{a.name}")
                    else:
                        pf.imports.add(a.name)
            elif top and isinstance(node, (ast.If, ast.Try, ast.With)):
                # a module-level `if`/`try` may hold defs and imports (fallbacks, __main__)
                inner: list[ast.stmt] = []
                for part in ("body", "orelse", "finalbody"):
                    inner += getattr(node, part, [])
                for h in getattr(node, "handlers", []):
                    inner += h.body
                defs = [s for s in inner if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                                           ast.Import, ast.ImportFrom))]
                visit(defs, prefix, True)
                other.append(node)
            elif top:
                other.append(node)
    visit(tree.body, "", True)
    pf.module_key = _dump(other)
    return pf


def _target_name(t: ast.expr) -> str | None:
    if isinstance(t, ast.Name):
        return t.id
    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):   # MIGRATIONS[5] = ...
        return f"{t.value.id}[{ast.unparse(t.slice)}]"
    return None


@dataclass
class SymChange:
    path: str
    kind: str            # def | class | const
    op: str              # + | - | ~ | >  (> = renamed)
    qual: str
    head: Sym | None
    base: Sym | None
    tags: list[str] = field(default_factory=list)
    plus: int = 0
    minus: int = 0
    changed_lines: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.path}:{self.qual}"


def _line_diff(a: list[str], b: list[str]) -> tuple[int, int, list[str]]:
    plus = minus = 0
    changed: list[str] = []
    for line in difflib.unified_diff(a, b, lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            plus += 1
            changed.append(line)
        elif line.startswith("-"):
            minus += 1
            changed.append(line)
    return plus, minus, changed


HUNK_RE = re.compile(r"^@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@")


def symbol_diff(base: Sym, head: Sym, context: int = 2) -> list[str]:
    """The unified diff of one symbol's source, with file line numbers in the @@ headers."""
    out: list[str] = []
    for ln in difflib.unified_diff(base.src, head.src, lineterm="", n=context):
        if ln.startswith(("---", "+++")):
            continue
        m = HUNK_RE.match(ln)
        if m:
            ln = (f"@@ -{int(m.group(1)) + base.start - 1}{m.group(2) or ''} "
                  f"+{int(m.group(3)) + head.start - 1}{m.group(4) or ''} @@")
        out.append(ln)
    return out


def compare_py(path: str, base: PyFile | None, head: PyFile | None) -> list[SymChange]:
    b = base.syms if base else {}
    h = head.syms if head else {}
    out: list[SymChange] = []
    for q, hs in h.items():
        bs = b.get(q)
        if bs is None:
            out.append(SymChange(path, hs.kind, "+", q, hs, None, plus=len(hs.src), changed_lines=hs.src))
            continue
        if bs.kind != hs.kind:
            out.append(SymChange(path, bs.kind, "-", q, None, bs, minus=len(bs.src), changed_lines=bs.src))
            out.append(SymChange(path, hs.kind, "+", q, hs, None, plus=len(hs.src), changed_lines=hs.src))
            continue
        tags = []
        if bs.sig_key != hs.sig_key:
            tags.append("sig")
        if bs.body_key != hs.body_key:
            tags.append("body" if hs.kind != "const" else "value")
        if bs.doc != hs.doc:
            tags.append("doc")
        if tags:
            p, m, ch = _line_diff(bs.src, hs.src)
            out.append(SymChange(path, hs.kind, "~", q, hs, bs, tags, p, m, ch))
    for q, bs in b.items():
        if q not in h:
            out.append(SymChange(path, bs.kind, "-", q, None, bs, minus=len(bs.src), changed_lines=bs.src))
    # a removed + an added symbol with the same signature/body (name aside) is a rename
    added = [c for c in out if c.op == "+" and c.kind == "def"]
    for r in [c for c in out if c.op == "-" and c.kind == "def"]:
        assert r.base is not None
        for a in added:
            assert a.head is not None
            if a.head.body_key == r.base.body_key and \
                    a.head.sig_key == r.base.sig_key and a in out and r in out:
                out.remove(r)
                a.op, a.base, a.tags = ">", r.base, [f"renamed from {r.qual}"]
                a.plus, a.minus, a.changed_lines = 0, 0, [f"-{r.qual}", f"+{a.qual}"]
                break
    return out


# ---------------------------------------------------------------- interfaces

DDL_RE = re.compile(r"CREATE\s+(UNIQUE\s+)?(TABLE|INDEX|TRIGGER|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w{}]+)",
                    re.I)
ALTER_RE = re.compile(r"\b(ALTER\s+TABLE|DROP\s+(?:TABLE|INDEX|TRIGGER|VIEW))\b[^;]*", re.I)
TOUCH_RE = re.compile(r"\b(?:FROM|INTO|UPDATE|TABLE|JOIN)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?([A-Za-z_]\w*)", re.I)
CONSTRAINT_WORDS = {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}


def _strings(tree: ast.AST | None) -> list[str]:
    if tree is None:
        return []
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _strip_sql_comments(s: str) -> str:
    return re.sub(r"--[^\n]*", "", s)


def _table_columns(stmt: str) -> dict[str, str]:
    """'CREATE TABLE x (a INT, b TEXT, PRIMARY KEY (a))' -> {'a': 'INT', 'b': 'TEXT'}."""
    i = stmt.find("(")
    if i < 0:
        return {}
    depth, items, cur = 0, [], ""
    for ch in stmt[i + 1:]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            items.append(cur)
            cur = ""
        else:
            cur += ch
    items.append(cur)
    cols: dict[str, str] = {}
    for it in items:
        words = it.split()
        if words and words[0].upper() not in CONSTRAINT_WORDS:
            cols[words[0]] = " ".join(words[1:])
    return cols


def ddl_objects(tree: ast.AST | None) -> dict[str, str]:
    """{'table tasks': normalised statement, 'index x': ..., 'trigger y': ...} from every string
    constant of a module (DDL in SCHEMA strings, f-string fragments included)."""
    out: dict[str, str] = {}
    for s in _strings(tree):
        s = _strip_sql_comments(s)
        for m in DDL_RE.finditer(s):
            end = s.find(";", m.end())
            stmt = s[m.start():end if end >= 0 else len(s)]
            out[f"{m.group(2).lower()} {m.group(3)}"] = " ".join(stmt.split())
    return out


def schema_changes(path: str, base: PyFile | None, head: PyFile | None, changes: list[SymChange]) -> list[str]:
    lines: list[str] = []
    bo = ddl_objects(base.tree if base else None)
    ho = ddl_objects(head.tree if head else None)
    for k in sorted(set(bo) | set(ho)):
        if k not in bo:
            cols = _table_columns(ho[k]) if k.startswith("table ") else {}
            lines.append(f"+ {k}" + (f"({', '.join(cols)})" if cols else ""))
        elif k not in ho:
            lines.append(f"- {k}")
        elif bo[k] != ho[k]:
            if k.startswith("table "):
                bc, hc = _table_columns(bo[k]), _table_columns(ho[k])
                parts = [f"+{c} {hc[c]}".strip() for c in hc if c not in bc]
                parts += [f"-{c}" for c in bc if c not in hc]
                parts += [f"~{c}: {bc[c]} -> {hc[c]}" for c in hc if c in bc and bc[c] != hc[c]]
                lines.append(f"~ {k}: " + ("; ".join(parts) or "constraints changed"))
            else:
                lines.append(f"~ {k}")
    bs = {" ".join(m.group(0).split()) for s in _strings(base.tree if base else None)
          for m in ALTER_RE.finditer(_strip_sql_comments(s))}
    hs = {" ".join(m.group(0).split()) for s in _strings(head.tree if head else None)
          for m in ALTER_RE.finditer(_strip_sql_comments(s))}
    lines += [f"+ statement: {cut(s)}" for s in sorted(hs - bs)]
    lines += [f"- statement: {cut(s)}" for s in sorted(bs - hs)]
    by = {c.qual: c for c in changes if c.kind == "const"}
    for name in ("SCHEMA_VERSION",):
        c = by.get(name)
        if c is not None:
            old = ast.unparse(c.base.node.value) if c.base and isinstance(c.base.node, (ast.Assign, ast.AnnAssign)) \
                and c.base.node.value is not None else "-"
            new = ast.unparse(c.head.node.value) if c.head and isinstance(c.head.node, (ast.Assign, ast.AnnAssign)) \
                and c.head.node.value is not None else "-"
            lines.append(f"~ SCHEMA_VERSION {old} -> {new}")
    c = by.get("COLUMNS")
    if c is not None:
        bd = _dict_items(c.base.node if c.base else None)
        hd = _dict_items(c.head.node if c.head else None)
        for t in sorted(set(bd) | set(hd)):
            if bd.get(t) != hd.get(t):
                lines.append(f"~ COLUMNS[{t}]: {cut(bd.get(t, '-'), 60)} -> {cut(hd.get(t, '-'), 60)}")
    for ch in changes:
        if ch.kind == "const" and (ch.qual == "MIGRATIONS" or ch.qual.startswith("MIGRATIONS[")):
            lines.append(f"{ch.op} {ch.qual}")
    for ch in changes:
        if ch.kind == "def" and "migrat" in ch.qual.lower():
            sym = ch.head or ch.base
            assert sym is not None
            touched = sorted({m.group(1) for s in _strings(sym.node) for m in TOUCH_RE.finditer(s)})
            lines.append(f"{ch.op} migration {ch.qual}: touches {', '.join(touched) or '(no table names found)'}")
    return [f"{path}: {x}" for x in lines]


def _dict_items(node: ast.AST | None) -> dict[str, str]:
    """A module-level `NAME = {...}` (annotated or not) -> {key source: value source}."""
    value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
    if not isinstance(value, ast.Dict):
        return {}
    out: dict[str, str] = {}
    for k, v in zip(value.keys, value.values):
        key = k.value if isinstance(k, ast.Constant) else ("**" if k is None else ast.unparse(k))
        out[str(key)] = ast.dump(v) if v is not None else ""
    return out


SETTINGS_DICTS = ("SETTINGS", "SETTING_KEYS")


def settings_changes(path: str, changes: list[SymChange]) -> list[str]:
    out: list[str] = []
    for c in changes:
        if c.kind != "const" or c.qual not in SETTINGS_DICTS:
            continue
        bd = _dict_items(c.base.node if c.base else None)
        hd = _dict_items(c.head.node if c.head else None)
        add = [k for k in hd if k not in bd]
        rem = [k for k in bd if k not in hd]
        chg = [k for k in hd if k in bd and bd[k] != hd[k]]
        if add or rem or chg:
            parts = [f"+{k}" for k in add] + [f"-{k}" for k in rem] + [f"~{k}" for k in chg]
            out.append(f"{path} {c.qual}: " + ", ".join(parts))
    return out


def json_key_changes(path: str, old: str | None, new: str | None) -> list[str]:
    """Top-level (and one level down) key changes of a JSON settings file."""
    def load(t: str | None) -> Any:
        if t is None:
            return {}
        try:
            return json.loads(t)
        except ValueError as e:
            raise ManifestError(f"{path}: not valid JSON ({e})") from e

    a, b = load(old), load(new)
    if not isinstance(a, dict) or not isinstance(b, dict):
        return [f"{path}: changed (not a JSON object)"] if a != b else []
    out: list[str] = []
    for k in sorted(set(a) | set(b)):
        if k not in a:
            out.append(f"+{k}")
        elif k not in b:
            out.append(f"-{k}")
        elif a[k] != b[k]:
            if isinstance(a[k], dict) and isinstance(b[k], dict):
                for kk in sorted(set(a[k]) | set(b[k])):
                    av, bv = a[k].get(kk), b[k].get(kk)
                    if av != bv:
                        if isinstance(av, list) and isinstance(bv, list):
                            plus = [x for x in bv if x not in av]
                            minus = [x for x in av if x not in bv]
                            out.append(f"~{k}.{kk}: " + ", ".join([f"+{json.dumps(x)}" for x in plus]
                                                                   + [f"-{json.dumps(x)}" for x in minus]))
                        else:
                            out.append(f"~{k}.{kk}")
            else:
                out.append(f"~{k}")
    return [f"{path}: " + "; ".join(out)] if out else []


def cli_commands(tree: ast.Module | None) -> dict[str, set[str]]:
    """argparse subcommands and their options: {'project add': {'--rank', 'name'}, '': {...}}.
    Follows `x = parser.add_subparsers()`, `y = x.add_parser("name")`, `y.add_argument(...)`
    in order inside each function (a `for n in ("a", "b")` loop over literals is expanded).
    Parent parsers (add_help=False) are skipped."""
    out: dict[str, set[str]] = {}
    if tree is None:
        return out

    def first_str(call: ast.Call, env: dict[str, str]) -> str | None:
        if not call.args:
            return None
        a = call.args[0]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
        if isinstance(a, ast.Name) and a.id in env:
            return env[a.id]
        return "<dynamic>"

    def walk(body: list[ast.stmt], paths: dict[str, str], env: dict[str, str]) -> None:
        for st in body:
            if isinstance(st, ast.For) and isinstance(st.target, ast.Name) and \
                    isinstance(st.iter, (ast.Tuple, ast.List)) and \
                    all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in st.iter.elts):
                for e in st.iter.elts:
                    assert isinstance(e, ast.Constant)
                    walk(st.body, paths, {**env, st.target.id: str(e.value)})
                continue
            for sub in ("body", "orelse"):
                if isinstance(st, (ast.If, ast.For, ast.While, ast.With, ast.Try)) and getattr(st, sub, None):
                    walk(getattr(st, sub), paths, env)
            call, target = None, None
            if isinstance(st, ast.Assign) and isinstance(st.value, ast.Call) and len(st.targets) == 1 \
                    and isinstance(st.targets[0], ast.Name):
                call, target = st.value, st.targets[0].id
            elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
                call = st.value
            if call is None:
                continue
            if ast.unparse(call.func).endswith("ArgumentParser"):     # a (top) parser; parents skipped
                if target and not any(k.arg == "add_help" for k in call.keywords):
                    paths[target] = ""
                    out.setdefault("", set())
                continue
            if not isinstance(call.func, ast.Attribute):
                continue
            recv = call.func.value
            rname = recv.id if isinstance(recv, ast.Name) else None
            meth = call.func.attr
            if rname is None or rname not in paths:
                continue
            elif meth == "add_subparsers" and target:
                paths[target] = paths[rname]
            elif meth == "add_parser":
                name = first_str(call, env)
                p = f"{paths[rname]} {name}".strip()
                out.setdefault(p, set())
                if target:
                    paths[target] = p
            elif meth == "add_argument":
                opts = [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if opts:
                    out.setdefault(paths[rname], set()).add(max(opts, key=len) if opts[0].startswith("-") else opts[0])
            elif meth in ("add_mutually_exclusive_group", "add_argument_group"):
                if target:
                    paths[target] = paths[rname]

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            walk(node.body, {}, {})
    walk(tree.body, {}, {})
    return out


def cli_changes(path: str, base: PyFile | None, head: PyFile | None) -> list[str]:
    b = cli_commands(base.tree if base else None)
    h = cli_commands(head.tree if head else None)
    out: list[str] = []
    for p in sorted(set(b) | set(h)):
        label = p or "(main)"
        if p not in b:
            out.append(f"+ {label}" + (f" [{' '.join(sorted(h[p]))}]" if h[p] else ""))
        elif p not in h:
            out.append(f"- {label}")
        elif b[p] != h[p]:
            out.append(f"~ {label}: " + " ".join([f"+{o}" for o in sorted(h[p] - b[p])]
                                                  + [f"-{o}" for o in sorted(b[p] - h[p])]))
    return [f"{path}: {x}" for x in out]


def _decorated(tree: ast.Module | None, deco: str) -> dict[str, tuple[str, ast.AST]]:
    """{first arg or function name: (function name, function node)} for every function whose
    decorator is `deco(...)` (a call)."""
    out: dict[str, tuple[str, ast.AST]] = {}
    if tree is None:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for d in node.decorator_list:
            if isinstance(d, ast.Call) and (isinstance(d.func, ast.Name) and d.func.id == deco or
                                            isinstance(d.func, ast.Attribute) and d.func.attr == deco):
                arg = d.args[0] if d.args else None
                name = arg.value if deco == "action" and isinstance(arg, ast.Constant) else node.name
                out[str(name)] = (node.name, node)
    return out


def decorated_changes(path: str, base: PyFile | None, head: PyFile | None, deco: str,
                      sym_changes: list[SymChange]) -> list[str]:
    b = _decorated(base.tree if base else None, deco)
    h = _decorated(head.tree if head else None, deco)
    by = {c.qual: c for c in sym_changes}
    out: list[str] = []
    for k in sorted(set(b) | set(h)):
        if k not in b:
            fn, node = h[k]
            out.append(f"+ {k}" + (f" ({fn})" if fn != k else "") + f" L{getattr(node, 'lineno', '?')}")
        elif k not in h:
            out.append(f"- {k}")
        else:
            fn = h[k][0]
            c = by.get(fn)
            if c is not None and c.op == "~":
                out.append(f"~ {k}" + (f" ({fn})" if fn != k else "") + f" [{','.join(c.tags)}]")
    return [f"{path}: {x}" for x in out]


def crontab_changes(path: str, old: str | None, new: str | None) -> list[str]:
    def jobs(t: str | None) -> list[str]:
        return [ln.strip() for ln in (t or "").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]

    a, b = jobs(old), jobs(new)
    out = [f"+ {cut(ln, 160)}" for ln in b if ln not in a] + [f"- {cut(ln, 160)}" for ln in a if ln not in b]
    return [f"{path}: {x}" for x in out]


# ---------------------------------------------------------------- decisions (D-242: swap the reader here)

@dataclass(frozen=True)
class Decision:
    id: str
    title: str
    keywords: tuple[str, ...]
    status: str


ENTRY_RE = re.compile(r"^### (D-\d+)\b[ \t]*(.*)$", re.M)
FIELD_RE = r"^\s*-\s*\*\*{}:\*\*\s*(.*)$"


def load_decisions(source: str) -> list[Decision]:
    """Every decision with its keywords and status. Today `source` is the DECISIONS.md path (each
    "### D-NNN title" entry's "- **Keywords:** a, b" and "- **Status:** ..." lines); the DB decision
    store (D-242) replaces this reader, nothing else. A missing or unreadable source raises."""
    try:
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        raise ManifestError(f"decisions: can't read {source} ({e}); pass --decisions PATH") from e
    heads = list(ENTRY_RE.finditer(text))
    out: list[Decision] = []
    for i, m in enumerate(heads):
        body = text[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        kw = re.search(FIELD_RE.format("Keywords"), body, re.M)
        st = re.search(FIELD_RE.format("Status"), body, re.M)
        words = tuple(w.strip() for w in kw.group(1).split(",") if w.strip()) if kw else ()
        out.append(Decision(m.group(1), m.group(2).strip(), words, st.group(1).strip() if st else ""))
    if not out:
        raise ManifestError(f"decisions: no '### D-NNN' entries in {source}")
    return out


def active(d: Decision) -> bool:
    return not d.status.lower().startswith("superseded")


def keyword_re(kw: str) -> re.Pattern[str] | None:
    """'keep-alive' matches keep-alive, keep_alive, keep alive and keepalive, as a whole word
    (snake_case parts count as words); a trailing plural s is optional. Keywords under 3
    characters match nothing."""
    parts = [p for p in re.split(r"[-_\s]+", kw.lower()) if p]
    if not parts or len("".join(parts)) < 3:
        return None
    if parts[-1].endswith("s") and len(parts[-1]) > 3:
        parts[-1] = parts[-1][:-1]
    body = r"[-_\s]?".join(re.escape(p) for p in parts)
    return re.compile(r"(?<![a-z0-9])" + body + r"s?(?![a-z0-9])", re.I)


CITE_RE = re.compile(r"\bD-(\d{3})\b")


# Keyword matching must single out symbols, not tag every one (on the D-238 merge plain matching
# hit 119 of 119): a keyword listed on more than KEYWORD_MAX_DECISIONS decisions is a topic
# ("prompts", "keep-alive"), not a pointer, and one that matches more than BROAD_SHARE of this
# diff's changed symbols (at least BROAD_MIN) is too broad for this diff; both are skipped and
# named in 3b. One common word ("now", "stop") is not a match either: a decision needs
# MIN_KEYWORDS of its specific keywords. A changed line citing a decision id always matches.
KEYWORD_MAX_DECISIONS = 3
BROAD_SHARE = 0.2
BROAD_MIN = 5
MIN_KEYWORDS = 2         # distinct specific keywords of one decision a symbol must match (or cite it)


@dataclass
class Match:
    change: SymChange
    hits: dict[str, list[str]]     # D-id -> the keywords (or "cites") that matched


@dataclass
class Matching:
    matches: list[Match]
    topic_keywords: int            # skipped: on more than KEYWORD_MAX_DECISIONS decisions
    broad: dict[str, int]          # skipped for this diff: keyword -> symbols it matched


def match_decisions(changes: list[SymChange], decisions: list[Decision]) -> Matching:
    """Every changed symbol whose name, file or changed lines match a specific keyword of an
    active decision, or whose changed lines cite a decision id (see KEYWORD_MAX_DECISIONS)."""
    known = {d.id for d in decisions}
    act = [d for d in decisions if active(d)]
    df: dict[str, int] = {}
    for d in act:
        for kw in {k.lower() for k in d.keywords}:
            df[kw] = df.get(kw, 0) + 1
    topic = {kw for kw, n in df.items() if n > KEYWORD_MAX_DECISIONS}
    pats: dict[str, re.Pattern[str]] = {}
    for kw in df:
        rx = keyword_re(kw)
        if kw not in topic and rx is not None:
            pats[kw] = rx
    owners: dict[str, list[str]] = {}
    for d in act:
        for kw in {k.lower() for k in d.keywords}:
            owners.setdefault(kw, []).append(d.id)
    texts = {c.key: "\n".join([c.qual, c.path, *c.changed_lines]) for c in changes}
    hit_by_kw = {kw: [k for k, t in texts.items() if rx.search(t)] for kw, rx in pats.items()}
    limit = max(BROAD_MIN, BROAD_SHARE * len(changes))
    broad = {kw: len(keys) for kw, keys in hit_by_kw.items() if len(keys) > limit}
    out: list[Match] = []
    for c in changes:
        hits: dict[str, list[str]] = {}
        for kw, keys in hit_by_kw.items():
            if kw not in broad and c.key in keys:
                for did in owners[kw]:
                    hits.setdefault(did, []).append(kw)
        hits = {did: sorted(kws) for did, kws in hits.items() if len(kws) >= MIN_KEYWORDS}
        for m in CITE_RE.finditer("\n".join(c.changed_lines)):
            did = f"D-{m.group(1)}"
            if did in known:
                hits.setdefault(did, [])
                if "cites" not in hits[did]:
                    hits[did].append("cites")
        if hits:
            out.append(Match(c, hits))
    return Matching(out, len(topic), broad)


# ---------------------------------------------------------------- mapping (worker report)

MAP_LINE = re.compile(r"^\s*(?:[-*]\s+)?(.+?)\s*:\s*((?:D-\d+[\s,;/|]*)+|none)\s*$", re.I)


def load_mapping(path: str) -> dict[str, list[str]]:
    """'symbol: D-xxx[, D-yyy]' or 'symbol: none' per line ('#' comments, '- ' bullets ok).
    A line that fits neither raises (the worker fixes the file)."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        raise ManifestError(f"mapping: can't read {path} ({e})") from e
    out: dict[str, list[str]] = {}
    for n, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = MAP_LINE.match(line)
        if not m:
            raise ManifestError(f"mapping {path}:{n}: expected 'symbol: D-xxx' or 'symbol: none', got {line!r}")
        ids = re.findall(r"D-\d+", m.group(2), re.I)
        out[m.group(1).strip().strip("`")] = [i.upper() for i in ids]
    return out


def mapping_section(changes: list[SymChange], mapping: dict[str, list[str]], decisions: list[Decision],
                    matched: list[Match]) -> str:
    required = [c for c in changes if c.kind in ("def", "class")]
    by_qual: dict[str, list[SymChange]] = {}
    for c in required:
        by_qual.setdefault(c.qual, []).append(c)
    resolved: dict[str, list[str]] = {}
    unknown: list[str] = []
    for k, ids in mapping.items():
        if any(k == c.key for c in required):
            resolved[k] = ids
        elif ":" not in k and len(by_qual.get(k, [])) == 1:
            resolved[by_qual[k][0].key] = ids
        else:
            unknown.append(k)
    dec = {d.id: d for d in decisions}
    lines = [f"{len(required)} changed functions/classes, {len(mapping)} mapping entries"]
    missing = [c for c in required if c.key not in resolved]
    lines.append(f"missing from the mapping ({len(missing)}):" + ("" if missing else " none"))
    lines += [f"  {c.op} {c.key}" for c in missing]
    if unknown:
        lines.append(f"mapping entries that name no changed function/class ({len(unknown)}):")
        lines += [f"  {k}" for k in unknown]
    bad = [(k, i) for k, ids in mapping.items() for i in ids if i not in dec or not active(dec[i])]
    if bad:
        lines.append("unknown or superseded D-ids:")
        lines += [f"  {k}: {i} ({'unknown' if i not in dec else dec[i].status})" for k, i in bad]
    cites = {m.change.key: sorted(d for d, why in m.hits.items() if "cites" in why) for m in matched}
    disagree = [(k, ids, cites[k]) for k, ids in resolved.items()
                if cites.get(k) and not set(cites[k]) & set(ids)]
    if disagree:
        lines.append("changed lines cite a D-id the mapping doesn't give:")
        lines += [f"  {k}: mapping {', '.join(ids) or 'none'}; cites {', '.join(c)}" for k, ids, c in disagree]
    return "\n".join(lines)


# ---------------------------------------------------------------- non-file changes: audit log, snapshot

def utc_iso(s: str) -> str:
    """Any ISO time (naive = UTC) -> '2026-10-14T19:16:05.000Z', the store's sortable form."""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ManifestError(f"not an ISO time: {s!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def ro_connect(db: str) -> sqlite3.Connection:
    """The DB read-only (never store.connect(), which may upgrade the schema)."""
    if not os.path.isfile(db):
        raise ManifestError(f"DB {db} does not exist; pass --db PATH")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as e:
        raise ManifestError(f"DB {db}: {e}") from e
    conn.row_factory = sqlite3.Row
    return conn


def audit_section(db: str, since: str, until: str | None) -> str:
    lo, hi = utc_iso(since), utc_iso(until) if until else "9999"
    conn = ro_connect(db)
    try:
        rows = conn.execute("SELECT ts, actor, action, target_type, target_id FROM audit_log "
                            "WHERE ts >= ? AND ts < ? ORDER BY id", (lo, hi)).fetchall()
    except sqlite3.Error as e:
        raise ManifestError(f"audit_log in {db}: {e}") from e
    finally:
        conn.close()
    head = f"audit_log {lo} .. {until and hi or 'now'}: {len(rows)} rows"
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["action"], []).append(r)
    out = [head]
    for action, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        actors = sorted({r["actor"] for r in rs})
        targets = list(dict.fromkeys(f"{r['target_type'] or ''}:{r['target_id'] or ''}".strip(":") for r in rs))
        tl = ", ".join(t for t in targets[:TARGETS_MAX] if t)
        if len(targets) > TARGETS_MAX:
            tl += f", +{len(targets) - TARGETS_MAX} more"
        out.append(f"  {action} x{len(rs)}  by {', '.join(actors)}  {rs[0]['ts'][5:16]}..{rs[-1]['ts'][5:16]}"
                   + (f"  [{tl}]" if tl else ""))
    return "\n".join(out)


def run_cmd(argv: list[str], ok_codes: Sequence[int] = (0,)) -> subprocess.CompletedProcess[str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ManifestError(f"snapshot: {argv[0]}: {e}") from e
    if p.returncode not in ok_codes:
        raise ManifestError(f"snapshot: {' '.join(argv)} exited {p.returncode}: {p.stderr.strip()[:300]}")
    return p


def take_snapshot(db: str) -> dict[str, Any]:
    """Read-only state: containers (docker inspect of every one), image ids, the host crontab,
    the host at queue, the scheduled_jobs rows. Any source that fails raises."""
    ids = run_cmd(["docker", "ps", "-aq", "--no-trunc"]).stdout.split()
    containers: dict[str, dict[str, Any]] = {}
    if ids:
        for c in json.loads(run_cmd(["docker", "inspect", *ids]).stdout):
            containers[c["Name"].lstrip("/")] = {
                "id": c["Id"][:12], "image": c["Image"][7:19] if c["Image"].startswith("sha256:") else c["Image"],
                "config_image": c.get("Config", {}).get("Image"), "status": c["State"]["Status"],
                "started_at": c["State"].get("StartedAt"), "restarts": c.get("RestartCount", 0)}
    images: dict[str, str] = {}
    for line in run_cmd(["docker", "images", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}"]
                        ).stdout.splitlines():
        if "\t" in line:
            name, iid = line.split("\t", 1)
            images[name] = iid.replace("sha256:", "")[:12]
    p = run_cmd(["crontab", "-l"], ok_codes=(0, 1))
    if p.returncode == 1 and "no crontab" not in p.stderr.lower():
        raise ManifestError(f"snapshot: crontab -l: {p.stderr.strip()[:300]}")
    crontab = p.stdout if p.returncode == 0 else ""
    at_jobs = sorted(run_cmd(["atq"]).stdout.splitlines())
    conn = ro_connect(db)
    try:
        jobs = {r["id"]: f"{r['status']} {r['run_at']} {cut(r['cmd'], 100)}"
                for r in conn.execute("SELECT id, run_at, status, cmd FROM scheduled_jobs ORDER BY id")}
    except sqlite3.Error as e:
        raise ManifestError(f"snapshot: scheduled_jobs in {db}: {e}") from e
    finally:
        conn.close()
    return {"taken_at": utc_iso(datetime.now(timezone.utc).isoformat()), "containers": containers,
            "images": images, "host_crontab": crontab, "at_jobs": at_jobs, "scheduled_jobs": jobs}


def compare_snapshots(old: dict[str, Any], new: dict[str, Any]) -> str:
    out = [f"state {old['taken_at']} -> {new['taken_at']}"]

    def dict_diff(title: str, a: dict[str, Any], b: dict[str, Any], show: Any) -> None:
        lines = [f"+ {k}: {show(b[k])}" for k in sorted(b) if k not in a]
        lines += [f"- {k}" for k in sorted(a) if k not in b]
        for k in sorted(set(a) & set(b)):
            if a[k] != b[k]:
                if isinstance(a[k], dict):
                    ch = [f"{f} {a[k].get(f)} -> {b[k].get(f)}" for f in b[k] if a[k].get(f) != b[k].get(f)]
                    lines.append(f"~ {k}: " + "; ".join(ch))
                else:
                    lines.append(f"~ {k}: {a[k]} -> {b[k]}")
        out.append(f"{title}: " + ("no change" if not lines else ""))
        out.extend("  " + ln for ln in lines)

    dict_diff("containers", old["containers"], new["containers"],
              lambda c: f"{c['status']} image {c['image']}")
    dict_diff("images", old["images"], new["images"], lambda i: i)
    ca, cb = old["host_crontab"].splitlines(), new["host_crontab"].splitlines()
    cl = [f"+ {ln}" for ln in cb if ln not in ca] + [f"- {ln}" for ln in ca if ln not in cb]
    out.append("host crontab: " + ("no change" if not cl else ""))
    out.extend("  " + ln for ln in cl)
    aa, ab = set(old["at_jobs"]), set(new["at_jobs"])
    al = [f"+ {j}" for j in sorted(ab - aa)] + [f"- {j}" for j in sorted(aa - ab)]
    out.append("at jobs: " + ("no change" if not al else ""))
    out.extend("  " + ln for ln in al)
    dict_diff("scheduled_jobs", old["scheduled_jobs"], new["scheduled_jobs"], lambda j: j)
    return "\n".join(out)


# ---------------------------------------------------------------- the manifest

@dataclass
class Manifest:
    sections: dict[str, tuple[str, str]] = field(default_factory=dict)   # id -> (title, text)
    parse_errors: list[str] = field(default_factory=list)


def _fmt_sym(c: SymChange) -> str:
    s = c.head or c.base
    assert s is not None
    where = f"L{s.start}" if c.head else f"base L{s.start}"
    if c.kind == "const":
        return f"{c.op}{c.qual}"
    line = f"  {c.op} {cut(s.sig)}  {where}"
    if c.op == "+":
        line += f" ({len(s.src)} lines)"
    elif c.op == "-":
        line += f" ({len(s.src)} lines)"
    elif c.op == ">":
        line += f" [{c.tags[0]}]"
    else:
        line += f" [{','.join(c.tags)} +{c.plus}/-{c.minus}]"
        if "sig" in c.tags and c.base is not None:
            line += f" was {cut(c.base.sig, 80)}"
    return line


def build(base: str, head: str, *, cwd: str | None = None, decisions: list[Decision] | None = None,
          mapping: dict[str, list[str]] | None = None, db: str | None = None, since: str | None = None,
          until: str | None = None, compare: dict[str, Any] | None = None, live_hunks: bool = False) -> Manifest:
    man = Manifest()
    files = changed_files(base, head, cwd)
    tests = [f for f in files if is_test(f.path)]
    files = [f for f in files if not is_test(f.path)]

    # -- 1: files, symbols, interfaces
    pys: dict[str, tuple[PyFile | None, PyFile | None]] = {}
    texts: dict[str, tuple[str | None, str | None]] = {}
    all_changes: list[SymChange] = []
    for f in files:
        if f.added is None:
            continue
        old = show(base, f.old, cwd) if f.status != "A" else None
        new = show(head, f.path, cwd) if f.status != "D" else None
        texts[f.path] = (old, new)
        if f.path.endswith(".py"):
            bp = parse_py(old) if old is not None else None
            hp = parse_py(new) if new is not None else None
            for which, p in (("base", bp), ("head", hp)):
                if p is not None and p.error:
                    man.parse_errors.append(f"{f.path} ({which}): {p.error}")
            pys[f.path] = (bp, hp)
            all_changes += compare_py(f.path, bp, hp)

    ta = sum(f.added or 0 for f in files)
    tr = sum(f.removed or 0 for f in files)
    s1 = [f"{base[:10]}..{head[:10]}: {len(files)} files, +{ta}/-{tr}"]
    if tests:
        s1.append(f"tests: {len(tests)} files changed (omitted)")
    s1.append("")
    s1.append("## Files")
    for f in files:
        counts = "binary" if f.added is None else f"+{f.added}/-{f.removed}"
        tags = []
        if f.path in LIVE_FILES:
            tags.append("live")
        if matches(f.path, COUNTS_ONLY):
            tags.append("counts only")
        elif matches(f.path, FULL_DIFF) and not f.path.endswith(".py"):
            tags.append("§2")
        ren = f" (from {f.old})" if f.old != f.path else ""
        s1.append(f"{f.status} {f.path}{ren} {counts}" + (f" [{', '.join(tags)}]" if tags else ""))

    s1.append("")
    s1.append("## Python symbols (+ added, - removed, ~ changed, > renamed; L = head line, base L = base line)")
    if man.parse_errors:
        s1 += [f"!! AST parse failed: {e}" for e in man.parse_errors]
    for path, (bp, hp) in pys.items():
        ch = [c for c in all_changes if c.path == path]
        imp_b = bp.imports if bp else set()
        imp_h = hp.imports if hp else set()
        mod_changed = bp is not None and hp is not None and bp.module_key != hp.module_key
        if not ch and imp_b == imp_h and not mod_changed:
            continue
        s1.append(path)
        s1 += [_fmt_sym(c) for c in ch if c.kind != "const"]
        consts = [_fmt_sym(c) for c in ch if c.kind == "const"]
        if consts:
            s1.append("  constants: " + ", ".join(consts))
        if imp_b != imp_h:
            s1.append("  imports: " + ", ".join([f"+{i}" for i in sorted(imp_h - imp_b)]
                                                + [f"-{i}" for i in sorted(imp_b - imp_h)]))
        if mod_changed:
            s1.append("  other module-level code changed")

    inter: dict[str, list[str]] = {k: [] for k in ("DB schema / migrations", "settings keys", "CLI commands/options",
                                                    "MCP tools", "audited actions", "cron / compose / docker",
                                                    ".claude settings, hooks, agents")}
    for f in files:
        old, new = texts.get(f.path, (None, None))
        if f.path.endswith(".py"):
            bp, hp = pys[f.path]
            ch = [c for c in all_changes if c.path == f.path]
            inter["DB schema / migrations"] += schema_changes(f.path, bp, hp, ch)
            inter["settings keys"] += settings_changes(f.path, ch)
            inter["CLI commands/options"] += cli_changes(f.path, bp, hp)
            inter["MCP tools"] += decorated_changes(f.path, bp, hp, "tool", ch)
            inter["audited actions"] += decorated_changes(f.path, bp, hp, "action", ch)
        if f.path.endswith(".json") and f.added is not None and not matches(f.path, COUNTS_ONLY):
            target = ".claude settings, hooks, agents" if matches(f.path, CLAUDE_DIRS) else "settings keys"
            inter[target] += json_key_changes(f.path, old, new)
        if "crontab" in os.path.basename(f.path):
            inter["cron / compose / docker"] += crontab_changes(f.path, old, new) or [f"{f.path}: comments only"]
        elif f.path.startswith("docker/") or "compose" in os.path.basename(f.path) \
                or os.path.basename(f.path) == "Dockerfile":
            inter["cron / compose / docker"].append(f"{f.status} {f.path} +{f.added}/-{f.removed}")
        if matches(f.path, CLAUDE_DIRS) and not f.path.endswith(".json"):
            inter[".claude settings, hooks, agents"].append(f"{f.status} {f.path} +{f.added}/-{f.removed}")
    s1.append("")
    s1.append("## Interfaces (none = checked, nothing changed)")
    for k, v in inter.items():
        s1.append(f"{k}: " + ("none" if not v else ""))
        s1 += ["  " + x for x in v]
    man.sections["1"] = ("manifest", "\n".join(s1))

    # -- 2: docs and prompts as a word diff; settings and infra as a line diff
    full = [f.path for f in files if matches(f.path, FULL_DIFF) and not matches(f.path, COUNTS_ONLY)
            and not f.path.endswith(".py")]
    docs = [p for p in full if matches(p, WORD_DIFF)]
    rest = [p for p in full if p not in docs]
    s2 = [x for x in (file_diff(base, head, docs, cwd, words=True), file_diff(base, head, rest, cwd)) if x]
    man.sections["2"] = ("docs, prompts (word diff), settings, infra (line diff)", "\n".join(s2) or "none")

    # -- 3a: live code hunks (only with live_hunks, D-243: off by default)
    live = [f.path for f in files if f.path in LIVE_FILES or f.old in LIVE_FILES]
    if live_hunks:
        man.sections["3a"] = ("live code (" + ", ".join(live or ["none"]) + "): full hunks",
                              file_diff(base, head, live, cwd) or "none")
    else:
        man.sections["3a"] = ("live code: full hunks", "off (--live-hunks); live files: "
                              + (", ".join(live) or "none") + ", their decision-matched symbols are in 3b")

    # -- 3b: decision matches
    code = all_changes
    if decisions is None:
        man.sections["3b"] = ("decision matches", "skipped (no decisions source)")
    else:
        act = [d for d in decisions if active(d)]
        nokw = sum(1 for d in act if not d.keywords)
        mr = match_decisions(code, decisions)
        matched = mr.matches
        s3 = [f"{len(act)} active decisions ({nokw} without keywords), {len(decisions) - len(act)} superseded "
              f"skipped; {len(matched)} of {len(code)} changed symbols match",
              f"keywords skipped: {mr.topic_keywords} topic words (on more than {KEYWORD_MAX_DECISIONS} "
              f"decisions); too broad for this diff: "
              + (", ".join(f"{k} ({n})" for k, n in sorted(mr.broad.items(), key=lambda kv: -kv[1])) or "none")]
        for m in matched:
            ids = ", ".join(f"{d}({'/'.join(w)})" for d, w in sorted(m.hits.items()))
            s3.append(f"{m.change.op} {m.change.key}: {ids}")
        hunks: list[str] = []
        for m in matched:
            c = m.change
            if (live_hunks and c.path in live) or c.op == ">":
                continue
            hunks.append(f"--- {c.op} {c.key} [{c.kind}]")
            if c.op == "~":
                assert c.base is not None and c.head is not None
                hunks += symbol_diff(c.base, c.head)
            else:
                s = c.head or c.base
                assert s is not None
                hunks += [("+" if c.op == "+" else "-") + ln for ln in s.src]
        if hunks:
            s3.append("")
            s3.append("## Hunks of the matched symbols" + (" outside the live files (those are in 3a)"
                                                            if live_hunks else ""))
            s3 += hunks
        man.sections["3b"] = ("decision matches", "\n".join(s3))

    # -- 4: non-file changes
    s4: list[str] = []
    if since:
        if db is None:
            raise ManifestError("--since needs the DB (--db)")
        s4.append(audit_section(db, since, until))
    else:
        s4.append("audit_log: not checked (no --since)")
    if compare is not None:
        if db is None:
            raise ManifestError("--compare needs the DB (--db)")
        s4.append(compare_snapshots(compare, take_snapshot(db)))
    else:
        s4.append("containers / crontab / jobs: not checked (no --compare)")
    man.sections["4"] = ("non-file changes", "\n".join(s4))

    # -- 5: mapping
    if mapping is None:
        man.sections["5"] = ("decision mapping", "not checked (no --mapping)")
    else:
        matched5 = match_decisions(code, decisions).matches if decisions is not None else []
        man.sections["5"] = ("decision mapping", mapping_section(code, mapping, decisions or [], matched5))
    return man


def render(man: Manifest, only: Sequence[str] = SECTIONS) -> str:
    out = []
    for sid in only:
        title, text = man.sections[sid]
        out.append(f"# {sid} {title} (≈{tokens(text)} tok)\n{text}")
    return "\n\n".join(out) + "\n"


def budget(man: Manifest, only: Sequence[str] = SECTIONS) -> str:
    lines = [f"{sid:>3} {title}: ≈{tokens(man.sections[sid][1])} tok" for sid, (title, _) in
             ((s, man.sections[s]) for s in only)]
    total = sum(tokens(man.sections[s][1]) for s in only)
    lines.append(f"total ≈{total} tok (≈ chars / {CHARS_PER_TOKEN})")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("range", nargs="?", help="<base>..<head> (or <base>...<head>: from their merge base)")
    ap.add_argument("--since", help="start of the run (ISO): audit-log rows from then (default with --compare: "
                                    "the snapshot's time)")
    ap.add_argument("--until", help="end of the run (ISO, default now)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--snapshot", metavar="FILE", help="save the containers/crontab/jobs state to FILE and exit")
    g.add_argument("--compare", metavar="FILE", help="diff the current state against a saved --snapshot")
    ap.add_argument("--mapping", metavar="FILE", help="the worker's 'symbol: D-xxx|none' list")
    ap.add_argument("--decisions", default=DEFAULT_DECISIONS, help="DECISIONS.md (default: next to this script)")
    ap.add_argument("--db", default=DEFAULT_DB, help="the AFClaude DB, opened read-only")
    ap.add_argument("--live-hunks", action="store_true",
                    help=f"also the full hunks of the live-imported files ({', '.join(LIVE_FILES)}); off by "
                         "default (D-243): their decision-matched symbols are in 3b anyway")
    ap.add_argument("--budget", action="store_true", help="print only the approximate tokens per section")
    ap.add_argument("--sections", help=f"comma list of {','.join(SECTIONS)} (default all)")
    a = ap.parse_args(argv)
    try:
        if a.snapshot:
            snap = take_snapshot(a.db)
            with open(a.snapshot, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, indent=1)
            print(f"snapshot saved: {a.snapshot} ({snap['taken_at']}; {len(snap['containers'])} containers, "
                  f"{len(snap['scheduled_jobs'])} scheduled jobs)")
            return 0
        if not a.range:
            ap.error("a <base>..<head> range is needed (or --snapshot FILE)")
        only = SECTIONS if not a.sections else tuple(s.strip() for s in a.sections.split(","))
        bad = [s for s in only if s not in SECTIONS]
        if bad:
            ap.error(f"unknown sections {bad} (known: {', '.join(SECTIONS)})")
        compare = None
        if a.compare:
            try:
                with open(a.compare, encoding="utf-8") as fh:
                    compare = json.load(fh)
            except (OSError, ValueError) as e:
                raise ManifestError(f"--compare {a.compare}: {e}") from e
        since = a.since or (compare["taken_at"] if compare else None)
        base, head = resolve_range(a.range)
        man = build(base, head, decisions=load_decisions(a.decisions),
                    mapping=load_mapping(a.mapping) if a.mapping else None,
                    db=a.db, since=since, until=a.until, compare=compare, live_hunks=a.live_hunks)
    except ManifestError as e:
        print(f"change_manifest: ERROR: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(budget(man, only) if a.budget else render(man, only))
    if man.parse_errors:
        print("change_manifest: ERROR: Python files that don't parse: " + "; ".join(man.parse_errors),
              file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
