#!/usr/bin/env python3
"""Tests for dashboard/auth/oidc.py and oidc_groups.py (phase 6b P5, the pure part;
docs/phase6b_plan.md §3.6; design §9.3, §9.4; D-150, D-172, D-235): a local fake provider
(discovery, JWKS with key rotation, token endpoint with PKCE, userinfo) over httpx2's
MockTransport, keys from auth_testkit, tokens signed with joserfc. No network.

Needs the project venv (Authlib, joserfc, httpx2, cryptography; Python 3.12):
    .venv/bin/python -m unittest test_oidc
"""
import base64
import hashlib
import importlib.util
import json
import os
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_MISSING = [m for m in ("authlib", "joserfc", "httpx2", "cryptography", "fido2")
            if importlib.util.find_spec(m) is None]
SKIP = (f"needs Python 3.12 with {', '.join(_MISSING) or 'authlib'} (not {sys.executable}): "
        "run with .venv/bin/python -m unittest test_oidc") if _MISSING or sys.version_info < (3, 12) else ""

if not SKIP:
    import httpx2
    from joserfc import jwt
    from joserfc.jwk import ECKey, OctKey, RSAKey

    import auth_testkit as kit
    from dashboard.auth import oidc, oidc_groups as og

ISSUER = "https://idp.example.test"
CLIENT_ID = "afclaude"
SECRET = "s3cret/with+chars"
REDIRECT = "https://afc.example.test/auth/oidc/callback"
NOW = 1_792_670_400            # 2026-10-22 12:00 UTC


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def half_hash(value, bits=256):
    return b64url(getattr(hashlib, f"sha{bits}")(value.encode()).digest()[: bits // 16])


class FakeProvider:
    """An in-process OIDC provider: discovery, JWKS, authorize (called directly), token, userinfo."""

    def __init__(self, *, alg="ES256", iss_param=True, **meta_overrides):
        self.alg = alg
        self.keys = {}
        self.current = None
        self.rotate()
        self.codes = {}
        self.tokens = {}
        self.hits = {"discovery": 0, "jwks": 0, "token": 0, "userinfo": 0}
        self.meta = {
            "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token", "jwks_uri": f"{ISSUER}/jwks",
            "userinfo_endpoint": f"{ISSUER}/userinfo", "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256", "ES256"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            "authorization_response_iss_parameter_supported": iss_param,
            "backchannel_logout_supported": True,
        }
        self.meta.update(meta_overrides)
        self.jwks_extra = []
        self.token_override = None
        self.userinfo_claims = {}
        self.down = False

    def rotate(self):
        kid = f"k{len(self.keys) + 1}"
        native = kit.ec_key() if self.alg.startswith("ES") else kit.rsa_key()
        key = (ECKey if self.alg.startswith("ES") else RSAKey).import_key(native, parameters={"kid": kid})
        self.keys[kid] = key
        self.current = kid
        return kid

    def jwks(self):
        return {"keys": [dict(k.as_dict(private=False), use="sig") for k in self.keys.values()] + self.jwks_extra}

    def sign(self, claims, *, kid=None, alg=None, key=None, header=None):
        kid = kid or self.current
        h = {"alg": alg or self.alg, "kid": kid, **(header or {})}
        return jwt.encode(h, claims, key or self.keys[kid], algorithms=[h["alg"]])

    def claims(self, nonce, **over):
        c = {"iss": ISSUER, "sub": "user-1", "aud": CLIENT_ID, "exp": NOW + 300, "iat": NOW,
             "auth_time": NOW - 5, "nonce": nonce, "sid": "sid-1", "amr": ["pwd", "otp", "mfa"],
             "groups": ["afclaude-admins", "team-x"], "preferred_username": "alice"}
        c.update(over)
        return {k: v for k, v in c.items() if v is not None}

    def authorize(self, url, **claims):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = dict(q, claims=claims)
        params = {"code": code, "state": q["state"]}
        if self.meta.get("authorization_response_iss_parameter_supported"):
            params["iss"] = ISSUER
        return params

    def handle(self, request):
        if self.down:
            return httpx2.Response(503)
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            self.hits["discovery"] += 1
            return httpx2.Response(200, json=self.meta)
        if path == "/jwks":
            self.hits["jwks"] += 1
            return httpx2.Response(200, json=self.jwks())
        if path == "/token":
            self.hits["token"] += 1
            return self.token(request)
        if path == "/userinfo":
            self.hits["userinfo"] += 1
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] not in self.tokens:
                return httpx2.Response(401)
            return httpx2.Response(200, json={"sub": self.tokens[auth[7:]], **self.userinfo_claims})
        return httpx2.Response(404)

    def token(self, request):
        auth = request.headers.get("authorization", "")
        user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        from urllib.parse import unquote_plus
        if (unquote_plus(user), unquote_plus(pw)) != (CLIENT_ID, SECRET):
            return httpx2.Response(401, json={"error": "invalid_client"})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        flow = self.codes.pop(form.get("code"), None)
        if flow is None or form.get("grant_type") != "authorization_code":
            return httpx2.Response(400, json={"error": "invalid_grant", "error_description": "<script>x</script>"})
        challenge = b64url(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
        if challenge != flow["code_challenge"] or form.get("redirect_uri") != flow["redirect_uri"]:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        if self.token_override is not None:
            return httpx2.Response(200, json=self.token_override)
        access = f"at-{len(self.tokens)}"
        self.tokens[access] = flow["claims"].get("sub", "user-1")
        claims = self.claims(flow["nonce"], at_hash=half_hash(access), **flow["claims"])
        return httpx2.Response(200, json={"access_token": access, "token_type": "Bearer",
                                          "id_token": self.sign(claims), "expires_in": 300})

    def transport(self):
        return httpx2.MockTransport(self.handle)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@unittest.skipIf(bool(SKIP), SKIP)
class Base(unittest.TestCase):
    alg = "ES256"

    def setUp(self):
        self.p = FakeProvider(alg=self.alg)
        self.meta = oidc.discover(ISSUER, transport=self.p.transport())
        self.clock = Clock()
        self.keys = oidc.JwksCache(self.meta.jwks_uri, transport=self.p.transport(), clock=self.clock)

    def validate(self, token, nonce="n-1", access_token=None, now=NOW, **kw):
        return oidc.validate_id_token(token, keys=self.keys, meta=self.meta, client_id=CLIENT_ID,
                                      nonce=nonce, access_token=access_token, now=now, **kw)

    def refused(self, expect, fn, *a, **kw):
        with self.assertRaises(oidc.OidcError) as cm:
            fn(*a, **kw)
        self.assertEqual(cm.exception.code, expect, cm.exception)
        return cm.exception


class Discovery(Base):
    def parse(self, **over):
        meta = dict(self.p.meta, **over)
        return oidc.parse_metadata({k: v for k, v in meta.items() if v is not None}, ISSUER)

    def test_ok(self):
        m = self.meta
        self.assertEqual((m.issuer, m.token_endpoint, m.id_token_algs), (ISSUER, f"{ISSUER}/token", ("RS256", "ES256")))
        self.assertTrue(m.iss_parameter_supported and m.backchannel_logout_supported)
        self.assertEqual(oidc.well_known_url(ISSUER + "/"), f"{ISSUER}/.well-known/openid-configuration")

    def test_issuer_must_be_the_settings(self):
        self.refused("issuer_mismatch", self.parse, issuer="https://evil.example.test")
        self.refused("issuer_mismatch", oidc.parse_metadata, self.p.meta, ISSUER + "/")

    def test_https_only(self):
        self.refused("metadata_invalid", self.parse, issuer="http://idp.example.test")
        self.refused("metadata_invalid", self.parse, token_endpoint="http://idp.example.test/token")
        self.refused("metadata_invalid", self.parse, userinfo_endpoint="http://idp.example.test/u")
        self.refused("metadata_invalid", oidc.discover, "http://idp.example.test", transport=self.p.transport())

    def test_required_fields(self):
        self.refused("metadata_invalid", self.parse, jwks_uri=None)
        self.refused("metadata_invalid", self.parse, subject_types_supported=None)
        self.refused("metadata_invalid", self.parse, id_token_signing_alg_values_supported=None)
        self.refused("metadata_invalid", self.parse, authorization_response_iss_parameter_supported="yes")

    def test_only_symmetric_or_none_algorithms_refused(self):
        self.refused("no_usable_alg", self.parse, id_token_signing_alg_values_supported=["HS256", "none"])
        self.assertEqual(self.parse(id_token_signing_alg_values_supported=["ES256", "HS256"]).id_token_algs, ("ES256",))

    def test_flow_requirements(self):
        self.refused("no_code_flow", self.parse, response_types_supported=["id_token"])
        self.refused("no_pkce_s256", self.parse, code_challenge_methods_supported=["plain"])
        self.refused("no_client_secret_basic", self.parse, token_endpoint_auth_methods_supported=["private_key_jwt"],
                     token_endpoint_auth_signing_alg_values_supported=["RS256"])
        m = self.parse(code_challenge_methods_supported=None, token_endpoint_auth_methods_supported=None,
                       authorization_response_iss_parameter_supported=None, backchannel_logout_supported=None)
        self.assertFalse(m.iss_parameter_supported or m.backchannel_logout_supported)

    def test_http_failures(self):
        t = httpx2.MockTransport(lambda r: httpx2.Response(404))
        self.refused("http_status", oidc.discover, ISSUER, transport=t)
        t = httpx2.MockTransport(lambda r: httpx2.Response(301, headers={"Location": ISSUER + "/x"}))
        self.refused("http_status", oidc.discover, ISSUER, transport=t)
        t = httpx2.MockTransport(lambda r: httpx2.Response(200, content=b"<html>"))
        self.refused("not_json", oidc.discover, ISSUER, transport=t)
        t = httpx2.MockTransport(lambda r: httpx2.Response(200, content=b"x" * (oidc.HTTP_MAX_BYTES + 1)))
        self.refused("too_large", oidc.discover, ISSUER, transport=t)

        def slow(request):
            raise httpx2.ReadTimeout("slow", request=request)
        self.refused("timeout", oidc.discover, ISSUER, transport=httpx2.MockTransport(slow))

        def down(request):
            raise httpx2.ConnectError("refused", request=request)
        self.refused("http", oidc.discover, ISSUER, transport=httpx2.MockTransport(down))


class Jwks(Base):
    def test_rotation_refetches_once(self):
        self.validate(self.p.sign(self.p.claims("n-1")))
        self.assertEqual(self.keys.fetches, 1)
        self.p.rotate()
        self.clock.t += oidc.JWKS_MIN_REFRESH_S
        self.validate(self.p.sign(self.p.claims("n-1")))
        self.assertEqual(self.keys.fetches, 2)

    def test_unknown_kid_after_refresh_and_no_flooding(self):
        self.validate(self.p.sign(self.p.claims("n-1")))
        stranger = ECKey.import_key(kit.ec_key(), parameters={"kid": "zz"})
        token = self.p.sign(self.p.claims("n-1"), kid="zz", key=stranger)
        self.clock.t += oidc.JWKS_MIN_REFRESH_S
        self.refused("unknown_kid", self.validate, token)
        self.assertEqual(self.keys.fetches, 2)
        self.refused("unknown_kid", self.validate, token)       # within the minimum interval: no fetch
        self.assertEqual(self.keys.fetches, 2)

    def test_max_age_refetches(self):
        self.validate(self.p.sign(self.p.claims("n-1")))
        self.clock.t += oidc.JWKS_MAX_AGE_S
        self.validate(self.p.sign(self.p.claims("n-1")))
        self.assertEqual(self.keys.fetches, 2)

    def test_failed_fetch_raises_and_keeps_no_keys(self):
        self.validate(self.p.sign(self.p.claims("n-1")))
        self.clock.t += oidc.JWKS_MAX_AGE_S
        self.p.down = True
        self.refused("http_status", self.validate, self.p.sign(self.p.claims("n-1")))
        self.p.down = False
        self.validate(self.p.sign(self.p.claims("n-1")))
        self.assertEqual(self.keys.fetches, 2)

    def test_parse(self):
        good = self.p.jwks()["keys"][0]
        self.refused("jwks_symmetric", oidc.parse_jwks, {"keys": [good, {"kty": "oct", "k": "AAAA"}]})
        private = dict(ECKey.import_key(kit.ec_key()).as_dict(private=True))
        self.refused("jwks_private", oidc.parse_jwks, {"keys": [private]})
        self.refused("jwks_invalid", oidc.parse_jwks, {"keys": [{"kty": "EC", "crv": "P-256", "x": "AA"}]})
        self.refused("jwks_invalid", oidc.parse_jwks, {"keys": [{"kty": "XYZ"}]})
        self.refused("jwks_invalid", oidc.parse_jwks, {"nokeys": []})
        enc = dict(good, use="enc", kid="enc-1")
        ops = dict(good, kid="ops-1", key_ops=["encrypt"])
        self.assertEqual([k.kid for k in oidc.parse_jwks({"keys": [good, enc, ops]})], [good["kid"]])

    def test_token_without_kid_needs_a_single_key(self):
        self.validate(jwt.encode({"alg": "ES256"}, self.p.claims("n-1"), self.p.keys["k1"], algorithms=["ES256"]))
        self.p.rotate()
        self.clock.t += oidc.JWKS_MAX_AGE_S
        token = jwt.encode({"alg": "ES256"}, self.p.claims("n-1"), self.p.keys["k1"], algorithms=["ES256"])
        self.refused("unknown_kid", self.validate, token)

    def test_key_type_must_fit_the_alg(self):
        rsa = RSAKey.import_key(kit.rsa_key(), parameters={"kid": "k1"})
        token = jwt.encode({"alg": "RS256", "kid": "k1"}, self.p.claims("n-1"), rsa, algorithms=["RS256"])
        self.refused("unknown_kid", self.validate, token)        # k1 is an EC key: no RS256 key k1


class AuthRequestTest(Base):
    def test_parameters(self):
        r = oidc.new_auth_request(self.meta, client_id=CLIENT_ID, redirect_uri=REDIRECT)
        q = {k: v[0] for k, v in parse_qs(urlsplit(r.url).query).items()}
        self.assertTrue(r.url.startswith(f"{ISSUER}/authorize?"))
        self.assertEqual((q["response_type"], q["client_id"], q["redirect_uri"], q["scope"], q["code_challenge_method"]),
                         ("code", CLIENT_ID, REDIRECT, "openid profile email groups", "S256"))
        self.assertEqual((q["state"], q["nonce"]), (r.state, r.nonce))
        self.assertEqual(q["code_challenge"], b64url(hashlib.sha256(r.code_verifier.encode()).digest()))
        self.assertTrue(43 <= len(r.code_verifier) <= 128)
        self.assertNotIn("prompt", q)
        self.assertNotIn(r.code_verifier, r.url)
        r2 = oidc.new_auth_request(self.meta, client_id=CLIENT_ID, redirect_uri=REDIRECT)
        self.assertEqual(len({r.state, r.nonce, r.code_verifier, r2.state, r2.nonce, r2.code_verifier}), 6)

    def test_rfc7636_vector(self):
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"  # public-check: allow (RFC 7636 appendix B vector)
        challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"  # public-check: allow (RFC 7636 appendix B vector)
        self.assertEqual(oidc.code_challenge(verifier), challenge)

    def test_step_up(self):
        r = oidc.new_auth_request(self.meta, client_id=CLIENT_ID, redirect_uri=REDIRECT, step_up=True)
        q = parse_qs(urlsplit(r.url).query)
        self.assertEqual((q["prompt"], q["max_age"]), (["login"], ["0"]))

    def test_endpoint_query_kept_and_clash_refused(self):
        m = oidc.parse_metadata(dict(self.p.meta, authorization_endpoint=f"{ISSUER}/authorize?tenant=a"), ISSUER)
        r = oidc.new_auth_request(m, client_id=CLIENT_ID, redirect_uri=REDIRECT)
        self.assertEqual(parse_qs(urlsplit(r.url).query)["tenant"], ["a"])
        m = oidc.parse_metadata(dict(self.p.meta, authorization_endpoint=f"{ISSUER}/authorize?state=x"), ISSUER)
        self.refused("metadata_invalid", oidc.new_auth_request, m, client_id=CLIENT_ID, redirect_uri=REDIRECT)

    def test_scopes(self):
        with self.assertRaises(ValueError):
            oidc.new_auth_request(self.meta, client_id=CLIENT_ID, redirect_uri=REDIRECT, scopes=("profile",))
        with self.assertRaises(ValueError):
            oidc.new_auth_request(self.meta, client_id=CLIENT_ID, redirect_uri=REDIRECT, scopes=("openid", "a b"))


class Callback(Base):
    def setUp(self):
        super().setUp()
        self.req = oidc.new_auth_request(self.meta, client_id=CLIENT_ID, redirect_uri=REDIRECT)
        self.digest = oidc.state_digest(self.req.state)

    def check(self, params, meta=None):
        return oidc.check_callback(params, state_sha256=self.digest, meta=meta or self.meta)

    def test_ok(self):
        self.assertEqual(self.check({"code": "c", "state": self.req.state, "iss": ISSUER}), "c")

    def test_provider_error_text_not_kept(self):
        e = self.refused("provider_error", self.check, {"error": "access_denied", "error_description": "<b>hi</b>",
                                                         "state": self.req.state})
        self.assertNotIn("<b>", str(e))

    def test_state(self):
        self.refused("state_mismatch", self.check, {"code": "c", "iss": ISSUER})
        other = oidc.new_auth_request(self.meta, client_id=CLIENT_ID, redirect_uri=REDIRECT)
        self.refused("state_mismatch", self.check, {"code": "c", "state": other.state, "iss": ISSUER})

    def test_rfc9207_iss(self):
        self.refused("iss_missing", self.check, {"code": "c", "state": self.req.state})
        self.refused("iss_mismatch", self.check, {"code": "c", "state": self.req.state, "iss": "https://evil.example.test"})
        no9207 = oidc.parse_metadata(dict(self.p.meta, authorization_response_iss_parameter_supported=False), ISSUER)
        self.assertEqual(self.check({"code": "c", "state": self.req.state}, no9207), "c")
        self.refused("iss_mismatch", self.check, {"code": "c", "state": self.req.state, "iss": "x"}, no9207)

    def test_code_missing(self):
        self.refused("code_missing", self.check, {"state": self.req.state, "iss": ISSUER})


class Flow(Base):
    """begin → provider → callback → token → ID token → groups → grants."""

    def login(self, verifier=None, **claims):
        req = oidc.new_auth_request(self.meta, client_id=CLIENT_ID, redirect_uri=REDIRECT)
        params = self.p.authorize(req.url, **claims)
        code = oidc.check_callback(params, state_sha256=oidc.state_digest(req.state), meta=self.meta)
        tok = oidc.exchange_code(self.meta, code=code, code_verifier=verifier or req.code_verifier, client_id=CLIENT_ID,
                                 client_secret=SECRET, redirect_uri=REDIRECT, transport=self.p.transport())
        return req, code, tok

    def test_happy_path(self):
        req, _, tok = self.login()
        idt = self.validate(tok.id_token, nonce=req.nonce, access_token=tok.access_token)
        self.assertEqual((idt.sub, idt.sid, idt.amr, idt.issuer), ("user-1", "sid-1", ("pwd", "otp", "mfa"), ISSUER))
        entries = og.parse_group_map([{"group": "afclaude-admins", "grant": "owner"},
                                      {"group": "team-x", "grant": "project:spending:editor"},
                                      {"group": "other", "grant": "project:x:viewer"}])
        groups = og.require_groups(entries, oidc.groups_claim(idt.claims), None)
        grants = og.desired_grants(groups, entries)
        self.assertEqual(grants, {og.Grant("owner", None, None, "oidc:afclaude-admins"),
                                  og.Grant("project", "spending", "editor", "oidc:team-x")})
        oidc.check_second_factor(idt, oidc.SecondFactorPolicy(), gives_owner=og.gives_owner(grants))

    def test_pkce_verifier_mismatch(self):
        e = self.refused("token_error", self.login, verifier="x" * 50)
        self.assertIn("invalid_grant", e.detail)
        self.assertNotIn("<script>", e.detail)

    def test_code_single_use(self):
        req, code, _ = self.login()
        self.refused("token_error", oidc.exchange_code, self.meta, code=code, code_verifier=req.code_verifier,
                     client_id=CLIENT_ID, client_secret=SECRET, redirect_uri=REDIRECT, transport=self.p.transport())

    def test_wrong_client_secret(self):
        req = oidc.new_auth_request(self.meta, client_id=CLIENT_ID, redirect_uri=REDIRECT)
        code = self.p.authorize(req.url)["code"]
        self.refused("token_error", oidc.exchange_code, self.meta, code=code, code_verifier=req.code_verifier,
                     client_id=CLIENT_ID, client_secret="wrong", redirect_uri=REDIRECT, transport=self.p.transport())

    def test_token_response_shape(self):
        for body in ({"access_token": "a", "token_type": "Bearer"}, {"id_token": "x", "token_type": "Bearer"},
                     {"id_token": "x", "access_token": "a", "token_type": "mac"}):
            self.p.token_override = body
            self.refused("token_invalid", self.login)

    def test_nonce_replay_from_another_flow(self):
        req, _, tok = self.login()
        other = oidc.new_auth_request(self.meta, client_id=CLIENT_ID, redirect_uri=REDIRECT)
        self.refused("nonce_mismatch", self.validate, tok.id_token, nonce=other.nonce, access_token=tok.access_token)

    def test_at_hash(self):
        req, _, tok = self.login()
        self.refused("at_hash_mismatch", self.validate, tok.id_token, nonce=req.nonce, access_token="another")

    def test_groups_from_userinfo(self):
        req, _, tok = self.login(groups=None)
        idt = self.validate(tok.id_token, nonce=req.nonce, access_token=tok.access_token)
        self.assertIsNone(oidc.groups_claim(idt.claims))
        self.p.userinfo_claims = {"groups": ["team-x"]}
        info = oidc.fetch_userinfo(self.meta, access_token=tok.access_token, sub=idt.sub, transport=self.p.transport())
        entries = og.parse_group_map([{"group": "team-x", "grant": "project:spending:manager"}])
        groups = og.require_groups(entries, oidc.groups_claim(idt.claims), oidc.groups_claim(info))
        self.assertEqual(og.desired_grants(groups, entries), {og.Grant("project", "spending", "manager", "oidc:team-x")})
        self.p.userinfo_claims = {}
        info = oidc.fetch_userinfo(self.meta, access_token=tok.access_token, sub=idt.sub, transport=self.p.transport())
        with self.assertRaises(og.GroupsMissing):
            og.require_groups(entries, None, oidc.groups_claim(info))
        self.assertEqual(og.require_groups((), None, None), frozenset())

    def test_userinfo_sub_must_match(self):
        req, _, tok = self.login()
        self.refused("userinfo_sub_mismatch", oidc.fetch_userinfo, self.meta, access_token=tok.access_token,
                     sub="user-2", transport=self.p.transport())
        self.refused("http_status", oidc.fetch_userinfo, self.meta, access_token="bogus", sub="user-1",
                     transport=self.p.transport())
        self.p.userinfo_claims = {"sub": 5}
        self.refused("userinfo_sub_mismatch", oidc.fetch_userinfo, self.meta, access_token=tok.access_token,
                     sub="user-1", transport=self.p.transport())


class IdTokenChecks(Base):
    def tok(self, **over):
        claims = dict(self.p.claims("n-1"), **over)
        return self.p.sign({k: v for k, v in claims.items() if v is not None})

    def test_ok_and_leeway(self):
        self.validate(self.tok())
        self.validate(self.tok(exp=NOW - oidc.LEEWAY_S + 1))
        self.validate(self.tok(iat=NOW + oidc.LEEWAY_S))

    def test_tampered(self):
        h, p, s = self.tok().split(".")
        claims = json.loads(base64.urlsafe_b64decode(p + "=="))
        claims["sub"] = "admin"
        forged = ".".join([h, b64url(json.dumps(claims).encode()), s])
        self.refused("bad_signature", self.validate, forged)
        self.refused("bad_signature", self.validate, ".".join([h, p, b64url(b"\x00" * 64)]))

    def test_signed_by_another_key_with_a_known_kid(self):
        evil = (ECKey.import_key(kit.ec_key(), parameters={"kid": "k1"}) if self.alg.startswith("ES")
                else RSAKey.import_key(kit.rsa_key(), parameters={"kid": "k1"}))
        self.refused("bad_signature", self.validate, self.p.sign(self.p.claims("n-1"), key=evil))

    def test_expired(self):
        self.refused("id_token_expired", self.validate, self.tok(exp=NOW - oidc.LEEWAY_S - 1))

    def test_iat(self):
        self.refused("iat_invalid", self.validate, self.tok(iat=NOW + oidc.LEEWAY_S + 1))
        self.refused("iat_invalid", self.validate, self.tok(iat=NOW - oidc.MAX_ID_TOKEN_AGE_S - 1, exp=NOW + 60))

    def test_audience(self):
        self.refused("audience_mismatch", self.validate, self.tok(aud="someone-else"))
        self.refused("audience_mismatch", self.validate, self.tok(aud=[CLIENT_ID, "other"], azp=CLIENT_ID))
        self.validate(self.tok(aud=[CLIENT_ID, "other"], azp=CLIENT_ID), trusted_audiences=frozenset({"other"}))
        self.refused("claim_missing", self.validate, self.tok(aud=[CLIENT_ID, "other"]),
                     trusted_audiences=frozenset({"other"}))                       # several audiences need azp
        self.refused("azp_mismatch", self.validate, self.tok(azp="someone-else"))

    def test_issuer(self):
        self.refused("issuer_mismatch", self.validate, self.tok(iss="https://evil.example.test"))
        self.refused("claim_missing", self.validate, self.tok(iss=None))

    def test_nonce(self):
        self.refused("nonce_mismatch", self.validate, self.tok(nonce="other"))
        self.refused("claim_missing", self.validate, self.tok(nonce=None))
        with self.assertRaises(ValueError):
            self.validate(self.tok(), nonce="")

    def test_required_claims(self):
        for claim in ("sub", "exp", "iat", "aud"):
            self.assertIn(self.refused_any(self.tok(**{claim: None})), {"claim_missing", "audience_mismatch"}, claim)
        self.refused("claim_invalid", self.validate, self.tok(sub=""))
        self.refused("claim_invalid", self.validate, self.tok(sub="x" * 256))
        self.refused("claim_invalid", self.validate, self.tok(amr="mfa"))
        self.refused("claim_invalid", self.validate, self.tok(acr=2))

    def refused_any(self, token):
        with self.assertRaises(oidc.OidcError) as cm:
            self.validate(token)
        return cm.exception.code

    def test_alg_none_and_hs256(self):
        claims = self.p.claims("n-1")
        none = b64url(json.dumps({"alg": "none", "kid": "k1"}).encode()) + "." + b64url(json.dumps(claims).encode()) + "."
        self.refused("alg_not_allowed", self.validate, none)
        hs = jwt.encode({"alg": "HS256", "kid": "k1"}, claims, OctKey.import_key(SECRET), algorithms=["HS256"])
        self.refused("alg_not_allowed", self.validate, hs)

    def test_alg_outside_the_providers_list(self):
        other = "RS256" if self.alg.startswith("ES") else "ES256"
        self.meta = oidc.parse_metadata(dict(self.p.meta, id_token_signing_alg_values_supported=[other]), ISSUER)
        self.refused("alg_not_allowed", self.validate, self.tok())

    def test_malformed(self):
        self.refused("id_token_malformed", self.validate, "not-a-jwt")
        self.refused("id_token_malformed", self.validate, "a.b.c.d.e")
        h = b64url(json.dumps({"alg": self.alg, "kid": "k1", "crit": ["exp"], "exp": 1}).encode())
        self.refused("id_token_malformed", self.validate, f"{h}.{self.tok().split('.', 1)[1]}")
        self.refused("id_token_malformed", self.validate, self.p.sign(self.p.claims("n-1"), header={"jku": "https://evil.example.test/k"}))

    def test_step_up_auth_time(self):
        began = NOW - 30
        self.validate(self.tok(auth_time=NOW - 10), auth_time_after=began)
        self.refused("auth_time_too_old", self.validate, self.tok(auth_time=NOW - 3600), auth_time_after=began)
        self.refused("auth_time_missing", self.validate, self.tok(auth_time=None), auth_time_after=began)


class RsaProvider(IdTokenChecks):
    alg = "RS256"


@unittest.skipIf(bool(SKIP), SKIP)
class SecondFactor(unittest.TestCase):
    def token(self, amr=(), acr=None):
        return oidc.IdToken(issuer=ISSUER, sub="u", iat=NOW, exp=NOW + 60, auth_time=None, sid=None,
                            amr=tuple(amr), acr=acr, claims={})

    def test_default_is_the_recommendation(self):
        self.assertEqual(oidc.SecondFactorPolicy().when, "owner")

    def test_matrix(self):
        mfa, pwd = self.token(["pwd", "mfa"]), self.token(["pwd"])
        for when, owner, tok, ok in [
                ("all", False, mfa, True), ("all", False, pwd, False), ("all", True, pwd, False),
                ("owner", False, pwd, True), ("owner", True, pwd, False), ("owner", True, mfa, True),
                ("off", True, pwd, True), ("off", False, self.token(), True)]:
            policy = oidc.SecondFactorPolicy(when=when)
            with self.subTest(when=when, owner=owner, amr=tok.amr):
                if ok:
                    oidc.check_second_factor(tok, policy, gives_owner=owner)
                else:
                    with self.assertRaises(oidc.OidcError) as cm:
                        oidc.check_second_factor(tok, policy, gives_owner=owner)
                    self.assertEqual(cm.exception.code, "second_factor_missing")

    def test_custom_values_and_acr(self):
        p = oidc.SecondFactorPolicy(when="all", amr=frozenset({"hwk", "otp"}), acr=frozenset({"gold"}))
        oidc.check_second_factor(self.token(["pwd", "otp"]), p, gives_owner=False)
        oidc.check_second_factor(self.token(["pwd"], acr="gold"), p, gives_owner=False)
        with self.assertRaises(oidc.OidcError):
            oidc.check_second_factor(self.token(["pwd", "mfa"], acr="silver"), p, gives_owner=False)

    def test_invalid_policy(self):
        with self.assertRaises(ValueError):
            oidc.SecondFactorPolicy(when="sometimes")
        with self.assertRaises(ValueError):
            oidc.SecondFactorPolicy(when="all", amr=frozenset())
        oidc.SecondFactorPolicy(when="off", amr=frozenset())


@unittest.skipIf(bool(SKIP), SKIP)
class Groups(unittest.TestCase):
    def test_claim_shapes(self):
        self.assertEqual(oidc.groups_claim({"groups": ["a", "b"]}), {"a", "b"})
        self.assertEqual(oidc.groups_claim({"groups": "a"}), {"a"})
        self.assertEqual(oidc.groups_claim({"groups": []}), frozenset())
        self.assertIsNone(oidc.groups_claim({}))
        self.assertEqual(oidc.groups_claim({"roles": ["x"]}, "roles"), {"x"})
        for bad in ([1], {"a": 1}, [""], ["x" * 257], ["g"] * 1001):
            with self.assertRaises(oidc.OidcError):
                oidc.groups_claim({"groups": bad})

    def test_parse_map(self):
        e = og.parse_group_map([{"group": "Admins", "grant": "owner"},
                                {"group": "t", "grant": "project:a:b:viewer"}])
        self.assertEqual([(x.scope, x.project, x.level) for x in e], [("owner", None, None), ("project", "a:b", "viewer")])
        self.assertTrue(og.maps_owner(e))
        self.assertFalse(og.maps_owner(e[1:]))
        self.assertEqual(og.parse_group_map([]), ())

    def test_parse_map_refusals(self):
        for bad in ({}, [{"group": "a"}], [{"group": "a", "grant": "owner", "x": 1}], [{"group": "", "grant": "owner"}],
                    [{"group": "a", "grant": "admin"}], [{"group": "a", "grant": "project:x:owner"}],
                    [{"group": "a", "grant": "project::viewer"}], [{"group": "a", "grant": "project: x :viewer"}],
                    [{"group": "a", "grant": "project:x"}], [{"group": 1, "grant": "owner"}],
                    [{"group": "a", "grant": "owner"}, {"group": "a", "grant": "owner"}],
                    [{"group": f"g{i}", "grant": "owner"} for i in range(og.MAX_ENTRIES + 1)]):
            with self.subTest(bad=str(bad)[:60]), self.assertRaises(og.GroupMapError):
                og.parse_group_map(bad)

    def test_case_sensitive_and_no_group_no_access(self):
        e = og.parse_group_map([{"group": "Admins", "grant": "owner"}])
        self.assertEqual(og.desired_grants({"admins"}, e), frozenset())
        self.assertEqual(og.desired_grants(set(), e), frozenset())

    def test_diff(self):
        owner = og.Grant("owner", None, None, "oidc:admins")
        ed = og.Grant("project", "p", "editor", "oidc:team")
        vw = og.Grant("project", "p", "viewer", "oidc:readers")
        d = og.diff([ed, vw], frozenset({owner, ed}))
        self.assertEqual((d.add, d.remove, d.owner_granted), ({owner}, {vw}, True))
        d = og.diff([owner], frozenset({owner}))
        self.assertEqual((d.add, d.remove, d.owner_granted), (frozenset(), frozenset(), False))
        d = og.diff([owner, ed], frozenset())
        self.assertEqual((d.remove, d.owner_granted), ({owner, ed}, False))
        with self.assertRaises(ValueError):
            og.diff([og.Grant("owner", None, None, "manual")], frozenset())


if __name__ == "__main__":
    unittest.main()
