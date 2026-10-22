"""The FIDO Metadata Service (MDS3) list (docs/phase6b_plan.md §0, §3.2, step P1; D-150, D-159):
fetch the signed blob, verify it, turn it into the record the cache stores, and answer which
authenticator models count as hardware-bound.

  fetch()        one GET of the blob (30 s in total, D-234; no redirects; size cap)
  verify_blob()  the JWS: the x5c chain is path-validated with `cryptography` to the pinned
                 GlobalSign Root R46 (fido_mds_root_r46.der, its SHA-256 below), with validity
                 dates, as a TLS server certificate for the MDS host; then the signature with the
                 leaf key (ES256 / RS256 only); then the payload through fido2's data model
  check_serial() the blob's serial against the stored one: lower = a rollback (refused), equal =
                 nothing new, higher = store it
  refresh()      the three together; any failure alerts and raises (D-235), nothing is returned
  MdsList        a verified list (from the cache record): look-ups, the two stale rules
  problem_statuses() / hardware_usable()  which entries refuse a passkey and which count as hardware

fido2's own parse_blob() is not used: it checks the chain strictly in x5c order with the root
appended, which fails since FIDO's move to Root R46 (the x5c also carries an R46-by-R3
cross-certificate), and it checks neither dates nor the certificate's name. Nothing here touches
the DB (the mds_cache table and its action come after the 3a merge, §4); refresh() takes the
stored serial and the last fetch time and returns the record to store.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import time
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal, Optional, Sequence

import httpx2
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509 import DNSName
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
from fido2.mds3 import AuthenticatorStatus, MetadataBlobPayload, MetadataBlobPayloadEntry
from fido2.webauthn import Aaguid

HERE = os.path.dirname(os.path.abspath(__file__))

MDS_URL = "https://mds.fidoalliance.org/"   # mds3.fidoalliance.org answers 301 to this one
MDS_HOST = "mds.fidoalliance.org"           # the name the signing certificate must carry
FETCH_TIMEOUT_S = 30.0                      # the whole GET, not per read (D-234)
FETCH_MAX_BYTES = 64 * 1024 * 1024          # the blob is ~10 MB (October 2026)
MIN_FETCH_INTERVAL = timedelta(hours=1)     # FIDO's rate limit
STALE_GRACE = timedelta(days=7)             # registrations refused past nextUpdate + this
MAX_AGE_DEFAULT_DAYS, MAX_AGE_MIN_DAYS, MAX_AGE_MAX_DAYS = 30, 7, 90   # auth_mds_max_age_days

ROOT_PATH = os.path.join(HERE, "fido_mds_root_r46.der")
ROOT_SHA256 = "4fa3126d8d3a11d1c4855a4f807cbad6cf919d3a5a88b03bea2c6372d93c40c9"   # GlobalSign Root R46
ALGS = ("ES256", "RS256")

# Any of these in an entry's reports: a passkey of that model is refused (fido2's default filter
# drops only REVOKED).
PROBLEM_STATUSES = frozenset({
    AuthenticatorStatus.REVOKED, AuthenticatorStatus.USER_VERIFICATION_BYPASS,
    AuthenticatorStatus.ATTESTATION_KEY_COMPROMISE, AuthenticatorStatus.USER_KEY_REMOTE_COMPROMISE,
    AuthenticatorStatus.USER_KEY_PHYSICAL_COMPROMISE})
# The newest report must be one of these for the entry to count as hardware.
USABLE_NEWEST = frozenset(
    {s for s in AuthenticatorStatus if s.name.startswith(("FIDO_CERTIFIED", "FIPS140_CERTIFIED"))}
    | {AuthenticatorStatus.UPDATE_AVAILABLE, AuthenticatorStatus.NOT_FIDO_CERTIFIED})
# D-150 "physical or stored in a secure part of a device": keyProtection only from these.
HARDWARE_PROTECTION = frozenset({"hardware", "secure_element", "tee"})
# MDS 3.1 marks passkey providers whose credentials sync; never hardware, whatever keyProtection says.
SYNCING = frozenset({"explicit", "implicit"})
# MDS 3.1 statements of passkey providers may leave these out; fido2 2.2.1 still requires them,
# and the policy here reads neither. Only these two get an empty default; every other parse error
# refuses the whole blob.
STATEMENT_LIST_DEFAULTS = ("userVerificationDetails", "attachmentHint")

AlertFn = Callable[[str, str], object]
ALERT_SUBJECT = "FIDO metadata refresh failed"   # stable (failstop: no numbers in the subject)


class MdsError(Exception):
    """The blob could not be fetched or did not verify; `code` is a fixed word for logs and tests."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class MdsRollback(MdsError):
    """The fetched blob is older than the stored one."""


class MdsTooSoon(MdsError):
    """A fetch within an hour of the last one (FIDO's rate limit); not a failure, no alert."""


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime (use UTC-aware times)")
    return dt.astimezone(timezone.utc)


# --- the pinned root -----------------------------------------------------------------------------

def pinned_root(path: str = ROOT_PATH, sha256: str = ROOT_SHA256) -> x509.Certificate:
    with open(path, "rb") as fh:
        der = fh.read()
    got = hashlib.sha256(der).hexdigest()
    if got != sha256:
        raise MdsError("root_mismatch", f"{os.path.basename(path)} has SHA-256 {got}, expected {sha256}")
    return x509.load_der_x509_certificate(der)


# --- fetch ---------------------------------------------------------------------------------------

def may_fetch(last_fetched_at: Optional[datetime], now: datetime) -> bool:
    return last_fetched_at is None or utc(now) - utc(last_fetched_at) >= MIN_FETCH_INTERVAL


def fetch(url: str = MDS_URL, *, timeout: float = FETCH_TIMEOUT_S, max_bytes: int = FETCH_MAX_BYTES,
          transport: Optional[httpx2.BaseTransport] = None) -> bytes:
    """One GET of the blob. A timeout (of any phase or of the whole GET), a redirect, a status
    other than 200 or a body over `max_bytes` raises MdsError."""
    deadline = time.monotonic() + timeout
    body = bytearray()
    try:
        with httpx2.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
            with client.stream("GET", url, headers={"Accept": "application/jwt, */*"}) as resp:
                if resp.status_code != 200:
                    raise MdsError("http_status", f"GET {url}: HTTP {resp.status_code}")
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise MdsError("too_large", f"GET {url}: more than {max_bytes} bytes")
                    if time.monotonic() > deadline:
                        raise MdsError("timeout", f"GET {url}: not done after {timeout:.0f} s")
    except httpx2.TimeoutException as exc:
        raise MdsError("timeout", f"GET {url}: {type(exc).__name__} after {timeout:.0f} s") from exc
    except httpx2.HTTPError as exc:
        raise MdsError("fetch", f"GET {url}: {type(exc).__name__}: {exc}") from exc
    return bytes(body)


# --- verify --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class VerifiedBlob:
    serial: int
    next_update: date
    sha256: str              # of the whole blob as fetched
    payload_json: bytes      # the JWS payload, verified
    payload: MetadataBlobPayload
    chain: tuple[str, ...]   # the validated path, leaf first (subjects, for the log)


def _b64url(part: bytes, what: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(part + b"=" * (-len(part) % 4))
    except (binascii.Error, ValueError) as exc:
        raise MdsError("malformed", f"{what} is not base64url") from exc


def parse_payload(payload_json: bytes) -> MetadataBlobPayload:
    try:
        data = json.loads(payload_json)
    except ValueError as exc:
        raise MdsError("payload_malformed", f"payload is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise MdsError("payload_malformed", "payload has no entries list")
    for entry in data["entries"]:
        statement = entry.get("metadataStatement") if isinstance(entry, dict) else None
        if isinstance(statement, dict):
            for key in STATEMENT_LIST_DEFAULTS:
                statement.setdefault(key, [])
    try:
        payload = MetadataBlobPayload.from_dict(data)
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise MdsError("payload_malformed", f"{type(exc).__name__}: {exc}") from exc
    return payload


def _verify_signature(leaf: x509.Certificate, alg: str, signed: bytes, signature: bytes) -> None:
    key = leaf.public_key()
    try:
        if alg == "ES256":
            if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
                raise MdsError("bad_key", "ES256 needs a P-256 leaf key")
            if len(signature) != 64:
                raise MdsError("bad_signature", "ES256 signature is not 64 bytes")
            der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
            key.verify(der, signed, ec.ECDSA(hashes.SHA256()))
        else:
            if not isinstance(key, rsa.RSAPublicKey):
                raise MdsError("bad_key", "RS256 needs an RSA leaf key")
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise MdsError("bad_signature", "the blob's signature does not verify with the leaf key") from exc


def verify_blob(blob: bytes, *, now: datetime, root: Optional[x509.Certificate] = None,
                host: str = MDS_HOST) -> VerifiedBlob:
    """Verify a fetched blob (compact JWS) and parse its payload; MdsError on any failure."""
    parts = blob.strip().split(b".")
    if len(parts) != 3:
        raise MdsError("malformed", f"a compact JWS has 3 parts, this one {len(parts)}")
    try:
        header = json.loads(_b64url(parts[0], "header"))
    except ValueError as exc:
        raise MdsError("malformed", f"header is not JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise MdsError("malformed", "header is not an object")
    alg = header.get("alg")
    if alg not in ALGS:
        raise MdsError("bad_alg", f"alg {alg!r} not in {ALGS}")
    if "crit" in header:
        raise MdsError("malformed", "header has crit parameters")
    x5c = header.get("x5c")
    if not isinstance(x5c, list) or not x5c or not all(isinstance(c, str) for c in x5c):
        raise MdsError("malformed", "header has no x5c chain")
    try:
        certs = [x509.load_der_x509_certificate(base64.b64decode(c, validate=True)) for c in x5c]
    except (binascii.Error, ValueError) as exc:
        raise MdsError("malformed", f"x5c: {exc}") from exc

    anchor = root if root is not None else pinned_root()
    verifier = (PolicyBuilder().store(Store([anchor])).time(utc(now))
                .build_server_verifier(DNSName(host)))
    try:
        path = verifier.verify(certs[0], certs[1:])
    except VerificationError as exc:
        raise MdsError("chain", f"no valid path to the pinned root: {exc}") from exc

    _verify_signature(certs[0], str(alg), parts[0] + b"." + parts[1], _b64url(parts[2], "signature"))
    payload_json = _b64url(parts[1], "payload")
    payload = parse_payload(payload_json)
    return VerifiedBlob(serial=payload.no, next_update=payload.next_update,
                        sha256=hashlib.sha256(blob).hexdigest(), payload_json=payload_json,
                        payload=payload, chain=tuple(c.subject.rfc4514_string() for c in path))


def check_serial(new: int, stored: Optional[int]) -> Literal["new", "unchanged"]:
    if stored is None or new > stored:
        return "new"
    if new == stored:
        return "unchanged"
    raise MdsRollback("rollback", f"blob serial {new} is lower than the stored {stored}")


# --- the cache record and the verified list ------------------------------------------------------

@dataclass(frozen=True)
class CacheRecord:
    """What mds_cache stores per verified blob (§3.2)."""
    serial: int
    next_update: date
    fetched_at: datetime
    sha256: str
    payload_z: bytes         # zlib of the verified payload JSON
    entry_count: int


def to_record(blob: VerifiedBlob, fetched_at: datetime) -> CacheRecord:
    return CacheRecord(serial=blob.serial, next_update=blob.next_update, fetched_at=utc(fetched_at),
                       sha256=blob.sha256, payload_z=zlib.compress(blob.payload_json, 9),
                       entry_count=len(blob.payload.entries))


@dataclass(frozen=True)
class MdsList:
    """The newest verified list. `verified_at` is when a fetch last confirmed it current (a fetch
    that finds the same serial counts: the cache must record that time too, else the age rule
    below would run out under a list that is still current)."""
    serial: int
    next_update: date
    verified_at: datetime
    payload: MetadataBlobPayload
    _aaguids: dict[Aaguid, MetadataBlobPayloadEntry] = field(init=False, repr=False, compare=False)
    _key_ids: dict[bytes, MetadataBlobPayloadEntry] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        aaguids: dict[Aaguid, MetadataBlobPayloadEntry] = {}
        key_ids: dict[bytes, MetadataBlobPayloadEntry] = {}
        for e in self.payload.entries:       # the same tables (and last-wins) as fido2's verifier
            if e.aaguid:
                aaguids[e.aaguid] = e
            for ski in e.attestation_certificate_key_identifiers or []:
                key_ids[ski] = e
        object.__setattr__(self, "_aaguids", aaguids)
        object.__setattr__(self, "_key_ids", key_ids)

    def by_aaguid(self, aaguid: Aaguid) -> Optional[MetadataBlobPayloadEntry]:
        return self._aaguids.get(aaguid)

    def by_key_id(self, key_id: bytes) -> Optional[MetadataBlobPayloadEntry]:
        return self._key_ids.get(key_id)

    def by_chain(self, chain: Sequence[bytes]) -> tuple[Optional[MetadataBlobPayloadEntry], Optional[bytes]]:
        """The entry whose attestationCertificateKeyIdentifiers lists a key of `chain` (zero
        AAGUID), and that key identifier; malformed certificates raise ValueError."""
        for der in chain:
            cert = x509.load_der_x509_certificate(der)
            ski = x509.SubjectKeyIdentifier.from_public_key(cert.public_key()).digest
            entry = self.by_key_id(ski)
            if entry is not None:
                return entry, ski
        return None, None

    def registration_stale(self, now: datetime) -> bool:
        """Past nextUpdate + 7 days: no new registration is classified on this list."""
        due = datetime.combine(self.next_update, datetime.min.time(), tzinfo=timezone.utc)
        return utc(now) > due + STALE_GRACE

    def too_old_for_hardware_login(self, now: datetime, max_age_days: int = MAX_AGE_DEFAULT_DAYS) -> bool:
        """Past auth_mds_max_age_days since the list was last confirmed: a hardware passkey then
        needs a second factor."""
        if not MAX_AGE_MIN_DAYS <= max_age_days <= MAX_AGE_MAX_DAYS:
            raise ValueError(f"auth_mds_max_age_days {max_age_days} outside {MAX_AGE_MIN_DAYS}..{MAX_AGE_MAX_DAYS}")
        return utc(now) - utc(self.verified_at) > timedelta(days=max_age_days)


def load_record(record: CacheRecord, verified_at: Optional[datetime] = None) -> MdsList:
    """The verified list from its cache record (the record was verified before it was stored)."""
    try:
        payload_json = zlib.decompress(record.payload_z)
    except zlib.error as exc:
        raise MdsError("cache_corrupt", f"payload of serial {record.serial}: {exc}") from exc
    payload = parse_payload(payload_json)
    if payload.no != record.serial:
        raise MdsError("cache_corrupt", f"payload serial {payload.no} != record serial {record.serial}")
    return MdsList(serial=record.serial, next_update=record.next_update,
                   verified_at=utc(verified_at or record.fetched_at), payload=payload)


# --- which entries count -------------------------------------------------------------------------

def problem_statuses(entry: MetadataBlobPayloadEntry) -> list[str]:
    """The problem statuses among the entry's reports (any one refuses a passkey of that model)."""
    return sorted({r.status.value for r in entry.status_reports if r.status in PROBLEM_STATUSES})


def newest_status(entry: MetadataBlobPayloadEntry) -> Optional[AuthenticatorStatus]:
    if not entry.status_reports:
        return None
    dated = [(r.effective_date or date.min, i, r.status) for i, r in enumerate(entry.status_reports)]
    return max(dated)[2]     # the latest effective date; on a tie (or none), the later report


HardwareWhy = Literal["ok", "problem_status", "status", "no_statement", "key_protection", "syncing"]


def hardware_usable(entry: MetadataBlobPayloadEntry) -> tuple[bool, HardwareWhy]:
    """Whether a verified attestation to this entry makes a passkey hardware-bound (§3.2)."""
    if problem_statuses(entry):
        return False, "problem_status"
    if newest_status(entry) not in USABLE_NEWEST:
        return False, "status"
    statement = entry.metadata_statement
    if statement is None:
        return False, "no_statement"
    protection = set(statement.key_protection)
    if not protection or not protection <= HARDWARE_PROTECTION:
        return False, "key_protection"
    if statement.multi_device_credential_support in SYNCING:
        return False, "syncing"
    return True, "ok"


def describe(entry: Optional[MetadataBlobPayloadEntry]) -> Optional[str]:
    if entry is None or entry.metadata_statement is None:
        return None
    return entry.metadata_statement.description


# --- refresh -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RefreshResult:
    outcome: Literal["new", "unchanged"]
    serial: int
    fetched_at: datetime
    verify_seconds: float
    record: Optional[CacheRecord]    # only for "new"
    chain: tuple[str, ...]


def _notify_alert(subject: str, body: str) -> object:
    import notify                    # the repo's alert path (DB docs + push); late: keeps this module pure
    return notify.notify(subject, body)


def refresh(*, stored_serial: Optional[int], last_fetched_at: Optional[datetime], now: datetime,
            fetcher: Callable[[], bytes] = fetch, alert: AlertFn = _notify_alert,
            root: Optional[x509.Certificate] = None, host: str = MDS_HOST) -> RefreshResult:
    """Fetch, verify, compare: the record to store (outcome "new"), or "unchanged" (the caller
    records the confirmation time). Any failure alerts and raises (D-235); a runner job also pauses
    through failstop (its wrapper, once the runner is live). Within an hour of the last fetch it
    raises MdsTooSoon without alerting (nothing failed)."""
    if not may_fetch(last_fetched_at, now):
        raise MdsTooSoon("too_soon", f"last fetch {last_fetched_at:%Y-%m-%d %H:%M} UTC, FIDO allows one an hour"
                         if last_fetched_at else "too soon")
    try:
        blob = fetcher()
        started = time.monotonic()
        verified = verify_blob(blob, now=now, root=root, host=host)
        seconds = time.monotonic() - started
        outcome = check_serial(verified.serial, stored_serial)
    except Exception as exc:
        alert(ALERT_SUBJECT, f"{type(exc).__name__}: {exc}. New hardware passkeys are refused once the "
              "stored list is past nextUpdate + 7 days; see docs/phase6b_plan.md §3.2.")
        raise
    record = to_record(verified, now) if outcome == "new" else None
    return RefreshResult(outcome=outcome, serial=verified.serial, fetched_at=utc(now), verify_seconds=seconds,
                         record=record, chain=verified.chain)
