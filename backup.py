#!/usr/bin/env python3
"""
backup.py: the DB backup bundle and its restore (design §11.1, §11.3; phase 5c step B1 of
docs/phase5c_plan.md).

A bundle is one file, afclaude-backup-<install_id[:8]>-<utc, ms>.tar.gz, with exactly two members:

  - manifest.json: format + format_version, install_id and install_epoch, app_version, schema_version,
    created_at, kind ("backup"), row_counts (every table except sqlite_*, Litestream's _litestream_*
    and virtual tables, whose rows live in their shadow tables), files {name: {sha256, size}},
    master_key and wal_archive (null until steps B2 / B5);
  - db/afclaude.db: a consistent snapshot of the database. It is taken with SQLite's online backup
    API in one step (one read transaction) from a read-only connection, so `create` never writes to
    the source; the copy is then checked (integrity_check), VACUUMed and switched to a rollback
    journal, so the member is one self-contained file. (Not VACUUM INTO: the host's SQLite is 3.26.)

Fail fast (D-235): every check that fails raises BackupError and leaves nothing behind that looks
complete: `create` writes <name>.partial, verifies it like a restore would and only then renames it;
`restore` never overwrites a database and moves the restored file into place only after every check
and the audited install.restored action (install_epoch + 1, restored_from, and a paused install when
restore_starts_paused is on, D-173). Nothing schedules this yet (B3: the runner's daily job), and
the bundle is not encrypted yet (B2): it holds the whole database, so it is written mode 0600.

    python3 backup.py create --out DIR [--db PATH]     # -> the bundle path and its manifest (JSON)
    python3 backup.py verify BUNDLE                    # every check of a restore, nothing written
    python3 backup.py restore BUNDLE --into DIR        # DIR/afclaude.db; DIR must not hold a database
    python3 backup.py stamp [--db PATH]                # one-off: give an old database its install_id
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from typing import IO, Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import actions  # noqa: E402
import store  # noqa: E402

FORMAT = "afclaude-backup"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
DB_MEMBER = "db/afclaude.db"
MEMBERS = (MANIFEST, DB_MEMBER)
SUFFIX = ".tar.gz"
DB_NAME = "afclaude.db"
MANIFEST_MAX = 1024 * 1024          # bytes: a manifest is a few kB; anything larger is not ours
CHUNK = 1024 * 1024
MANIFEST_KEYS = ("format", "format_version", "install_id", "install_epoch", "app_version", "schema_version",
                 "created_at", "kind", "row_counts", "files", "master_key", "wal_archive")


class BackupError(RuntimeError):
    """A backup, verify or restore that failed; nothing partial was left behind."""


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """{table: rows} for every ordinary table: not sqlite_*, not Litestream's _litestream_* (it adds
    them to the live database, phase 5c B4), not virtual tables (their rows are in shadow tables)."""
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
        "AND name NOT LIKE '\\_litestream\\_%' ESCAPE '\\' "
        "AND COALESCE(sql, '') NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name")]
    return {n: int(conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]) for n in names}


def _check_integrity(conn: sqlite3.Connection, what: str) -> None:
    res = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
    if res != ["ok"]:
        raise BackupError(f"{what} fails the integrity check: {'; '.join(map(str, res[:5]))}")


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
        return None
    return store.get_meta(conn, key)


def snapshot(src: str, dst: str) -> None:
    """Copy the database at src into the new file dst: one online-backup step from a read-only
    connection (consistent, never writes to src), then integrity-checked, VACUUMed and switched to a
    rollback journal (one file)."""
    if not os.path.isfile(src):
        raise BackupError(f"no database at {src}")
    if os.path.exists(dst):
        raise BackupError(f"{dst} already exists")
    s = sqlite3.connect(pathlib.Path(os.path.abspath(src)).as_uri() + "?mode=ro", uri=True,
                        timeout=store.BUSY_TIMEOUT)
    try:
        d = sqlite3.connect(dst)
        try:
            s.backup(d)
            _check_integrity(d, "the snapshot")
            d.execute("PRAGMA journal_mode=DELETE")
            d.execute("VACUUM")
        finally:
            d.close()
    finally:
        s.close()


def _tarinfo(name: str, size: int, mtime: float) -> tarfile.TarInfo:
    """A member header without the host's user, group or file mode."""
    ti = tarfile.TarInfo(name)
    ti.size, ti.mtime, ti.mode = size, int(mtime), 0o600
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def _open_private(path: str) -> IO[bytes]:
    """A new file only its owner can read (the bundle holds the whole database); never an existing one."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def create(out_dir: str, db: str | None = None) -> dict[str, Any]:
    """Write a bundle of the database `db` (default store.DB_PATH) into out_dir. -> {path, sha256,
    size, manifest}. Refuses a database without meta.install_id (`backup.py stamp` gives it one)."""
    db = os.path.abspath(db or store.DB_PATH)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".backup-", dir=out_dir)
    partial: str | None = None
    try:
        copy = os.path.join(work, DB_NAME)
        snapshot(db, copy)
        conn = sqlite3.connect(copy)
        try:
            iid = _meta(conn, "install_id")
            if iid is None:
                raise BackupError(f"the database {db} has no install_id (it predates the install marker): "
                                  "give it one first with `python3 backup.py stamp`")
            raw_version = _meta(conn, "schema_version")
            if raw_version is None or not raw_version.isdigit():
                raise BackupError(f"the database {db} has no valid schema_version ({raw_version!r})")
            epoch = actions.install_epoch(conn)
            counts = row_counts(conn)
        finally:
            conn.close()
        created = store.now_iso()
        manifest: dict[str, Any] = {
            "format": FORMAT, "format_version": FORMAT_VERSION, "install_id": iid, "install_epoch": epoch,
            "app_version": actions._code_version(), "schema_version": int(raw_version), "created_at": created,
            "kind": "backup", "row_counts": counts,
            "files": {DB_MEMBER: {"sha256": _sha256(copy), "size": os.path.getsize(copy)}},
            "master_key": None, "wal_archive": None}
        ts = created.replace("-", "").replace(":", "").replace(".", "")     # 20261022T031200123Z (ms)
        final = os.path.join(out_dir, f"afclaude-backup-{iid[:8]}-{ts}{SUFFIX}")
        if os.path.exists(final):
            raise BackupError(f"{final} already exists")
        partial = final + ".partial"
        body = json.dumps(manifest, indent=2, sort_keys=True).encode()
        with _open_private(partial) as fh, tarfile.open(fileobj=fh, mode="w:gz", compresslevel=6) as tar:
            tar.addfile(_tarinfo(MANIFEST, len(body), os.path.getmtime(copy)), io.BytesIO(body))
            with open(copy, "rb") as src:
                tar.addfile(_tarinfo(DB_MEMBER, os.path.getsize(copy), os.path.getmtime(copy)), src)
        verify(partial)
        os.rename(partial, final)
        partial = None
        return {"path": final, "sha256": _sha256(final), "size": os.path.getsize(final), "manifest": manifest}
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if partial is not None and os.path.exists(partial):
            os.remove(partial)


def _check_manifest(m: Any) -> dict[str, Any]:
    if not isinstance(m, dict) or set(m) != set(MANIFEST_KEYS):
        raise BackupError(f"the manifest must have exactly the keys {', '.join(MANIFEST_KEYS)}")
    if m["format"] != FORMAT or m["format_version"] != FORMAT_VERSION:
        raise BackupError(f"not an {FORMAT} bundle of format {FORMAT_VERSION} "
                          f"(format {m['format']!r}, version {m['format_version']!r})")
    for k in ("install_id", "created_at", "kind"):
        if not isinstance(m[k], str) or not m[k]:
            raise BackupError(f"manifest.{k} must be a non-empty string")
    for k in ("install_epoch", "schema_version"):
        if isinstance(m[k], bool) or not isinstance(m[k], int) or m[k] < 0:
            raise BackupError(f"manifest.{k} must be a non-negative integer")
    if m["kind"] != "backup":
        raise BackupError(f"manifest.kind {m['kind']!r} is not supported by this code")
    if m["app_version"] is not None and not isinstance(m["app_version"], str):
        raise BackupError("manifest.app_version must be a string or null")
    if m["master_key"] is not None or m["wal_archive"] is not None:
        raise BackupError("this bundle carries a master key or WAL archive, which this code can't restore yet")
    counts = m["row_counts"]
    if not isinstance(counts, dict) or not all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
                                               for k, v in counts.items()):
        raise BackupError("manifest.row_counts must map table names to integers")
    files = m["files"]
    if not isinstance(files, dict) or set(files) != {DB_MEMBER}:
        raise BackupError(f"manifest.files must list exactly {DB_MEMBER}")
    f = files[DB_MEMBER]
    if (not isinstance(f, dict) or set(f) != {"sha256", "size"} or not isinstance(f["sha256"], str)
            or len(f["sha256"]) != 64 or isinstance(f["size"], bool) or not isinstance(f["size"], int)):
        raise BackupError(f"manifest.files[{DB_MEMBER}] must be {{sha256, size}}")
    return m


def _read(bundle: str, into: str) -> dict[str, Any]:
    """Check the bundle and extract its database to into/afclaude.db: exactly the expected members
    (regular files, fixed names: no path from the archive is ever used), a valid manifest, the
    database's size and sha256, its integrity, and its meta and row counts equal to the manifest.
    -> the manifest."""
    if not os.path.isfile(bundle):
        raise BackupError(f"no bundle at {bundle}")
    dst = os.path.join(into, DB_NAME)
    try:
        with tarfile.open(bundle, mode="r:gz") as tar:
            members = tar.getmembers()
            names = [mi.name for mi in members]
            if sorted(names) != sorted(MEMBERS) or not all(mi.isfile() for mi in members):
                raise BackupError(f"the bundle must hold exactly the files {', '.join(MEMBERS)}; it holds "
                                  f"{', '.join(repr(n) for n in names[:10]) or 'nothing'}")
            by_name = {mi.name: mi for mi in members}
            if by_name[MANIFEST].size > MANIFEST_MAX:
                raise BackupError("the manifest is too large")
            mf = tar.extractfile(by_name[MANIFEST])
            if mf is None:
                raise BackupError("the manifest can't be read")
            try:
                manifest = _check_manifest(json.loads(mf.read().decode()))
            except (ValueError, UnicodeDecodeError) as e:
                raise BackupError(f"the manifest is not valid JSON ({e})") from e
            want = manifest["files"][DB_MEMBER]
            member = tar.extractfile(by_name[DB_MEMBER])
            if member is None:
                raise BackupError("the database member can't be read")
            src: IO[bytes] = member
            h, size = hashlib.sha256(), 0
            with _open_private(dst) as out:
                for block in iter(lambda: src.read(CHUNK), b""):
                    h.update(block)
                    size += len(block)
                    out.write(block)
    except (tarfile.TarError, EOFError, OSError) as e:
        raise BackupError(f"the bundle {bundle} can't be read ({e})") from e
    if (h.hexdigest(), size) != (want["sha256"], want["size"]):
        raise BackupError(f"the database in the bundle does not match its manifest (sha256/size): "
                          "damaged or tampered")
    conn = sqlite3.connect(pathlib.Path(dst).as_uri() + "?mode=ro", uri=True)
    try:
        _check_integrity(conn, "the database in the bundle")
        have = (_meta(conn, "install_id"), _meta(conn, "schema_version"), actions.install_epoch(conn))
        if have != (manifest["install_id"], str(manifest["schema_version"]), manifest["install_epoch"]):
            raise BackupError(f"the database's install_id / schema_version / install_epoch {have} do not "
                              "match the manifest")
        counts = row_counts(conn)
    finally:
        conn.close()
    if counts != manifest["row_counts"]:
        diff = sorted(t for t in set(counts) | set(manifest["row_counts"])
                      if counts.get(t) != manifest["row_counts"].get(t))
        raise BackupError(f"row counts differ from the manifest in: {', '.join(diff[:10])}")
    return manifest


def verify(bundle: str) -> dict[str, Any]:
    """Every check a restore makes, into a temporary directory that is removed. -> the manifest."""
    work = tempfile.mkdtemp(prefix="afclaude-verify-")
    try:
        return _read(os.path.abspath(bundle), work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def restore(bundle: str, into: str, actor: str = "cli", via: str = "cli") -> dict[str, Any]:
    """Restore the bundle as into/afclaude.db (design §11.3): refuses when into already holds a
    database; checks everything (_read); refuses a schema newer than this code and migrates an older
    one (store.migrate); then install.restored (audited: install_epoch + 1, restored_from, paused when
    restore_starts_paused is on) and only then moves the file into place.
    -> {db, manifest, install_epoch, paused, reason}."""
    bundle, into = os.path.abspath(bundle), os.path.abspath(into)
    os.makedirs(into, exist_ok=True)
    target = os.path.join(into, DB_NAME)
    taken = [p for p in (target, target + "-wal", target + "-shm", target + "-journal") if os.path.lexists(p)]
    if taken:
        raise BackupError(f"{into} already holds a database ({os.path.basename(taken[0])}): a restore never "
                          "overwrites one; choose an empty data directory")
    bundle_sha = _sha256(bundle) if os.path.isfile(bundle) else ""
    work = tempfile.mkdtemp(prefix=".restore-", dir=into)
    try:
        manifest = _read(bundle, work)
        if manifest["schema_version"] > store.SCHEMA_VERSION:
            raise BackupError(f"the bundle's schema_version {manifest['schema_version']} is newer than this "
                              f"code's {store.SCHEMA_VERSION}: update the code first")
        db = os.path.join(work, DB_NAME)
        store.migrate(db)
        source = {"install_id": manifest["install_id"], "install_epoch": manifest["install_epoch"],
                  "created_at": manifest["created_at"], "app_version": manifest["app_version"],
                  "schema_version": manifest["schema_version"], "bundle_sha256": bundle_sha,
                  "kind": manifest["kind"]}
        conn = store.connect(db)
        try:
            res = actions.perform(conn, "install.restored", {"source": source}, actor=actor, via=via)
            conn.execute("PRAGMA journal_mode=DELETE")       # fold the WAL in: one file to move
        finally:
            conn.close()
        os.link(db, target)          # fails if a database appeared at target meanwhile: never overwritten
        os.remove(db)
        return {"db": target, "manifest": manifest, "install_epoch": res["install_epoch"],
                "paused": res["paused"], "reason": res["reason"]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def stamp(db: str | None = None, actor: str = "cli", via: str = "cli") -> dict[str, Any]:
    """install.stamp on the database at db (default store.DB_PATH): its one-off install_id."""
    conn = store.connect(db)
    try:
        out: dict[str, Any] = actions.perform(conn, "install.stamp", {}, actor=actor, via=via)
        return out
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="The DB backup bundle and its restore (design §11; phase 5c B1).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="write a bundle of the database into DIR")
    c.add_argument("--out", required=True, metavar="DIR")
    c.add_argument("--db", default=None, help="database path (default: $AFCLAUDE_DB or data/afclaude.db)")
    v = sub.add_parser("verify", help="check a bundle as a restore would, writing nothing")
    v.add_argument("bundle")
    r = sub.add_parser("restore", help="restore a bundle as DIR/afclaude.db (DIR must not hold a database)")
    r.add_argument("bundle")
    r.add_argument("--into", required=True, metavar="DIR")
    s = sub.add_parser("stamp", help="give a database that predates the install marker its install_id")
    s.add_argument("--db", default=None, help="database path (default: $AFCLAUDE_DB or data/afclaude.db)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "create":
            out: Any = create(a.out, a.db)
        elif a.cmd == "verify":
            out = verify(a.bundle)
        elif a.cmd == "restore":
            out = restore(a.bundle, a.into)
        else:
            out = stamp(a.db)
    except (BackupError, ValueError, LookupError, store.DBError, sqlite3.Error, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
