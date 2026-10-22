#!/usr/bin/env python3
"""Tests for dashboard/ (phase 6a W1/W2, docs/phase6a_plan.md): the endpoint table (deny by
default), the error shape on every error path, the write guard, the body and idempotency rules,
the headers and CSP, the Host check, the app shell and assets, health, and the generated schema
and TS types (stale = fail; every API response in these tests is validated against the committed
schema.json).

Needs the project venv (Starlette, pydantic; Python 3.12):
    .venv/bin/python -m unittest test_dashboard
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import testenv  # noqa: E402,F401  (temp DB first)
import jsonschema  # noqa: E402
from pydantic import JsonValue  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import store  # noqa: E402
from dashboard import codegen, routes, security  # noqa: E402
from dashboard.api_types import Model  # noqa: E402
from dashboard.app import create_app  # noqa: E402
from dashboard.routes import ENDPOINTS, Call, Endpoint, Principal, Reply  # noqa: E402

PUBLIC = "https://afc.example.test"
ORIGIN = PUBLIC
KEY = "0b7e2a0c-5d1e-4c55-9a0f-3b8c1d2e4f60"
with open(codegen.SCHEMA_PATH, encoding="utf-8") as _fh:
    SCHEMA = json.load(_fh)


# --- test-only endpoints (writes, a bug, a DB failure) -------------------------------------------

class EchoRequest(Model):
    text: str
    n: int = 0


class EchoResponse(Model):
    text: str
    n: int
    key: str | None
    user: int | None


async def echo(call: Call) -> Reply:
    body = call.json_body(EchoRequest)
    return Reply(EchoResponse(text=body.text, n=body.n, key=call.idempotency_key,
                              user=call.principal.user_id if call.principal else None))


async def boom(call: Call) -> Reply:
    raise RuntimeError("secret internal detail /some/path")


async def wrong_model(call: Call) -> Reply:
    return Reply(EchoRequest(text="x"))


async def db_down(call: Call) -> Reply:
    raise store.DBUnavailable("database disk full: /secret/path/afclaude.db", "disk_full")


async def nothing(call: Call) -> Reply:
    return Reply(EchoResponse(text="", n=0, key=None, user=None))


TEST_ENDPOINTS = ENDPOINTS + (
    Endpoint("echo", "POST", "/api/v1/test/echo", "session", EchoRequest, EchoResponse, echo, "echo",
             idempotent=True),
    Endpoint("echo_plain", "POST", "/api/v1/test/echo_plain", "session", EchoRequest, EchoResponse, echo, "echo"),
    Endpoint("no_body", "DELETE", "/api/v1/test/no_body", "session", None, EchoResponse, nothing, "no body"),
    Endpoint("boom", "GET", "/api/v1/test/boom", "session", None, EchoResponse, boom, "bug"),
    Endpoint("wrong_model", "GET", "/api/v1/test/wrong", "session", None, EchoResponse, wrong_model, "bug"),
    Endpoint("db_down", "GET", "/api/v1/test/db", "session", None, EchoResponse, db_down, "db"),
)


def test_schema():
    """The schema of the test app (the committed one plus the test endpoints)."""
    return codegen.openapi(TEST_ENDPOINTS)


class Checked:
    """A TestClient whose every /api response is validated against `schema` (the contract test)."""

    def __init__(self, client, schema, test):
        self.client, self.schema, self.test = client, schema, test

    def request(self, method, path, **kw):
        r = self.client.request(method, path, **kw)
        if path.startswith("/api"):
            self.test.assertEqual(r.headers.get("content-type"), "application/json", f"{method} {path}")
            self.validate(method, path, r)
        return r

    def validate(self, method, path, r):
        comps = self.schema["components"]
        op = self.schema["paths"].get(path, {}).get(method.lower())
        if op is not None and str(r.status_code) in op["responses"]:
            ref = op["responses"][str(r.status_code)]["content"]["application/json"]["schema"]
        else:
            self.test.assertGreaterEqual(r.status_code, 400, f"{method} {path}: undeclared status {r.status_code}")
            ref = {"$ref": "#/components/schemas/ErrorResponse"}
        jsonschema.Draft202012Validator({**ref, "components": comps}).validate(r.json())

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)


class Base(unittest.TestCase):
    endpoints = TEST_ENDPOINTS
    signed_in = False

    def setUp(self):
        self.dist = tempfile.mkdtemp(prefix="afc-dist-")
        self.addCleanup(shutil.rmtree, self.dist, True)
        os.makedirs(os.path.join(self.dist, "assets"))
        with open(os.path.join(self.dist, "index.html"), "w") as fh:
            fh.write("<!doctype html><title>AFClaude</title><script src=/assets/app-abc.js></script>")
        with open(os.path.join(self.dist, "assets", "app-abc.js"), "w") as fh:
            fh.write("console.log(1)")

        async def auth(request):
            return Principal(user_id=7) if self.signed_in else None
        self.app = create_app(PUBLIC, self.dist, auth, db_path=testenv.DB, endpoints=self.endpoints)
        raw = TestClient(self.app, base_url=PUBLIC, raise_server_exceptions=False)
        self.c = Checked(raw, test_schema() if self.endpoints is TEST_ENDPOINTS else SCHEMA, self)

    def write(self, path, body=None, *, headers=None, content=None, method="POST"):
        h = {"Content-Type": "application/json", "Origin": ORIGIN, "Idempotency-Key": KEY}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        if content is None:
            content = json.dumps({"text": "hi"} if body is None else body)
        return self.c.request(method, path, content=content, headers=h)

    def assertError(self, r, status, code):
        self.assertEqual(r.status_code, status, r.text)
        self.assertEqual(r.json()["error"]["code"], code, r.text)
        self.assertNotIn("Traceback", r.text)


class RouteTable(Base):
    def test_only_the_allowed_endpoints_are_public(self):
        self.assertEqual({e.name for e in ENDPOINTS if e.access == "public"}, {"health"})
        self.assertEqual(routes.PUBLIC_ALLOWED, frozenset({"health"}))

    def test_every_session_endpoint_refuses_without_a_session(self):
        for ep in ENDPOINTS:
            if ep.access == "session":
                r = self.c.request(ep.method, ep.path) if ep.method == "GET" else self.write(ep.path, method=ep.method)
                self.assertError(r, 401, "unauthenticated")

    def test_a_public_endpoint_outside_the_allowed_list_refuses_to_start(self):
        leak = Endpoint("leak", "GET", "/api/v1/leak", "public", None, routes.HealthResponse, routes.health, "x")
        with self.assertRaisesRegex(ValueError, "PUBLIC_ALLOWED"):
            create_app(PUBLIC, self.dist, routes.no_sessions, endpoints=ENDPOINTS + (leak,))

    def test_table_invariants(self):
        dup = Endpoint("health", "GET", "/api/v1/other", "session", None, routes.HealthResponse, routes.health, "x")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            routes.check_table(ENDPOINTS + (dup,))
        with self.assertRaises(ValueError):
            Endpoint("x", "GET", "/api/v1/x", "session", routes.HealthResponse, routes.HealthResponse, routes.health, "x")
        with self.assertRaises(ValueError):
            Endpoint("x", "GET", "/other/x", "session", None, routes.HealthResponse, routes.health, "x")

    def test_missing_frontend_build_refuses_to_start(self):
        os.remove(os.path.join(self.dist, "index.html"))
        with self.assertRaises(FileNotFoundError):
            create_app(PUBLIC, self.dist, routes.no_sessions)

    def test_public_url_rules(self):
        for bad in ("http://afc.example.test", "ftp://x", "https://x/sub", "https://u:p@x", "https://x/?q=1", "x"):
            with self.assertRaises(ValueError, msg=bad):
                security.PublicUrl.parse(bad)
        self.assertEqual(security.PublicUrl.parse("http://localhost:8080").origin, "http://localhost:8080")
        self.assertEqual(security.PublicUrl.parse("https://AFC.example.test/").host, "afc.example.test")


class ErrorShape(Base):
    signed_in = True

    def test_unknown_api_path_is_json_404_not_the_shell(self):
        self.assertError(self.c.get("/api/v1/nope"), 404, "not_found")
        self.assertError(self.c.get("/api"), 404, "not_found")

    def test_known_path_wrong_method_is_405_with_allow(self):
        r = self.write("/api/v1/health")
        self.assertError(r, 405, "method_not_allowed")
        self.assertEqual(r.headers["allow"], "GET")

    def test_shell_with_a_write_method_is_405(self):
        r = self.client_raw().post("/some/view", content="{}", headers={"Content-Type": "application/json"})
        self.assertError(r, 405, "method_not_allowed")

    def test_handler_bug_is_500_without_internals(self):
        with self.assertLogs("afclaude.web", "ERROR"):
            r = self.c.get("/api/v1/test/boom")
        self.assertError(r, 500, "internal")
        self.assertNotIn("secret", r.text)
        with self.assertLogs("afclaude.web", "ERROR"):
            self.assertError(self.c.get("/api/v1/test/wrong"), 500, "internal")

    def test_db_error_is_503_unavailable_with_the_kind_only(self):
        with self.assertLogs("afclaude.web", "ERROR"):
            r = self.c.get("/api/v1/test/db")
        self.assertError(r, 503, "unavailable")
        self.assertEqual(r.json()["error"]["details"], {"kind": "disk_full"})
        self.assertNotIn("secret", r.text)

    def test_missing_asset_is_404_shape(self):
        self.assertError(self.client_raw().get("/assets/missing.js"), 404, "not_found")

    def client_raw(self):
        return self.c.client


class WriteGuard(Base):
    signed_in = True

    def test_same_origin_json_write_passes(self):
        r = self.write("/api/v1/test/echo", {"text": "hi", "n": 2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"text": "hi", "n": 2, "key": KEY, "user": 7})

    def test_csrf_failures_are_403(self):
        cases = [
            {"Content-Type": "application/x-www-form-urlencoded"},
            {"Content-Type": "text/plain"},
            {"Content-Type": "multipart/form-data; boundary=x"},
            {"Content-Type": "application/json; charset=latin-1"},
            {"Content-Type": None},
            {"Origin": None},
            {"Origin": "https://evil.example"},
            {"Origin": "http://afc.example.test"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertError(self.write("/api/v1/test/echo", headers=headers), 403, "forbidden")

    def test_guard_runs_before_the_session_check(self):
        self.signed_in = False
        self.assertError(self.write("/api/v1/test/echo", headers={"Origin": "https://evil.example"}), 403, "forbidden")
        self.assertError(self.write("/api/v1/test/echo"), 401, "unauthenticated")

    def test_sec_fetch_site_same_origin_passes(self):
        r = self.write("/api/v1/test/echo", headers={"Sec-Fetch-Site": "same-origin",
                                                     "Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(r.status_code, 200, r.text)


class Body(Base):
    signed_in = True

    def test_unknown_field_rejected_without_echoing_the_input(self):
        r = self.write("/api/v1/test/echo", {"text": "hi", "sneaky": "SECRETVALUE"})
        self.assertError(r, 422, "validation")
        self.assertEqual(r.json()["error"]["details"]["fields"][0]["loc"], "sneaky")
        self.assertNotIn("SECRETVALUE", r.text)

    def test_strict_types_no_coercion(self):
        self.assertError(self.write("/api/v1/test/echo", {"text": "hi", "n": "2"}), 422, "validation")
        self.assertError(self.write("/api/v1/test/echo", {"n": 1}), 422, "validation")

    def test_malformed_json(self):
        for content in ('{"text": "a", "text": "b"}', "{nope", '{"text": NaN}', b"\xff\xfe"):
            with self.subTest(content=content):
                self.assertError(self.write("/api/v1/test/echo", content=content), 400, "validation")
        for content in ("[1]", '"x"', ""):
            with self.subTest(content=content):
                self.assertError(self.write("/api/v1/test/echo", content=content), 422, "validation")

    def test_body_cap_with_and_without_content_length(self):
        big = json.dumps({"text": "x" * (security.BODY_MAX + 10)})
        self.assertError(self.write("/api/v1/test/echo", content=big), 413, "payload_too_large")

        def chunks():
            for _ in range(security.BODY_MAX // 1000 + 2):
                yield b" " * 1000
        r = self.c.request("POST", "/api/v1/test/echo", content=chunks(),
                           headers={"Content-Type": "application/json", "Origin": ORIGIN, "Idempotency-Key": KEY})
        self.assertNotIn("content-length", {k.lower() for k in r.request.headers})
        self.assertError(r, 413, "payload_too_large")

    def test_idempotency_key(self):
        for key in (None, "not-a-uuid", KEY + "0"):
            with self.subTest(key=key):
                self.assertError(self.write("/api/v1/test/echo", headers={"Idempotency-Key": key}), 400, "validation")
        r = self.write("/api/v1/test/echo", headers={"Idempotency-Key": KEY.upper()})
        self.assertEqual(r.json()["key"], KEY)
        r = self.write("/api/v1/test/echo_plain", headers={"Idempotency-Key": None})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["key"])

    def test_no_body_endpoint(self):
        for content in ("", "{}"):
            r = self.write("/api/v1/test/no_body", content=content, method="DELETE")
            self.assertEqual(r.status_code, 200, r.text)
        self.assertError(self.write("/api/v1/test/no_body", {"a": 1}, method="DELETE"), 422, "validation")

    def test_query_parameters_refused(self):
        self.assertError(self.c.get("/api/v1/health?x=1"), 400, "validation")


class Headers(Base):
    def check_security_headers(self, r):
        h = r.headers
        self.assertEqual(h["content-security-policy"], security.CSP)
        for part in ("default-src 'none'", "script-src 'self'", "frame-ancestors 'none'", "base-uri 'none'",
                     "connect-src 'self'"):
            self.assertIn(part, h["content-security-policy"])
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["referrer-policy"], "no-referrer")
        self.assertIn("max-age=", h["strict-transport-security"])
        self.assertNotIn("access-control-allow-origin", h)

    def test_shell_api_error_and_asset(self):
        shell = self.c.client.get("/projects/3")
        self.assertEqual(shell.status_code, 200)
        self.assertIn("text/html", shell.headers["content-type"])
        self.assertIn("<title>AFClaude</title>", shell.text)
        self.assertEqual(shell.headers["cache-control"], "no-store")
        self.assertEqual(self.c.client.get("/").text, shell.text)
        api = self.c.get("/api/v1/health")
        err = self.c.get("/api/v1/nope")
        asset = self.c.client.get("/assets/app-abc.js")
        for r in (shell, api, err, asset):
            self.check_security_headers(r)
        self.assertEqual(api.headers["cache-control"], "no-store")
        self.assertEqual(err.headers["cache-control"], "no-store")
        self.assertEqual(asset.headers["cache-control"], "public, max-age=31536000, immutable")

    def test_cors_preflight_gets_no_cors_headers(self):
        r = self.c.request("OPTIONS", "/api/v1/health", headers={"Origin": "https://evil.example",
                                                                  "Access-Control-Request-Method": "POST"})
        self.assertNotIn("access-control-allow-origin", r.headers)
        self.assertGreaterEqual(r.status_code, 400)

    def test_wrong_host_refused(self):
        for host in ("evil.example", "127.0.0.1", "afc.example.test.evil.example"):
            with self.subTest(host=host):
                r = self.c.client.get("/api/v1/health", headers={"Host": host})
                self.assertError(r, 403, "forbidden")
                self.check_security_headers(r)
        r = self.c.client.get("/", headers={"Host": "evil.example"})
        self.assertError(r, 403, "forbidden")


class Health(Base):
    def test_ok(self):
        r = self.c.get("/api/v1/health")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"status": "ok", "db": "ok"})

    def test_unavailable_shows_the_kind_only(self):
        problem = store.DBUnavailable(f"the database {testenv.DB} is corrupt", "corrupt")
        with mock.patch.object(store, "health", return_value=problem), self.assertLogs("afclaude.web", "ERROR"):
            r = self.c.get("/api/v1/health")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json(), {"status": "unavailable", "db": "corrupt"})
        self.assertNotIn(testenv.DIR, r.text)

    def test_missing_db(self):
        d = tempfile.mkdtemp(prefix="afc-nodb-")
        self.addCleanup(shutil.rmtree, d, True)
        app = create_app(PUBLIC, self.dist, routes.no_sessions, db_path=os.path.join(d, "none.db"))
        with mock.patch.object(store, "MISSING_RECHECK_DELAY", 0), self.assertLogs("afclaude.web", "ERROR"):
            r = Checked(TestClient(app, base_url=PUBLIC), SCHEMA, self).get("/api/v1/health")
        self.assertEqual((r.status_code, r.json()), (503, {"status": "unavailable", "db": "missing"}))
        self.assertFalse(os.path.exists(os.path.join(d, "none.db")))   # health never creates it

    def test_health_works_on_a_real_db_file(self):
        self.assertTrue(sqlite3.connect(testenv.DB).execute("SELECT 1").fetchone())


class ApiSchema(Base):
    endpoints = ENDPOINTS
    signed_in = True

    def test_schema_endpoint_serves_the_committed_document(self):
        r = self.c.get("/api/v1/schema.json")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), SCHEMA)


class Codegen(unittest.TestCase):
    def test_committed_files_are_fresh(self):
        self.assertEqual(codegen.stale(), [], "run: .venv/bin/python -m dashboard.codegen")

    def test_every_endpoint_has_a_schema_entry(self):
        for ep in ENDPOINTS:
            op = SCHEMA["paths"][ep.path][ep.method.lower()]
            self.assertEqual(op["operationId"], ep.name)
            self.assertIn("default", op["responses"])
            for s in ep.statuses:
                self.assertIn(str(s), op["responses"])
        self.assertEqual(SCHEMA["openapi"], "3.1.0")
        jsonschema.Draft202012Validator.check_schema(
            {"$ref": "#/components/schemas/ErrorResponse", "components": SCHEMA["components"]})

    def test_ts_constructs(self):
        from typing import Literal

        class Inner(Model):
            a: int

        class Fixture(Model):
            """Doc line."""
            req: str
            opt: int | None = None
            kind: Literal["x", "y"]
            items: list[Inner]
            pair: tuple[int, str]
            names: dict[str, list[str]]
            either: int | str
            raw: JsonValue
            flag: bool = False

        class Req(Model):
            x: float
            y: str = "d"

        async def h(call):
            return Reply(Fixture(req="", kind="x", items=[], pair=(1, "a"), names={}, either=1, raw=None))
        eps = (Endpoint("fix", "PUT", "/api/v1/fix/{id:int}", "session", Req, Fixture, h, "fixture", idempotent=True),)
        doc = codegen.openapi(eps)
        ts = codegen.typescript(doc, eps)
        for part in ("/**\n * Doc line.\n */\nexport interface Fixture {",
                     "  readonly req: string;",
                     "  readonly opt: number | null;",          # a response always carries it
                     '  readonly kind: "x" | "y";',
                     "  readonly items: readonly Inner[];",
                     "  readonly pair: readonly [number, string];",
                     "  readonly names: { readonly [key: string]: readonly string[] };",
                     "  readonly either: number | string;",
                     "  readonly raw: JsonValue;",
                     "  readonly flag: boolean;",
                     "export interface Req {\n  readonly x: number;\n  readonly y?: string;\n}",   # a request may omit it
                     'fix: { method: "PUT", path: "/api/v1/fix/{id:int}", access: "session", idempotent: true },',
                     "readonly fix: { readonly request: Req; readonly response: Fixture };"):
            self.assertIn(part, ts)
        self.assertEqual(doc["paths"]["/api/v1/fix/{id:int}"]["put"]["parameters"][0]["name"], "Idempotency-Key")

    def test_unsupported_constructs_are_errors(self):
        for schema in ({"type": "object"}, {"not": {"type": "string"}}, {"type": "array"},
                       {"$ref": "http://elsewhere/x"}, {"type": "object", "properties": {"a-b": {"type": "string"}}},
                       {"allOf": [{"type": "string"}]}, {"type": "weird"}):
            with self.subTest(schema=schema), self.assertRaises(codegen.UnsupportedSchema):
                codegen.ts_type(schema)

    def test_check_cli(self):
        self.assertEqual(codegen.main(["--check"]), 0)
        with mock.patch.object(codegen, "generate", return_value={codegen.SCHEMA_PATH: "{}\n"}):
            with mock.patch("sys.stderr"):
                self.assertEqual(codegen.main(["--check"]), 1)


if __name__ == "__main__":
    unittest.main()
