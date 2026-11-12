import logging
import re
import time
import uuid
from contextvars import ContextVar

from flask import Flask, Response, g, request

HEADER = "X-Correlation-Id"
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")

logger = logging.getLogger("app.request")


def get_correlation_id() -> str:
    return correlation_id_var.get()


def init_app(app: Flask) -> None:
    """Use the caller's X-Correlation-Id, or a new UUID, for the whole request."""

    @app.before_request
    def set_correlation_id() -> None:
        incoming = request.headers.get(HEADER)
        cid = incoming if incoming and _VALID_ID.match(incoming) else str(uuid.uuid4())
        g.correlation_token = correlation_id_var.set(cid)
        g.request_started = time.perf_counter()

    @app.after_request
    def add_correlation_header(response: Response) -> Response:
        response.headers[HEADER] = get_correlation_id()
        started = g.get("request_started")
        level = logging.DEBUG if request.path in ("/health", "/ready") else logging.INFO
        logger.log(
            level,
            "request completed",
            extra={
                "method": request.method,
                "path": request.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1)
                if started is not None
                else None,
            },
        )
        return response

    @app.teardown_request
    def reset_correlation_id(_error: BaseException | None) -> None:
        token = g.pop("correlation_token", None)
        if token is not None:
            correlation_id_var.reset(token)
