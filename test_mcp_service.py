"""mcp_service.py (phase 5b K4): the gate in front of the MCP HTTP app is closed (K5 opens it with the machine
token check), /health, the start refusals, and the real app behind the gate."""
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

    def test_every_request_is_a_401_while_the_gate_is_closed(self):
        inner = Inner()
        gate = mcp_service.Gate(inner)
        for headers in ((), (("Authorization", "Bearer something"),), (("Authorization", "Basic abc"),),
                        (("Authorization", "Bearer "),), (("Authorization", "bearer"),)):
            status, hdrs, body = call(gate, headers=headers)
            self.assertEqual((status, hdrs["www-authenticate"], body), (401, "Bearer", {"error": "unauthorized"}), headers)
        self.assertEqual(inner.calls, [])        # nothing reached the app

    def test_a_token_the_authorizer_accepts_reaches_the_app(self):
        inner = Inner()
        seen = []
        gate = mcp_service.Gate(inner, lambda token: seen.append(token) or token == "good")
        self.assertEqual(call(gate, headers=(("Authorization", "Bearer good"),))[0], 200)
        self.assertEqual(call(gate, headers=(("Authorization", "Bearer bad"),))[0], 401)
        self.assertEqual(seen, ["good", "bad"])
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

    def test_the_default_authorizer_accepts_nothing(self):
        self.assertFalse(mcp_service.reject_all("anything"))

    def test_the_real_mcp_app_sits_behind_the_closed_gate(self):
        gate = mcp_service.build_app()
        status, hdrs, _ = call(gate, "POST", "/mcp", (("Content-Type", "application/json"),))
        self.assertEqual((status, hdrs["www-authenticate"]), (401, "Bearer"))


class StartTest(unittest.TestCase):
    def test_a_refused_start_exits_78_without_listening(self):
        with mock.patch.object(service, "start", side_effect=service.ServiceError("not mounted")), \
                mock.patch.object(store, "_notify"), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(mcp_service.uvicorn, "run", side_effect=AssertionError("must not listen")):
            self.assertEqual(mcp_service.serve(8000), 78)

    def test_serve_hands_the_gated_app_to_uvicorn_on_all_interfaces(self):
        with mock.patch.object(service, "start", return_value="id"), \
                mock.patch.object(mcp_service.uvicorn, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mcp_service.serve(8123), 0)
        app = run.call_args[0][0]
        self.assertIsInstance(app, mcp_service.Gate)
        self.assertEqual((run.call_args[1]["host"], run.call_args[1]["port"]), ("0.0.0.0", 8123))

    def test_the_health_command_reports_a_dead_listener(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mcp_service.health_cli(1), 1)       # nothing listens on port 1
        self.assertIn("unhealthy: no answer", out.getvalue())


if __name__ == "__main__":
    unittest.main()
