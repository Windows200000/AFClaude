"""TOTP, RFC 6238 (phase 6a W6, docs/phase6a_plan.md §0 / §3.6; design §9.3; D-150: the second
factor of a password login).

`cryptography`'s TOTP (D-007: already in the tree; pyotp not needed) with the plan's settings:
160-bit seed, SHA-1, 6 digits, 30-second steps, one step of clock drift either way. Replay rule
(RFC 6238 §5.2: a code must not be accepted twice): the caller stores the step of the last
accepted code, and `verify()` accepts only a later step, so neither the same code nor an older
one still inside the window works after a successful login.

Pure: the seed, the time and the last used step come in; the step to store goes out. Storing
the seed (encrypted with the master key, §3.6) and the step is the caller's (the `credentials` table, W5)."""
from __future__ import annotations

import hmac
import secrets
from typing import Optional

from cryptography.hazmat.primitives.hashes import SHA1
from cryptography.hazmat.primitives.twofactor.totp import TOTP

SEED_BYTES = 20   # 160 bit (RFC 4226 §4's recommended length)
DIGITS = 6
STEP_SECONDS = 30
WINDOW = 1        # steps accepted before and after the current one
ISSUER = "AFClaude"


def new_seed() -> bytes:
    return secrets.token_bytes(SEED_BYTES)


def _totp(seed: bytes, digits: int) -> TOTP:
    if len(seed) < SEED_BYTES:
        raise ValueError(f"TOTP seed has {len(seed)} bytes, needs {SEED_BYTES}")
    return TOTP(seed, digits, SHA1(), STEP_SECONDS)


def step_at(now: float) -> int:
    """The time step of a Unix time (RFC 6238 §4.2, T0 = 0)."""
    return int(now // STEP_SECONDS)


def code_at(seed: bytes, step: int, digits: int = DIGITS) -> str:
    return _totp(seed, digits).generate(step * STEP_SECONDS).decode("ascii")


def normalize_code(code: str) -> Optional[str]:
    """The code without spaces if it is DIGITS ASCII digits, else None."""
    c = code.replace(" ", "")
    return c if len(c) == DIGITS and c.isascii() and c.isdigit() else None


def verify(seed: bytes, code: str, now: float, last_step: Optional[int]) -> Optional[int]:
    """The step the code belongs to (to store as the new last step), or None: malformed, wrong,
    outside now ± WINDOW steps, or not after `last_step` (a replay). Every candidate step is
    compared in constant time."""
    c = normalize_code(code)
    if c is None:
        return None
    current = step_at(now)
    matched: Optional[int] = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        ok = hmac.compare_digest(code_at(seed, step).encode("ascii"), c.encode("ascii"))
        if ok and (last_step is None or step > last_step):
            matched = step
    return matched


def provisioning_uri(seed: bytes, account: str, issuer: str = ISSUER) -> str:
    """The `otpauth://totp/...` URI for an authenticator app (shown as text and as a QR code at
    enrolment)."""
    return _totp(seed, DIGITS).get_provisioning_uri(account, issuer)
