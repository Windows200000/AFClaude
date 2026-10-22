"""The API schema (design §4, §8, D-209): every request and response body of /api/v1 as a pydantic
model, declared once here and checked by mypy --strict. `python -m dashboard.codegen` turns them
(through routes.ENDPOINTS) into dashboard/schema.json (OpenAPI 3.1) and the frontend's TS types.

Conventions: every model derives from `Model` (unknown request fields are rejected, §8; instances
are immutable); a model is either a request or a response, never both, so its schema is one;
times are ISO 8601 UTC strings. Each phase adds the models of the endpoints it builds."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

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


class ApiSchemaResponse(Model):
    """GET /api/v1/schema.json (session): the OpenAPI 3.1 document of this API (codegen.openapi())."""
    openapi: Literal["3.1.0"]
    info: dict[str, JsonValue]
    paths: dict[str, JsonValue]
    components: dict[str, JsonValue]
