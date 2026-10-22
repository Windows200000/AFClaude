"""OIDC login, the relying-party protocol (docs/phase6b_plan.md §0, §3.6, step P5; design §8,
§9.3; D-150, D-157, D-172, D-235): the one module that imports Authlib (no py.typed; mypy.ini
ignores its missing types, nothing of it leaves this module untyped).

  discover()           the provider's metadata: one GET of its discovery document, validated with
                       Authlib (oidc.discovery + RFC 9207), its `issuer` equal to the setting's, an
                       asymmetric ID-token algorithm we accept, `code` + PKCE S256 + client_secret_basic
  JwksCache            the provider's signing keys (joserfc): fetched on first use, again after
                       JWKS_MAX_AGE_S, and once on an unknown `kid` (key rotation) at most every
                       JWKS_MIN_REFRESH_S; symmetric or private keys refuse the whole set
  new_auth_request()   state, nonce, the PKCE S256 verifier (Authlib's challenge) and the provider URL
  check_callback()     the callback's query: the provider's error, `state`, RFC 9207 `iss`, the code
  exchange_code()      the token request (client_secret_basic, the verifier)
  validate_id_token()  signature (joserfc, alg from ALLOWED_ALGS, key by `kid`), then Authlib's
                       CodeIDToken rules (iss, aud, azp, exp, iat with LEEWAY_S, nonce, at_hash) and
                       ours (every audience trusted, iat not older than MAX_ID_TOKEN_AGE_S, `sub`,
                       `amr` / `acr` types, `auth_time` after the flow began for step-up)
  fetch_userinfo()     the userinfo endpoint (plain JSON), its `sub` equal to the ID token's
  groups_claim()       the groups from a claim set (ID token or userinfo)
  SecondFactorPolicy / check_second_factor()   the `amr` / `acr` rule (owner question 2, §6)

The three HTTP calls go through httpx2 with a fixed timeout each (D-234), no redirects, a size cap;
`transport` is injectable (tests use a local fake provider). Every failure raises OidcError with a
fixed `code` (the callback turns it into `/login?error=<code>`); `detail` is for the log only and
may carry the provider's error code, never its free text to the browser. Nothing here touches the
DB: the flow row (`oidc_flows`), identities, `grant.sync_groups` and the endpoints come with the
5a / 6a pieces they need (§4). The group map is oidc_groups.py.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import httpx2
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from authlib.oauth2.rfc9207 import AuthorizationServerMetadata as Rfc9207Metadata
from authlib.oidc.core import CodeIDToken
from authlib.oidc.discovery import OpenIDProviderMetadata, get_well_known_url
from joserfc import jwk, jws
from joserfc.errors import (BadSignatureError, ExpiredTokenError, JoseError, MissingClaimError,
                            InvalidClaimError)

HTTP_TIMEOUT_S = 10.0              # each call in total (discovery, JWKS, token, userinfo), D-234
HTTP_MAX_BYTES = 1024 * 1024       # a discovery document, a JWKS or a token response is a few KB
LEEWAY_S = 60                      # clock skew for exp / iat / nbf (§3.6)
MAX_ID_TOKEN_AGE_S = 10 * 60       # an ID token from the token endpoint is fresh; the flow lives 10 min
JWKS_MAX_AGE_S = 60 * 60           # refetch the keys at least hourly (a key the provider withdrew)
JWKS_MIN_REFRESH_S = 60            # an unknown kid refetches at most this often (no flooding)
FLOW_TTL_S = 10 * 60               # an oidc_flows row expires after this (§3.6)
SUB_MAX = 255                      # OIDC Core §2: sub at most 255 ASCII characters
STATE_BYTES = NONCE_BYTES = 32
VERIFIER_BYTES = 64                # 86 base64url characters (RFC 7636: 43–128)

# Asymmetric only: no `none`, no HS* (a shared secret would let anyone holding the client secret
# mint ID tokens). Ed25519 / Ed448 (RFC 9864) are left out: OIDC defines no at_hash for them yet.
ALLOWED_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")
_KTY_FOR = {"RS": "RSA", "PS": "RSA", "ES": "EC", "Ed": "OKP"}
DEFAULT_SCOPES = ("openid", "profile", "email", "groups")

CODES = frozenset({
    # transport
    "timeout", "http", "http_status", "too_large", "not_json",
    # discovery
    "metadata_invalid", "issuer_mismatch", "no_usable_alg", "no_code_flow", "no_pkce_s256",
    "no_client_secret_basic",
    # keys
    "jwks_invalid", "jwks_symmetric", "jwks_private", "unknown_kid",
    # callback and token
    "provider_error", "state_mismatch", "iss_missing", "iss_mismatch", "code_missing", "token_error",
    "token_invalid",
    # the ID token
    "id_token_malformed", "alg_not_allowed", "bad_signature", "id_token_expired", "nonce_mismatch",
    "audience_mismatch", "azp_mismatch", "at_hash_mismatch", "iat_invalid", "claim_missing",
    "claim_invalid", "auth_time_missing", "auth_time_too_old",
    # userinfo, groups, the second-factor rule
    "userinfo_invalid", "userinfo_sub_mismatch", "groups_invalid", "second_factor_missing",
})


class OidcError(Exception):
    """An OIDC step failed. `code` is from CODES (shown as `/login?error=<code>`, logged, audited);
    `detail` is for the log only."""

    def __init__(self, code: str, detail: str) -> None:
        if code not in CODES:
            raise ValueError(f"unknown OIDC error code {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --- HTTP ----------------------------------------------------------------------------------------

def _request(method: Literal["GET", "POST"], url: str, *, transport: Optional[httpx2.BaseTransport],
             timeout: float = HTTP_TIMEOUT_S, max_bytes: int = HTTP_MAX_BYTES,
             headers: Optional[Mapping[str, str]] = None, form: Optional[Mapping[str, str]] = None
             ) -> tuple[int, bytes]:
    """One request, the body read with a size cap and a deadline for the whole call. A timeout, a
    transport error or a body over `max_bytes` raises OidcError; the status is the caller's."""
    deadline = time.monotonic() + timeout
    body = bytearray()
    try:
        with httpx2.Client(timeout=timeout, follow_redirects=False, transport=transport) as client:
            with client.stream(method, url, headers=dict(headers or {}),
                               data=dict(form) if form is not None else None) as resp:
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise OidcError("too_large", f"{method} {url}: more than {max_bytes} bytes")
                    if time.monotonic() > deadline:
                        raise OidcError("timeout", f"{method} {url}: not done after {timeout:.0f} s")
                return resp.status_code, bytes(body)
    except httpx2.TimeoutException as exc:
        raise OidcError("timeout", f"{method} {url}: {type(exc).__name__} after {timeout:.0f} s") from exc
    except httpx2.HTTPError as exc:
        raise OidcError("http", f"{method} {url}: {type(exc).__name__}: {exc}") from exc


def _json_object(body: bytes, what: str) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise OidcError("not_json", f"{what}: not JSON") from exc
    if not isinstance(data, dict):
        raise OidcError("not_json", f"{what}: not a JSON object")
    return data


def _get_json(url: str, what: str, transport: Optional[httpx2.BaseTransport], timeout: float,
              headers: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    status, body = _request("GET", url, transport=transport, timeout=timeout,
                            headers={"Accept": "application/json", **(headers or {})})
    if status != 200:
        raise OidcError("http_status", f"{what} {url}: HTTP {status}")
    return _json_object(body, what)


def _https(url: Any, what: str) -> str:
    if not isinstance(url, str) or urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
        raise OidcError("metadata_invalid", f"{what} is not an https URL: {url!r}")
    return url


# --- discovery -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderMetadata:
    """What AFClaude uses of the provider's discovery document (validated)."""
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: Optional[str]
    id_token_algs: tuple[str, ...]          # the provider's, intersected with ALLOWED_ALGS (never empty)
    iss_parameter_supported: bool           # RFC 9207: the callback then must carry `iss`
    backchannel_logout_supported: bool      # shown next to the setting (§3.7)
    raw: Mapping[str, Any] = field(repr=False, compare=False)


class _Metadata(OpenIDProviderMetadata):  # type: ignore[misc]  # Authlib is untyped (Any base class)
    """Authlib's OIDC Discovery rules, except "RS256 MUST be listed": a provider set up with an EC
    key lists only ES256 (Authentik does), and which algorithms we accept is ALLOWED_ALGS's job."""

    def validate_id_token_signing_alg_values_supported(self) -> None:
        values = self.get("id_token_signing_alg_values_supported")
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError('"id_token_signing_alg_values_supported" is required, a JSON array of strings')


def well_known_url(issuer: str) -> str:
    return str(get_well_known_url(issuer, external=True))


def parse_metadata(data: Mapping[str, Any], issuer: str) -> ProviderMetadata:
    """Validate a discovery document for `issuer` (the setting): Authlib's OIDC Discovery and
    RFC 9207 rules (https endpoints, jwks_uri, subject types, …; _Metadata), then ours: `issuer` exactly the
    setting's (OIDC Discovery §4.3), the code flow, PKCE S256 when the provider lists its methods,
    client_secret_basic when it lists its auth methods, at least one algorithm of ALLOWED_ALGS."""
    _https(issuer, "issuer")
    meta = _Metadata(dict(data))
    try:
        meta.validate(metadata_classes=[Rfc9207Metadata])
    except (ValueError, TypeError, KeyError) as exc:
        raise OidcError("metadata_invalid", f"{issuer}: {exc}") from exc
    if data.get("issuer") != issuer:
        raise OidcError("issuer_mismatch", f"discovery says issuer {data.get('issuer')!r}, the setting is {issuer!r}")
    if "code" not in (data.get("response_types_supported") or []):
        raise OidcError("no_code_flow", f"{issuer}: response_types_supported has no 'code'")
    methods = data.get("code_challenge_methods_supported")
    if methods is not None and (not isinstance(methods, list) or "S256" not in methods):
        raise OidcError("no_pkce_s256", f"{issuer}: code_challenge_methods_supported is {methods!r}")
    auth_methods = data.get("token_endpoint_auth_methods_supported")
    if auth_methods is not None and "client_secret_basic" not in auth_methods:   # RFC 8414: absent = basic
        raise OidcError("no_client_secret_basic", f"{issuer}: token_endpoint_auth_methods_supported is {auth_methods!r}")
    offered = data.get("id_token_signing_alg_values_supported") or []
    algs = tuple(a for a in ALLOWED_ALGS if a in offered)
    if not algs:
        raise OidcError("no_usable_alg", f"{issuer}: ID-token algorithms {offered!r}, none of {ALLOWED_ALGS}")
    userinfo = data.get("userinfo_endpoint")
    return ProviderMetadata(
        issuer=issuer,
        authorization_endpoint=_https(data.get("authorization_endpoint"), "authorization_endpoint"),
        token_endpoint=_https(data.get("token_endpoint"), "token_endpoint"),
        jwks_uri=_https(data.get("jwks_uri"), "jwks_uri"),
        userinfo_endpoint=_https(userinfo, "userinfo_endpoint") if userinfo is not None else None,
        id_token_algs=algs,
        iss_parameter_supported=data.get("authorization_response_iss_parameter_supported") is True,
        backchannel_logout_supported=data.get("backchannel_logout_supported") is True,
        raw=dict(data))


def discover(issuer: str, *, transport: Optional[httpx2.BaseTransport] = None,
             timeout: float = HTTP_TIMEOUT_S) -> ProviderMetadata:
    """GET `<issuer>/.well-known/openid-configuration` and validate it (parse_metadata). Run when
    the settings are saved (a failing provider is refused there) and before a login."""
    _https(issuer, "issuer")
    return parse_metadata(_get_json(well_known_url(issuer), "discovery", transport, timeout), issuer)


# --- the provider's keys -------------------------------------------------------------------------

@dataclass(frozen=True)
class SigningKey:
    kid: Optional[str]
    kty: str
    alg: Optional[str]          # the JWK's own `alg`, when it names one
    key: jwk.Key = field(repr=False)


def parse_jwks(data: Mapping[str, Any]) -> tuple[SigningKey, ...]:
    """The signature keys of a JWK set. Encryption keys (`use: enc`, or `key_ops` without
    `verify`) are not signature keys and are left out; a symmetric (`oct`) or private key, an
    unknown key type or a key joserfc can't import refuses the whole set."""
    keys = data.get("keys")
    if not isinstance(keys, list):
        raise OidcError("jwks_invalid", "no 'keys' array")
    out: list[SigningKey] = []
    for raw in keys:
        if not isinstance(raw, dict):
            raise OidcError("jwks_invalid", "a key is not an object")
        kty = raw.get("kty")
        if kty == "oct":
            raise OidcError("jwks_symmetric", f"key {raw.get('kid')!r} is symmetric")
        if kty not in ("RSA", "EC", "OKP"):
            raise OidcError("jwks_invalid", f"key {raw.get('kid')!r}: unknown kty {kty!r}")
        if any(p in raw for p in ("d", "p", "q", "dp", "dq", "qi")):
            raise OidcError("jwks_private", f"key {raw.get('kid')!r} carries private parameters")
        ops = raw.get("key_ops")
        if raw.get("use") == "enc" or (isinstance(ops, list) and "verify" not in ops):
            continue
        kid = raw.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise OidcError("jwks_invalid", f"kid {kid!r} is not a string")
        try:
            key = jwk.import_key(dict(raw))
        except (JoseError, ValueError, TypeError) as exc:
            raise OidcError("jwks_invalid", f"key {kid!r}: {type(exc).__name__}: {exc}") from exc
        alg = raw.get("alg")
        out.append(SigningKey(kid=kid, kty=kty, alg=alg if isinstance(alg, str) else None, key=key))
    return tuple(out)


def _pick(keys: Sequence[SigningKey], kid: Optional[str], alg: str) -> Optional[SigningKey]:
    kty = _KTY_FOR[alg[:2]]
    fits = [k for k in keys if k.kty == kty and k.alg in (None, alg)]
    if kid is None:                      # OIDC Core §10.1: no kid only with one possible key
        return fits[0] if len(fits) == 1 else None
    same = [k for k in fits if k.kid == kid]
    return same[0] if len(same) == 1 else None


class JwksCache:
    """The provider's signing keys, cached per process. `key_for()` fetches them on first use and
    when older than `max_age_s`; a `kid` it doesn't know (the provider rotated its keys) refetches
    once, at most every `min_refresh_s`, and is refused (unknown_kid) if still unknown. Thread-safe
    (the web process answers in a thread pool). A failed fetch raises; the old keys are not used
    in its place (D-235)."""

    def __init__(self, jwks_uri: str, *, transport: Optional[httpx2.BaseTransport] = None,
                 timeout: float = HTTP_TIMEOUT_S, max_age_s: float = JWKS_MAX_AGE_S,
                 min_refresh_s: float = JWKS_MIN_REFRESH_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.jwks_uri = _https(jwks_uri, "jwks_uri")
        self._transport = transport
        self._timeout = timeout
        self._max_age_s = max_age_s
        self._min_refresh_s = min_refresh_s
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: Optional[tuple[SigningKey, ...]] = None
        self._fetched_at = 0.0
        self.fetches = 0

    def _fetch(self) -> None:
        self._keys = None                 # a failed refetch leaves no keys behind
        keys = parse_jwks(_get_json(self.jwks_uri, "jwks", self._transport, self._timeout))
        self._keys, self._fetched_at = keys, self._clock()
        self.fetches += 1

    def key_for(self, kid: Optional[str], alg: str) -> jwk.Key:
        with self._lock:
            now = self._clock()
            if self._keys is None or now - self._fetched_at >= self._max_age_s:
                self._fetch()
            assert self._keys is not None
            found = _pick(self._keys, kid, alg)
            if found is None and now - self._fetched_at >= self._min_refresh_s:
                self._fetch()
                assert self._keys is not None
                found = _pick(self._keys, kid, alg)
            if found is None:
                raise OidcError("unknown_kid", f"no {alg} key with kid {kid!r} in {self.jwks_uri}")
            return found.key


# --- the authorization request -------------------------------------------------------------------

def _b64(nbytes: int) -> str:
    return secrets.token_urlsafe(nbytes)


def state_digest(state: str) -> str:
    """What the flow row stores instead of the state itself (§3.6)."""
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def code_challenge(verifier: str) -> str:
    return str(create_s256_code_challenge(verifier))


@dataclass(frozen=True)
class AuthRequest:
    url: str                # where the client navigates
    state: str              # back in the callback; the row keeps state_digest(state)
    nonce: str              # must come back in the ID token
    code_verifier: str      # PKCE, kept server-side until the token request
    step_up: bool


def new_auth_request(meta: ProviderMetadata, *, client_id: str, redirect_uri: str,
                     scopes: Sequence[str] = DEFAULT_SCOPES, step_up: bool = False) -> AuthRequest:
    """A fresh authorization request: random state, nonce and PKCE verifier, `response_type=code`,
    `code_challenge_method=S256`; for step-up `prompt=login` and `max_age=0` (§3.8). The endpoint's
    own query parameters are kept; one that would collide with ours refuses the provider."""
    if "openid" not in scopes:
        raise ValueError("the scopes must include 'openid'")
    if any(not s or any(c.isspace() for c in s) for s in scopes):
        raise ValueError(f"invalid scope in {list(scopes)!r}")
    state, nonce, verifier = _b64(STATE_BYTES), _b64(NONCE_BYTES), _b64(VERIFIER_BYTES)
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
              "scope": " ".join(scopes), "state": state, "nonce": nonce,
              "code_challenge": code_challenge(verifier), "code_challenge_method": "S256"}
    if step_up:
        params.update(prompt="login", max_age="0")
    parts = urlsplit(meta.authorization_endpoint)
    existing = parse_qsl(parts.query, keep_blank_values=True)
    clash = {k for k, _ in existing} & params.keys()
    if clash:
        raise OidcError("metadata_invalid", f"authorization_endpoint already sets {sorted(clash)}")
    query = urlencode(existing + list(params.items()))
    return AuthRequest(url=urlunsplit(parts._replace(query=query)), state=state, nonce=nonce,
                       code_verifier=verifier, step_up=step_up)


# --- the callback --------------------------------------------------------------------------------

def check_callback(params: Mapping[str, str], *, state_sha256: str, meta: ProviderMetadata) -> str:
    """The callback's query against the flow row (found by state_digest of the `state` it carries;
    this re-checks it): the provider's `error` (its code goes to the log, never its description),
    RFC 9207's `iss` (required when the provider says it sends it, and equal to the issuer when
    present), the authorization code. Returns the code."""
    if "error" in params:
        raise OidcError("provider_error", f"error={params['error'][:64]!r}")
    state = params.get("state", "")
    if not state or not hmac.compare_digest(state_digest(state), state_sha256):
        raise OidcError("state_mismatch", "the callback's state is not this flow's")
    iss = params.get("iss")
    if iss is None and meta.iss_parameter_supported:
        raise OidcError("iss_missing", "the provider sends iss (RFC 9207) but this response has none")
    if iss is not None and iss != meta.issuer:
        raise OidcError("iss_mismatch", f"iss {iss[:200]!r} is not {meta.issuer!r}")
    code = params.get("code", "")
    if not code or len(code) > 4096:
        raise OidcError("code_missing", "no authorization code")
    return code


# --- the token request ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenResponse:
    id_token: str = field(repr=False)
    access_token: str = field(repr=False)     # used once (userinfo), never stored (§0)


def _basic(client_id: str, client_secret: str) -> str:
    # RFC 6749 §2.3.1: both form-urlencoded before the Basic encoding
    raw = f"{quote_plus(client_id)}:{quote_plus(client_secret)}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def exchange_code(meta: ProviderMetadata, *, code: str, code_verifier: str, client_id: str,
                  client_secret: str, redirect_uri: str, transport: Optional[httpx2.BaseTransport] = None,
                  timeout: float = HTTP_TIMEOUT_S) -> TokenResponse:
    """The authorization code for tokens (client_secret_basic, the PKCE verifier). A non-200 answer
    raises token_error with the provider's `error` code in the detail; a 200 without a Bearer
    access token and an ID token raises token_invalid."""
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "code_verifier": code_verifier}
    status, body = _request("POST", meta.token_endpoint, transport=transport, timeout=timeout, form=form,
                            headers={"Authorization": _basic(client_id, client_secret),
                                     "Accept": "application/json"})
    if status != 200:
        err = None
        try:
            err = _json_object(body, "token error").get("error")
        except OidcError:
            err = "(no JSON body)"        # the status is the error either way; the body only adds its code
        raise OidcError("token_error", f"HTTP {status}, error={str(err)[:64]!r}")
    data = _json_object(body, "token response")
    id_token, access_token = data.get("id_token"), data.get("access_token")
    token_type = data.get("token_type")
    if not isinstance(id_token, str) or not id_token:
        raise OidcError("token_invalid", "no id_token")
    if not isinstance(access_token, str) or not access_token:
        raise OidcError("token_invalid", "no access_token")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise OidcError("token_invalid", f"token_type {token_type!r}")
    return TokenResponse(id_token=id_token, access_token=access_token)


# --- the ID token --------------------------------------------------------------------------------

@dataclass(frozen=True)
class IdToken:
    issuer: str
    sub: str
    iat: int
    exp: int
    auth_time: Optional[int]
    sid: Optional[str]                  # for back-channel logout (P6)
    amr: tuple[str, ...]
    acr: Optional[str]
    claims: Mapping[str, Any] = field(repr=False)   # all of them (display name, e-mail, groups)


_CLAIM_CODES = {"nonce": "nonce_mismatch", "iss": "issuer_mismatch", "aud": "audience_mismatch",
                "azp": "azp_mismatch", "at_hash": "at_hash_mismatch", "iat": "iat_invalid"}


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_id_token(id_token: str, *, keys: JwksCache, meta: ProviderMetadata, client_id: str,
                      nonce: str, access_token: Optional[str], now: float,
                      trusted_audiences: frozenset[str] = frozenset(),
                      auth_time_after: Optional[float] = None) -> IdToken:
    """Verify an ID token from the token endpoint. `nonce`: the flow's (a token from another flow
    fails). `access_token`: the token response's, for at_hash. `auth_time_after` (step-up): the
    flow's start; `auth_time` must be present and not before it (a provider that ignores
    `max_age=0` fails here, §3.8). `trusted_audiences`: audiences besides the client id that may
    appear (none by default: OIDC Core's ID-token validation refuses untrusted extra audiences). `now`: epoch
    seconds."""
    if not nonce:
        raise ValueError("a flow always has a nonce")
    try:
        obj = jws.extract_compact(id_token.encode("ascii"))
    except (JoseError, ValueError, UnicodeEncodeError) as exc:
        raise OidcError("id_token_malformed", f"{type(exc).__name__}: {exc}") from exc
    header = obj.headers()
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in meta.id_token_algs:
        raise OidcError("alg_not_allowed", f"alg {alg!r}, allowed {meta.id_token_algs}")
    if "crit" in header or "b64" in header or "jku" in header or "x5u" in header:
        raise OidcError("id_token_malformed", f"header has {sorted(set(header) & {'crit', 'b64', 'jku', 'x5u'})}")
    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise OidcError("id_token_malformed", "kid is not a string")
    key = keys.key_for(kid, alg)
    try:
        verified = jws.validate_compact(obj, key, algorithms=[alg])   # False on a bad signature
    except BadSignatureError as exc:
        raise OidcError("bad_signature", f"kid {kid!r}") from exc
    except (JoseError, ValueError) as exc:
        raise OidcError("bad_signature", f"kid {kid!r}: {type(exc).__name__}: {exc}") from exc
    if verified is not True:
        raise OidcError("bad_signature", f"kid {kid!r}")
    try:
        payload = json.loads(obj.payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise OidcError("id_token_malformed", "the payload is not JSON") from exc
    if not isinstance(payload, dict):
        raise OidcError("id_token_malformed", "the payload is not an object")

    options = {"iss": {"essential": True, "value": meta.issuer},
               "aud": {"essential": True, "value": client_id}}
    params = {"nonce": nonce, "client_id": client_id, "access_token": access_token}
    claims = CodeIDToken(payload, header, options=options, params=params)
    try:
        claims.validate(now=int(now), leeway=LEEWAY_S)
    except ExpiredTokenError as exc:
        raise OidcError("id_token_expired", f"exp {payload.get('exp')!r}, now {int(now)}") from exc
    except (MissingClaimError, InvalidClaimError) as exc:
        code = "claim_missing" if isinstance(exc, MissingClaimError) else _CLAIM_CODES.get(exc.claim, "claim_invalid")
        raise OidcError(code, f"{exc.claim}: {exc.description}") from exc
    except JoseError as exc:
        raise OidcError("claim_invalid", f"{type(exc).__name__}: {exc}") from exc

    aud = payload["aud"]
    auds = {aud} if isinstance(aud, str) else set(aud)
    if auds - {client_id} - trusted_audiences:
        raise OidcError("audience_mismatch", f"untrusted audiences {sorted(auds - {client_id})}")
    sub = payload["sub"]
    if not isinstance(sub, str) or not sub or len(sub) > SUB_MAX:
        raise OidcError("claim_invalid", "sub must be a string of 1–255 characters")
    iat, exp = payload["iat"], payload["exp"]
    if not _num(iat) or not _num(exp):
        raise OidcError("claim_invalid", "iat / exp are not numbers")
    if iat < now - MAX_ID_TOKEN_AGE_S:
        raise OidcError("iat_invalid", f"issued {int(now - iat)} s ago")
    auth_time = payload.get("auth_time")
    if auth_time is not None and not _num(auth_time):
        raise OidcError("claim_invalid", "auth_time is not a number")
    if auth_time_after is not None:
        if auth_time is None:
            raise OidcError("auth_time_missing", "step-up needs auth_time")
        if auth_time < int(auth_time_after):
            raise OidcError("auth_time_too_old", f"auth_time {int(auth_time)} before the flow began {int(auth_time_after)}")
    amr = payload.get("amr", [])
    if not isinstance(amr, list) or not all(isinstance(a, str) for a in amr):
        raise OidcError("claim_invalid", "amr is not an array of strings")
    acr, sid = payload.get("acr"), payload.get("sid")
    if acr is not None and not isinstance(acr, str):
        raise OidcError("claim_invalid", "acr is not a string")
    if sid is not None and (not isinstance(sid, str) or not sid):
        raise OidcError("claim_invalid", "sid is not a string")
    return IdToken(issuer=meta.issuer, sub=sub, iat=int(iat), exp=int(exp),
                   auth_time=int(auth_time) if auth_time is not None else None, sid=sid,
                   amr=tuple(amr), acr=acr, claims=dict(payload))


# --- userinfo and groups -------------------------------------------------------------------------

def fetch_userinfo(meta: ProviderMetadata, *, access_token: str, sub: str,
                   transport: Optional[httpx2.BaseTransport] = None,
                   timeout: float = HTTP_TIMEOUT_S) -> dict[str, Any]:
    """The userinfo claims (plain JSON; a signed or encrypted answer is not accepted), when the
    groups are not in the ID token. Its `sub` must be the ID token's (OIDC Core §5.3.2)."""
    if meta.userinfo_endpoint is None:
        raise OidcError("userinfo_invalid", f"{meta.issuer} has no userinfo endpoint")
    data = _get_json(meta.userinfo_endpoint, "userinfo", transport, timeout,
                     headers={"Authorization": f"Bearer {access_token}"})
    if not isinstance(data.get("sub"), str) or not hmac.compare_digest(data["sub"], sub):
        raise OidcError("userinfo_sub_mismatch", "userinfo's sub is not the ID token's")
    return data


MAX_GROUPS = 1000
MAX_GROUP_LEN = 256


def groups_claim(claims: Mapping[str, Any], claim: str = "groups") -> Optional[frozenset[str]]:
    """The groups in `claims[claim]`: an array of strings, or one string (some providers send a
    lone group so). None when the claim is absent (then userinfo is asked, and if it lacks the
    claim too the caller decides, see oidc_groups.require_groups). Anything else, an empty name
    or more than MAX_GROUPS raises groups_invalid."""
    if claim not in claims:
        return None
    value = claims[claim]
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(g, str) for g in items):
        raise OidcError("groups_invalid", f"claim {claim!r} is not a string array")
    if len(items) > MAX_GROUPS or any(not g or len(g) > MAX_GROUP_LEN for g in items):
        raise OidcError("groups_invalid", f"claim {claim!r}: {len(items)} groups, or an empty / too long name")
    return frozenset(items)


# --- the second-factor rule (owner question 2, docs/phase6b_plan.md §6) --------------------------

SecondFactorWhen = Literal["all", "owner", "off"]
SECOND_FACTOR_WHEN: tuple[SecondFactorWhen, ...] = ("all", "owner", "off")


@dataclass(frozen=True)
class SecondFactorPolicy:
    """Whether a sign-in through the provider must show that the provider asked for a second
    factor: `all` (a) every sign-in, `owner` (b, the plan's recommendation and the default until
    the owner answers) only a sign-in that gives owner rights, `off` (c) never. The provider shows
    it with one of `amr` (RFC 8176 values; default `mfa`, which Authelia and Authentik send after a
    second factor) or an `acr` value from `acr` (empty: `acr` not used)."""
    when: SecondFactorWhen = "owner"
    amr: frozenset[str] = frozenset({"mfa"})
    acr: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.when not in SECOND_FACTOR_WHEN:
            raise ValueError(f"when must be one of {SECOND_FACTOR_WHEN}, not {self.when!r}")
        if self.when != "off" and not (self.amr or self.acr):
            raise ValueError("a required second factor needs at least one amr or acr value")


def second_factor_shown(token: IdToken, policy: SecondFactorPolicy) -> bool:
    """The ID token reports a second factor by the policy's values."""
    return bool(set(token.amr) & policy.amr) or (token.acr is not None and token.acr in policy.acr)


def check_second_factor(token: IdToken, policy: SecondFactorPolicy, *, gives_owner: bool) -> None:
    """Refuse the sign-in (second_factor_missing) when the policy requires a second factor here
    and the token doesn't show one. `gives_owner`: the session would be an owner's (a group maps
    to owner, oidc_groups.gives_owner, or the user holds a manual owner grant)."""
    required = policy.when == "all" or (policy.when == "owner" and gives_owner)
    if required and not second_factor_shown(token, policy):
        raise OidcError("second_factor_missing",
                        f"policy {policy.when}, amr {list(token.amr)}, acr {token.acr!r}, owner {gives_owner}")
