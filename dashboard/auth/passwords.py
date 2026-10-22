"""Password hashing and the password rule (phase 6a W6, docs/phase6a_plan.md §0 / §3.6; design
§9.3; D-150: password login, always with a second factor).

Argon2id from `cryptography` (D-007: already in the tree through mcp; argon2-cffi not needed),
stored as a PHC string that carries its own parameters, so `PARAMS` can be raised later:
`needs_rehash()` says when a stored hash was made with other parameters, and the login rehashes
it with the password it just verified. `FLOOR` (RFC 9106's second recommended set: 64 MiB, t=3,
p=4) is the weakest hash this code ever wrote; a stored hash below it, or one that isn't an
Argon2id PHC string, is a damaged or tampered row: `StoredHashError`, never a quiet "wrong
password" (D-235).

The rule (NIST SP 800-63B): 12–128 characters after NFKC normalisation, no composition rules, no
breach list (the plan asks for none). Normalising first means the same password typed on another
keyboard or OS still matches.

Pure: no tables, no I/O. A login for an unknown username calls `verify_dummy()` so it costs the
same time as a wrong password (§3.6)."""
from __future__ import annotations

import base64
import binascii
import functools
import re
import secrets
import unicodedata
from dataclasses import dataclass

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

MIN_LENGTH = 12
MAX_LENGTH = 128
SALT_BYTES = 16
HASH_BYTES = 32
ARGON2_VERSION = 19


@dataclass(frozen=True, order=True)
class Params:
    """Argon2id cost: memory in KiB, passes, lanes."""
    memory_kib: int
    iterations: int
    lanes: int

    def at_least(self, other: Params) -> bool:
        return (self.memory_kib >= other.memory_kib and self.iterations >= other.iterations
                and self.lanes >= other.lanes)


FLOOR = Params(memory_kib=64 * 1024, iterations=3, lanes=4)
PARAMS = FLOOR   # what new hashes use; raise it here (never below FLOOR), old hashes rehash at login


class PasswordRuleError(ValueError):
    """The password breaks the rule; `code` is `too_short` or `too_long` (the API's message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StoredHashError(ValueError):
    """A stored hash that this code would never have written (malformed, another algorithm or
    version, parameters below FLOOR): a data error, not a failed login."""


_PHC = re.compile(r"\$argon2id\$v=(\d+)\$m=(\d+),t=(\d+),p=(\d+)\$([A-Za-z0-9+/]+)\$([A-Za-z0-9+/]+)")


@dataclass(frozen=True)
class StoredHash:
    params: Params
    salt_bytes: int
    hash_bytes: int


def normalize(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def check_rule(password: str) -> None:
    """Raise PasswordRuleError unless the (normalised) password has 12–128 characters."""
    n = len(normalize(password))
    if n < MIN_LENGTH:
        raise PasswordRuleError("too_short", f"The password needs at least {MIN_LENGTH} characters.")
    if n > MAX_LENGTH:
        raise PasswordRuleError("too_long", f"The password can have at most {MAX_LENGTH} characters.")


def _b64len(text: str) -> int:
    try:
        return len(base64.b64decode(text + "=" * (-len(text) % 4), validate=True))
    except (binascii.Error, ValueError) as e:
        raise StoredHashError("stored password hash: bad base64") from e


def parse(phc: str) -> StoredHash:
    """The parameters of a stored PHC string; StoredHashError if it isn't one of ours."""
    m = _PHC.fullmatch(phc)
    if m is None:
        raise StoredHashError("stored password hash is not an Argon2id PHC string")
    version, mem, t, p = (int(g) for g in m.group(1, 2, 3, 4))
    if version != ARGON2_VERSION:
        raise StoredHashError(f"stored password hash: Argon2 version {version}, expected {ARGON2_VERSION}")
    stored = StoredHash(Params(mem, t, p), _b64len(m.group(5)), _b64len(m.group(6)))
    if not stored.params.at_least(FLOOR) or stored.salt_bytes < SALT_BYTES or stored.hash_bytes < HASH_BYTES:
        raise StoredHashError("stored password hash is weaker than the floor (m=64 MiB, t=3, p=4, "
                              "16-byte salt, 32-byte hash)")
    return stored


def hash_password(password: str, params: Params = PARAMS) -> str:
    """A new PHC string for a password that passes the rule (PasswordRuleError otherwise)."""
    check_rule(password)
    if not params.at_least(FLOOR):
        raise ValueError(f"Argon2id parameters {params} are below the floor {FLOOR}")
    kdf = Argon2id(salt=secrets.token_bytes(SALT_BYTES), length=HASH_BYTES,
                   iterations=params.iterations, lanes=params.lanes, memory_cost=params.memory_kib)
    return kdf.derive_phc_encoded(normalize(password).encode("utf-8"))


def verify_password(password: str, phc: str) -> bool:
    """True when the password matches the stored hash. The stored hash is checked first
    (StoredHashError for a malformed or weak one); the comparison is cryptography's. A password
    outside the rule is checked like any other (the rule is for setting one, and an old password
    must keep working if the rule changes)."""
    parse(phc)
    try:
        Argon2id.verify_phc_encoded(normalize(password).encode("utf-8"), phc)
    except InvalidKey:
        return False
    return True


def needs_rehash(phc: str, params: Params = PARAMS) -> bool:
    """True when a (valid) stored hash was made with other parameters or sizes than new ones get."""
    stored = parse(phc)
    return (stored.params != params or stored.salt_bytes != SALT_BYTES
            or stored.hash_bytes != HASH_BYTES)


@functools.cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(24))


def verify_dummy(password: str) -> bool:
    """Verify against a throwaway hash with the current parameters (always False): the login's
    path for an unknown username, so it takes as long as a wrong password."""
    verify_password(password, _dummy_hash())
    return False
