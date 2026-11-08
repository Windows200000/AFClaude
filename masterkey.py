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
  seal(purpose, plaintext, context=None) -> "v1:<key-id>:<nonce>:<ciphertext>" (AES-256-GCM, a per-purpose
                           key from HKDF-SHA256, the purpose and the optional context (a record's id)
                           as the authenticated data: a blob can't be moved to another record)
  open(purpose, blob, context=None)   the plaintext, or MasterKeyError

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
    """The key file's key; a file group- or world-accessible is refused (the key must stay private)."""
    try:
        fd = os.open(path, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="ascii", errors="replace") as fh:
            mode = os.fstat(fh.fileno()).st_mode
            if mode & 0o077:
                raise MasterKeyError(f"the master key file {path} is accessible to others (mode "
                                     f"{mode & 0o777:03o}): chmod 600 it (and treat the key as exposed if it "
                                     "was ever shared)")
            text = fh.read()
    except OSError as e:
        raise MasterKeyError(f"cannot read the master key file {path}: {e}") from None
    return _decode_key(text, path)


def _file_key(data_dir: str) -> bytes | None:
    """The key file's key, or None when there is no file (then the caller decides; a present but empty
    or malformed file is an error, never regenerated)."""
    path = os.path.join(data_dir, KEY_FILE)
    return _read_file(path) if os.path.exists(path) else None


def _create_file(data_dir: str) -> bytes:
    """Generate the key file: O_EXCL, 0600, fsynced (the caller holds the lock, so no one reads half)."""
    raw = os.urandom(KEY_BYTES)
    path = os.path.join(data_dir, KEY_FILE)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(_b64(raw) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        dfd = os.open(data_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as e:
        raise MasterKeyError(f"cannot write the master key file {path}: {e}") from None
    return raw


def find_key(data_dir: str, check: str | None = None) -> bytes:
    """The install's key: the env value, else the file, generated once. Every file access is under an
    flock on a lock file, so concurrent services (web, runner, MCP) end up with one key and none reads a
    half-written file. Both env and file set and different is an error. A set env value with no file
    creates no file. `check`: the DB's master_key_check, when it has one: then a key that exists
    already (the DB sealed something under it), so with neither env nor file nothing is generated
    (a new key beside that check could never match, and a wrong file would stay behind): an error."""
    if not os.path.isdir(data_dir):
        raise MasterKeyError(f"data directory {data_dir} does not exist: not mounted?")
    env = os.environ.get(ENV, "")
    lock_path = os.path.join(data_dir, LOCK_FILE)
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise MasterKeyError(f"cannot open the master key lock file {lock_path}: {e}") from None
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        from_file = _file_key(data_dir)
        if env.strip():
            from_env = _decode_key(env, f"${ENV}")
            if from_file is not None and not hmac.compare_digest(from_env, from_file):
                raise MasterKeyError(f"${ENV} and {os.path.join(data_dir, KEY_FILE)} hold different keys: remove "
                                     "the one that is wrong (the DB's sealed values belong to only one of them)")
            return from_env
        if from_file is not None:
            return from_file
        if check is not None:
            raise MasterKeyError(
                f"this database has a master_key_check but no key is available (${ENV} unset, "
                f"{os.path.join(data_dir, KEY_FILE)} missing): a DB restored without its key; put the key back, "
                "no new one is made")
        return _create_file(data_dir)
    finally:
        os.close(lock_fd)   # releases the flock


def ensure(data_dir: str | os.PathLike[str], conn: sqlite3.Connection) -> str:
    """Find or create the key, check it against meta.master_key_check (first use writes it) and keep it
    for seal/open. -> the key id. Raises MasterKeyError on any problem."""
    global _key
    have = store.get_meta(conn, CHECK_META)
    key = find_key(os.fspath(data_dir), have)
    value = check_value(key)
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
    if not PURPOSE_RE.fullmatch(purpose):
        raise MasterKeyError(f"bad purpose {purpose!r}: lower-case letters, digits, . _ -")
    return _derive(key, b"afclaude seal " + purpose.encode("ascii"))


def _aad(purpose: str, context: str | None) -> bytes:
    """The authenticated data: the purpose, plus the context (a record's id) when there is one, so a blob
    can't be moved to another record or purpose. No context and an empty context differ."""
    if context is None:
        return purpose.encode("ascii")
    return purpose.encode("ascii") + b"\0" + context.encode("utf-8")


def _unb64(text: str) -> bytes:
    """Strict base64url: only the alphabet, padded, and exactly what _b64 writes."""
    raw = base64.b64decode(text, altchars=b"-_", validate=True)
    if _b64(raw) != text:
        raise ValueError("not canonical base64url")
    return raw


def seal(purpose: str, plaintext: bytes, context: str | None = None) -> str:
    """Encrypt `plaintext` for `purpose` -> "v1:<key-id>:<nonce>:<ciphertext>" (base64url parts).
    `context` (e.g. the row or user id) is bound in: open needs the same one (none = none)."""
    key = _current()
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(_purpose_key(key, purpose)).encrypt(nonce, plaintext, _aad(purpose, context))
    return f"{VERSION}:{key_id(key)}:{_b64(nonce)}:{_b64(ct)}"


def open(purpose: str, blob: str, context: str | None = None) -> bytes:  # noqa: A001  (the plan's name; the module uses os.open only)
    """Decrypt a sealed blob for `purpose` and `context`; a malformed, tampered, foreign-key, wrong-purpose
    or wrong-context blob raises."""
    key = _current()
    parts = blob.split(":")
    if len(parts) != 4 or parts[0] != VERSION:
        raise MasterKeyError(f"not a sealed {VERSION} blob")
    _, kid, n, c = parts
    if kid != key_id(key):
        raise MasterKeyError(f"sealed with another master key (id {kid}, this install's is {key_id(key)})")
    pkey = _purpose_key(key, purpose)
    try:
        nonce, ct = _unb64(n), _unb64(c)
        if len(nonce) != NONCE_BYTES:
            raise ValueError("bad nonce length")
        return AESGCM(pkey).decrypt(nonce, ct, _aad(purpose, context))
    except (InvalidTag, ValueError, binascii.Error):
        raise MasterKeyError("the sealed blob does not open: tampered, damaged, or sealed for another purpose "
                             "or context") from None


def forget() -> None:
    """Drop the key kept in this process (tests)."""
    global _key
    with _lock:
        _key = None
