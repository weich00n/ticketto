"""Each published event must carry exactly the `data` fields the Technical Contract
section 8 lists for it. A missing or extra key would either break a consumer or violate
the broker's `additionalProperties: false`, so the key set is asserted here, not assumed.
"""

from datetime import UTC, datetime

from app.messaging.events import (
    order_confirmed_data,
    order_refunded_data,
    refund_completed_data,
    refund_failed_data,
)
from app.models import Order, Refund

NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)


def _order() -> Order:
    return Order(
        order_id="o1",
        hold_id="h1",
        admission_id="a1",
        user_id="u1",
        event_id="e1",
        category_id="c1",
        quantity=2,
        unit_amount=100_000,
        total_amount=200_000,
        currency="SGD",
        delivery_email="buyer@example.com",
        booking_status="CONFIRMED",
        payment_expires_at=NOW,
        hold_expires_at=NOW,
        confirmed_at=NOW,
    )


def _refund(order: Order):
    return Refund(
        refund_id="r1",
        order_id=order.order_id,
        amount=order.total_amount,
        currency=order.currency,
        reason="event_cancelled",
        idempotency_key=f"{order.order_id}:event_cancelled",
        status="SUCCEEDED",
    )


def test_order_confirmed_fields():
    data = order_confirmed_data(_order())
    assert set(data) == {
        "order_id",
        "hold_id",
        "admission_id",
        "user_id",
        "event_id",
        "category_id",
        "quantity",
        "delivery_email",
        "total_amount",
        "currency",
        "confirmed_at",
    }


def test_refund_completed_fields():
    order = _order()
    data = refund_completed_data(order, _refund(order))
    # TC §8: order id, event id, refund id, amount and currency.
    assert set(data) == {"order_id", "event_id", "refund_id", "amount", "currency"}


def test_order_refunded_fields():
    order = _order()
    data = order_refunded_data(order, _refund(order))
    assert set(data) == {
        "order_id",
        "user_id",
        "event_id",
        "refund_id",
        "amount",
        "currency",
        "reason",
    }


def test_refund_failed_fields():
    order = _order()
    refund = _refund(order)
    refund.last_error = "stripe refund failed"
    data = refund_failed_data(order, refund)
    assert set(data) == {
        "order_id",
        "user_id",
        "event_id",
        "refund_id",
        "amount",
        "currency",
        "reason",
        "last_error",
    }
