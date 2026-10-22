#!/usr/bin/env python3
"""Tests for dashboard/ (phase 6a W1/W2, docs/phase6a_plan.md; 6c R0, docs/phase6c_plan.md §3.2):
the endpoint table (deny by default), the error shape on every error path, the write guard, the
body and idempotency rules, path and query parameters, paging, the visibility hook, SanitizedHtml,
the response cap, the headers and CSP, the Host check, the app shell and assets, health, and the
generated schema and TS types (stale = fail; every API response in these tests is validated
against the committed schema.json).

Needs the project venv (Starlette, pydantic; Python 3.12):
    .venv/bin/python -m unittest test_dashboard
"""
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from typing import Literal
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# The dashboard needs Python 3.12 with the .venv's web stack (in the image through mcp, so the
# container's python3 runs these too); under another interpreter every test here is skipped (with the reason),
# like test_typing without mypy, so a combined `python3 -m unittest discover` stays green.
_MISSING = [m for m in ("starlette", "pydantic", "jsonschema") if importlib.util.find_spec(m) is None]
SKIP = (f"needs Python 3.12 with {', '.join(_MISSING) or 'the web stack'} (not {sys.executable}): "
        "run with .venv/bin/python -m unittest test_dashboard") if _MISSING or sys.version_info < (3, 12) else ""
PUBLIC = "https://afc.example.test"
ORIGIN = PUBLIC
KEY = "0b7e2a0c-5d1e-4c55-9a0f-3b8c1d2e4f60"

if SKIP:
    ENDPOINTS = TEST_ENDPOINTS = ()   # placeholders: every TestCase below is skipped
else:
    import testenv  # noqa: E402,F401  (temp DB first)
    import jsonschema  # noqa: E402
    from pydantic import Field, JsonValue, ValidationError  # noqa: E402
    from starlette.testclient import TestClient  # noqa: E402

    import store  # noqa: E402
    from dashboard import codegen, routes, security  # noqa: E402
    from dashboard.api_types import Model, PageParams, Params, SanitizedHtml  # noqa: E402
    from dashboard.app import create_app  # noqa: E402
    from dashboard.routes import ENDPOINTS, Call, Endpoint, Principal, Reply, Visibility  # noqa: E402

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


    async def too_big(call: Call) -> Reply:
        return Reply(EchoResponse(text="x" * routes.RESPONSE_MAX, n=0, key=None, user=None))


    class ItemParams(Params):
        id: int
        state: Literal["open", "done"] | None = None
        own: bool = False
        q: str | None = None


    class ItemResponse(Model):
        id: int
        state: Literal["open", "done"] | None
        own: bool
        q: str | None


    async def item(call: Call) -> Reply:
        p = call.typed_params(ItemParams)
        return Reply(ItemResponse(id=p.id, state=p.state, own=p.own, q=p.q))


    class LogParams(PageParams):
        limit: int = Field(default=3, ge=1, le=5)


    class LogPage(Model):
        ids: list[int]
        next_before: int | None


    async def log_page(call: Call) -> Reply:
        p = call.typed_params(LogParams)
        rows = [i for i in range(10, 0, -1) if p.before is None or i < p.before][:p.limit + 1]
        shown, next_before = routes.page(rows, p.limit, lambda r: r)
        return Reply(LogPage(ids=shown, next_before=next_before))


    class VisibleResponse(Model):
        all_projects: bool
        ids: list[int]


    async def visible(call: Call) -> Reply:
        v = await call.visible_projects()
        return Reply(VisibleResponse(all_projects=v.all_projects, ids=sorted(v.project_ids)))


    class HtmlResponse(Model):
        html: SanitizedHtml


    async def html_from_str(call: Call) -> Reply:
        return Reply(HtmlResponse(html="<b>x</b>"))     # a plain string: refused (500)


    TEST_ENDPOINTS = ENDPOINTS + (
        Endpoint("echo", "POST", "/api/v1/test/echo", "session", EchoRequest, EchoResponse, echo, "echo",
                 idempotent=True),
        Endpoint("echo_plain", "POST", "/api/v1/test/echo_plain", "session", EchoRequest, EchoResponse, echo, "echo"),
        Endpoint("no_body", "DELETE", "/api/v1/test/no_body", "session", None, EchoResponse, nothing, "no body"),
        Endpoint("boom", "GET", "/api/v1/test/boom", "session", None, EchoResponse, boom, "bug"),
        Endpoint("wrong_model", "GET", "/api/v1/test/wrong", "session", None, EchoResponse, wrong_model, "bug"),
        Endpoint("db_down", "GET", "/api/v1/test/db", "session", None, EchoResponse, db_down, "db"),
        Endpoint("too_big", "GET", "/api/v1/test/big", "session", None, EchoResponse, too_big, "bug"),
        Endpoint("item", "GET", "/api/v1/test/items/{id}", "session", None, ItemResponse, item, "item",
                 params=ItemParams),
        Endpoint("log", "GET", "/api/v1/test/log", "session", None, LogPage, log_page, "paged", params=LogParams),
        Endpoint("visible", "GET", "/api/v1/test/visible", "session", None, VisibleResponse, visible, "visible"),
        Endpoint("html_str", "GET", "/api/v1/test/html", "session", None, HtmlResponse, html_from_str, "bug"),
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
        path = path.split("?", 1)[0]
        op = None
        for template, ops in self.schema["paths"].items():
            if re.fullmatch(re.sub(r"\\\{[^/]*?\\\}", "[^/]+", re.escape(template)), path):
                op = ops.get(method.lower())
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


@unittest.skipIf(bool(SKIP), SKIP)
class Base(unittest.TestCase):
    endpoints = TEST_ENDPOINTS
    signed_in = False
    visible = None          # a visibility hook to inject (5a's roles fill it later)

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
        extra = {"visible_projects": self.visible} if self.visible else {}
        self.app = create_app(PUBLIC, self.dist, auth, db_path=testenv.DB, endpoints=self.endpoints, **extra)
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


class Parameters(Base):
    """Path and query parameters (docs/phase6c_plan.md §3.2, R0)."""
    signed_in = True

    def assertBad(self, path, loc=None, secret="SECRETVALUE"):
        r = self.c.get(path)
        self.assertError(r, 422, "validation")
        self.assertNotIn(secret, r.text)
        if loc is not None:
            self.assertEqual([f["loc"] for f in r.json()["error"]["details"]["fields"]], [loc], r.text)
        return r

    def test_path_int_and_query_types(self):
        r = self.c.get("/api/v1/test/items/42")
        self.assertEqual(r.json(), {"id": 42, "state": None, "own": False, "q": None})
        r = self.c.get("/api/v1/test/items/7?state=done&own=true&q=a%20b")
        self.assertEqual(r.json(), {"id": 7, "state": "done", "own": True, "q": "a b"})
        self.assertEqual(self.c.get("/api/v1/test/items/7?own=false").json()["own"], False)

    def test_bad_values_are_422_without_echo(self):
        self.assertBad("/api/v1/test/items/1.5", "id", secret="1.5")
        self.assertBad("/api/v1/test/items/true", "id", secret="true")
        self.assertBad("/api/v1/test/items/SECRETVALUE", "id")
        self.assertBad("/api/v1/test/items/1?own=1", "own")
        self.assertBad("/api/v1/test/items/1?own=SECRETVALUE", "own")
        self.assertBad("/api/v1/test/items/1?own=", "own")
        self.assertBad("/api/v1/test/items/1?state=SECRETVALUE", "state")

    def test_unknown_parameter(self):
        self.assertBad("/api/v1/test/items/1?sneaky=SECRETVALUE", "sneaky")
        r = self.assertBad("/api/v1/test/items/1?%3Cscript%3E=1", "?")
        self.assertNotIn("script", r.text)

    def test_repeated_parameter(self):
        r = self.assertBad("/api/v1/test/items/1?q=SECRETVALUE&q=b", "q")
        self.assertEqual(r.json()["error"]["details"]["fields"][0]["type"], "repeated")
        self.assertBad("/api/v1/test/items/1?id=2", "id")

    def test_query_length_cap(self):
        self.assertError(self.c.get("/api/v1/test/items/1?q=" + "x" * routes.QUERY_MAX), 400, "validation")

    def test_an_endpoint_without_params_still_refuses_a_query(self):
        for q in ("x=1", "x", "limit=1&before=2"):
            self.assertError(self.c.get("/api/v1/test/visible?" + q), 400, "validation")

    def test_parameters_after_the_session_check(self):
        self.signed_in = False
        self.assertError(self.c.get("/api/v1/test/items/x?sneaky=1"), 401, "unauthenticated")

    def test_params_model_rules(self):
        for bad in ("x: list[str]", "x: float", "x: int | str", "X: int", "x: Literal[1, 2]"):
            with self.subTest(field=bad), self.assertRaises(TypeError):
                exec(f"class P(Params):\n    {bad}\n", {"Params": Params, "Literal": Literal})

    def test_endpoint_path_placeholders(self):
        cases = [("/api/v1/x/{id:int}", ItemParams),     # no convertor: the params model coerces
                 ("/api/v1/x/{id}", None),                 # a placeholder needs params
                 ("/api/v1/x/{own}", ItemParams),          # a bool can't be a path parameter
                 ("/api/v1/x/{q}", ItemParams),            # nor an optional field
                 ("/api/v1/x/{nope}", ItemParams)]
        for path, params in cases:
            with self.subTest(path=path), self.assertRaises(ValueError):
                Endpoint("x", "GET", path, "session", None, ItemResponse, item, "x", params=params)


class Paging(Base):
    signed_in = True

    def test_pages_by_cursor(self):
        seen, before = [], None
        while True:
            r = self.c.get("/api/v1/test/log" + (f"?before={before}" if before else ""))
            self.assertEqual(r.status_code, 200, r.text)
            seen += r.json()["ids"]
            before = r.json()["next_before"]
            if before is None:
                break
        self.assertEqual(seen, list(range(10, 0, -1)))
        self.assertEqual(self.c.get("/api/v1/test/log?limit=5&before=6").json(), {"ids": [5, 4, 3, 2, 1],
                                                                                 "next_before": None})

    def test_limit_and_cursor_bounds(self):
        for q in ("limit=6", "limit=0", "before=0", "limit=-1", "before=1.5"):
            with self.subTest(q=q):
                self.assertError(self.c.get("/api/v1/test/log?" + q), 422, "validation")

    def test_page_helper(self):
        self.assertEqual(routes.page([5, 4, 3], 3, lambda r: r), ([5, 4, 3], None))
        self.assertEqual(routes.page([5, 4, 3, 2], 3, lambda r: r), ([5, 4, 3], 3))
        self.assertEqual(routes.page([], 3, lambda r: r), ([], None))
        with self.assertRaises(ValueError):
            routes.page([5, 4, 3, 2, 1], 3, lambda r: r)

    def test_page_params_defaults(self):
        p = PageParams.model_validate({})
        self.assertEqual((p.limit, p.before), (50, None))


class VisibilityHook(Base):
    signed_in = True

    def test_without_5a_a_handler_asking_fails_loudly(self):
        with self.assertLogs("afclaude.web", "ERROR"):
            self.assertError(self.c.get("/api/v1/test/visible"), 500, "internal")

    def test_injected_hook(self):
        async def hook(principal):
            return Visibility(all_projects=False, project_ids=frozenset({principal.user_id, 3}))
        self.visible = hook
        self.setUp()
        self.assertEqual(self.c.get("/api/v1/test/visible").json(), {"all_projects": False, "ids": [3, 7]})
        v = Visibility(all_projects=False, project_ids=frozenset({3}))
        self.assertTrue(v.allows(3))
        self.assertFalse(v.allows(4))
        self.assertTrue(Visibility(all_projects=True).allows(4))


class ResponseRules(Base):
    signed_in = True

    def test_response_over_the_cap_is_a_bug(self):
        with self.assertLogs("afclaude.web", "ERROR"):
            self.assertError(self.c.get("/api/v1/test/big"), 500, "internal")

    def test_sanitized_html_refuses_a_plain_string(self):
        with self.assertLogs("afclaude.web", "ERROR"):
            self.assertError(self.c.get("/api/v1/test/html"), 500, "internal")
        with self.assertRaises(ValidationError):
            SanitizedHtml("<b>x</b>")
        with self.assertRaises(ValidationError):
            SanitizedHtml.model_validate("<b>x</b>")

    def test_only_the_renderer_makes_sanitized_html(self):
        makers = re.compile(r"SanitizedHtml\.(model_construct|construct)\b|SanitizedHtml\.__new__")
        found = []
        for root, dirs, files in os.walk(HERE):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "data", "__pycache__")]
            for name in files:
                path = os.path.join(root, name)
                if name.endswith(".py") and not name.startswith("test_"):
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        if makers.search(fh.read()):
                            found.append(os.path.relpath(path, HERE))
        self.assertEqual(found, [os.path.join("dashboard", "markdown.py")])


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


@unittest.skipIf(bool(SKIP), SKIP)
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
        class FixParams(Params):
            id: int
            mode: Literal["a", "b"] | None = None

        eps = (Endpoint("fix", "PUT", "/api/v1/fix/{id}", "session", Req, Fixture, h, "fixture", idempotent=True,
                        params=FixParams),)
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
                     'fix: { method: "PUT", path: "/api/v1/fix/{id}", access: "session", idempotent: true },',
                     "readonly fix: { readonly params: FixParams; readonly request: Req; readonly response: Fixture };",
                     'export interface FixParams {\n  readonly id: number;\n  readonly mode?: "a" | "b" | null;\n}'):
            self.assertIn(part, ts)
        params = doc["paths"]["/api/v1/fix/{id}"]["put"]["parameters"]
        self.assertEqual([(p["name"], p["in"], p["required"]) for p in params],
                         [("Idempotency-Key", "header", True), ("id", "path", True), ("mode", "query", False)])

    def test_parameters_match_the_models(self):
        doc = test_schema()
        for ep in TEST_ENDPOINTS:
            op = doc["paths"][ep.path][ep.method.lower()]
            params = [p for p in op.get("parameters", []) if p["in"] != "header"]
            if ep.params is None:
                self.assertEqual(params, [], ep.name)
                self.assertNotIn("x-params", op)
                continue
            fields = ep.params.model_fields
            self.assertEqual([p["name"] for p in params], list(fields), ep.name)
            for p in params:
                in_path = "{" + p["name"] + "}" in ep.path
                self.assertEqual(p["in"], "path" if in_path else "query")
                self.assertEqual(p["required"], in_path or fields[p["name"]].is_required())
            comp = doc["components"]["schemas"][op["x-params"]["$ref"].rsplit("/", 1)[1]]
            self.assertEqual({p["name"]: p["schema"] for p in params}, comp["properties"])
        ts = codegen.typescript(doc, TEST_ENDPOINTS)
        self.assertIn("readonly item: { readonly params: ItemParams; readonly request: null; "
                      "readonly response: ItemResponse };", ts)
        self.assertIn("export interface LogParams {\n  readonly limit?: number;\n  readonly before?: number | null;\n}", ts)

    def test_sanitized_html_is_a_branded_string(self):
        self.assertEqual(SCHEMA["components"]["schemas"]["SanitizedHtml"]["x-brand"], "SanitizedHtml")
        self.assertEqual(SCHEMA["components"]["schemas"]["SanitizedHtml"]["type"], "string")
        with open(codegen.TS_PATH, encoding="utf-8") as fh:
            self.assertIn('export type SanitizedHtml = string & { readonly __brand: "SanitizedHtml" };', fh.read())
        doc = codegen.openapi((Endpoint("h", "GET", "/api/v1/h", "session", None, HtmlResponse, html_from_str, "h"),))
        self.assertIn("readonly html: SanitizedHtml;", codegen.typescript(doc, ()))
        for bad in ({"type": "integer", "x-brand": "X"}, {"type": "string", "x-brand": 1}):
            with self.subTest(schema=bad), self.assertRaises(codegen.UnsupportedSchema):
                codegen.ts_type(bad)

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
