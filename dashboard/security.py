"""Hardening that applies to every request (design §9.8; docs/phase6a_plan.md §3.3, §3.4).

- SecurityMiddleware (pure ASGI, outermost): refuses a Host other than the public URL's (a
  localhost-bound app reached through a tunnel is otherwise open to DNS rebinding), and puts the
  security headers and the CSP on every response, errors included. No CORS header is ever sent.
- write_guard(): the CSRF checks of a write (JSON content type, same Origin, Sec-Fetch-Site), run
  by the dispatcher before the session, the body or the handler. Every failure is a 403
  "forbidden" (a cross-site form post included).
- csrf_guard(): the per-session X-CSRF-Token of a write (W5), after the session is known.
- read_body(): the body of a write, at most BODY_MAX bytes, counted while reading (a chunked
  body has no Content-Length to trust).
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from dashboard.errors import ApiError, error_response

BODY_MAX = 64 * 1024     # the largest field is a prompt (16 KB, actions.PROMPT_MAX) plus JSON overhead
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "x-csrf-token"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
       "require-trusted-types-for 'script'")
HEADERS: tuple[tuple[bytes, bytes], ...] = tuple((k.encode(), v.encode()) for k, v in (
    ("content-security-policy", CSP),
    ("x-frame-options", "DENY"),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("strict-transport-security", "max-age=63072000; includeSubDomains"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
))
ASSETS_PREFIX = "/assets/"     # hashed file names: immutable, cacheable; everything else no-store
CACHE_ASSET = b"public, max-age=31536000, immutable"
CACHE_NONE = b"no-store"
# set here, never by a handler (a handler's own value would be dropped)
_OWNED = frozenset(k for k, _ in HEADERS) | {b"cache-control"}


@dataclass(frozen=True)
class PublicUrl:
    """AFCLAUDE_PUBLIC_URL (§10.2), checked: https, or http only for a local host (an SSH tunnel
    before 8d); no path beyond "/", no query, fragment or credentials."""
    origin: str     # scheme://host[:port], what a same-origin browser sends as Origin
    host: str       # host[:port], what the Host header must be

    @classmethod
    def parse(cls, url: str) -> PublicUrl:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("https", "http") or not host:
            raise ValueError(f"the public URL must be https://host[:port], got {url!r}")
        if parts.scheme == "http" and host not in LOCAL_HOSTS:
            raise ValueError(f"the public URL must use https (plain http only for localhost), got {url!r}")
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError(f"the public URL must be only scheme://host[:port], got {url!r}")
        netloc = parts.netloc.lower()
        return cls(origin=f"{parts.scheme}://{netloc}", host=netloc)


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, public: PublicUrl) -> None:
        self.app = app
        self.public = public

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":           # no websockets: closed before the handshake (= 403)
            await send({"type": "websocket.close", "code": 1008})
            return
        path: str = scope.get("path", "")
        is_asset = path.startswith(ASSETS_PREFIX)

        async def send_secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in _OWNED]
                headers.extend(HEADERS)
                ok_asset = is_asset and message["status"] == 200
                headers.append((b"cache-control", CACHE_ASSET if ok_asset else CACHE_NONE))
                message = {**message, "headers": headers}
            await send(message)

        host = _header(scope, b"host")
        if host is None or host.lower() != self.public.host:
            response = error_response(ApiError(403, "forbidden", "unknown host"))
            await response(scope, receive, send_secured)
            return
        await self.app(scope, receive, send_secured)


def _header(scope: Scope, name: bytes) -> str | None:
    values = [v for k, v in scope.get("headers", []) if k.lower() == name]
    if len(values) != 1:                  # missing, or sent twice: refuse rather than pick one
        return None
    return bytes(values[0]).decode("latin-1")


def write_guard(request: Request, public: PublicUrl) -> None:
    """The CSRF checks of §9.8 for a write that need no session (csrf_guard then checks the token).
    Raises ApiError 403."""
    ctype = request.headers.get("content-type", "")
    mime, _, params = ctype.partition(";")
    charset = params.strip().lower()
    if mime.strip().lower() != "application/json" or charset not in ("", "charset=utf-8"):
        raise ApiError(403, "forbidden", "writes must be sent as application/json")
    if request.headers.getlist("origin") != [public.origin]:
        raise ApiError(403, "forbidden", "cross-origin request refused")
    site = request.headers.getlist("sec-fetch-site")
    if site and site != ["same-origin"]:
        raise ApiError(403, "forbidden", "cross-site request refused")


def csrf_guard(request: Request, expected: str | None) -> None:
    """A write's X-CSRF-Token (§9.8): exactly one, equal to the session's token (`expected`; None =
    the request has no session, so it can carry no token). Raises ApiError 403."""
    if expected is None:
        raise ApiError(403, "forbidden", "no session: reload the page")
    sent = request.headers.getlist(CSRF_HEADER)
    if len(sent) != 1 or not hmac.compare_digest(sent[0].encode("utf-8"), expected.encode("utf-8")):
        raise ApiError(403, "forbidden", "missing or wrong CSRF token: reload the page")


async def read_body(request: Request, limit: int = BODY_MAX) -> bytes:
    """The request body, refusing more than `limit` bytes (413) without reading past it."""
    declared = request.headers.getlist("content-length")
    if declared:
        if len(declared) != 1 or not declared[0].isdigit():
            raise ApiError(400, "validation", "bad Content-Length")
        if int(declared[0]) > limit:
            raise ApiError(413, "payload_too_large", f"the request body is larger than {limit} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "payload_too_large", f"the request body is larger than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
