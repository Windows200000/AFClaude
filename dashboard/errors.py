"""The API's one error shape (design §8): every error is `{"error": {"code", "message", "details"?}}`
with an HTTP status, never a traceback or an internal detail. The client maps the codes to UI
states (sign in again, re-authenticate, reload and retry on a conflict)."""
from __future__ import annotations

from typing import get_args

from pydantic import JsonValue
from starlette.responses import Response

from dashboard.api_types import ErrorBody, ErrorCode, ErrorResponse

ERROR_CODES: tuple[ErrorCode, ...] = get_args(ErrorCode)


class ApiError(Exception):
    """An error answered with the error shape. `message` is shown to the user as is, so it
    must never carry internals (paths, SQL, exception text)."""

    def __init__(self, status: int, code: ErrorCode, message: str,
                 details: dict[str, JsonValue] | None = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        if not 400 <= status <= 599:
            raise ValueError(f"an API error needs a 4xx/5xx status, got {status}")
        self.status = status
        self.code: ErrorCode = code
        self.message = message
        self.details = details
        self.headers = headers or {}


def error_response(err: ApiError) -> Response:
    body = ErrorResponse(error=ErrorBody(code=err.code, message=err.message, details=err.details))
    return Response(body.model_dump_json(), status_code=err.status, media_type="application/json",
                    headers=err.headers)


def internal() -> ApiError:
    return ApiError(500, "internal", "internal error (logged on the server)")
