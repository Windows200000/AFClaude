"""mcp_service.py (phase 5b K4/K5): the one serve path (D-343): /health answered by the Gate, everything else
through to the app (token check, hostname header: test_mcp_http.py), the start refusals (unmounted data, a bind beyond
loopback while roles are not on), and the real app behind the gate."""
import asyncio
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401  (hermetic: before the AFClaude imports)
import actions  # noqa: E402
import mcp_http  # noqa: E402
import mcp_service  # noqa: E402
import service  # noqa: E402
import store  # noqa: E402


def call(app, method="POST", path="/mcp", headers=()):
    """Drive an ASGI app once. -> (status, headers dict, JSON body or None, whether the inner app ran)."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)
    scope = {"type": "http", "method": method, "path": path,
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, (json.loads(body) if body else None)


class Inner:
    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope["type"])
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})


class GateTest(unittest.TestCase):
    def setUp(self):
        testenv.fresh_db(self)

    def test_a_request_goes_to_the_app_whose_bearer_middleware_is_the_token_check(self):
        inner = Inner()
        self.assertEqual(call(mcp_service.Gate(inner), headers=(("Authorization", "Bearer x"),))[0], 200)
        self.assertEqual(inner.calls, ["http"])

    def test_health_is_answered_by_the_gate_without_a_token(self):
        inner = Inner()
        status, _, body = call(mcp_service.Gate(inner), "GET", "/health")
        self.assertEqual((status, body["status"], body["problems"]), (200, "ok", []))
        self.assertIsNone(body["inbox_key"])        # the MCP service has no inbox key to report
        self.assertEqual(call(mcp_service.Gate(inner), "POST", "/health")[0], 405)
        self.assertEqual(inner.calls, [])

    def test_health_is_503_for_an_unreachable_database(self):
        with mock.patch.object(store, "connect", side_effect=store.DBUnavailable("gone", "missing")):
            status, _, body = call(mcp_service.Gate(Inner()), "GET", "/health")
        self.assertEqual((status, body["status"]), (503, "unhealthy"))

    def test_lifespan_goes_straight_to_the_app(self):
        inner = Inner()

        async def go():
            await mcp_service.Gate(inner)({"type": "lifespan"}, None, None)
        asyncio.run(go())
        self.assertEqual(inner.calls, ["lifespan"])

    def test_a_websocket_is_refused_and_never_reaches_the_app(self):
        """Review k4a L2: only http (gated) and lifespan reach the SDK app."""
        inner = Inner()
        sent = []

        async def send(msg):
            sent.append(msg)

        async def go():
            await mcp_service.Gate(inner)({"type": "websocket", "path": "/mcp", "headers": []}, None, send)
        asyncio.run(go())
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])
        self.assertEqual(inner.calls, [])

    def test_the_real_mcp_app_refuses_a_request_without_or_with_a_bad_token(self):
        gate = mcp_service.build_app("127.0.0.1", 8000)
        status, hdrs, _ = call(gate, "POST", "/mcp", (("Content-Type", "application/json"),))
        self.assertEqual((status, hdrs["www-authenticate"].split()[0]), (401, "Bearer"))
        status, hdrs, _ = call(gate, "POST", "/mcp", (("Authorization", "Bearer mt_bad"), ("X-AFClaude-Hostname", "h")))
        self.assertEqual(status, 401)
        self.assertEqual(call(gate, "GET", "/health")[0], 200)          # the Gate answers it before the app


class StartTest(unittest.TestCase):
    def test_a_refused_start_exits_78_without_listening(self):
        with mock.patch.object(service, "start", side_effect=service.ServiceError("not mounted")), \
                mock.patch.object(store, "_notify"), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(mcp_service.uvicorn, "run", side_effect=AssertionError("must not listen")):
            self.assertEqual(mcp_service.serve("127.0.0.1", 8000), 78)

    def test_any_other_start_error_exits_78_with_an_alert_not_a_traceback(self):
        """A bad public URL (pydantic) and an unreachable DB end like a refusal (D-235): exit 78, the alert, no listener."""
        for fail in ({"target": mcp_service, "attr": "build_app", "exc": ValueError("bad public url")},
                     {"target": store, "attr": "connect", "exc": store.DBUnavailable("database is locked")}):
            err = io.StringIO()
            with mock.patch.object(service, "start", return_value="id"), \
                    mock.patch.object(fail["target"], fail["attr"], side_effect=fail["exc"]), \
                    mock.patch.object(store, "alert_once") as alert, contextlib.redirect_stderr(err), \
                    mock.patch.object(mcp_service.uvicorn, "run", side_effect=AssertionError("must not listen")):
                self.assertEqual(mcp_service.serve("127.0.0.1", 8000), 78)
            self.assertIn(type(fail["exc"]).__name__, err.getvalue())
            alert.assert_called_once()

    def test_serve_hands_the_gated_app_to_uvicorn(self):
        with mock.patch.object(service, "start", return_value="id"), \
                mock.patch.object(mcp_service.uvicorn, "run") as run, contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(mcp_http, "REQUIRE_REMOTE", False):
            self.assertEqual(mcp_service.serve("127.0.0.1", 8123), 0)
            self.assertTrue(mcp_http.REQUIRE_REMOTE)                       # the serve process refuses a call with no machine
        self.assertIsInstance(run.call_args[0][0], mcp_service.Gate)
        self.assertEqual((run.call_args[1]["host"], run.call_args[1]["port"]), ("127.0.0.1", 8123))

    def test_a_bind_beyond_loopback_is_refused_until_roles_are_on(self):
        """M1, on the service's own bind (the container's 0.0.0.0): exit 78 and no listener while roles_enforce is shadow."""
        testenv.fresh_db(self)
        conn = store.connect()
        self.assertEqual(actions.roles_mode(conn), "shadow")
        err = io.StringIO()
        with mock.patch.object(service, "start", return_value="id"), mock.patch.object(store, "_notify"), \
                contextlib.redirect_stderr(err), \
                mock.patch.object(mcp_service.uvicorn, "run", side_effect=AssertionError("must not listen")):
            self.assertEqual(mcp_service.serve("0.0.0.0", 8000), 78)
        self.assertIn("roles_enforce", err.getvalue())
        with mock.patch.object(service, "start", return_value="id"), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(mcp_service.uvicorn, "run") as run, mock.patch.object(mcp_http, "REQUIRE_REMOTE", False):
            self.assertEqual(mcp_service.serve("127.0.0.1", 8000), 0)        # loopback is fine in shadow
            run.assert_called_once()
        with mock.patch.object(actions, "roles_mode", return_value="on"), \
                mock.patch.object(service, "start", return_value="id"), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(mcp_service.uvicorn, "run") as run, mock.patch.object(mcp_http, "REQUIRE_REMOTE", False):
            self.assertEqual(mcp_service.serve("0.0.0.0", 8000), 0)
            run.assert_called_once()

    def test_the_default_host_is_all_interfaces_only_in_the_container(self):
        with mock.patch.dict(os.environ, {"AFCLAUDE_IN_CONTAINER": "1"}):
            os.environ.pop("AFCLAUDE_MCP_HOST", None)
            self.assertEqual(mcp_service.default_host(), "0.0.0.0")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AFCLAUDE_IN_CONTAINER", None)
            os.environ.pop("AFCLAUDE_MCP_HOST", None)
            self.assertEqual(mcp_service.default_host(), "127.0.0.1")

    def test_the_health_command_reports_a_dead_listener(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mcp_service.health_cli(1), 1)       # nothing listens on port 1
        self.assertIn("unhealthy: no answer", out.getvalue())


if __name__ == "__main__":
    unittest.main()
