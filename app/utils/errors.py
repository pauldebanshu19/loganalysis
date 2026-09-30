from __future__ import annotations

import logging
from enum import Enum
from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("app.errors")


class ErrorCode(str, Enum):
    """Every failure the API can report.

    Published in the OpenAPI schema so generated clients get them as a union
    rather than as free-form strings.
    """

    MISSING_FILE = "missing_file"
    EMPTY_FILE = "empty_file"
    ANALYSIS_NOT_FOUND = "analysis_not_found"
    UPLOAD_TIMEOUT = "upload_timeout"
    FILE_TOO_LARGE = "file_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    SERVER_BUSY = "server_busy"
    UNAUTHORIZED = "unauthorized"
    INTERNAL_ERROR = "internal_error"


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str = Field(description="Human-readable; may be reworded at any time.")
    details: dict[str, Any] = Field(
        default_factory=dict, description="Machine-readable specifics, e.g. the limit."
    )
    request_id: str = Field(description="Echoes X-Request-ID; quote it in a bug report.")


class ErrorResponse(BaseModel):
    """The body of every non-2xx response."""

    error: ErrorDetail


class AppError(Exception):
    """Base class for everything the API reports as a failure.

    Subclasses set ``code``, ``status`` and a default ``message``; anything
    passed as ``details`` is echoed back so a client can act on the specifics
    without parsing prose.
    """

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR
    status: ClassVar[int] = 500
    message: ClassVar[str] = "Something went wrong."
    #: Seconds to put in ``Retry-After``; ``None`` means the error is not worth
    #: retrying and no header is sent.
    retry_after: ClassVar[int | None] = None

    def __init__(
        self,
        message: str | None = None,
        *,
        retry_after: int | None = None,
        **details: Any,
    ) -> None:
        self.detail_message = message or self.message
        self.details = details
        if retry_after is not None:
            self.retry_after = retry_after
        super().__init__(self.detail_message)

    def to_response(self, request_id: str) -> JSONResponse:
        headers = {"X-Request-ID": request_id}
        if self.retry_after is not None:
            headers["Retry-After"] = str(self.retry_after)
        body = ErrorResponse(
            error=ErrorDetail(
                code=self.code,
                message=self.detail_message,
                details=self.details,
                request_id=request_id,
            )
        )
        return JSONResponse(
            status_code=self.status,
            content=body.model_dump(mode="json"),
            headers=headers,
        )


# -- the errors ------------------------------------------------------------


class MissingFile(AppError):
    code = ErrorCode.MISSING_FILE
    status = 400
    message = "No log file in the request: send a multipart `file` field or a text/plain body."


class EmptyFile(AppError):
    code = ErrorCode.EMPTY_FILE
    status = 400
    message = "The uploaded file is empty."


class AnalysisNotFound(AppError):
    code = ErrorCode.ANALYSIS_NOT_FOUND
    status = 404
    message = "No analysis with that id. Results are kept for a limited time and then expire."


class UploadTimeout(AppError):
    code = ErrorCode.UPLOAD_TIMEOUT
    status = 408
    message = "The upload stalled and was cancelled."
    retry_after = 5


class FileTooLarge(AppError):
    code = ErrorCode.FILE_TOO_LARGE
    status = 413
    message = "The file is larger than this server accepts."


class UnsupportedMediaType(AppError):
    code = ErrorCode.UNSUPPORTED_MEDIA_TYPE
    status = 415
    message = "Send multipart/form-data or text/plain containing a text log file."


class ValidationFailed(AppError):
    code = ErrorCode.VALIDATION_ERROR
    status = 422
    message = "A request parameter is invalid."


class RateLimited(AppError):
    code = ErrorCode.RATE_LIMITED
    status = 429
    message = "Too many requests. Slow down and try again shortly."
    retry_after = 60


class ServerBusy(AppError):
    code = ErrorCode.SERVER_BUSY
    status = 503
    message = "Every analysis slot is busy. Try again in a moment."
    retry_after = 5


class Unauthorized(AppError):
    code = ErrorCode.UNAUTHORIZED
    status = 401
    message = "A valid X-API-Key header is required."


class InternalError(AppError):
    code = ErrorCode.INTERNAL_ERROR
    status = 500
    message = "Something went wrong on our side. Quote the request id if you report it."


#: Starlette raises bare HTTPExceptions for things we never raise ourselves --
#: an unrouted path, a method that does not exist.  Mapping them here keeps
#: those responses in the same shape as everything else, rather than letting
#: Starlette's ``{"detail": ...}`` leak out as a second error format.
_STATUS_TO_ERROR: dict[int, type[AppError]] = {
    400: MissingFile,
    401: Unauthorized,
    404: AnalysisNotFound,
    408: UploadTimeout,
    413: FileTooLarge,
    415: UnsupportedMediaType,
    422: ValidationFailed,
    429: RateLimited,
    503: ServerBusy,
}


def request_id_of(request: Request) -> str:
    """The id assigned to this request by the middleware.

    Falls back to a placeholder so that an error raised before the middleware
    ran still produces a well-formed body.
    """
    return getattr(request.state, "request_id", "req_unknown")


def install_error_handlers(app: FastAPI) -> None:
    """Point every kind of failure at the one response shape."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        count_error(exc.code)
        return exc.to_response(request_id_of(request))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's own validation failures go through the same shape, so a
        # client never has to handle a second error format.
        problems = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())),
                "problem": err.get("msg", ""),
            }
            for err in exc.errors()
        ]
        error = ValidationFailed(
            problems[0]["problem"] if len(problems) == 1 else ValidationFailed.message,
            problems=problems,
        )
        count_error(error.code)
        return error.to_response(request_id_of(request))

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        error_cls = _STATUS_TO_ERROR.get(exc.status_code, InternalError)
        detail = exc.detail if isinstance(exc.detail, str) else None
        error = error_cls(detail)
        # Keep the original status: 405 must not become 500 just because it has
        # no dedicated subclass.
        error.status = exc.status_code  # type: ignore[misc]
        count_error(error.code)
        response = error.to_response(request_id_of(request))
        for header, value in (exc.headers or {}).items():
            response.headers.setdefault(header, value)
        return response

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = request_id_of(request)
        # The stack trace goes to the log with the request id and nothing about
        # the failure goes to the client, so a bug cannot leak internals.
        log.exception(
            "unhandled error",
            extra={"request_id": request_id, "path": request.url.path},
        )
        error = InternalError()
        count_error(error.code)
        return error.to_response(request_id)


def count_error(code: ErrorCode) -> None:
    """Record the error for ``/metrics``.

    Imported lazily because ``metrics`` imports settings, and an error can be
    raised during startup before those exist.
    """
    from app.utils.metrics import errors_total

    errors_total.labels(code=code.value).inc()


__all__ = [
    "AnalysisNotFound",
    "AppError",
    "EmptyFile",
    "ErrorCode",
    "count_error",
    "ErrorDetail",
    "ErrorResponse",
    "FileTooLarge",
    "InternalError",
    "MissingFile",
    "RateLimited",
    "ServerBusy",
    "Unauthorized",
    "UnsupportedMediaType",
    "UploadTimeout",
    "ValidationFailed",
    "install_error_handlers",
    "request_id_of",
]
