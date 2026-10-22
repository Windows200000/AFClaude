"""The passkey policy (docs/phase6b_plan.md §3.3, §3.4, step P2; D-159, D-150): pure functions over
fido2's types and a verified MDS list (mds.MdsList), no DB, no ceremony state.

  classify()         a registration fido2 has already checked (origin, challenge, RP ID, UP, UV,
                     signature) -> hardware / synced / refused, with a fixed reason word
  check_assertion()  a login or second-factor assertion against the stored passkey: UV, BE/BS,
                     the sign counter, a model revoked since registration, the list's age

D-159: a hardware-bound passkey (BE = 0, attestation verified to an MDS root, the model listed as
hardware) is enough on its own; any other passkey is allowed as one factor only, with the warning
SYNCED_WARNING shown before it is saved. A registration is never classified on an out-of-date list
(§3.2): it is refused, not quietly saved as synced.

fido2 2.2.1 does not check the sign counter, BE/BS, the MDS entry's key protection or its status
beyond REVOKED; that is what this module adds. The attestation chain itself is checked by fido2's
MdsAttestationVerifier (signatures to the root the MDS entry lists).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional, Sequence

import fido2.attestation  # noqa: F401  (registers every attestation format with Attestation.for_type)
from fido2.attestation.base import (Attestation, AttestationType, InvalidAttestation, UnsupportedType,
                                    UntrustedAttestation)
from cryptography import x509
from fido2.mds3 import MdsAttestationVerifier, MetadataBlobPayloadEntry
from fido2.webauthn import Aaguid, AttestationObject, AuthenticatorData

from dashboard.auth import mds

Kind = Literal["hardware", "synced", "refused"]

SYNCED_WARNING = ("This passkey is not hardware-bound (it can be copied to other devices). "
                  "You will still need a second factor when you sign in with it.")

# Every reason word and what the user is told (plain words, D-263). The stored class reason is the word.
MESSAGES: dict[str, str] = {
    # refused at registration
    "no_credential_data": "The security key sent no credential. Try again.",
    "uv_missing": "The passkey did not verify you (PIN or biometrics). Try again and confirm with your PIN or fingerprint.",
    "bs_without_be": "The passkey reported an impossible state (backed up but not backup-eligible) and was not saved.",
    "not_discoverable": "The passkey was created without storing your account on the device, so it can't be used to sign in.",
    "credential_exists": "This passkey is already registered.",
    "mds_unavailable": "The hardware check is not available yet. Try again later; the owner has been alerted.",
    "mds_out_of_date": "The hardware check is out of date, so no new passkey can be added right now. The owner has been alerted.",
    "mds_problem_status": "This security key model has a known security problem (FIDO metadata) and can't be used.",
    "attestation_invalid": "The passkey's attestation is invalid, so it was not saved.",
    # synced (one factor)
    "backup_eligible": "This passkey can be synced to other devices.",
    "attestation_none": "The device did not prove what kind of authenticator it is (the browser's attestation prompt was declined, or the device doesn't offer it).",
    "self_attestation": "The device only vouched for itself, so it can't be checked as hardware.",
    "attestation_unsupported": "The device's attestation format can't be checked.",
    "not_in_mds": "This device model is not listed in the FIDO metadata.",
    "chain_not_verified": "The device's attestation does not chain to its maker's root in the FIDO metadata.",
    "mds_not_hardware": "The FIDO metadata does not list this model as keeping its keys in hardware.",
    # hardware
    "mds_verified": "Hardware-bound: the attestation is verified against the FIDO metadata.",
    # assertion checks
    "ok_hardware": "Hardware-bound passkey.",
    "ok_synced": "Synced passkey: one factor.",
    "be_changed": "This passkey changed its backup eligibility, which a genuine passkey never does. It has been disabled.",
    "counter_not_increased": "This passkey's signature counter went backwards, a sign it may have been copied. It has been disabled.",
    "model_not_usable": "This security key model is no longer accepted (FIDO metadata). It has been disabled; please remove it and add another.",
    "mds_too_old": "The hardware check is out of date, so this sign-in needs a second factor too.",
}


@dataclass(frozen=True)
class Classification:
    kind: Kind
    reason: str
    aaguid: str                       # "00000000-…" when the authenticator sends none
    backup_eligible: bool
    backed_up: bool
    attestation_format: str
    sign_count: int
    mds_serial: Optional[int]
    mds_description: Optional[str]    # the MDS entry's description (the label's default)
    mds_key_id: Optional[str]         # hex attestation key identifier, for a zero-AAGUID model

    @property
    def counts_alone(self) -> bool:
        return self.kind == "hardware"

    @property
    def warning(self) -> Optional[str]:
        return SYNCED_WARNING if self.kind == "synced" else None

    @property
    def message(self) -> str:
        return MESSAGES[self.reason]


def _x5c(attestation: AttestationObject) -> list[bytes]:
    chain = attestation.att_stmt.get("x5c")
    if not chain:
        return []
    if not isinstance(chain, Sequence) or not all(isinstance(c, bytes) for c in chain):
        raise ValueError("x5c is not a list of certificates")
    return list(chain)


def _key_ids(attestation: AttestationObject) -> list[bytes]:
    return [x509.SubjectKeyIdentifier.from_public_key(x509.load_der_x509_certificate(c).public_key()).digest
            for c in _x5c(attestation)]


def _lookup(attestation: AttestationObject, aaguid: Aaguid,
            mds_list: mds.MdsList) -> tuple[Optional[MetadataBlobPayloadEntry], Optional[bytes]]:
    """The MDS entry the authenticator claims: by AAGUID, or (zero AAGUID) by the attestation
    chain's key identifiers; the same rule fido2's verifier uses to pick the root."""
    if aaguid:
        return mds_list.by_aaguid(aaguid), None
    return mds_list.by_chain(_x5c(attestation))


def classify(attestation: AttestationObject, client_data_hash: bytes, mds_list: Optional[mds.MdsList], *,
             now: datetime, cred_props_rk: Optional[bool], credential_known: bool) -> Classification:
    """Classify a registration (§3.3). `cred_props_rk` is the credProps extension's `rk` (None when
    the browser didn't report it); `credential_known` whether the credential id is registered to
    anyone already. fido2's Fido2Server must have checked the ceremony itself (with no attestation
    verifier: the attestation is judged here, where an untrusted one means synced, not an error)."""
    auth_data = attestation.auth_data
    cred = auth_data.credential_data
    fmt = attestation.fmt
    be, bs = auth_data.is_backup_eligible(), auth_data.is_backed_up()

    def result(kind: Kind, reason: str, entry: Optional[MetadataBlobPayloadEntry] = None,
               key_id: Optional[bytes] = None) -> Classification:
        return Classification(kind=kind, reason=reason, aaguid=str(cred.aaguid) if cred else "",
                              backup_eligible=be, backed_up=bs, attestation_format=fmt,
                              sign_count=auth_data.counter, mds_serial=mds_list.serial if mds_list else None,
                              mds_description=mds.describe(entry), mds_key_id=key_id.hex() if key_id else None)

    if cred is None:
        return result("refused", "no_credential_data")
    if not auth_data.is_user_verified():
        return result("refused", "uv_missing")
    if bs and not be:
        return result("refused", "bs_without_be")
    if cred_props_rk is False:
        return result("refused", "not_discoverable")
    if credential_known:
        return result("refused", "credential_exists")
    if mds_list is None:
        return result("refused", "mds_unavailable")
    if mds_list.registration_stale(now):
        return result("refused", "mds_out_of_date")

    try:
        entry, key_id = _lookup(attestation, cred.aaguid, mds_list)
    except ValueError:
        return result("refused", "attestation_invalid")
    if entry is not None and mds.problem_statuses(entry):
        return result("refused", "mds_problem_status", entry, key_id)
    if entry is None:   # a model whose entry fido2 couldn't parse is unknown, but a revoked one stays refused
        try:
            skipped = mds_list.skipped_for(cred.aaguid, _key_ids(attestation))
        except ValueError:
            return result("refused", "attestation_invalid")
        if skipped is not None and skipped.problem_statuses:
            return result("refused", "mds_problem_status")

    if be:
        return result("synced", "backup_eligible", entry, key_id)
    if fmt == "none":
        return result("synced", "attestation_none", entry, key_id)

    try:
        statement = Attestation.for_type(fmt)().verify(attestation.att_stmt, auth_data, client_data_hash)
    except (UnsupportedType, NotImplementedError):
        return result("synced", "attestation_unsupported", entry, key_id)
    except InvalidAttestation:
        return result("refused", "attestation_invalid", entry, key_id)
    if statement.attestation_type in (AttestationType.SELF, AttestationType.NONE):
        return result("synced", "self_attestation", entry, key_id)

    if entry is None:
        return result("synced", "not_in_mds")
    usable, _why = mds.hardware_usable(entry)
    if not usable:
        return result("synced", "mds_not_hardware", entry, key_id)

    verifier = MdsAttestationVerifier(mds_list.payload, entry_filter=None, attestation_filter=None)
    try:
        verifier.verify_attestation(attestation, client_data_hash)
    except UntrustedAttestation:
        return result("synced", "chain_not_verified", entry, key_id)
    except InvalidAttestation:
        return result("refused", "attestation_invalid", entry, key_id)
    return result("hardware", "mds_verified", entry, key_id)


# --- assertions ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class StoredPasskey:
    """What check_assertion needs of a stored passkey (the passkeys row, §3.3)."""
    kind: Literal["hardware", "synced"]
    backup_eligible: bool
    sign_count: int
    aaguid: str
    mds_key_id: Optional[str] = None


@dataclass(frozen=True)
class AssertionCheck:
    ok: bool
    reason: str
    counts_as: Optional[Literal["hardware", "one_factor"]]   # None when refused
    sign_count: int           # the new counter to store (ok only)
    backed_up: bool           # the BS flag to store as "last BS"
    disable: bool             # refused and the passkey is to be disabled
    alert: bool               # refused and the owners are to be alerted

    @property
    def message(self) -> str:
        return MESSAGES[self.reason]


def _refused(reason: str, auth_data: AuthenticatorData, *, disable: bool = False, alert: bool = False) -> AssertionCheck:
    return AssertionCheck(ok=False, reason=reason, counts_as=None, sign_count=auth_data.counter,
                          backed_up=auth_data.is_backed_up(), disable=disable, alert=alert)


def check_assertion(stored: StoredPasskey, auth_data: AuthenticatorData, mds_list: Optional[mds.MdsList], *,
                    now: datetime, max_age_days: int = mds.MAX_AGE_DEFAULT_DAYS) -> AssertionCheck:
    """The checks fido2's authenticate_complete doesn't do (§3.4). Run after fido2 has verified the
    signature, challenge, origin, RP ID and UP."""
    be, bs, counter = auth_data.is_backup_eligible(), auth_data.is_backed_up(), auth_data.counter
    if not auth_data.is_user_verified():
        return _refused("uv_missing", auth_data)
    if bs and not be:
        return _refused("bs_without_be", auth_data, alert=True)
    if be != stored.backup_eligible:
        return _refused("be_changed", auth_data, disable=True, alert=True)
    if (stored.sign_count or counter) and counter <= stored.sign_count:
        return _refused("counter_not_increased", auth_data, disable=True, alert=True)

    def ok(reason: str, counts_as: Literal["hardware", "one_factor"]) -> AssertionCheck:
        return AssertionCheck(ok=True, reason=reason, counts_as=counts_as, sign_count=counter,
                              backed_up=bs, disable=False, alert=False)

    if stored.kind == "synced":
        return ok("ok_synced", "one_factor")
    if mds_list is None:
        return ok("mds_too_old", "one_factor")
    entry = _stored_entry(stored, mds_list)
    if entry is None or not mds.hardware_usable(entry)[0]:
        return _refused("model_not_usable", auth_data, disable=True, alert=True)
    if mds_list.too_old_for_hardware_login(now, max_age_days):
        return ok("mds_too_old", "one_factor")
    return ok("ok_hardware", "hardware")


def _stored_entry(stored: StoredPasskey, mds_list: mds.MdsList) -> Optional[MetadataBlobPayloadEntry]:
    aaguid = Aaguid.parse(stored.aaguid)
    if aaguid:
        return mds_list.by_aaguid(aaguid)
    if stored.mds_key_id:
        return mds_list.by_key_id(bytes.fromhex(stored.mds_key_id))
    return None
