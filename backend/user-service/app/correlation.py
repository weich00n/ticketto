import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.context import correlation_id_var
from app.errors import error_response

HEADER = "X-Correlation-Id"
# Accept a caller-supplied ID only if it is short and safe to write into logs.
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

logger = logging.getLogger("app.request")


class CorrelationIdMiddleware:
    """Sets the correlation ID for each request, echoes it in the response header,
    writes one access-log line, and turns any unhandled exception into a 500 in
    the standard error format."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(HEADER)
        cid = incoming if incoming and _VALID_ID.match(incoming) else str(uuid.uuid4())
        token = correlation_id_var.set(cid)
        status = 500
        started = False

        async def send_with_header(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                started = True
                status = message["status"]
                MutableHeaders(scope=message)[HEADER] = cid
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_header)
        except Exception:  # pylint: disable=broad-exception-caught  # last-resort 500 handler
            logger.exception("Unhandled error")
            if started:
                raise
            response = error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")
            await response(scope, receive, send_with_header)
        finally:
            path = scope.get("path", "")
            # Health checks run every few seconds; keep them out of the default log level.
            level = logging.DEBUG if path.startswith("/health") else logging.INFO
            logger.log(
                level,
                "request completed",
                extra={
                    "method": scope.get("method"),
                    "path": path,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                },
            )
            correlation_id_var.reset(token)
