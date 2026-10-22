"""The endpoint table and its dispatcher (design §8, §9.4; docs/phase6a_plan.md §3.2, §3.3).

ENDPOINTS is the only list of API routes: the Starlette routes (app.py), the schema's `paths`
(codegen.py), the route-table test and (W5) the role matrix are all derived from it, so a route
can't exist outside it. Each endpoint is closed by default: `access="session"` unless it is one of
PUBLIC_ALLOWED (§9.4: health, and later the login, setup and OIDC endpoints and machines/register).

A request runs, in this order: the write guard (writes only: JSON, same Origin, Sec-Fetch-Site),
the session (401 without one on a session endpoint), the path and query parameters (the
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
from typing import Any, Literal, TypeVar

from pydantic import JsonValue, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

import store
from dashboard.api_types import PARAM_NAME_RE, ApiSchemaResponse, HealthResponse, Model, Params
from dashboard.errors import ApiError, error_response, internal
from dashboard.security import WRITE_METHODS, PublicUrl, read_body, write_guard

log = logging.getLogger("afclaude.web")

Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
Access = Literal["public", "session"]
API_PREFIX = "/api/v1/"
# §9.4: the only endpoints reachable without a session (W6/W7/6b/5a add theirs by name).
PUBLIC_ALLOWED = frozenset({"health"})
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
    """The signed-in user of a request (W5 fills it from the session)."""
    user_id: int


Authenticator = Callable[[Request], Awaitable[Principal | None]]


async def no_sessions(request: Request) -> Principal | None:
    """The authenticator until sessions exist (W5): nobody is signed in, every session endpoint
    answers 401."""
    return None


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
    visible_projects: VisibleProjects = no_grants


@dataclass(frozen=True)
class Call:
    """What a handler gets."""
    request: Request
    ctx: AppContext
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

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.name):
            raise ValueError(f"endpoint name {self.name!r}: lower_snake_case")
        if not self.path.startswith(API_PREFIX):
            raise ValueError(f"{self.name}: the path must start with {API_PREFIX}")
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


async def _dispatch(ep: Endpoint, ctx: AppContext, authenticate: Authenticator, request: Request) -> Response:
    is_write = ep.method in WRITE_METHODS
    if is_write:
        write_guard(request, ctx.public)
    principal = await authenticate(request)
    if ep.access == "session" and principal is None:
        raise ApiError(401, "unauthenticated", "sign in first")
    params = parse_params(ep, request)
    body = parse_body(ep.request, await read_body(request)) if is_write else None
    key = _idempotency_key(request) if ep.idempotent else None
    reply = await ep.handler(Call(request, ctx, principal, params, body, key))
    if type(reply.model) is not ep.response or reply.status not in ep.statuses:
        raise RuntimeError(f"{ep.name} returned a {type(reply.model).__name__} with status {reply.status}, "
                           f"declared {ep.response.__name__} with {ep.statuses}")
    out = reply.model.model_dump_json().encode("utf-8")
    if len(out) > RESPONSE_MAX:
        raise RuntimeError(f"{ep.name} answered {len(out)} bytes, over RESPONSE_MAX: it must page")
    return Response(out, status_code=reply.status, media_type="application/json")


def endpoint_app(ep: Endpoint, ctx: AppContext, authenticate: Authenticator) -> Callable[[Request], Awaitable[Response]]:
    """The Starlette endpoint of `ep`: every outcome is a JSON response (§8's error shape for errors)."""
    async def run(request: Request) -> Response:
        try:
            return await _dispatch(ep, ctx, authenticate, request)
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


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("health", "GET", "/api/v1/health", "public", None, HealthResponse, health,
             "Is the backend usable (the database writable and intact)? 503 when not.", statuses=(200, 503)),
    Endpoint("api_schema", "GET", "/api/v1/schema.json", "session", None, ApiSchemaResponse, api_schema,
             "This API's OpenAPI 3.1 document."),
)
