"""Builders for the four events checkout publishes, using the exact `payload` fields the
Technical Contract section 8 lists for each.

Keeping every field in one module (and asserting the key set in tests) means an event
can never silently gain or lose a required field, and a new consumer that depends on a
field can see it in one place. The envelope that wraps the payload is built by
app/messaging/outbox.py.
"""

from ..models import Order, Refund

# The PascalCase names are the envelope's message_type and the AMQP `type` property.
EVENT_ORDER_CONFIRMED = "OrderConfirmed"
EVENT_ORDER_REFUNDED = "OrderRefunded"
EVENT_REFUND_COMPLETED = "RefundCompleted"
EVENT_REFUND_FAILED = "RefundFailed"

# The routing keys on checkout.events, exactly as rabbitmq-service's topology/messages.yaml
# lists them (dotted lower case, never the PascalCase name).
ROUTING_KEYS = {
    EVENT_ORDER_CONFIRMED: "order.confirmed",
    EVENT_ORDER_REFUNDED: "order.refunded",
    EVENT_REFUND_COMPLETED: "refund.completed",
    EVENT_REFUND_FAILED: "refund.failed",
}


def _order_fields(order: Order) -> dict:
    return {
        "order_id": order.order_id,
        "hold_id": order.hold_id,
        "admission_id": order.admission_id,
        "user_id": order.user_id,
        "event_id": order.event_id,
        "category_id": order.category_id,
        "quantity": order.quantity,
        "delivery_email": order.delivery_email,
    }


def order_confirmed_data(order: Order) -> dict:
    # TC §8: order id, hold id, admission id, user id, event id, category id, quantity,
    # delivery email snapshot. The broker's messages.yaml note also wants the amount and
    # currency for downstream accounting, so they ride along.
    data = _order_fields(order)
    data["total_amount"] = order.total_amount
    data["currency"] = order.currency
    data["confirmed_at"] = _iso(order.confirmed_at)
    return data


def refund_completed_data(order: Order, refund: Refund) -> dict:
    # TC §8: order id, event id, refund id, amount and currency.
    return {
        "order_id": order.order_id,
        "event_id": order.event_id,
        "refund_id": refund.refund_id,
        "amount": refund.amount,
        "currency": refund.currency,
    }


def order_refunded_data(order: Order, refund: Refund) -> dict:
    # Consumer-less: a downstream notification. Carries enough to address the buyer.
    data = {
        "order_id": order.order_id,
        "user_id": order.user_id,
        "event_id": order.event_id,
        "refund_id": refund.refund_id,
        "amount": refund.amount,
        "currency": refund.currency,
        "reason": refund.reason,
    }
    return data


def refund_failed_data(order: Order, refund: Refund) -> dict:
    # Consumer-less: a downstream notification (and an ops signal that a refund is stuck).
    return {
        "order_id": order.order_id,
        "user_id": order.user_id,
        "event_id": order.event_id,
        "refund_id": refund.refund_id,
        "amount": refund.amount,
        "currency": refund.currency,
        "reason": refund.reason,
        "last_error": refund.last_error,
    }


def _iso(value) -> str | None:
    if value is None:
        return None
    from ..serializers import iso_utc

    return iso_utc(value)
