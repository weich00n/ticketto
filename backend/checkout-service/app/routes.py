"""HTTP routes for checkout-service.

Public API (behind API Gateway, so X-User-Sub is always present):
  POST /api/v1/orders          create an order from a hold; returns the client secret
  GET  /api/v1/orders          list the buyer's orders (paginated)
  GET  /api/v1/orders/<id>     one of the buyer's orders

Webhook (called directly by Stripe, NOT behind the gateway):
  POST /webhooks/stripe        payment_intent.succeeded / payment_failed / charge.refunded

Health:
  GET /health                  liveness
  GET /ready                   readiness (DB + broker reachable)

The health routes are root-level (not under /api/v1) because the Dockerfile
healthcheck and the ALB target group call them by absolute path.
"""

from __future__ import annotations

import logging
import re

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import text

from .auth import require_buyer
from .clients.stripe import StripeUnavailableError, WebhookSignatureError
from .errors import ApiError
from .serializers import iso_utc

log = logging.getLogger(__name__)

# A deliberately loose shape check (one @, a dot in the domain, no spaces); the email
# provider is the real validator.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

health = Blueprint("health", __name__)
api = Blueprint("api", __name__, url_prefix="/api/v1")
webhook = Blueprint("webhook", __name__)


# ----------------------------------------------------------------------- health


@health.route("/health", methods=["GET"])
def check_health() -> tuple[str, int]:
    return "ok", 200


@health.route("/ready", methods=["GET"])
def check_ready() -> tuple[str, int]:
    """Readiness: the DB must answer and the broker must be reachable.

    Returns 200 when both are up; 503 with a reason otherwise. The orchestrator uses
    this to decide whether the container is ready for traffic.
    """
    service = current_app.extensions["checkout_service"]
    outbox = current_app.extensions["checkout_outbox"]
    try:
        with service._sf() as db:  # pylint: disable=protected-access  # own service's factory
            db.execute(text("SELECT 1"))
    except Exception as exc:  # pylint: disable=broad-exception-caught  # readiness probes anything
        return jsonify({"ready": False, "reason": f"database: {type(exc).__name__}"}), 503
    try:
        outbox._connect().close()  # pylint: disable=protected-access  # own outbox client
    except Exception as exc:  # pylint: disable=broad-exception-caught  # readiness probes anything
        return jsonify({"ready": False, "reason": f"broker: {type(exc).__name__}"}), 503
    return jsonify({"ready": True}), 200


# ----------------------------------------------------------------------- orders


@api.route("/orders", methods=["POST"])
def create_order():
    buyer = require_buyer(request)
    service = current_app.extensions["checkout_service"]
    body = _parse_create_order()
    idem = request.headers.get("Idempotency-Key", "").strip() or None
    order, client_secret, created = service.create_order(
        user_id=buyer.user_id,
        hold_id=body["hold_id"],
        delivery_email=body["delivery_email"],
        idempotency_key=idem,
    )
    payload = _order_view(order, client_secret)
    return jsonify(payload), (201 if created else 200)


@api.route("/orders", methods=["GET"])
def list_orders():
    buyer = require_buyer(request)
    service = current_app.extensions["checkout_service"]
    page = _int_param("page", 1, min_=1, max_=10000)
    page_size = _int_param("page_size", 20, min_=1, max_=100)
    orders, total = service.list_orders(buyer.user_id, page, page_size)
    items = [_order_view(o, None) for o in orders]
    return jsonify({"items": items, "total": total, "page": page, "page_size": page_size}), 200


@api.route("/orders/<order_id>", methods=["GET"])
def get_order(order_id: str):
    buyer = require_buyer(request)
    service = current_app.extensions["checkout_service"]
    order = service.get_order(order_id, buyer.user_id)
    return jsonify(_order_view(order, None)), 200


# --------------------------------------------------------------------- webhook


@webhook.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    """Called directly by Stripe. The signature is the only auth; there is no
    X-User-Sub here because the gateway is not involved.
    """
    service = current_app.extensions["checkout_service"]
    stripe_client = current_app.extensions["checkout_stripe"]
    payload = request.get_data(cache=True)
    signature = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe_client.verify_webhook(payload, signature)
    except WebhookSignatureError:
        return (
            jsonify(
                {
                    "error": {
                        "code": "BAD_SIGNATURE",
                        "message": "Webhook signature did not verify.",
                    }
                }
            ),
            400,
        )
    except StripeUnavailableError as exc:
        return jsonify({"error": {"code": "STRIPE_UNAVAILABLE", "message": str(exc)}}), 503

    try:
        outcome = service.handle_stripe_event(event)
    except Exception:  # pylint: disable=broad-exception-caught  # 500 tells Stripe to retry
        log.exception("stripe webhook processing failed", extra={"event_id": event.event_id})
        # A 500 tells Stripe to retry, which is what we want for a transient failure.
        return jsonify({"error": {"code": "INTERNAL_ERROR", "message": "Retry later."}}), 500
    return jsonify({"status": outcome}), 200


# --------------------------------------------------------------------- helpers


def _parse_create_order() -> dict:
    """The body carries the hold and where to send the tickets. Quantity and price are
    never taken from the client: both come from the hold (the backend decides amounts)."""
    raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        raise ApiError(400, "VALIDATION_ERROR", "A JSON body is required.")
    hold_id = raw.get("hold_id")
    if not isinstance(hold_id, str) or not hold_id.strip():
        raise ApiError(400, "VALIDATION_ERROR", "hold_id is required.")
    # `customer_email` is the name the frontend's checkout form sends.
    email = raw.get("delivery_email", raw.get("customer_email"))
    email = email.strip() if isinstance(email, str) else ""
    if len(email) > 320 or not _EMAIL.match(email):
        raise ApiError(400, "VALIDATION_ERROR", "delivery_email must be a valid email address.")
    return {"hold_id": hold_id.strip(), "delivery_email": email}


def _int_param(name: str, default: int, *, min_: int, max_: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(400, "VALIDATION_ERROR", f"{name} must be an integer.") from exc
    return max(min_, min(max_, value))


def _order_view(order, client_secret: str | None) -> dict:

    payment = None
    if order.payment is not None:
        p = order.payment
        payment = {
            "status": p.status,
            "amount": p.amount,
            "currency": p.currency,
            "succeeded_at": iso_utc(p.succeeded_at) if p.succeeded_at else None,
            "failed_at": iso_utc(p.failed_at) if p.failed_at else None,
        }
    refunds = [
        {
            "refund_id": r.refund_id,
            "amount": r.amount,
            "currency": r.currency,
            "reason": r.reason,
            "status": r.status,
            "stripe_refund_id": r.stripe_refund_id,
            "last_error": r.last_error,
        }
        for r in (order.refunds or [])
    ]
    return {
        "order_id": order.order_id,
        "status": order.booking_status,
        "hold_id": order.hold_id,
        "event_id": order.event_id,
        "category_id": order.category_id,
        "quantity": order.quantity,
        "unit_amount": order.unit_amount,
        "total_amount": order.total_amount,
        "currency": order.currency,
        "delivery_email": order.delivery_email,
        "payment": payment,
        "refunds": refunds,
        "client_secret": client_secret,
        "created_at": iso_utc(order.created_at),
        "updated_at": iso_utc(order.updated_at),
    }
