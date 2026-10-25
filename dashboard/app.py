"""The dashboard's ASGI app (design §4, §8, §9.8; docs/phase6a_plan.md W1).

create_app() builds it from the endpoint table (routes.ENDPOINTS) and the built frontend
(`static_dir`: index.html + assets/, the esbuild output of dashboard/web, W3):

  /api/v1/...      the endpoints; an unknown /api path is a JSON 404 (405 for a known path with
                   another method), never the app shell
  /assets/...      the hashed bundle files (immutable)
  everything else  the app shell (GET/HEAD; the frontend's router decides), other methods 405

Every response, errors included, goes through security.SecurityMiddleware (Host check, headers,
CSP). Nothing here binds a port: the `afclaude web` service (uvicorn, W10) does, from 8b on.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Mount, Route, compile_path
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp

from dashboard import codegen
from dashboard.errors import ApiError, error_response, internal
from dashboard.routes import (ENDPOINTS, AppContext, Endpoint, SessionBackend, VisibleProjects, check_table,
                              endpoint_app, no_grants)
from dashboard.security import PublicUrl, SecurityMiddleware

log = logging.getLogger("afclaude.web")
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def create_app(public_url: str, static_dir: str, sessions: SessionBackend, *, db_path: str | None = None,
               endpoints: Sequence[Endpoint] = ENDPOINTS, visible_projects: VisibleProjects = no_grants) -> ASGIApp:
    """The app (`sessions`: dashboard.sessions.SessionStore, or routes.no_sessions in the skeleton's
    tests; `visible_projects`: which projects a principal may see, 5a's roles; until then
    tests inject it, routes.no_grants). Refuses to start (raises) on a bad public URL, a broken endpoint table or a
    missing frontend build: an app that would answer wrongly never comes up (D-235)."""
    public = PublicUrl.parse(public_url)
    table = tuple(endpoints)
    check_table(table)
    index_path = os.path.join(static_dir, "index.html")
    assets_dir = os.path.join(static_dir, "assets")
    if not os.path.isfile(index_path) or not os.path.isdir(assets_dir):
        raise FileNotFoundError(f"no built frontend in {static_dir} (index.html and assets/ expected): "
                                "build dashboard/web first")
    with open(index_path, "rb") as fh:
        shell_html = fh.read()
    ctx = AppContext(public=public, db_path=db_path, schema=codegen.openapi(table), sessions=sessions,
                     visible_projects=visible_projects)
    known = [(compile_path(ep.path)[0], ep.method) for ep in table]

    async def shell(request: Request) -> Response:
        return Response(shell_html, media_type="text/html")

    async def api_unknown(request: Request) -> Response:
        allowed = sorted({m for rx, m in known if rx.match(request.url.path)})
        if allowed:
            return error_response(ApiError(405, "method_not_allowed", f"use {', '.join(allowed)}",
                                           headers={"Allow": ", ".join(allowed)}))
        return error_response(ApiError(404, "not_found", "no such API endpoint"))

    routes: list[BaseRoute] = [Route(ep.path, endpoint_app(ep, ctx), methods=[ep.method])
                               for ep in table]
    routes += [
        Route("/api", api_unknown, methods=ALL_METHODS),
        Route("/api/{rest:path}", api_unknown, methods=ALL_METHODS),
        Mount("/assets", StaticFiles(directory=assets_dir, follow_symlink=False)),
        Route("/{rest:path}", shell, methods=["GET"]),
    ]
    app = Starlette(routes=routes, exception_handlers={HTTPException: _http_error, Exception: _internal_error})
    return SecurityMiddleware(app, public)


_HTTP_CODES: dict[int, ApiError] = {
    404: ApiError(404, "not_found", "not found"),
    405: ApiError(405, "method_not_allowed", "method not allowed"),
}


async def _http_error(request: Request, exc: Exception) -> Response:
    """Starlette's own errors (no route, wrong method, a missing asset) in the API's error shape."""
    status = exc.status_code if isinstance(exc, HTTPException) else 500
    known = _HTTP_CODES.get(status)
    if known is None:
        log.error("unexpected HTTP error %s on %s %s", status, request.method, request.url.path)
        return error_response(internal())
    headers = dict(exc.headers or {}) if isinstance(exc, HTTPException) else {}
    return error_response(ApiError(known.status, known.code, known.message, headers=headers))


async def _internal_error(request: Request, exc: Exception) -> Response:
    log.error("internal error on %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(internal())
