"""The API schema (design §4, §8, D-209): every request and response body of /api/v1 as a pydantic
model, declared once here and checked by mypy --strict. `python -m dashboard.codegen` turns them
(through routes.ENDPOINTS) into dashboard/schema.json (OpenAPI 3.1) and the frontend's TS types.

Conventions: every model derives from `Model` (unknown request fields are rejected, §8; instances
are immutable); a model is either a request or a response, never both, so its schema is one;
times are ISO 8601 UTC strings. An endpoint's path and query parameters are one `Params` model;
lists page by `PageParams` (docs/phase6c_plan.md §0, §3.2). HTML in a response is a
`SanitizedHtml`, which only dashboard.markdown.render() makes. Each phase adds the models of the
endpoints it builds."""
from __future__ import annotations

import re
import types
from typing import Any, ClassVar, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, model_validator

# §8's codes plus the ones every API needs (§8 says "codes such as"; docs/phase6a_plan.md §0).
ErrorCode = Literal[
    "unauthenticated",      # 401: no (valid) session
    "step_up_required",     # 401: a high-impact write without a fresh authentication (6b)
    "forbidden",            # 403: the role doesn't allow it, or a CSRF / Host check failed
    "not_found",            # 404
    "method_not_allowed",   # 405
    "conflict",             # 409: version mismatch or state conflict; details carry the current row
    "validation",           # 400 / 422: malformed or invalid request
    "payload_too_large",    # 413
    "rate_limited",         # 429
    "maintenance",          # 503: writes fenced (restore, rollback)
    "unavailable",          # 503: the database is not usable (§7.8)
    "internal",             # 500: a bug; the detail is in the server log only
]


class Model(BaseModel):
    # json_schema_serialization_defaults_required: a response always carries every field, so the
    # generated TS type marks fields with a default as present (not optional).
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True,
                              json_schema_serialization_defaults_required=True)


class ErrorBody(Model):
    code: ErrorCode
    message: str
    details: dict[str, JsonValue] | None = None


class ErrorResponse(Model):
    """Every error response (any 4xx/5xx) of every endpoint."""
    error: ErrorBody


class HealthResponse(Model):
    """GET /api/v1/health (public, no secrets): `db` is "ok" or the kind of the database problem
    (store.health(): missing, read_only, disk_full, corrupt, schema_too_new, ...)."""
    status: Literal["ok", "unavailable"]
    db: str


# 5a's grant levels and the owner (docs/phase5a_plan.md §3.2), lowest first.
Role = Literal["viewer", "editor", "manager", "owner"]


class MeUser(Model):
    id: int
    role: Role | None       # the highest role any grant gives (null = no grant yet)


class MeResponse(Model):
    """GET /api/v1/me (public): the signed-in user or null, the session's CSRF token (the client keeps
    it in memory only and sends it as X-CSRF-Token on every write) and the display time zone."""
    user: MeUser | None
    csrf_token: str
    time_zone: str


class ApiSchemaResponse(Model):
    """GET /api/v1/schema.json (session): the OpenAPI 3.1 document of this API (codegen.openapi())."""
    openapi: Literal["3.1.0"]
    info: dict[str, JsonValue]
    paths: dict[str, JsonValue]
    components: dict[str, JsonValue]


# --- path and query parameters (docs/phase6c_plan.md §3.2) -------------------------------------

ParamKind = Literal["str", "int", "bool"]
PARAM_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_INT_RE = re.compile(r"-?[0-9]{1,18}")
_BOOLS = {"true": True, "false": False}


def _param_kind(annotation: Any) -> tuple[ParamKind, bool] | None:
    """(kind, optional) of a parameter field's type, None when it is not one a parameter can be:
    str, int, bool, a Literal of strings, or one of them | None."""
    optional = False
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) != 1:
            return None
        annotation, optional = args[0], True
    if get_origin(annotation) is Literal:
        values = get_args(annotation)
        return ("str", optional) if values and all(isinstance(v, str) for v in values) else None
    kinds: dict[Any, ParamKind] = {str: "str", int: "int", bool: "bool"}
    kind = kinds.get(annotation)
    return None if kind is None else (kind, optional)


class Params(Model):
    """An endpoint's path and query parameters, one model per endpoint (`Endpoint.params`): unknown
    keys are refused (extra="forbid") and every field is a str, int, bool, a Literal of strings or
    one of them | None. They arrive as strings, so only here a string is coerced: to an int when it
    is a plain decimal integer, to a bool when it is "true" or "false". Anything else stays a string
    and fails the strict check ("1.5" or "true" for an int, "1" for a bool), so the coercion can't
    weaken the strict models elsewhere."""
    _param_kinds: ClassVar[dict[str, ParamKind]] = {}

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        kinds: dict[str, ParamKind] = {}
        for name, field in cls.model_fields.items():
            found = _param_kind(field.annotation)
            if found is None or not PARAM_NAME_RE.fullmatch(name) or field.alias not in (None, name):
                raise TypeError(f"{cls.__name__}.{name}: a parameter is a lower_snake_case field of type "
                                "str, int, bool or a Literal of strings (or one of them | None), without an alias")
            kinds[name] = found[0]
        cls._param_kinds = kinds

    @classmethod
    def param_kind(cls, name: str) -> ParamKind | None:
        return cls._param_kinds.get(name)

    @model_validator(mode="before")
    @classmethod
    def _from_strings(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for name, value in data.items():
            if not isinstance(value, str):
                continue
            kind = cls._param_kinds.get(name)
            if kind == "int" and _INT_RE.fullmatch(value):
                out[name] = int(value)
            elif kind == "bool" and value in _BOOLS:
                out[name] = _BOOLS[value]
        return out


PAGE_LIMIT_DEFAULT = 50
PAGE_LIMIT_MAX = 200


class PageParams(Params):
    """The paging convention of every long list (docs/phase6c_plan.md §0): newest first, at most
    `limit` rows, and `before` an id cursor (only rows with a smaller id). The response carries
    `next_before`, the cursor of the next page (null: nothing left; routes.page() computes it).
    An endpoint with another cap redeclares `limit` with its own default and `le`."""
    limit: int = Field(default=PAGE_LIMIT_DEFAULT, ge=1, le=PAGE_LIMIT_MAX)
    before: int | None = Field(default=None, ge=1)


# --- HTML -------------------------------------------------------------------------------------

class SanitizedHtml(RootModel[str]):
    """HTML rendered from markdown on the server and allowlist-sanitised (dashboard.markdown.render(),
    docs/phase6c_plan.md §0, design §9.8): the only HTML a response may carry. Validation always
    refuses, so a handler can't pass a plain string where a response field is SanitizedHtml; render()
    is the only place that makes one (model_construct, checked by a test). The schema names it with
    `x-brand`, and the generated TS type is branded, so the frontend's one raw-HTML sink takes
    nothing else. Responses only: never a request field."""
    model_config = ConfigDict(frozen=True, strict=True, json_schema_extra={"x-brand": "SanitizedHtml"})

    @model_validator(mode="before")
    @classmethod
    def _only_from_render(cls, data: Any) -> Any:
        raise ValueError("SanitizedHtml comes only from dashboard.markdown.render()")
