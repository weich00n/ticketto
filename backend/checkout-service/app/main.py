"""Application factory for checkout-service.

The API process is HTTP-only: the order routes and the Stripe webhook. The
background work (outbox relay, the payment-deadline sweep, the confirm-retry
sweep, the refund processor and the three queue consumers) runs in the separate
worker process, `python -m app.worker` - the same shape as event-service
(API + outbox-worker) and queue-service (api + consumer). That split means an
API redeploy never drops an in-flight message, and the worker can be restarted
independently.
"""

from __future__ import annotations

import logging

from flask import Flask

from . import correlation
from .clients.reservation import HttpReservationClient
from .clients.stripe import HttpStripeClient
from .config import Settings
from .db import create_session_factory
from .errors import ApiError
from .logging_config import configure_logging
from .messaging.outbox import Outbox
from .routes import api, health, webhook
from .service import CheckoutService

log = logging.getLogger(__name__)


def create_app(  # pylint: disable=too-many-arguments  # dependency-injection surface
    settings: Settings | None = None,
    *,
    session_factory=None,
    outbox=None,
    reservation=None,
    stripe=None,
    service=None,
) -> Flask:
    """Build the API. Tests pass their own session factory and fakes; production
    reads the environment and builds the real clients."""
    settings = settings or Settings.from_environment()
    configure_logging(settings.log_level)
    app = Flask(__name__)
    correlation.init_app(app)

    session_factory = session_factory or create_session_factory(settings)
    outbox = outbox or Outbox(settings)
    reservation = reservation or HttpReservationClient(settings.reservation_base_url)
    stripe = stripe or HttpStripeClient(settings.stripe_api_key, settings.stripe_webhook_secret)
    if service is None:
        service = CheckoutService(settings, session_factory, outbox, reservation, stripe)

    app.extensions["checkout_settings"] = settings
    app.extensions["checkout_service"] = service
    app.extensions["checkout_outbox"] = outbox
    app.extensions["checkout_stripe"] = stripe

    app.register_blueprint(health)
    app.register_blueprint(api)
    app.register_blueprint(webhook)

    @app.errorhandler(ApiError)
    def _api_error(error: ApiError):
        return error.body(), error.status_code

    @app.errorhandler(Exception)
    def _unhandled(_error: Exception):
        log.exception("unhandled error")
        return (
            {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Something went wrong on our side.",
                },
                "correlation_id": correlation.get_correlation_id(),
            },
            500,
        )

    return app
