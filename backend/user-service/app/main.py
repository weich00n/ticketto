from fastapi import FastAPI

from app.config import get_settings
from app.correlation import CorrelationIdMiddleware
from app.errors import register_error_handlers
from app.internal_port import InternalPortMiddleware
from app.logging_config import configure_logging
from app.routers import health, internal, users


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="Ticketto User Service", version="1.0.0")
    # Added first so it runs inside CorrelationIdMiddleware: refusals are logged and keep the ID.
    app.add_middleware(InternalPortMiddleware, internal_port=settings.internal_port)
    app.add_middleware(CorrelationIdMiddleware)
    register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(internal.router)
    return app


app = create_app()
