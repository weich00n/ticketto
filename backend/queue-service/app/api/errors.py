"""Error types and handlers. Every error body is
{"error": {"code": "<CODE>", "message": "<text>", "details": {...}}}.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.cursor import InvalidCursor
from app.core.timefmt import to_iso

log = logging.getLogger(__name__)

# Path parameters whose malformed values mean "no such resource" rather than a
# bad request: a non-UUID event_id can never match a queue.
_NOT_FOUND_PATH_PARAMS = {
    "event_id": ("EVENT_NOT_FOUND", "No queue exists for this event."),
    "admission_id": ("ADMISSION_NOT_FOUND", "Admission ended or never existed."),
}


class AppError(Exception):
    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "Internal server error."

    def __init__(self, message: str | None = None, details: dict[str, Any] | None = None):
        self.message = message or self.message
        self.details = details or {}
        super().__init__(self.message)


class Unauthenticated(AppError):
    status_code, code = 401, "UNAUTHENTICATED"
    message = "X-User-Sub header missing or not a UUID."


class EventNotFound(AppError):
    status_code, code = 404, "EVENT_NOT_FOUND"
    message = "No queue exists for this event."


class NotInQueue(AppError):
    status_code, code = 404, "NOT_IN_QUEUE"
    message = "You are not waiting in this queue."


class AdmissionNotFound(AppError):
    status_code, code = 404, "ADMISSION_NOT_FOUND"
    message = "Admission ended or never existed."


class QueueNotOpen(AppError):
    status_code, code = 409, "QUEUE_NOT_OPEN"

    def __init__(self, queue_opens_at: datetime):
        opens = to_iso(queue_opens_at)
        super().__init__(f"The queue for this event opens at {opens}.", {"queue_opens_at": opens})


class AlreadyAdmitted(AppError):
    status_code, code = 409, "ALREADY_ADMITTED"
    message = "You have already been admitted and cannot leave the queue."


class EventSoldOut(AppError):
    status_code, code = 410, "EVENT_SOLD_OUT"
    message = "This event is sold out."


class SaleEnded(AppError):
    status_code, code = 410, "SALE_ENDED"
    message = "Sales for this event have ended."


class EventCancelled(AppError):
    status_code, code = 410, "EVENT_CANCELLED"
    message = "This event has been cancelled."


class ServiceUnavailable(AppError):
    status_code, code = 503, "SERVICE_UNAVAILABLE"
    message = "The service is temporarily unavailable. Please retry shortly."


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _response(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error_body(code, message, details))


async def _app_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return _response(exc.status_code, exc.code, exc.message, exc.details)


async def _dependency_down(request: Request, exc: Exception) -> JSONResponse:
    log.warning("dependency unavailable on %s %s: %r", request.method, request.url.path, exc)
    return _response(503, ServiceUnavailable.code, ServiceUnavailable.message)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    for err in exc.errors():
        loc = tuple(err.get("loc", ()))
        if len(loc) == 2 and loc[0] == "path" and loc[1] in _NOT_FOUND_PATH_PARAMS:
            code, message = _NOT_FOUND_PATH_PARAMS[str(loc[1])]
            return _response(404, code, message)
    fields = [
        {"loc": [str(part) for part in e.get("loc", ())], "message": e.get("msg", "")}
        for e in exc.errors()
    ]
    return _response(400, "INVALID_REQUEST", "Request parameters are invalid.", {"errors": fields})


async def _invalid_cursor(_: Request, _exc: Exception) -> JSONResponse:
    # Same shape as other parameter errors (decision 16 in the implementation plan).
    return _response(
        400,
        "INVALID_REQUEST",
        "Request parameters are invalid.",
        {"errors": [{"loc": ["query", "cursor"], "message": "cursor is malformed or expired"}]},
    )


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
    code = codes.get(exc.status_code, "HTTP_ERROR")
    return _response(exc.status_code, code, str(exc.detail))


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return _response(500, AppError.code, AppError.message)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    for exc_type in (RedisConnectionError, RedisTimeoutError, OperationalError, InterfaceError):
        app.add_exception_handler(exc_type, _dependency_down)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(InvalidCursor, _invalid_cursor)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled)
