#!/usr/bin/env python3
"""The MCP service of the image (phase 5b K4, docs/phase5b_plan.md §3.3): `afclaude mcp serve`.

The MCP server's streamable HTTP app (`mcp_server.server.streamable_http_app`) under uvicorn on an
internal TCP port (compose publishes none; `compose.tunnel.yml` would, on the host's loopback only), behind
a gate that checks the bearer token of every request. **K4 ships the gate closed**: `authorize` accepts no
token, so every MCP request is a 401 (`WWW-Authenticate: Bearer`) and nothing reaches a tool. K5 puts 5a's
machine-token check there (hash lookup, last_seen, revoked/unknown refused and audited) and passes the
principal on; until then the service only proves the listener, the start check and the compose wiring.

  GET /health   no token, no secrets: the same body as the web service's (service.health_report); 503
                when unhealthy. Not an MCP route and not forwarded.

Start: the data directory must be mounted and the master key must load (service.start; it refuses with a
message, exit 78). The listener binds 0.0.0.0 in the container (the SDK's DNS-rebinding protection is on
only for a loopback host: the port is internal to the compose network, K0).

  mcp_service.py serve [--port N]    the server (`afclaude mcp serve`); N default $AFCLAUDE_MCP_PORT or 8000
  mcp_service.py health [--port N]   GET /health on the loopback: exit 0 healthy (the compose health check)
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import service  # noqa: E402
import store  # noqa: E402

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGI = Callable[[Scope, Receive, Send], Awaitable[None]]

PORT = int(os.environ.get("AFCLAUDE_MCP_PORT") or 8000)
HEALTH_TIMEOUT = 10.0


def reject_all(token: str) -> bool:
    """The gate's closed state (K4): no token is good. K5 replaces it with the machine-token check."""
    return False


async def _respond(send: Send, status: int, body: object, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                            (b"cache-control", b"no-store"), *(extra or [])]})
    await send({"type": "http.response.body", "body": data})


def health_body() -> tuple[int, dict[str, Any]]:
    try:
        conn = store.connect()
    except Exception as e:   # noqa: BLE001 - unreachable is the answer, not a crash
        return 503, {"status": "unhealthy", "problems": [f"database: {type(e).__name__}: {e}"]}
    try:
        body = service.health_report(conn)
    finally:
        conn.close()
    return (200 if body["status"] == "ok" else 503), body


class Gate:
    """ASGI wrapper: /health is answered here, everything else needs a bearer token `authorize` accepts
    (lifespan events go straight to the app: its session manager starts there)."""

    def __init__(self, app: ASGI, authorize: Callable[[str], bool] = reject_all) -> None:
        self.app, self.authorize = app, authorize

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/health":
            if scope["method"] != "GET":
                await _respond(send, 405, {"error": "method not allowed"})
                return
            status, body = health_body()
            await _respond(send, status, body)
            return
        auth = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"authorization"), "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip() or not self.authorize(token.strip()):
            await _respond(send, 401, {"error": "unauthorized"}, [(b"www-authenticate", b"Bearer")])
            return
        await self.app(scope, receive, send)


def build_app(authorize: Callable[[str], bool] = reject_all) -> Gate:
    import mcp_server        # imported here: the health check and the start check need none of it
    return Gate(mcp_server.server.streamable_http_app(host="0.0.0.0"), authorize)


def serve(port: int) -> int:
    try:
        service.start("mcp", mounted=service.in_container())
    except service.ServiceError as e:
        return service.refuse("mcp", e)
    print(f"afclaude mcp: serving on 0.0.0.0:{port} (every MCP request is refused until K5's token check)", flush=True)
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level="info", lifespan="on", server_header=False)
    return 0


def health_cli(port: int) -> int:
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=HEALTH_TIMEOUT)
        c.request("GET", "/health")
        r = c.getresponse()
        text = r.read().decode("utf-8", "replace")
        c.close()
    except (OSError, http.client.HTTPException) as e:
        print(f"unhealthy: no answer on 127.0.0.1:{port}: {type(e).__name__}: {e}")
        return 1
    print(text if r.status == 200 else f"unhealthy: HTTP {r.status}: {text}")
    return 0 if r.status == 200 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The AFClaude MCP service (phase 5b K4).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (("serve", "run the HTTP server"), ("health", "GET /health on the loopback")):
        sub.add_parser(name, help=text).add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    return serve(args.port) if args.cmd == "serve" else health_cli(args.port)


if __name__ == "__main__":
    sys.exit(main())
