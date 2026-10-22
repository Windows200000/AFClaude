"""The factor rule (phase 6a W6, docs/phase6a_plan.md §3.6; design §9.3 / §9.5; D-150, D-159,
D-179): when the factors a session has done make a complete login.

A login is complete with (a) OIDC, (b) a hardware passkey with user verification, or (c) two
different factors from {password, TOTP, synced passkey, recovery code}. 6b's factors
(docs/phase6b_plan.md §3.3, §3.4): `passkey_hw` (passkeys.check_assertion counted it "hardware":
hardware-bound, UV, a current MDS list) and `oidc` complete it alone; `passkey_synced` (a synced
passkey, or a hardware one whose MDS list is too old) is one factor of (c), and two synced passkeys
of the same user count once (the same name). A remembered device (§9.5, default 7 days, D-179)
stands in for the second factor after a first one (a password or a synced passkey, 6b §0); it
never stands in for the first factor and never counts for step-up.

Pure: the session's factors come in as their names (as stored in the session row); a name this
code doesn't know is a data error (ValueError), never ignored."""
from __future__ import annotations

from typing import Iterable, Literal, get_args

Factor = Literal["password", "totp", "recovery_code", "passkey_hw", "passkey_synced", "oidc"]
FACTORS: frozenset[str] = frozenset(get_args(Factor))

ALONE: frozenset[Factor] = frozenset({"oidc", "passkey_hw"})          # (a), (b)
PAIRABLE: frozenset[Factor] = frozenset({"password", "totp", "recovery_code", "passkey_synced"})   # (c)
REMEMBERED_AFTER: frozenset[Factor] = frozenset({"password", "passkey_synced"})   # first factors a remembered device completes


def _checked(factors: Iterable[str]) -> frozenset[str]:
    done = frozenset(factors)
    unknown = done - FACTORS
    if unknown:
        raise ValueError(f"unknown login factor(s): {', '.join(sorted(unknown))}")
    return done


def is_complete(factors: Iterable[str], *, remembered_device: bool = False,
                step_up: bool = False) -> bool:
    """True when `factors` (the distinct factors done in this login or step-up) complete it.
    `remembered_device`: the request carries a valid remembered-device token for this user;
    ignored for a step-up."""
    done = _checked(factors)
    if done & ALONE or len(done & PAIRABLE) >= 2:
        return True
    return remembered_device and not step_up and bool(done & REMEMBERED_AFTER)
