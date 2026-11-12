#!/usr/bin/env python3
"""txsource.py: the reader switch (phase 5b K4, docs/phase5b_plan.md §3.4, D-164, D-234, D-235).

Today the readers of Claude's own files (the transcripts under <claude dir>/projects, the session
registry <claude dir>/sessions, the project docs of the host repo) read them through read-only mounts. The
runner container of 5b gets no such mounts (design §10.1): the same bytes come through the host kit's bridge
(docker/host-kit/host_exec.py: `transcripts`, `transcript-read`, `sessions`, `doc-files read`).

The switch is the environment variable AFCLAUDE_READERS:

  mount   (the default)  every reader reads the path it always read: the host, or a container with the mounts
  bridge                 every reader reads through the kit; nothing falls back to a mount (a failure of the
                         bridge raises, D-234/D-235). K7's cut-over sets it together with the removal of the mounts

Two ways to use the bridge, so each reader keeps its file-reading core:

  - A MIRROR for readers that open a transcript they found by session id (keepalive, dispatcher, compaction,
    usage review...): mirror() appends the missing bytes of the remote file to a local copy under
    <data>/tx_mirror/projects/<same relative path> (transcripts are append-only; a file that got shorter is
    fetched again) and sets the copy's mtime to the host's, so every getmtime / getsize / open of the existing
    code reads what the host has. A subagent file used only for its mtime is a stub (empty, mtime set); a
    later content read fills it in. The session registry is mirrored the same way (sessions_dir()).
  - RANGE READS for the scans that keep a byte offset per file (stalled.py, usage_sampler.py): listing() gives
    every file's size and mtime, read_range() the bytes from the stored offset on.

Every bridge answer is checked: a non-zero exit, bad JSON, a `truncated` flag or a non-empty `errors` list of
the listing raise TxError (or FileNotFoundError for a transcript that is gone); a timeout raises as the
subprocess does. The mirror holds transcript text: its directories are 0700 and its files 0600.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Callable, Optional

import host

MOUNT, BRIDGE = "mount", "bridge"
CHUNK = 1024 * 1024          # bytes per transcript-read call (host_exec.py TX_READ_MAX)
LIST_TTL = 1.0               # seconds a listing is reused (one tick looks up many sessions)
SESSIONS_TTL = 2.0           # seconds a mirrored session registry is reused (one preflight reads many pids)
CALL_TIMEOUT = 60            # seconds one bridge call may take (D-234: a timeout raises)

_listing: tuple[float, dict[str, tuple[int, float]]] | None = None
_sessions_at: float | None = None


class TxError(RuntimeError):
    """A bridge read failed or answered something a reader must not use (D-235)."""


def mode() -> str:
    """AFCLAUDE_READERS: `mount` (default, also when unset or empty) or `bridge`; anything else raises."""
    m = os.environ.get("AFCLAUDE_READERS", "").strip() or MOUNT
    if m not in (MOUNT, BRIDGE):
        raise TxError(f"AFCLAUDE_READERS={m!r}: expected {MOUNT!r} or {BRIDGE!r}")
    return m


def bridge_on() -> bool:
    return mode() == BRIDGE


def config_id() -> str:
    """The Claude config dir id on the host (`default` = ~/.claude; more ids come with 5d)."""
    return os.environ.get("AFCLAUDE_READ_CONFIG", "").strip() or "default"


def mirror_root() -> str:
    """The local mirror directory: AFCLAUDE_TX_MIRROR, else tx_mirror/ next to the database (the data dir)."""
    env = os.environ.get("AFCLAUDE_TX_MIRROR", "").strip()
    if env:
        return os.path.abspath(env)
    import store
    return os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)), "tx_mirror")


def projects_mirror() -> str:
    return os.path.join(mirror_root(), "projects")


def _private_dirs(path: str) -> None:
    """Create `path` and its missing parents below the mirror root, 0700."""
    root = mirror_root()
    os.makedirs(root, mode=0o700, exist_ok=True)
    if not os.path.abspath(path).startswith(root + os.sep) and os.path.abspath(path) != root:
        raise TxError(f"{path!r} is outside the mirror {root!r}")
    os.makedirs(path, mode=0o700, exist_ok=True)


def _call(argv: list[str]) -> Any:
    """One kit command that answers JSON. -> the parsed answer; rc != 0 or bad JSON raises TxError, a
    timeout raises subprocess.TimeoutExpired."""
    run: Callable[..., Any] = getattr(host, "run_on_host")      # host.py is untyped; looked up per call (tests patch it)
    r = run(argv, timeout=CALL_TIMEOUT)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[:300]
        if "No such file" in err and argv[0] == "transcript-read":
            raise FileNotFoundError(f"{argv[2]}: gone on the host ({err})")
        raise TxError(f"`{' '.join(argv[:2])}` failed on the host, rc {r.returncode}: {err}")
    try:
        return json.loads(r.stdout)
    except ValueError as e:
        raise TxError(f"`{' '.join(argv[:2])}` did not answer JSON: {e}") from None


# ------------------------------------------------------------------ transcripts: listing and bytes

def listing(max_age: float = LIST_TTL) -> dict[str, tuple[int, float]]:
    """{path relative to <claude dir>/projects: (size, mtime)} of every transcript (main sessions and
    subagents) on the host. Reused for `max_age` seconds. A cut-off or unreadable listing raises."""
    global _listing
    if _listing is not None and time.monotonic() - _listing[0] <= max_age:
        return _listing[1]
    out = _call(["transcripts", config_id(), "--changed-since", "0", "--head", "0", "--tail", "0"])
    if not isinstance(out, dict) or not isinstance(out.get("files"), list):
        raise TxError("`transcripts` answered an unexpected shape")
    if out.get("truncated"):
        raise TxError("`transcripts` was cut off by a cap of the host kit (too many files): raise TX_MAX_FILES in "
                      "docker/host-kit/host_exec.py, bump the kit version and re-run the install")
    if out.get("errors"):
        raise TxError(f"the host could not read part of the transcripts: {str(out['errors'])[:300]}")
    files = {str(f["path"]): (int(f["size"]), float(f["mtime"])) for f in out["files"]}
    _listing = (time.monotonic(), files)
    return files


def scan_listing(base: str, top_level_only: bool) -> dict[str, int]:
    """For a scan that keeps a byte offset per file: {local-style path under `base`: size} of every transcript
    (top_level_only: <project dir>/<session>.jsonl only, as stalled.py scans). `base` is the path string the
    scan stores in its state (the host's projects dir), never opened in bridge mode. Always a fresh listing."""
    return {os.path.join(base, *r.split("/")): size for r, (size, _m) in listing(0).items()
            if not top_level_only or r.count("/") == 1}


def read_scan_chunk(base: str, path: str, offset: int, size: int) -> bytes:
    """The bytes [offset, size) of a transcript named by scan_listing's path (fewer when the host's file got
    shorter since the listing)."""
    rel = os.path.relpath(path, base).replace(os.sep, "/")
    data, _size, _mtime = read_range(rel, offset, size - offset) if size > offset else (b"", 0, 0.0)
    return data


def drop_listing() -> None:
    """Forget the cached listing (tests; a reader that must see a new file at once)."""
    global _listing, _sessions_at
    _listing = None
    _sessions_at = None


def _read_chunk(rel: str, offset: int, length: int) -> tuple[bytes, int, float]:
    out = _call(["transcript-read", config_id(), rel, str(offset), str(length)])
    try:
        data = base64.b64decode(out["data"], validate=True)
        size, mtime = int(out["size"]), float(out["mtime"])
        if out["path"] != rel or out["offset"] != offset or len(data) > length:
            raise ValueError("another file, offset or length than asked")
    except (KeyError, TypeError, ValueError) as e:
        raise TxError(f"`transcript-read` answered an unusable shape for {rel}: {e}") from None
    return data, size, mtime


def read_range(rel: str, offset: int, length: int) -> tuple[bytes, int, float]:
    """Up to `length` bytes of the transcript from `offset` (several calls for more than CHUNK).
    -> (bytes, the file's size now, its mtime): fewer bytes than asked means the end of the file. The host's
    own error (the file is gone) raises FileNotFoundError."""
    if length <= 0:
        raise ValueError("read_range: length must be positive")
    buf = bytearray()
    while True:
        want = min(CHUNK, length - len(buf))
        data, size, mtime = _read_chunk(rel, offset + len(buf), want)
        buf += data
        if len(buf) >= length or len(data) < want or offset + len(buf) >= size:
            return bytes(buf), size, mtime


# ------------------------------------------------------------------ transcripts: the mirror

def mirror_path(rel: str) -> str:
    parts = rel.split("/")
    if not rel or rel.startswith("/") or "" in parts or ".." in parts or "." in parts:
        raise TxError(f"bad transcript path {rel!r}")
    return os.path.join(projects_mirror(), *parts)


def _set_mtime(path: str, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def mirror(rel: str) -> str:
    """Bring the local copy of the transcript up to date (appending what it lacks) -> its local path. The
    host's current size decides (no cached listing): a copy longer than the host's file is thrown away and
    fetched again. FileNotFoundError when the host no longer has it."""
    path = mirror_path(rel)
    _private_dirs(os.path.dirname(path))
    have = os.path.getsize(path) if os.path.exists(path) else 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        while True:
            data, size, mtime = _read_chunk(rel, have, CHUNK)
            if size < have:                       # shorter than our copy: rewritten on the host
                os.ftruncate(fd, 0)
                have = 0
                continue
            if data:
                os.pwrite(fd, data, have)
                have += len(data)
            if have >= size:
                break
    finally:
        os.close(fd)
    _set_mtime(path, mtime)
    return path


def stub(rel: str, mtime: float) -> str:
    """A subagent file only looked at for its mtime: the local file exists (empty if never fetched) and has the
    host's mtime. Its size is the part mirrored so far, never the host's: a reader of the content calls mirror()."""
    path = mirror_path(rel)
    _private_dirs(os.path.dirname(path))
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    _set_mtime(path, mtime)
    return path


def rel_of(path: str) -> Optional[str]:
    """The transcript path relative to the mirror, or None when `path` is not a mirrored file."""
    root = projects_mirror() + os.sep
    p = os.path.abspath(path)
    return p[len(root):].replace(os.sep, "/") if p.startswith(root) else None


def refresh_path(path: str, main_only: bool = False) -> None:
    """A reader is about to open `path`: when it is a mirrored transcript, bring it up to date first.
    main_only: leave a subagent file alone (a reader that only wants its mtime, which a stub carries)."""
    rel = rel_of(path)
    if rel is not None and not (main_only and rel.count("/") != 1):
        mirror(rel)


def session_mains(session_id: str) -> list[str]:
    """Relative paths of every main transcript <project dir>/<session id>.jsonl (a session whose cwd changed
    has one per project dir), sorted."""
    want = f"{session_id}.jsonl"
    return sorted(r for r in listing() if r.count("/") == 1 and r.endswith("/" + want))


def session_main(session_id: str) -> Optional[str]:
    """The first of session_mains (the glob of the mount mode takes any), or None."""
    hits = session_mains(session_id)
    return hits[0] if hits else None


def session_transcript(session_id: str) -> Optional[str]:
    """keepalive.transcript_path in bridge mode: the mirrored (current) main transcript, or None."""
    rel = session_main(session_id)
    return mirror(rel) if rel else None


def subagent_rels(main_path: str) -> list[str]:
    """The subagent transcripts (relative paths) of the session whose main transcript is the mirrored `main_path`."""
    rel = rel_of(main_path)
    if rel is None:
        raise TxError(f"{main_path!r} is not a mirrored transcript")
    pre = rel[:-len(".jsonl")] + "/subagents/"
    return sorted(r for r in listing() if r.startswith(pre) and r.endswith(".jsonl") and "/" not in r[len(pre):])


def subagent_files(main_path: str, content: bool = False) -> list[str]:
    """Local paths of the session's subagent transcripts: stubs with the host's mtime, or (content=True) full
    copies."""
    lst = listing()
    return [mirror(r) if content else stub(r, lst[r][1]) for r in subagent_rels(main_path)]


# ------------------------------------------------------------------ the session registry

def sessions_dir() -> str:
    """The local copy of <claude dir>/sessions: every readable *.json of the host, others removed (reused for
    SESSIONS_TTL seconds). A registry file that is being rewritten on the host (the kit reports it with an
    `error`) is left out, as the mount mode's readers skip a file they cannot parse; the listing cut by a cap
    raises."""
    global _sessions_at
    path = os.path.join(mirror_root(), "sessions")
    if _sessions_at is not None and time.monotonic() - _sessions_at <= SESSIONS_TTL and os.path.isdir(path):
        return path
    out = _call(["sessions", config_id()])
    if not isinstance(out, dict) or not isinstance(out.get("files"), list):
        raise TxError("`sessions` answered an unexpected shape")
    if out.get("truncated"):
        raise TxError("`sessions` was cut off by a cap of the host kit")
    _private_dirs(path)
    keep = set()
    for f in out["files"]:
        name = str(f.get("file", ""))
        if "data" not in f or not name.endswith(".json") or "/" in name or name.startswith("."):
            continue
        tmp = os.path.join(path, "." + name + ".new")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            json.dump(f["data"], fh)
        _set_mtime(tmp, float(f.get("mtime", time.time())))
        os.replace(tmp, os.path.join(path, name))
        keep.add(name)
    for name in os.listdir(path):
        if name not in keep:
            os.unlink(os.path.join(path, name))
    _sessions_at = time.monotonic()
    return path


# ------------------------------------------------------------------ the project docs

def read_docs(directory: str, names: tuple[str, ...]) -> dict[str, Optional[str]]:
    """`doc-files read`: {name: text, or None for a file the host does not have} of the named docs in the host
    project directory (PROGRESS.md, GOALS.md, EXCEPTIONS.md, DECISIONS.md only)."""
    out = _call(["doc-files", "read", directory, *names])
    if not isinstance(out, dict) or set(out) != set(names) or not all(v is None or isinstance(v, str) for v in out.values()):
        raise TxError(f"`doc-files read` answered an unexpected shape for {list(names)}")
    return {n: out[n] for n in names}
