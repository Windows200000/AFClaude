#!/usr/bin/env python3
"""The master key and the envelope helper (docs/phase5b_plan.md §0, §3.5, design §10.2; D-163, D-161).

One 32-byte key per install protects the secrets the DB will hold sealed (TOTP seeds, the OIDC secret:
phases 6a/6b; nothing calls this in 5b before K4 wires the services' start). It comes from
`AFCLAUDE_MASTER_KEY` (base64; an empty value counts as unset, so a compose `${VAR:-}` works) or, when
that is unset, from `<data>/master.key`, generated once (mode 0600). The key lives next to the DB, so
it protects a DB copy that leaves without it; losing the key file loses every sealed secret (5c B2
wraps it into every backup bundle).

  ensure(data_dir, conn)   at every service start: finds or creates the key, checks it against
                           meta.master_key_check (written on first use through the audited action
                           masterkey.check) and keeps it for seal/open -> the key id
  seal(purpose, plaintext) -> "v1:<key-id>:<nonce>:<ciphertext>" (AES-256-GCM, a per-purpose key from
                           HKDF-SHA256, the purpose also as the authenticated data)
  open(purpose, blob)      the plaintext, or MasterKeyError

Fail fast (D-235, D-239): every key problem raises MasterKeyError (a missing or unmounted data dir, an
unreadable or malformed key, env and file disagreeing, a key that doesn't match the stored check, a
tampered or foreign blob): never a new key beside an existing check, never a silent fallback. In K3 it
only raises; the services that call it alert and pause through failstop.fail (component `masterkey`).
`ensure` is race-safe: the key file is created under an flock with O_EXCL, so concurrent services
(web, runner, MCP) end up with one key.
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import os
import re
import sqlite3
import sys
import threading
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import actions  # noqa: E402
import store  # noqa: E402

ENV: Final = "AFCLAUDE_MASTER_KEY"
KEY_FILE: Final = "master.key"
LOCK_FILE: Final = "master.key.lock"
CHECK_META: Final = "master_key_check"
KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12
VERSION: Final = "v1"
CHECK_LABEL: Final = b"afclaude master key check v1"
PURPOSE_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
ACTOR: Final = "runner:masterkey"


class MasterKeyError(Exception):
    """A key problem: raised, never worked around (D-235)."""


_lock = threading.Lock()
_key: bytes | None = None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_key(text: str, where: str) -> bytes:
    """A key as stored: base64 (standard or URL-safe alphabet) of exactly 32 bytes."""
    s = text.strip()
    try:
        raw = base64.b64decode(s.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError) as e:
        raise MasterKeyError(f"master key in {where} is not valid base64: {e}") from None
    if len(raw) != KEY_BYTES:
        raise MasterKeyError(f"master key in {where} must be {KEY_BYTES} bytes (base64 of 32 random bytes), "
                             f"got {len(raw)}")
    return raw


def _derive(key: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=info).derive(key)


def key_id(key: bytes) -> str:
    """A short public name of the key (in every sealed blob): derived, so it reveals nothing of the key."""
    return _derive(key, b"afclaude key id").hex()[:12]


def check_value(key: bytes) -> str:
    """The value stored as meta.master_key_check: an HMAC of a fixed label."""
    return hmac.new(_derive(key, b"afclaude key check"), CHECK_LABEL, hashlib.sha256).hexdigest()


def _read_file(path: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        raise MasterKeyError(f"cannot read the master key file {path}: {e}") from None
    with os.fdopen(fd, "r", encoding="ascii", errors="replace") as fh:
        text = fh.read()
    return _decode_key(text, path)


def _file_key(data_dir: str) -> bytes | None:
    """The key file's key, or None when there is no file (then the caller decides; a present but empty
    or malformed file is an error, never regenerated)."""
    path = os.path.join(data_dir, KEY_FILE)
    return _read_file(path) if os.path.exists(path) else None


def _create_file(data_dir: str) -> bytes:
    """Generate the key file: O_EXCL, 0600, fsynced (the caller holds the lock, so no one reads half)."""
    raw = os.urandom(KEY_BYTES)
    fd = os.open(os.path.join(data_dir, KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(_b64(raw) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    dfd = os.open(data_dir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return raw


def find_key(data_dir: str) -> bytes:
    """The install's key: the env value, else the file, generated once. Every file access is under an
    flock on a lock file, so concurrent services (web, runner, MCP) end up with one key and none reads a
    half-written file. Both env and file set and different is an error. A set env value with no file
    creates no file."""
    if not os.path.isdir(data_dir):
        raise MasterKeyError(f"data directory {data_dir} does not exist: not mounted?")
    env = os.environ.get(ENV, "")
    lock_fd = os.open(os.path.join(data_dir, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        from_file = _file_key(data_dir)
        if env.strip():
            from_env = _decode_key(env, f"${ENV}")
            if from_file is not None and not hmac.compare_digest(from_env, from_file):
                raise MasterKeyError(f"${ENV} and {os.path.join(data_dir, KEY_FILE)} hold different keys: remove "
                                     "the one that is wrong (the DB's sealed values belong to only one of them)")
            return from_env
        return from_file if from_file is not None else _create_file(data_dir)
    finally:
        os.close(lock_fd)   # releases the flock


def ensure(data_dir: str | os.PathLike[str], conn: sqlite3.Connection) -> str:
    """Find or create the key, check it against meta.master_key_check (first use writes it) and keep it
    for seal/open. -> the key id. Raises MasterKeyError on any problem."""
    global _key
    key = find_key(os.fspath(data_dir))
    value = check_value(key)
    have = store.get_meta(conn, CHECK_META)
    if have is not None and not hmac.compare_digest(have, value):
        raise MasterKeyError(
            f"the master key (id {key_id(key)}) does not match this database's master_key_check: a DB "
            f"restored without its key, or a wrong ${ENV}/{KEY_FILE}; refusing to start (no new key is made)")
    if have is None:
        actions.perform(conn, "masterkey.check", {"value": value, "key_id": key_id(key)}, actor=ACTOR, via="runner")
    with _lock:
        _key = key
    return key_id(key)


def _current() -> bytes:
    with _lock:
        if _key is None:
            raise MasterKeyError("the master key is not loaded: masterkey.ensure() has not run in this process")
        return _key


def _purpose_key(key: bytes, purpose: str) -> bytes:
    if not PURPOSE_RE.match(purpose):
        raise MasterKeyError(f"bad purpose {purpose!r}: lower-case letters, digits, . _ -")
    return _derive(key, b"afclaude seal " + purpose.encode("ascii"))


def seal(purpose: str, plaintext: bytes) -> str:
    """Encrypt `plaintext` for `purpose` -> "v1:<key-id>:<nonce>:<ciphertext>" (base64url parts)."""
    key = _current()
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(_purpose_key(key, purpose)).encrypt(nonce, plaintext, purpose.encode("ascii"))
    return f"{VERSION}:{key_id(key)}:{_b64(nonce)}:{_b64(ct)}"


def open(purpose: str, blob: str) -> bytes:  # noqa: A001  (the plan's name; the module uses os.open only)
    """Decrypt a sealed blob for `purpose`; a malformed, tampered, foreign-key or wrong-purpose blob raises."""
    key = _current()
    parts = blob.split(":")
    if len(parts) != 4 or parts[0] != VERSION:
        raise MasterKeyError(f"not a sealed {VERSION} blob")
    _, kid, n, c = parts
    if kid != key_id(key):
        raise MasterKeyError(f"sealed with another master key (id {kid}, this install's is {key_id(key)})")
    try:
        nonce, ct = base64.urlsafe_b64decode(n), base64.urlsafe_b64decode(c)
        if len(nonce) != NONCE_BYTES:
            raise ValueError("bad nonce length")
        return AESGCM(_purpose_key(key, purpose)).decrypt(nonce, ct, purpose.encode("ascii"))
    except (InvalidTag, ValueError, binascii.Error):
        raise MasterKeyError("the sealed blob does not open: tampered, damaged or sealed for another purpose") from None


def forget() -> None:
    """Drop the key kept in this process (tests)."""
    global _key
    with _lock:
        _key = None
