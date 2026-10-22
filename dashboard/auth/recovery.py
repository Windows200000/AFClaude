"""Recovery codes (phase 6a W6, docs/phase6a_plan.md §3.6 / §3.7; design §9.3): 10 one-time
codes, each one factor and never a login alone (the factor rule, factors.py).

A code is 16 characters of RFC 4648 base32 (80 bit) shown as four groups of four, like the setup
code (§3.7). Only its HMAC-SHA256 under the install's recovery-code pepper is stored (the pepper
itself encrypted with the master key, §3.6), so a stolen DB without the master key can't be
brute-forced offline. Typed input is forgiving: case, spaces and hyphens don't matter, and 0 / 1
(not in the alphabet) read as O / I.

Pure: `find()` takes the stored rows and says which one matches; marking it used (single use)
is the caller's write. Every stored hash is compared, in constant time, whatever matches."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Optional, Sequence

COUNT = 10
CODE_BYTES = 10              # 80 bit = 16 base32 characters
GROUP = 4
PEPPER_BYTES = 32
_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
_CONFUSABLE = str.maketrans({"0": "O", "1": "I", "-": None, " ": None})


@dataclass(frozen=True)
class StoredCode:
    code_hash: str           # hex HMAC-SHA256 of the normalised code
    used: bool


def new_pepper() -> bytes:
    return secrets.token_bytes(PEPPER_BYTES)


def _display(raw: str) -> str:
    return "-".join(raw[i:i + GROUP] for i in range(0, len(raw), GROUP))


def generate(count: int = COUNT) -> list[str]:
    """`count` distinct new codes in display form (shown once, never stored in clear)."""
    codes: set[str] = set()
    while len(codes) < count:
        codes.add(base64.b32encode(secrets.token_bytes(CODE_BYTES)).decode("ascii"))
    return [_display(c) for c in sorted(codes)]


def normalize(code: str) -> Optional[str]:
    """The code as hashed (16 alphabet characters, upper case), or None if it can't be one."""
    c = code.upper().translate(_CONFUSABLE)
    return c if len(c) == CODE_BYTES * 8 // 5 and set(c) <= _ALPHABET else None


def hash_code(pepper: bytes, code: str) -> str:
    if len(pepper) < PEPPER_BYTES:
        raise ValueError(f"recovery-code pepper has {len(pepper)} bytes, needs {PEPPER_BYTES}")
    c = normalize(code)
    if c is None:
        raise ValueError("not a recovery code")
    return hmac.new(pepper, c.encode("ascii"), hashlib.sha256).hexdigest()


def find(pepper: bytes, code: str, stored: Sequence[StoredCode]) -> Optional[int]:
    """The index in `stored` of the unused code matching `code`, or None (malformed, wrong or
    already used)."""
    c = normalize(code)
    if c is None:
        return None
    h = hash_code(pepper, c).encode("ascii")
    hit: Optional[int] = None
    for i, row in enumerate(stored):
        if hmac.compare_digest(row.code_hash.encode("ascii"), h) and not row.used:
            hit = i
    return hit
