"""FastAPI app factory. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.api import buyer, internal
from app.api.deps import Services
from app.api.errors import register_error_handlers
from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_session_factory
from app.core.logging import configure_logging
from app.core.redis import create_redis
from app.core.status import RandInt
from app.core.tokens import AdmissionTokens
from app.queue.admin import AdminService
from app.queue.config_repo import ConfigRepository
from app.queue.repository import QueueRepository
from app.queue.service import QueueService


def create_app(settings: Settings | None = None, *, rng: RandInt | None = None) -> FastAPI:
    """Build the API. Tests pass their own settings (and optionally a seeded rng
    for poll jitter); production reads settings from the environment and also
    configures logging (tests keep pytest's log capture)."""
    if settings is None:
        settings = get_settings()
        configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Load keys first: a bad key config should stop startup before any I/O.
        tokens = AdmissionTokens.from_settings(settings)
        redis = create_redis(settings.redis_url)
        engine = create_engine(settings.mysql_url)
        try:
            configs = ConfigRepository(create_session_factory(engine), redis)
            queues = QueueRepository(
                redis,
                selection_window_seconds=settings.selection_window_seconds,
                admission_window_seconds=settings.admission_window_seconds,
            )
            app.state.services = Services(
                settings=settings,
                redis=redis,
                engine=engine,
                configs=configs,
                queues=queues,
                tokens=tokens,
                queue_service=QueueService(configs, queues, tokens, redis, rng),
                admin_service=AdminService(configs, queues, redis),
            )
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Queue Service",
        version="1.0.0",
        summary="Waiting room and admission tokens for ticket sales.",
        lifespan=lifespan,
    )
    register_error_handlers(app)
    app.include_router(buyer.router)
    app.include_router(internal.router)
    app.include_router(internal.ops_router)
    _drop_default_422(app)
    return app


def _drop_default_422(app: FastAPI) -> None:
    """FastAPI documents a 422 on every route, but validation errors here become
    404 (malformed ids) or 400 INVALID_REQUEST, so keep the schema truthful."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = FastAPI.openapi(app)
            for operations in schema.get("paths", {}).values():
                for operation in operations.values():
                    operation.get("responses", {}).pop("422", None)
            for name in ("HTTPValidationError", "ValidationError"):
                schema.get("components", {}).get("schemas", {}).pop(name, None)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
