"""The MCP server's streamable HTTP transport with machine-token identity (phase 5b K5, docs/phase5b_plan.md §3.3).

`afclaude mcp serve` runs `mcp_server.server` as the SDK's streamable HTTP app under uvicorn. Every request
carries `Authorization: Bearer <machine token>` (5a's machine_tokens, issued by `afclaude machine register`)
and `X-AFClaude-Hostname: <the client's host name>`; the SDK's bearer middleware calls `MachineTokenVerifier`,
which accepts only a well-formed, unrevoked token of an unrevoked machine user, records the use and the
two-hostname clone alert (D-198: actions.authenticate_machine, transport `http`), and answers 401 for anything
else. A refused token (the verifier ran) leaves an audit row (`machine.token_refused`, bounded: see
actions.record_token_refusal; D-235, a refusal is never silent); a request with no `Authorization: Bearer` header
never reaches the verifier and gets the SDK's bare 401 with no audit row (scanners). The audit row's `hostname`
is the client's unvalidated text: whatever shows the audit log must escape it. The authenticated machine reaches a tool call as `remote_machine(context)`; mcp_server.who()
hands it to identity.resolve, so the actor is `machine:<id>` and the server process's own env is never read for
a remote caller. The caller's session comes from the hook's `_afc` stamp only (no Claude session id travels
on the transport, K0): a stamp naming an AFClaude session is never accepted over HTTP, a token's holder stays a
machine user.

The header is required: without it the clone check could not see a second host, so a stolen token used
without the header would go unnoticed. It is a claim of the client, like the token file's host; the token is the
credential. Plan §3.3: no route through a proxy until 8d (D-157/D-198): the port stays internal or on a tunnel.
HTTP is for remote hosts only: this host's sessions use the wrapper (stdio), which is what makes AFClaude-started
sessions task-manager / agent (D-198) and tokenless ones the host owner (D-330); over HTTP a token is required and
the holder is always a machine user.

A bind other than loopback is refused unless `roles_enforce` is `on` (`check_bind`): in shadow mode every valid
token would write like the host owner (D-235: no quiet exposure).
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

import actions
import store

HOSTNAME_HEADER = "x-afclaude-hostname"
DEFAULT_HOST = "127.0.0.1"            # the container passes --host 0.0.0.0 (the service name is then the Host header)
DEFAULT_PORT = 8000
TRANSPORT = "http"
HOSTNAME: ContextVar[str | None] = ContextVar("afclaude_http_hostname", default=None)


class Refused(RuntimeError):
    """The server will not start like this (a message for the owner; D-235)."""


LOOPBACK = ("127.0.0.1", "localhost", "::1")


class HostnameMiddleware:
    """Pure ASGI: puts the request's `X-AFClaude-Hostname` into HOSTNAME for the bearer middleware behind it
    (a TokenVerifier is handed the token only)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        value = next((v.decode("latin-1") for k, v in scope["headers"] if k.decode("latin-1").lower() == HOSTNAME_HEADER),
                     None)
        reset = HOSTNAME.set(value.strip() if value else None)
        try:
            await self.app(scope, receive, send)
        finally:
            HOSTNAME.reset(reset)


def db_path() -> str:
    return os.environ.get("AFCLAUDE_DB") or store.DB_PATH


def authenticate(token: str, hostname: str | None) -> dict[str, Any] | None:
    """The token check on its own connection (this runs in a worker thread: a clone alert goes out through
    notify.py, which can take a while). -> {user_id, token_id, label} or None (the refusal is audited)."""
    conn = store.connect(db_path())
    try:
        who, _ = actions.authenticate_remote(conn, token, hostname, TRANSPORT, via="mcp")
        return who
    finally:
        conn.close()


class MachineTokenVerifier:
    """mcp's TokenVerifier over 5a's machine tokens: `client_id` = `machine:<user id>`, `claims` = the machine."""

    async def verify_token(self, token: str) -> AccessToken | None:
        who = await asyncio.to_thread(authenticate, token, HOSTNAME.get())
        if who is None:
            return None
        return AccessToken(token=f"machine-token:{who['token_id']}", client_id=f"machine:{who['user_id']}", scopes=[],
                           claims={"user_id": who["user_id"], "token_id": who["token_id"], "label": who["label"]})


def remote_machine(context: Any) -> dict[str, Any] | None:
    """The machine behind an HTTP tool call (the claims the verifier set), None for a stdio or direct call (no
    HTTP request, no request context). An HTTP request without an authenticated machine is an error, never a
    guess (D-235): the bearer middleware refuses those before a tool runs."""
    try:
        request = getattr(getattr(context, "request_context", None), "request", None)
    except ValueError:          # the SDK's Context raises while it has no request context (a direct call)
        return None
    if request is None:
        return None
    user = request.scope.get("user")
    claims = user.access_token.claims if isinstance(user, AuthenticatedUser) else None
    if not isinstance(claims, Mapping) or "user_id" not in claims:
        raise PermissionError("an HTTP call without an authenticated machine token")
    return dict(claims)


def placeholder_url() -> str:
    """What the server object is built with at import (the SDK wants auth settings next to a token verifier);
    `serve` replaces it. No env is read here: a bad AFCLAUDE_PUBLIC_URL must not stop a stdio server."""
    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def public_url_of(given: str | None, port: int) -> str:
    return (given or os.environ.get("AFCLAUDE_PUBLIC_URL") or f"http://{DEFAULT_HOST}:{port}").rstrip("/")


def auth_settings(url: str) -> AuthSettings:
    """What the 401 advertises (RFC 9728 metadata URL); no OAuth server exists behind it: Claude Code with an
    Authorization header set does not go on to OAuth (K0). `validate_token_resource` is off: the verifier owns
    the token."""
    return AuthSettings(issuer_url=url, resource_server_url=url, validate_token_resource=False)  # type: ignore[arg-type]  # pydantic coerces the str to AnyHttpUrl


def check_bind(host: str, mode: str) -> None:
    """Refuse a non-loopback bind unless roles are enforced (`mode` is the roles_enforce setting): in shadow mode a
    valid token writes like the host owner, so the port must not be reachable beyond this machine (M1)."""
    if host not in LOOPBACK and mode != "on":
        raise Refused(f"refusing to listen on {host}: roles_enforce is {mode!r}, not 'on', so every machine token "
                      "would write like the host owner; bind to 127.0.0.1 (a tunnel) or set roles_enforce on first "
                      "(tasks.py roles ...)")


def check_stdio_env(env: Mapping[str, str]) -> None:
    """Inside the container (`AFCLAUDE_IN_CONTAINER`) a token in the env that the host wrapper did not bring (it
    always brings AFCLAUDE_MACHINE_HOST with it) is a token of the container's own: refused, or a host session
    without a token file would resolve as that machine instead of the host owner (D-330, L1)."""
    if env.get("AFCLAUDE_IN_CONTAINER") and env.get("AFCLAUDE_MACHINE_TOKEN") and not env.get("AFCLAUDE_MACHINE_HOST"):
        raise Refused("AFCLAUDE_MACHINE_TOKEN is set in the container's own environment: the container carries no "
                      "machine token, the host wrapper passes one per call (remove it from the container's env)")


def build_app(server: Any, host: str = DEFAULT_HOST, public_url: str | None = None,
              port: int = DEFAULT_PORT) -> Starlette:
    """The ASGI app of `server` (an MCPServer built with MachineTokenVerifier): token required on every request,
    the hostname header read before the bearer middleware. A bind host other than loopback (the container's
    0.0.0.0) turns the SDK's DNS-rebinding check off (K0: it answered 421 for the service name)."""
    server.settings.auth = auth_settings(public_url_of(public_url, port))
    app: Starlette = server.streamable_http_app(host=host)
    app.add_middleware(HostnameMiddleware)
    return app


def serve(server: Any, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, public_url: str | None = None) -> None:
    conn = store.connect(db_path())
    try:
        check_bind(host, actions.roles_mode(conn))
    finally:
        conn.close()
    import uvicorn          # not needed by the stdio server, which every session starts
    uvicorn.run(build_app(server, host, public_url, port), host=host, port=port, log_level="warning")
