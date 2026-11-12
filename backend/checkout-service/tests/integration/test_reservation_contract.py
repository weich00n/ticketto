"""Checkout's behaviour for every answer reservation-service can give, and for every
message it writes (or must write), through the real service + DB with the faked
reservation/Stripe/broker. Complements test_flows.py, which covers the main flows.

Decisions pinned here (2026-10-22):
  D1  confirm 404 (HOLD_NOT_FOUND / EVENT_NOT_FOUND) -> RECONCILIATION, never a refund
  D2  EventCancelled also refunds CONFIRMED orders; a later RefundRequired is a no-op
  D3  inbound messages must match the broker envelope exactly (see test_message_contracts)
"""

# pylint: disable=protected-access  # tests reach into the service's own fakes/factories
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.clients.reservation import ReservationError
from app.messaging.envelope import PoisonMessage
from app.models import Order, Payment, ProcessedMessage
from tests.conftest import EMAIL, buy, incoming, order_by_hold
from tests.fakes.stripe import payment_succeeded_event

pytestmark = pytest.mark.integration

USER = "u-alice"


def _seed(service, hold_id="h1", event_id="e1"):
    service._reservation.add_hold(
        hold_id=hold_id,
        user_id=USER,
        event_id=event_id,
        category_id="c1",
        admission_id="a1",
        quantity=1,
        unit_amount=100_000,
    )


def _create(service, hold_id="h1") -> Order:
    order, _secret, _created = service.create_order(
        user_id=USER, hold_id=hold_id, delivery_email=EMAIL, idempotency_key=None
    )
    return order


def _set(service, hold_id, booking_status, payment_status=None, intent_id="keep"):
    with service._sf() as db:
        order = db.scalar(select(Order).where(Order.hold_id == hold_id))
        order.booking_status = booking_status
        payment = db.scalar(select(Payment).where(Payment.order_id == order.order_id))
        if payment_status is not None:
            payment.status = payment_status
        if intent_id != "keep":
            payment.stripe_payment_intent_id = intent_id
        db.commit()


def _pay(service, hold_id="h1", event_id="evt_1") -> str:
    order = order_by_hold(service._sf, hold_id)
    return service.handle_stripe_event(
        payment_succeeded_event(order.payment.stripe_payment_intent_id, event_id)
    )


def _refund_required(order: Order, **overrides) -> dict:
    """RefundRequired's payload exactly as reservation's OutboxWriter writes it."""
    payload = {
        "hold_id": order.hold_id,
        "order_id": order.order_id,
        "user_id": USER,
        "event_id": order.event_id,
        "total_amount": order.total_amount,
        "currency": order.currency,
        "reason": "EVENT_CANCELLED",
        "required_at": "2026-11-20T12:00:00Z",
    }
    payload.update(overrides)
    return payload


def _event_cancelled(event_id="e1") -> dict:
    return {
        "event_id": event_id,
        "event_version": 3,
        "cancellation_id": "c0a1b2c3-d4e5-4f60-8a1b-2c3d4e5f6a7b",
        "cancelled_at": "2026-11-20T12:00:00Z",
        "reason": "venue unavailable",
    }


# ------------------------------------------------------- GET hold (create order)


def test_reservation_down_on_create_order_is_503(service, client):
    _seed(service)
    service._reservation.get_unavailable = True
    r = client.post(
        "/api/v1/orders",
        json={"hold_id": "h1", "delivery_email": EMAIL},
        headers={"X-User-Sub": USER},
    )
    assert r.status_code == 503
    assert json.loads(r.get_data())["error"]["code"] == "RESERVATION_UNAVAILABLE"


@pytest.mark.parametrize(
    ("status", "code"),
    [("EXPIRED", "HOLD_EXPIRED"), ("CANCELLED", "EVENT_CANCELLED"), ("CONFIRMED", None)],
)
def test_an_ended_hold_cannot_be_bought(service, client, status, code):
    service._reservation.add_hold(hold_id="h1", user_id=USER, event_id="e1", status=status)
    r = client.post(
        "/api/v1/orders",
        json={"hold_id": "h1", "delivery_email": EMAIL},
        headers={"X-User-Sub": USER},
    )
    assert r.status_code == 409
    assert json.loads(r.get_data())["error"]["code"] == (code or "ORDER_STATE_CONFLICT")


# ------------------------------------------------------------------- confirm


@pytest.mark.parametrize("outcome", ["not_found", "event_not_found"])
def test_confirm_404_is_reconciliation_not_refund(service, client, outbox, outcome):
    _seed(service)
    buy(client, "h1", USER)
    service._reservation.set_outcome(outcome)
    assert _pay(service) == "reconciliation"
    outbox.deliver_pending(service._sf())

    order = order_by_hold(service._sf, "h1")
    assert order.booking_status == "RECONCILIATION"
    assert order.payment.status == "SUCCEEDED"
    assert order.refunds == []
    assert outbox.published == []

    # The retry sweep leaves a reconciliation order alone.
    calls = len(service._reservation.confirm_calls)
    service.retry_confirms()
    assert len(service._reservation.confirm_calls) == calls


def test_confirm_400_stays_confirming_and_is_retried(service, client, outbox):
    _seed(service)
    buy(client, "h1", USER)
    service._reservation.set_outcome("bad_request")
    assert _pay(service) == "contract_error"
    order = order_by_hold(service._sf, "h1")
    assert order.booking_status == "CONFIRMING"  # never refunded over our own bug
    assert order.refunds == []

    service._reservation.set_outcome("confirmed")  # the bug is fixed
    assert service.retry_confirms() == 1
    outbox.deliver_pending(service._sf())
    assert order_by_hold(service._sf, "h1").booking_status == "CONFIRMED"
    assert len(outbox.by_type("OrderConfirmed")) == 1


def test_paid_order_without_intent_id_is_not_sent_to_reservation(service):
    # ConfirmRequest.payment_reference is @NotBlank: never send "".
    _seed(service)
    _create(service)
    _set(service, "h1", "CONFIRMING", "SUCCEEDED", intent_id=None)
    service.retry_confirms()
    assert service._reservation.confirm_calls == []
    assert order_by_hold(service._sf, "h1").booking_status == "RECONCILIATION"


def test_one_failing_order_does_not_stop_the_confirm_sweep(service, monkeypatch):
    for hold in ("h1", "h2"):
        _seed(service, hold_id=hold)
        _create(service, hold)
        _set(service, hold, "CONFIRMING", "SUCCEEDED")

    real_confirm = service._reservation.confirm

    def flaky(hold_id, order_id, payment_reference):
        if hold_id == "h1":
            raise RuntimeError("unexpected")
        return real_confirm(hold_id, order_id, payment_reference)

    monkeypatch.setattr(service._reservation, "confirm", flaky)
    service.retry_confirms()
    assert order_by_hold(service._sf, "h1").booking_status == "CONFIRMING"
    assert order_by_hold(service._sf, "h2").booking_status == "CONFIRMED"


def test_confirm_transient_failure_is_retried_until_it_confirms(service, client):
    _seed(service)
    buy(client, "h1", USER)
    service._reservation.transient_failures = 1
    with pytest.raises(ReservationError):  # the webhook answers 500 so Stripe retries
        _pay(service)
    assert order_by_hold(service._sf, "h1").booking_status == "CONFIRMING"
    service.retry_confirms()
    assert order_by_hold(service._sf, "h1").booking_status == "CONFIRMED"


# ------------------------------------------------------------------ messages


def test_event_cancelled_refunds_a_confirmed_order(service):
    # D2: checkout refunds on EventCancelled without waiting for reservation's RefundRequired.
    _seed(service)
    _create(service)
    _set(service, "h1", "CONFIRMED", "SUCCEEDED")
    service.handle_event_cancelled(incoming("EventCancelled", _event_cancelled()))
    order = order_by_hold(service._sf, "h1")
    assert order.booking_status == "CANCELLED"
    assert [r.reason for r in order.refunds] == ["event_cancelled"]


@pytest.mark.parametrize("first", ["EventCancelled", "RefundRequired"])
def test_event_cancelled_and_refund_required_make_one_refund_either_order(service, first):
    _seed(service)
    order = _create(service)
    _set(service, "h1", "CONFIRMED", "SUCCEEDED")
    messages = {
        "EventCancelled": lambda: service.handle_event_cancelled(
            incoming("EventCancelled", _event_cancelled())
        ),
        "RefundRequired": lambda: service.handle_refund_required(
            incoming("RefundRequired", _refund_required(order))
        ),
    }
    messages[first]()
    messages["RefundRequired" if first == "EventCancelled" else "EventCancelled"]()
    service.process_refunds()
    order = order_by_hold(service._sf, "h1")
    assert order.booking_status == "CANCELLED"
    assert len(order.refunds) == 1
    assert len(service._stripe.refund_calls) == 1


def test_refund_required_naming_another_hold_is_dead_lettered(service):
    _seed(service)
    order = _create(service)
    _set(service, "h1", "CONFIRMED", "SUCCEEDED")
    with pytest.raises(PoisonMessage):
        service.handle_refund_required(
            incoming("RefundRequired", _refund_required(order, hold_id="someone-elses-hold"))
        )
    order = order_by_hold(service._sf, "h1")
    assert order.booking_status == "CONFIRMED"  # nothing changed
    assert order.refunds == []


def test_refund_required_with_a_different_amount_refunds_the_order_amount(service):
    _seed(service)
    order = _create(service)
    _set(service, "h1", "CONFIRMED", "SUCCEEDED")
    service.handle_refund_required(
        incoming("RefundRequired", _refund_required(order, total_amount=1))
    )
    assert [r.amount for r in order_by_hold(service._sf, "h1").refunds] == [100_000]


def test_refund_required_for_an_unknown_order_is_acked(service):
    payload = {
        "hold_id": "h-x",
        "order_id": "no-such-order",
        "total_amount": 1,
        "currency": "SGD",
    }
    service.handle_refund_required(incoming("RefundRequired", payload))  # must not raise


@pytest.mark.parametrize(
    ("message_type", "payload"),
    [
        ("EventCancelled", {"cancellation_id": "c1"}),
        ("ReservationExpired", {"expired_at": "2026-11-20T12:00:00Z"}),
        ("RefundRequired", {"order_id": "o1"}),
    ],
)
def test_unusable_payloads_are_poison_and_change_nothing(service, message_type, payload):
    handler = {
        "EventCancelled": service.handle_event_cancelled,
        "ReservationExpired": service.handle_reservation_expired,
        "RefundRequired": service.handle_refund_required,
    }[message_type]
    env = incoming(message_type, payload)
    with pytest.raises(PoisonMessage):
        handler(env)
    with service._sf() as db:
        # Not recorded as processed: nothing was committed.
        assert db.get(ProcessedMessage, env.message_id) is None


def test_reservation_expired_leaves_a_reconciliation_order_alone(service):
    _seed(service)
    _create(service)
    _set(service, "h1", "RECONCILIATION", "SUCCEEDED")
    # Must not raise: a raise would redeliver it until it is dead-lettered.
    service.handle_reservation_expired(incoming("ReservationExpired", {"hold_id": "h1"}))
    assert order_by_hold(service._sf, "h1").booking_status == "RECONCILIATION"


def test_expiry_does_not_need_reservation_expired(service):
    # The deadline sweep expires an unpaid order on its own, so a lost or late
    # ReservationExpired changes nothing.
    _seed(service)
    _create(service)
    assert service.expire_stale_orders(datetime.now(UTC) + timedelta(hours=1)) == 1
    order = order_by_hold(service._sf, "h1")
    assert order.booking_status == "EXPIRED"
    assert order.payment.status == "CANCELLED"


def test_repeating_create_after_payment_returns_the_order(service, client):
    # Once paid, reservation reports the hold CONFIRMED; POST /orders is still idempotent.
    _seed(service)
    first = buy(client, "h1", USER)
    _pay(service)
    service._reservation.add_hold(
        hold_id="h1", user_id=USER, event_id="e1", status="CONFIRMED", order_id=first["order_id"]
    )
    r = client.post(
        "/api/v1/orders",
        json={"hold_id": "h1", "delivery_email": EMAIL},
        headers={"X-User-Sub": USER},
    )
    assert r.status_code == 200
    body = json.loads(r.get_data())
    assert body["order_id"] == first["order_id"]
    assert body["status"] == "CONFIRMED"
    assert body["client_secret"] is None  # nothing left to pay


def test_someone_elses_ended_hold_is_404_not_its_state(service, client):
    service._reservation.add_hold(hold_id="h1", user_id="u-bob", event_id="e1", status="EXPIRED")
    r = client.post(
        "/api/v1/orders",
        json={"hold_id": "h1", "delivery_email": EMAIL},
        headers={"X-User-Sub": USER},
    )
    assert r.status_code == 404
    assert json.loads(r.get_data())["error"]["code"] == "HOLD_NOT_FOUND"
