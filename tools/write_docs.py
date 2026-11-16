#!/usr/bin/env python3
"""Write the public build log files from `afclaude tasks doc export --emit` (K5 part 2b, D-344, D-181).

The container cannot write the host's repo, so the export is two steps: the container prints the three files'
texts as JSON, and this helper (host side, stdlib only, Python 3.9) writes them:

    afclaude tasks doc export --emit | python3 tools/write_docs.py [--repo DIR] [--check]

Only PROGRESS.md, GOALS.md and EXCEPTIONS.md are written, nothing else, whatever the JSON names. A target that is
a symlink is refused. The emitted project must be the one whose directory the target is (`project_path` of the
export): another project's log is refused. A write is atomic per file (a temp file in the same directory, renamed
over the file, its mode kept), and all the temp files are written before the first rename, so a full disk or a
failed write changes no file. Everything is validated before the first write: a file the export refused (a conflict,
the public-repo check) or a name outside the list stops the whole call with exit 1 and nothing written. --check
writes nothing: exit 0 when every file already reads as emitted, 1 when one would change. Exit 2: the input file
could not be read or written.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

NAMES = ("PROGRESS.md", "GOALS.md", "EXCEPTIONS.md")
WRITABLE = ("emitted", "unchanged")      # the results of `doc export --emit` that carry a text to write


class Refused(Exception):
    """An input or a target this helper will not write (the message says why)."""


def parse(raw: str, repo: str) -> dict[str, str]:
    """{file name: text} from the emitter's JSON, validated against the target `repo`. Raises Refused."""
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise Refused("input is not JSON: %s" % e) from None
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict) or not files:
        raise Refused("input has no `files` object (expected the output of `doc export --emit`)")
    if data.get("emit") is not True:
        raise Refused("input is not an --emit result (`emit` is not true): it would not carry the texts")
    here = data.get("project_path")
    if not isinstance(here, str) or not here:
        raise Refused("input names no `project_path`: not the output of this version's `doc export --emit`")
    if os.path.realpath(os.path.expanduser(here)) != os.path.realpath(repo):
        raise Refused("the export is of project %r (directory %s), not of the target %s: refusing to write another "
                      "project's log" % (data.get("project"), here, repo))
    texts: dict[str, str] = {}
    for name, rep in files.items():
        if name not in NAMES:
            raise Refused("refusing %r: only %s are written" % (name, ", ".join(NAMES)))
        if not isinstance(rep, dict) or rep.get("result") not in WRITABLE or not isinstance(rep.get("text"), str):
            result = rep.get("result") if isinstance(rep, dict) else rep
            raise Refused("%s: the export did not produce a text (%s)%s" % (
                name, result, "".join("; " + str(f) for f in rep.get("findings", [])) if isinstance(rep, dict) else ""))
        texts[name] = rep["text"]
    if data.get("ok") is False:
        raise Refused("the export reported ok=false: nothing written")
    return texts


def target(repo: str, name: str) -> str:
    """The path of `name` in `repo`, after the checks (a whitelisted bare name, a real directory, no symlink)."""
    if name not in NAMES or os.path.basename(name) != name:
        raise Refused("refusing %r: only %s are written" % (name, ", ".join(NAMES)))
    if not os.path.isdir(repo):
        raise Refused("%s is not a directory" % repo)
    path = os.path.join(repo, name)
    if os.path.islink(path):
        raise Refused("%s is a symlink: refusing to write through it" % path)
    if os.path.lexists(path) and not os.path.isfile(path):
        raise Refused("%s is not a regular file" % path)
    return path


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return None


def stage(path: str, text: str) -> str:
    """Write `text` to a new temp file next to `path` (mode of `path` kept). -> the temp file's path."""
    d = os.path.dirname(os.path.abspath(path))
    try:
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return tmp


def write_atomic(path: str, text: str) -> None:
    os.replace(stage(path, text), path)


def run(raw: str, repo: str, check: bool = False) -> dict[str, str]:
    """{name: "unchanged" | "would write" | "written"}; raises Refused before anything is written. All temp
    files are written first; the renames come last, so a failure while writing leaves every file as it was."""
    texts = parse(raw, repo)
    paths = {name: target(repo, name) for name in texts}
    out: dict[str, str] = {}
    for name, text in texts.items():
        out[name] = "unchanged" if read_text(paths[name]) == text else ("would write" if check else "written")
    if not check:
        staged: dict[str, str] = {}
        try:
            for name, text in texts.items():
                if out[name] == "written":
                    staged[name] = stage(paths[name], text)
            while staged:
                name, tmp = next(iter(staged.items()))
                os.replace(tmp, paths[name])
                del staged[name]
        except BaseException:
            for tmp in staged.values():
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write PROGRESS.md, GOALS.md, EXCEPTIONS.md from `doc export --emit`.")
    ap.add_argument("--repo", default=None,
                    help="the repo directory (default: $CLAUDE_PROJECT_DIR, else the directory above tools/)")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file would change")
    ap.add_argument("--input", default="-", help="the JSON file (default: stdin)")
    args = ap.parse_args(argv)
    repo = args.repo or os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        if args.input == "-":
            raw = sys.stdin.read()
        else:
            with open(args.input, encoding="utf-8") as f:
                raw = f.read()
        res = run(raw, repo, args.check)
    except Refused as e:
        print("write_docs: refused: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("write_docs: %s" % e, file=sys.stderr)
        return 2
    for name, what in res.items():
        print("%-14s %s" % (name, what))
    return 1 if args.check and "would write" in res.values() else 0


if __name__ == "__main__":
    sys.exit(main())
