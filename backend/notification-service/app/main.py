"""The HTTP API: `uvicorn app.main:app`. Only the database is needed, so it has no broker,
user-service or email settings. Every response carries X-Correlation-Id, and every error uses
the team's shape: {"error": {"code", "message"}, "correlation_id"}.
"""

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import NO_STORE, Problem, problem_response, router
from .config import Settings
from .correlation import bound_correlation_id
from .db import create_session_factory
from .logging_config import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    if app.state.session_factory is None:
        settings = Settings.from_environment(require_runtime=False)
        configure_logging(settings.log_level)
        app.state.session_factory = create_session_factory(settings)
    yield


async def _correlate_and_log(request: Request, call_next):
    """Each request's correlation ID, access-log line, and a safe 500 for anything unexpected."""
    started = time.monotonic()
    with bound_correlation_id(request.headers.get("x-correlation-id")) as correlation_id:
        try:
            response = await call_next(request)
        except Exception:  # pylint: disable=broad-exception-caught  # always answer the browser
            logger.exception("request failed", extra={"path": request.url.path})
            response = JSONResponse(
                status_code=500,
                headers=NO_STORE,
                content={
                    "error": {"code": "INTERNAL", "message": "Notifications are unavailable."},
                    "correlation_id": correlation_id,
                },
            )
        response.headers["x-correlation-id"] = correlation_id
        if request.url.path != "/health":  # ECS checks it every 30 s
            logger.info(
                "request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "user_sub": getattr(request.state, "user_sub", None),
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                },
            )
        return response


def create_app(session_factory: Callable | None = None) -> FastAPI:
    """`session_factory` is for the tests; the real one is built at startup."""
    app = FastAPI(
        title="Ticketto Notification Service",
        lifespan=_lifespan,
        redirect_slashes=False,  # the gateway only routes the exact paths
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.session_factory = session_factory
    app.middleware("http")(_correlate_and_log)

    @app.exception_handler(Problem)
    async def problem(_request: Request, exc: Problem) -> JSONResponse:
        return problem_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def no_such_endpoint(_request: Request, _exc: StarletteHTTPException) -> JSONResponse:
        # An unknown path, or a known one with the wrong method.
        return problem_response(Problem(404, "NOT_FOUND", "No such endpoint."))

    @app.get("/health")
    def health() -> dict:
        """The process is up. Not routed by the gateway, and doesn't touch the database, so a
        database outage doesn't make ECS replace healthy tasks."""
        return {"status": "ok"}

    app.include_router(router)
    return app


app = create_app()
