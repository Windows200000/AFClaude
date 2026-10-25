"""The endpoint table and its dispatcher (design §8, §9.4; docs/phase6a_plan.md §3.2, §3.3).

ENDPOINTS is the only list of API routes: the Starlette routes (app.py), the schema's `paths`
(codegen.py), the route-table test and (W5) the role matrix are all derived from it, so a route
can't exist outside it. Each endpoint is closed by default: `access="session"` unless it is one of
PUBLIC_ALLOWED (§9.4: health, `me`, and later the login, setup and OIDC endpoints and
machines/register), and a session endpoint needs the role it declares (`role`, default "owner"; W5,
the route × role matrix of W9 checks it).

A request runs, in this order: the write guard (writes only: JSON, same Origin, Sec-Fetch-Site),
the session (the SessionBackend create_app was given: dashboard.sessions.SessionStore), the CSRF
token (writes only: X-CSRF-Token equal to the session's; a public write such as the login needs a
pre-session, from `me`), the principal and its role (session endpoints: 401 without a signed-in
user, 403 for a machine user or a role below the endpoint's), the path and query parameters (the
endpoint's Params model, docs/phase6c_plan.md §3.2; an endpoint without one takes no query
parameter), the body (writes: at most BODY_MAX bytes, JSON without duplicate keys, the endpoint's
request model with unknown fields rejected), the Idempotency-Key (idempotent writes), then the
handler. A handler returns a Reply whose model is exactly the endpoint's response model and whose
status is one it declares, at most RESPONSE_MAX bytes; anything else is a 500, so no response can
leave outside the schema. Lists page (api_types.PageParams, page()).

Which projects a principal may see is a hook (`visible_projects`, 5a's roles.visible_projects
fills it); handlers ask Call.visible_projects() and filter by it.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

from pydantic import JsonValue, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

import actions
import store
from dashboard.api_types import PARAM_NAME_RE, ApiSchemaResponse, HealthResponse, MeResponse, MeUser, Model, Params, Role
from dashboard.errors import ApiError, error_response, internal
from dashboard.security import WRITE_METHODS, PublicUrl, csrf_guard, read_body, write_guard

log = logging.getLogger("afclaude.web")

Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
Access = Literal["public", "session"]
API_PREFIX = "/api/v1/"
# §9.4: the only endpoints reachable without a session (W6/W7/6b/5a add theirs by name). `me` answers
# an anonymous browser too: it starts the pre-session whose CSRF token the login and setup posts carry.
PUBLIC_ALLOWED = frozenset({"health", "me"})
# The least a session endpoint needs: "signed_in" = any signed-in person, a grant or not (the "no
# access yet" view, sign out); then 5a's roles, each including the ones before it.
MinRole = Literal["signed_in", "viewer", "editor", "manager", "owner"]
ROLE_RANK: dict[str, int] = {"signed_in": 0, "viewer": 1, "editor": 2, "manager": 3, "owner": 4}
M = TypeVar("M", bound=Model)
P = TypeVar("P", bound=Params)
T = TypeVar("T")
QUERY_MAX = 2048                # bytes of the raw query string
RESPONSE_MAX = 256 * 1024       # docs/phase6c_plan.md §0: a longer list or doc pages instead
PATH_PARAM_RE = re.compile(r"\{([^{}]*)\}")
FIELD_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class Principal:
    """The signed-in user of a request, as 5a's users and grants describe it: a person or a machine
    user (refused: no dashboard login, §9.4), and the highest role any grant gives it (None = no
    grant). The per-project check is actions.perform's (5a A3); the dispatcher gates by this."""
    user_id: int
    kind: Literal["person", "machine"] = "person"
    role: Role | None = None


def role_allows(role: Role | None, needed: MinRole) -> bool:
    return needed == "signed_in" or (role is not None and ROLE_RANK[role] >= ROLE_RANK[needed])


@dataclass(frozen=True)
class Session:
    """The browser session of a request (dashboard.sessions): its stored id hash, its CSRF token
    (derived from the cookie's id, returned only by `me`) and its user (None = a pre-session)."""
    id_sha256: str
    csrf_token: str
    principal: Principal | None
    factors: tuple[str, ...] = ()
    authenticated_at: str | None = None     # the last full authentication (the step-up window)


class SessionBackend(Protocol):
    async def authenticate(self, request: Request) -> Session | None:
        """The request's live session (None: no cookie, an unknown, ended or expired one)."""
        ...

    async def start_pre(self, request: Request) -> tuple[Session, str]:
        """A new pre-session for an anonymous browser and its Set-Cookie value."""
        ...


class NoSessions:
    """The backend without sessions (tests of the skeleton): nobody is signed in, every session
    endpoint answers 401; starting a session fails (500, logged), never silently (D-235)."""
    async def authenticate(self, request: Request) -> Session | None:
        return None

    async def start_pre(self, request: Request) -> tuple[Session, str]:
        raise NotImplementedError("no session store (dashboard.sessions.SessionStore)")


no_sessions = NoSessions()


@dataclass(frozen=True)
class Visibility:
    """The projects a principal may see (5a's roles.visible_projects): every project (owners) or
    the listed project ids."""
    all_projects: bool
    project_ids: frozenset[int] = frozenset()

    def allows(self, project_id: int) -> bool:
        return self.all_projects or project_id in self.project_ids


VisibleProjects = Callable[[Principal], Awaitable[Visibility]]


async def no_grants(principal: Principal) -> Visibility:
    """The visibility hook until 5a's grants exist: there is nothing to decide it from, so a handler
    that asks fails (500, logged), never with a guessed default (D-235)."""
    raise NotImplementedError("project visibility needs 5a's grants (roles.visible_projects)")


@dataclass(frozen=True)
class AppContext:
    public: PublicUrl
    db_path: str | None                 # None = store.DB_PATH
    schema: Mapping[str, Any]           # the OpenAPI document (codegen.openapi)
    sessions: SessionBackend = no_sessions
    visible_projects: VisibleProjects = no_grants


@dataclass(frozen=True)
class Call:
    """What a handler gets."""
    request: Request
    ctx: AppContext
    session: Session | None
    principal: Principal | None
    params: Params | None
    body: Model | None
    idempotency_key: str | None

    def json_body(self, model: type[M]) -> M:
        if not isinstance(self.body, model):
            raise TypeError(f"expected a {model.__name__} body, got {type(self.body).__name__}")
        return self.body

    def typed_params(self, model: type[P]) -> P:
        if not isinstance(self.params, model):
            raise TypeError(f"expected {model.__name__} parameters, got {type(self.params).__name__}")
        return self.params

    async def visible_projects(self) -> Visibility:
        """The projects the signed-in principal may see (the hook create_app was given)."""
        if self.principal is None:
            raise RuntimeError("visible_projects needs a signed-in principal")
        return await self.ctx.visible_projects(self.principal)


@dataclass(frozen=True)
class Reply:
    model: Model
    status: int = 200
    cookie: str | None = None           # a Set-Cookie value (dashboard.sessions makes them)


Handler = Callable[[Call], Awaitable[Reply]]


@dataclass(frozen=True)
class Endpoint:
    name: str                           # unique; the operationId and the TS client's function name
    method: Method
    path: str                           # Starlette path syntax, under /api/v1/
    access: Access
    request: type[Model] | None         # the body model of a write; None = no body
    response: type[Model]
    handler: Handler
    summary: str
    statuses: tuple[int, ...] = (200,)  # the non-error statuses the handler may return
    idempotent: bool = False            # a write that needs an Idempotency-Key (§8)
    params: type[Params] | None = None  # the path and query parameters; None = none (§3.2)
    role: MinRole = "owner"             # session endpoints: the least role (deny by default); public: unused

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.name):
            raise ValueError(f"endpoint name {self.name!r}: lower_snake_case")
        if not self.path.startswith(API_PREFIX):
            raise ValueError(f"{self.name}: the path must start with {API_PREFIX}")
        if self.role not in ROLE_RANK:
            raise ValueError(f"{self.name}: unknown role {self.role!r}")
        is_write = self.method in WRITE_METHODS
        if not is_write and (self.request is not None or self.idempotent):
            raise ValueError(f"{self.name}: a {self.method} takes no body and no idempotency key")
        if not self.statuses or any(not 200 <= s <= 299 and s != 503 for s in self.statuses):
            raise ValueError(f"{self.name}: statuses must be 2xx (or 503 for health-like reads)")
        fields = self.params.model_fields if self.params is not None else {}
        for name in PATH_PARAM_RE.findall(self.path):
            if not PARAM_NAME_RE.fullmatch(name):
                raise ValueError(f"{self.name}: path parameter {{{name}}}: a lower_snake_case name without "
                                 "a convertor (the params model coerces)")
            field = fields.get(name)
            if (self.params is None or field is None or not field.is_required()
                    or self.params.param_kind(name) == "bool"):
                raise ValueError(f"{self.name}: path parameter {name} needs a required str or int field in params")


def check_table(endpoints: tuple[Endpoint, ...]) -> None:
    """The table's invariants (create_app refuses to start without them): unique names and
    (method, path) pairs, and no public endpoint outside PUBLIC_ALLOWED (deny by default, §9.4)."""
    names = [e.name for e in endpoints]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate endpoint names: {sorted(n for n in set(names) if names.count(n) > 1)}")
    pairs = [(e.method, e.path) for e in endpoints]
    if len(set(pairs)) != len(pairs):
        raise ValueError("two endpoints share a method and path")
    public = sorted(e.name for e in endpoints if e.access == "public" and e.name not in PUBLIC_ALLOWED)
    if public:
        raise ValueError(f"public endpoints outside PUBLIC_ALLOWED (§9.4): {public}")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _fields(e: ValidationError) -> list[JsonValue]:
    """A validation error's field paths and messages, never the input. An unknown key is the
    client's text: it is named only when it looks like a field name."""
    out: list[JsonValue] = []
    for err in e.errors(include_url=False, include_input=False, include_context=False):
        parts = [str(p) for p in err["loc"]]
        if err["type"] == "extra_forbidden" and parts and not FIELD_NAME_RE.fullmatch(parts[-1]):
            parts[-1] = "?"
        out.append({"loc": ".".join(parts), "msg": err["msg"], "type": err["type"]})
    return out


def parse_params(ep: Endpoint, request: Request) -> Params | None:
    """The path and query parameters of a request as `ep.params` (§3.2): a query string of at most
    QUERY_MAX bytes, no key twice (in the query, or in the query and the path), then the model
    (unknown keys refused; 422 with field paths, never the values). An endpoint without a params
    model takes no query parameter (400, as in 6a)."""
    if len(request.scope.get("query_string", b"")) > QUERY_MAX:
        raise ApiError(400, "validation", "the query string is too long")
    items = request.query_params.multi_items()
    if ep.params is None:
        if items:
            raise ApiError(400, "validation", "this endpoint takes no query parameters")
        return None
    data: dict[str, str] = {k: str(v) for k, v in request.path_params.items()}
    repeated: list[str] = []
    for key, value in items:
        if key in data:
            repeated.append(key)
        data[key] = value
    if repeated:
        fields: list[JsonValue] = [{"loc": k if FIELD_NAME_RE.fullmatch(k) else "?", "msg": "given more than once",
                                    "type": "repeated"} for k in dict.fromkeys(repeated)]
        raise ApiError(422, "validation", "a parameter is given more than once", {"fields": fields})
    try:
        return ep.params.model_validate(data)
    except ValidationError as e:
        raise ApiError(422, "validation", "the parameters are invalid", {"fields": _fields(e)}) from None


def page(rows: Sequence[T], limit: int, row_id: Callable[[T], int]) -> tuple[list[T], int | None]:
    """The response side of the paging convention (api_types.PageParams): `rows` fetched newest first
    with LIMIT limit + 1. Returns the page (the first `limit` rows) and its `next_before` (the id of
    the page's last row when a row is left beyond it, else None)."""
    if limit < 1 or len(rows) > limit + 1:
        raise ValueError(f"page(): limit {limit} with {len(rows)} rows (fetch at most limit + 1)")
    shown = list(rows[:limit])
    return shown, (row_id(shown[-1]) if len(rows) > limit else None)


def parse_body(model: type[Model] | None, raw: bytes) -> Model | None:
    """The body of a write as `model` (None: the endpoint takes no body; empty or {} accepted)."""
    try:
        text = raw.decode("utf-8")
        data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant) if text.strip() else None
    except ValueError:              # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise ApiError(400, "validation", "the body is not valid JSON (UTF-8, no duplicate keys)") from None
    if model is None:
        if data not in (None, {}):
            raise ApiError(422, "validation", "this endpoint takes no body")
        return None
    if not isinstance(data, dict):
        raise ApiError(422, "validation", "the body must be a JSON object")
    try:
        return model.model_validate_json(raw)
    except ValidationError as e:
        raise ApiError(422, "validation", "the request is invalid", {"fields": _fields(e)}) from None


def _idempotency_key(request: Request) -> str:
    keys = request.headers.getlist("idempotency-key")
    if len(keys) != 1 or not UUID_RE.match(keys[0]):
        raise ApiError(400, "validation", "this write needs one Idempotency-Key header (a UUID)")
    return keys[0].lower()


def check_access(ep: Endpoint, principal: Principal | None) -> None:
    """A session endpoint's principal check: 401 without a signed-in user, 403 for a machine user
    (no dashboard login, §9.4) or a role below the endpoint's."""
    if ep.access != "session":
        return
    if principal is None:
        raise ApiError(401, "unauthenticated", "sign in first")
    if principal.kind != "person":
        raise ApiError(403, "forbidden", "machine users can't sign in to the dashboard")
    if not role_allows(principal.role, ep.role):
        raise ApiError(403, "forbidden", "your role doesn't allow this")


async def _dispatch(ep: Endpoint, ctx: AppContext, request: Request) -> Response:
    is_write = ep.method in WRITE_METHODS
    if is_write:
        write_guard(request, ctx.public)
    session = await ctx.sessions.authenticate(request)
    if is_write and (session is not None or ep.access == "public"):
        csrf_guard(request, session.csrf_token if session is not None else None)
    principal = session.principal if session is not None else None
    check_access(ep, principal)
    params = parse_params(ep, request)
    body = parse_body(ep.request, await read_body(request)) if is_write else None
    key = _idempotency_key(request) if ep.idempotent else None
    reply = await ep.handler(Call(request, ctx, session, principal, params, body, key))
    if type(reply.model) is not ep.response or reply.status not in ep.statuses:
        raise RuntimeError(f"{ep.name} returned a {type(reply.model).__name__} with status {reply.status}, "
                           f"declared {ep.response.__name__} with {ep.statuses}")
    out = reply.model.model_dump_json().encode("utf-8")
    if len(out) > RESPONSE_MAX:
        raise RuntimeError(f"{ep.name} answered {len(out)} bytes, over RESPONSE_MAX: it must page")
    headers = {"Set-Cookie": reply.cookie} if reply.cookie is not None else None
    return Response(out, status_code=reply.status, media_type="application/json", headers=headers)


def endpoint_app(ep: Endpoint, ctx: AppContext) -> Callable[[Request], Awaitable[Response]]:
    """The Starlette endpoint of `ep`: every outcome is a JSON response (§8's error shape for errors)."""
    async def run(request: Request) -> Response:
        try:
            return await _dispatch(ep, ctx, request)
        except ApiError as e:
            return error_response(e)
        except store.DBError as e:
            log.error("database error in %s: %s", ep.name, e)
            return error_response(ApiError(503, "unavailable", "the database is unavailable", {"kind": e.kind}))
        except Exception:
            log.exception("internal error in %s", ep.name)
            return error_response(internal())
    return run


# --- handlers ---------------------------------------------------------------------------------

async def health(call: Call) -> Reply:
    problem = await run_in_threadpool(store.health, call.ctx.db_path)
    if problem is None:
        return Reply(HealthResponse(status="ok", db="ok"))
    log.error("health: %s", problem)
    return Reply(HealthResponse(status="unavailable", db=problem.kind), 503)


async def api_schema(call: Call) -> Reply:
    return Reply(ApiSchemaResponse.model_validate(dict(call.ctx.schema), strict=False))


def _display_tz(db_path: str | None) -> str:
    conn = store.connect(db_path)
    try:
        value, source = actions.effective_setting(conn, "window_tz")
    finally:
        conn.close()
    if source.startswith("invalid") or not isinstance(value, str):
        raise ValueError(f"setting window_tz is {source}")
    return value


async def me(call: Call) -> Reply:
    """Who is signed in (or nobody), this session's CSRF token and the display time zone. An
    anonymous browser gets a new pre-session (its cookie and token) for the login and setup posts."""
    session, cookie = call.session, None
    if session is None:
        session, cookie = await call.ctx.sessions.start_pre(call.request)
    p = session.principal
    if p is not None and p.kind != "person":
        raise ApiError(403, "forbidden", "machine users can't sign in to the dashboard")
    tz = await run_in_threadpool(_display_tz, call.ctx.db_path)
    user = MeUser(id=p.user_id, role=p.role) if p is not None else None
    return Reply(MeResponse(user=user, csrf_token=session.csrf_token, time_zone=tz), cookie=cookie)


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("health", "GET", "/api/v1/health", "public", None, HealthResponse, health,
             "Is the backend usable (the database writable and intact)? 503 when not.", statuses=(200, 503)),
    Endpoint("api_schema", "GET", "/api/v1/schema.json", "session", None, ApiSchemaResponse, api_schema,
             "This API's OpenAPI 3.1 document.", role="viewer"),
    Endpoint("me", "GET", "/api/v1/me", "public", None, MeResponse, me,
             "The signed-in user (null when nobody is), this session's CSRF token (send it as X-CSRF-Token "
             "on every write; keep it in memory only) and the display time zone. Starts a pre-session for "
             "an anonymous browser."),
)
