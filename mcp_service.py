#!/usr/bin/env python3
"""The MCP service of the image (phase 5b K4/K5, docs/phase5b_plan.md §3.3): `afclaude mcp serve|health`, the one
MCP HTTP serve path (D-343).

The MCP server's streamable HTTP app (mcp_http.build_app over `mcp_server.server`) under uvicorn on an internal TCP
port (compose publishes none; `compose.tunnel.yml` would, on the host's loopback only). Every MCP request needs a
machine token (`Authorization: Bearer`, the SDK's bearer middleware over mcp_http.MachineTokenVerifier) and the
client's `X-AFClaude-Hostname` (mcp_http.HostnameMiddleware); a refusal is a 401 (`WWW-Authenticate: Bearer`) and,
for a token that reached the verifier, a bounded `machine.token_refused` audit row (D-235). The process refuses a
tool call without an authenticated machine (mcp_http.require_remote).

  GET /health   no token, no secrets: the same body as the web service's (service.health_report); 503
                when unhealthy. Not an MCP route and not forwarded (the Gate answers it before the app).

Start: the data directory must be mounted and the master key must load (service.start; it refuses with a
message, exit 78). A bind other than loopback (the container's default, 0.0.0.0: the SDK's DNS-rebinding
protection is on only for a loopback host, the port is internal to the compose network, K0) is refused, also exit
78, unless `roles_enforce` is `on` (mcp_http.check_bind, M1): the compose MCP service starts only once roles are on.

  mcp_service.py serve [--host H] [--port N] [--public-url U]   the server (`afclaude mcp serve`); host default
                                $AFCLAUDE_MCP_HOST, else 0.0.0.0 in the container and 127.0.0.1 elsewhere; N default
                                $AFCLAUDE_MCP_PORT or 8000; U (what the 401 advertises) default $AFCLAUDE_PUBLIC_URL
  mcp_service.py health [--port N]   GET /health on the loopback: exit 0 healthy (the compose health check)
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import traceback
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import uvicorn
from starlette.concurrency import run_in_threadpool

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import actions  # noqa: E402
import mcp_http  # noqa: E402
import service  # noqa: E402
import store  # noqa: E402

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGI = Callable[[Scope, Receive, Send], Awaitable[None]]

PORT = int(os.environ.get("AFCLAUDE_MCP_PORT") or 8000)
HEALTH_TIMEOUT = 10.0


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
    """ASGI wrapper: /health is answered here; everything else goes to the app, whose bearer middleware is the
    token check (lifespan events go to the app too: its session manager starts there; any other non-http scope, a
    websocket, is refused: nothing here speaks it)."""

    def __init__(self, app: ASGI) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        if scope["path"] == "/health":
            if scope["method"] != "GET":
                await _respond(send, 405, {"error": "method not allowed"})
                return
            status, body = await run_in_threadpool(health_body)     # a DB read: not on the event loop
            await _respond(send, status, body)
            return
        await self.app(scope, receive, send)


def build_app(host: str, port: int, public_url: str | None = None) -> Gate:
    import mcp_server        # imported here: the health check and the start check need none of it
    return Gate(mcp_http.build_app(mcp_server.server, host, public_url, port))


def default_host() -> str:
    return os.environ.get("AFCLAUDE_MCP_HOST") or ("0.0.0.0" if service.in_container() else mcp_http.DEFAULT_HOST)


def check_bind(host: str) -> None:
    """mcp_http.check_bind against the DB's roles_enforce setting (read now: the start check, M1)."""
    conn = store.connect()
    try:
        mcp_http.check_bind(host, actions.roles_mode(conn))
    finally:
        conn.close()


def serve(host: str, port: int, public_url: str | None = None) -> int:
    try:
        service.start("mcp", mounted=service.in_container())
        check_bind(host)
        app = build_app(host, port, public_url)
    except (service.ServiceError, mcp_http.Refused, actions.RolesFailure) as e:
        return service.refuse("mcp", e)
    except Exception as e:      # noqa: BLE001 - any other start error (a bad public URL, an unreachable DB): the same exit and alert
        traceback.print_exc(file=sys.stderr)    # an unexpected error: its traceback goes to the log, the alert has one line
        return service.refuse("mcp", RuntimeError(f"{type(e).__name__}: {e}"))
    mcp_http.require_remote()
    print(f"afclaude mcp: serving on {host}:{port} (machine token and host name header required)", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info", lifespan="on", server_header=False)
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
    ap = argparse.ArgumentParser(description="The AFClaude MCP service (phase 5b K4/K5).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the HTTP server")
    sv.add_argument("--host", default=None, help="the address to listen on (default: see the module doc)")
    sv.add_argument("--public-url", default=None, help="the URL clients reach this server at (default: AFCLAUDE_PUBLIC_URL)")
    for p in (sv, sub.add_parser("health", help="GET /health on the loopback")):
        p.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        return serve(args.host or default_host(), args.port, args.public_url)
    return health_cli(args.port)


if __name__ == "__main__":
    sys.exit(main())
