"""End-to-end flows through the real service + DB, with faked reservation/Stripe/broker.

These cover the acceptance behaviours that fall to checkout-service: the happy path,
payment failure, the confirmation-rejected refund, the cancellation/payment race, the
order-mismatch reconciliation case, duplicate webhooks, the Idempotency-Key rules, the
three consumer handlers, and the refund processor + sweeps.
"""

# pylint: disable=protected-access  # tests reach into the service's own fakes/factories
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.clients.stripe import StripeRejectedError, StripeUnavailableError
from app.models import Order, Payment, Refund
from tests.conftest import EMAIL, buy, incoming, order_by_hold
from tests.fakes.stripe import (
    charge_refunded_event,
    payment_failed_event,
    payment_succeeded_event,
)

pytestmark = pytest.mark.integration

USER = "u-alice"
OTHER = "u-bob"


def _seed(service, hold_id="h1", user_id=USER, quantity=1, **kw):
    service._reservation.add_hold(
        hold_id=hold_id,
        user_id=user_id,
        event_id="e1",
        category_id="c1",
        admission_id="a1",
        quantity=quantity,
        unit_amount=100_000,
        **kw,
    )


def _intent(service) -> str:
    return list(service._stripe.intents.values())[0].payment_intent_id


def _set_booking(
    service, hold_id: str, booking_status: str, payment_status: str | None = None
) -> None:
    """Directly move an order's machines to set up a race or replay scenario."""
    with service._sf() as db:
        order = db.scalar(select(Order).where(Order.hold_id == hold_id))
        order.booking_status = booking_status
        if payment_status is not None:
            payment = db.scalar(select(Payment).where(Payment.order_id == order.order_id))
            payment.status = payment_status
        db.commit()


# ------------------------------------------------------------------- happy path


def test_happy_path_creates_order_and_confirms(service, client, outbox, session_factory):
    _seed(service)
    body = buy(client, "h1", USER)
    assert body["status"] == "PENDING_PAYMENT"
    assert body["client_secret"]  # a PaymentIntent client secret was returned
    assert body["total_amount"] == 100_000  # integer minor units, taken from the hold
    order_id = body["order_id"]

    pi = _intent(service)
    service.handle_stripe_event(payment_succeeded_event(pi, "evt_1"))
    outbox.deliver_pending(service._sf())

    order = order_by_hold(session_factory, "h1")
    assert order.booking_status == "CONFIRMED"
    assert order.payment.status == "SUCCEEDED"
    # reservation was asked to confirm exactly once, with the order id + payment ref.
    assert service._reservation.confirm_calls == [("h1", order_id, pi)]
    confirmed = outbox.by_type("OrderConfirmed")
    assert len(confirmed) == 1
    payload = confirmed[0]["payload"]
    assert payload["order_id"] == order_id
    assert payload["user_id"] == USER
    # Everything ticket-service needs to issue and email the tickets.
    assert payload["admission_id"] == "a1"
    assert payload["category_id"] == "c1"
    assert payload["quantity"] == 1
    assert payload["delivery_email"] == EMAIL
    # Published to checkout.events under the catalogue's routing key.
    assert ("order.confirmed", confirmed[0]) in outbox.published
    # PaymentCompleted is no longer published: it had no consumer and piled up in
    # ticketto.unrouted.
    assert outbox.by_type("PaymentCompleted") == []


def test_quantity_comes_from_the_hold_not_the_browser(service, client, outbox):
    _seed(service, quantity=3)
    r = client.post(
        "/api/v1/orders",
        json={"hold_id": "h1", "delivery_email": EMAIL, "quantity": 1},
        headers={"X-User-Sub": USER},
    )
    body = json.loads(r.get_data())
    assert r.status_code == 201
    assert body["quantity"] == 3  # the browser's quantity is ignored
    assert body["total_amount"] == 300_000  # and so is any idea of what it should pay
    service.handle_stripe_event(payment_succeeded_event(_intent(service), "evt_1"))
    outbox.deliver_pending(service._sf())
    assert outbox.by_type("OrderConfirmed")[0]["payload"]["quantity"] == 3


def test_customer_email_is_accepted_as_the_delivery_email(service, client):
    _seed(service)
    r = client.post(
        "/api/v1/orders",
        json={"hold_id": "h1", "customer_email": " fan@example.com "},
        headers={"X-User-Sub": USER},
    )
    assert r.status_code == 201
    assert json.loads(r.get_data())["delivery_email"] == "fan@example.com"


@pytest.mark.parametrize("email", [None, "", "not-an-email", "a@b", "x" * 320 + "@example.com"])
def test_order_without_a_valid_delivery_email_is_rejected(service, client, email):
    _seed(service)
    body = {"hold_id": "h1"} if email is None else {"hold_id": "h1", "delivery_email": email}
    r = client.post("/api/v1/orders", json=body, headers={"X-User-Sub": USER})
    assert r.status_code == 400
    assert json.loads(r.get_data())["error"]["code"] == "VALIDATION_ERROR"
    assert not service._stripe.intents  # nothing was started


def test_retry_after_stripe_failure_creates_the_missing_intent(service, client, session_factory):
    _seed(service)
    service._stripe.intent_failures = 1
    r1 = client.post(
        "/api/v1/orders",
        json={"hold_id": "h1", "delivery_email": EMAIL},
        headers={"X-User-Sub": USER},
    )
    assert r1.status_code == 503
    assert order_by_hold(session_factory, "h1").payment.stripe_payment_intent_id is None

    # The same request again finds the order and finishes starting the payment.
    body = buy(client, "h1", USER)
    assert body["client_secret"]
    order = order_by_hold(session_factory, "h1")
    assert order.payment.stripe_payment_intent_id == _intent(service)
    assert len(service._stripe.intents) == 1


def test_webhook_route_end_to_end(service, client, outbox, session_factory):
    _seed(service)
    buy(client, "h1", USER)
    pi = _intent(service)
    payload = json.dumps(
        {"id": "evt_1", "type": "payment_intent.succeeded", "data": {"object": {"id": pi}}}
    ).encode()
    resp = client.post(
        "/webhooks/stripe",
        data=payload,
        content_type="application/json",
        headers={"Stripe-Signature": "sig"},
    )
    assert resp.status_code == 200
    outbox.deliver_pending(service._sf())
    assert order_by_hold(session_factory, "h1").booking_status == "CONFIRMED"


def test_duplicate_webhook_is_noop(service, client, outbox):
    _seed(service)
    buy(client, "h1", USER)
    pi = _intent(service)
    service.handle_stripe_event(payment_succeeded_event(pi, "evt_1"))
    outbox.deliver_pending(service._sf())
    n = len(outbox.by_type("OrderConfirmed"))

    # Same event id again: must not re-emit.
    out = service.handle_stripe_event(payment_succeeded_event(pi, "evt_1"))
    outbox.deliver_pending(service._sf())
    assert out in ("already_succeeded", "duplicate")
    assert len(outbox.by_type("OrderConfirmed")) == n


# ------------------------------------------------------------------- failures


def test_payment_failed_marks_payment_failed(service, client, outbox, session_factory):
    _seed(service)
    buy(client, "h1", USER)
    pi = _intent(service)
    out = service.handle_stripe_event(payment_failed_event(pi, "evt_f"))
    outbox.deliver_pending(service._sf())
    assert out == "payment_failed"
    order = order_by_hold(session_factory, "h1")
    assert order.payment.status == "FAILED"
    assert order.booking_status == "PENDING_PAYMENT"  # still payable until the deadline


def test_confirm_rejected_refunds_and_cancels(service, client, outbox, session_factory):
    _seed(service)
    buy(client, "h1", USER)
    service._reservation.set_outcome("expired")
    pi = _intent(service)
    out = service.handle_stripe_event(payment_succeeded_event(pi, "evt_1"))
    outbox.deliver_pending(service._sf())
    assert out == "refund_required"
    order = order_by_hold(session_factory, "h1")
    assert order.booking_status == "CANCELLED"
    assert len(order.refunds) == 1
    assert order.refunds[0].status == "REQUESTED"
    assert order.refunds[0].reason == "confirm_rejected"


def test_order_mismatch_flags_not_auto_fixed(service, client, outbox, session_factory):
    _seed(service)
    buy(client, "h1", USER)
    service._reservation.set_outcome("mismatch")
    pi = _intent(service)
    out = service.handle_stripe_event(payment_succeeded_event(pi, "evt_1"))
    outbox.deliver_pending(service._sf())
    assert out == "mismatch"
    order = order_by_hold(session_factory, "h1")
    # Parked for a person: not auto-cancelled, not auto-refunded, not confirmed.
    assert order.booking_status == "RECONCILIATION"
    assert order.payment.status == "SUCCEEDED"
    assert order.refunds == []
    # It is not announced as a failed refund (or anything else).
    assert outbox.by_type("RefundFailed") == []
    assert outbox.published == []

    # The confirm-retry sweep leaves it alone instead of re-flagging it every run.
    calls = len(service._reservation.confirm_calls)
    service.retry_confirms()
    service.retry_confirms()
    assert len(service._reservation.confirm_calls) == calls


# --------------------------------------------------------------- idempotency


def test_same_idempotency_key_same_payload_returns_original(service, client):
    _seed(service)
    r1 = client.post(
        "/api/v1/orders",
        json={"hold_id": "h1", "delivery_email": EMAIL},
        headers={"X-User-Sub": USER, "Idempotency-Key": "K1"},
    )
    r2 = client.post(
        "/api/v1/orders",
        json={"hold_id": "h1", "delivery_email": EMAIL},
        headers={"X-User-Sub": USER, "Idempotency-Key": "K1"},
    )
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert json.loads(r1.get_data())["order_id"] == json.loads(r2.get_data())["order_id"]


def test_same_idempotency_key_different_payload_conflicts(service, client):
    _seed(service)  # h1 for USER
    _seed(service, hold_id="h2")  # a second hold for the same buyer
    client.post(
        "/api/v1/orders",
        json={"hold_id": "h1", "delivery_email": EMAIL},
        headers={"X-User-Sub": USER, "Idempotency-Key": "K1"},
    )
    # Reusing the same key for a *different* hold (different payload) is a conflict.
    r = client.post(
        "/api/v1/orders",
        json={"hold_id": "h2", "delivery_email": EMAIL},
        headers={"X-User-Sub": USER, "Idempotency-Key": "K1"},
    )
    assert r.status_code == 409
    assert json.loads(r.get_data())["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_foreign_hold_is_404_not_403(service, client):
    _seed(service, user_id=USER)
    r = client.post(
        "/api/v1/orders",
        json={"hold_id": "h1", "delivery_email": EMAIL},
        headers={"X-User-Sub": OTHER},
    )
    assert r.status_code == 404
    assert json.loads(r.get_data())["error"]["code"] == "HOLD_NOT_FOUND"


def test_missing_auth_is_401(client):
    r = client.get("/api/v1/orders")
    assert r.status_code == 401
    assert json.loads(r.get_data())["error"]["code"] == "UNAUTHENTICATED"


# ------------------------------------------------------------------- consumers


def _consume(service, message_type: str, payload: dict, correlation_id: str) -> None:
    """Parse a message as its publisher sends it and dispatch it to the service handler -
    the same path the pika consumer takes."""
    msg = incoming(message_type, payload, correlation_id)
    {
        "EventCancelled": service.handle_event_cancelled,
        "ReservationExpired": service.handle_reservation_expired,
        "RefundRequired": service.handle_refund_required,
    }[message_type](msg)


def test_event_cancelled_refunds_paid_unconfirmed_order(service, outbox, session_factory):
    # The TC §11 race: a payment just succeeded (CONFIRMING) but the event is cancelled.
    _seed(service)
    _create_direct(service)
    _set_booking(service, "h1", "CONFIRMING", "SUCCEEDED")

    _consume(
        service,
        "EventCancelled",
        {
            "event_id": "e1",
            "cancellation_id": "c1",
            "cancelled_at": "2026-11-20T12:00:00Z",
            "reason": "double booked",
        },
        "x",
    )
    outbox.deliver_pending(service._sf())
    order = order_by_hold(session_factory, "h1")
    assert order.booking_status == "CANCELLED"  # cancellation beats the in-flight confirm
    assert len(order.refunds) == 1
    assert order.refunds[0].reason == "event_cancelled"


def test_event_cancelled_cancels_unpaid_order(service, outbox, session_factory):
    _seed(service)
    _create_direct(service)
    _consume(
        service,
        "EventCancelled",
        {
            "event_id": "e1",
            "cancellation_id": "c1",
            "cancelled_at": "2026-11-20T12:00:00Z",
            "reason": "double booked",
        },
        "x",
    )
    outbox.deliver_pending(service._sf())
    order = order_by_hold(session_factory, "h1")
    assert order.booking_status == "CANCELLED"
    assert len(order.refunds) == 0  # nothing paid, nothing to refund
    # The buyer can no longer pay for a cancelled event.
    assert order.payment.status == "CANCELLED"
    assert service._stripe.cancelled == [order.payment.stripe_payment_intent_id]


def test_reservation_expired_expires_unpaid_order(service, outbox, session_factory):
    _seed(service)
    _create_direct(service)
    _consume(
        service,
        "ReservationExpired",
        {
            "hold_id": "h1",
            "admission_id": "a1",
            "user_id": USER,
            "event_id": "e1",
            "category_id": "c1",
            "quantity": 1,
            "expired_at": "2026-11-20T12:05:00Z",
        },
        "x",
    )
    outbox.deliver_pending(service._sf())
    order = order_by_hold(session_factory, "h1")
    assert order.booking_status == "EXPIRED"
    assert order.payment.status == "CANCELLED"  # intent cancelled so a late card can't charge
    assert service._stripe.cancelled  # the intent was cancelled


def test_refund_required_refunds_confirmed_order(service, outbox, session_factory):
    _seed(service)
    order = _create_direct(service)
    _set_booking(service, "h1", "CONFIRMED", "SUCCEEDED")
    _consume(service, "RefundRequired", _refund_required(order.order_id), order.order_id)
    outbox.deliver_pending(service._sf())
    order2 = order_by_hold(session_factory, "h1")
    # The money is going back, so the booking no longer stands.
    assert order2.booking_status == "CANCELLED"
    assert len(order2.refunds) == 1
    assert order2.refunds[0].reason == "refund_required"


def test_refund_required_leaves_an_expired_booking_alone(service, session_factory):
    _seed(service)
    order = _create_direct(service)
    _set_booking(service, "h1", "EXPIRED", "SUCCEEDED")
    # Must not raise (a raise would redeliver the message until it is dead-lettered).
    _consume(service, "RefundRequired", _refund_required(order.order_id), "x")
    order2 = order_by_hold(session_factory, "h1")
    assert order2.booking_status == "EXPIRED"
    assert len(order2.refunds) == 1


def test_event_cancelled_then_refund_required_is_one_refund(service, session_factory):
    # Cancelled mid-confirmation: checkout refunds, then reservation also says a refund
    # is owed for the same hold. One payment, so one refund.
    _seed(service)
    order = _create_direct(service)
    _set_booking(service, "h1", "CONFIRMING", "SUCCEEDED")
    _consume(service, "EventCancelled", {"event_id": "e1"}, "x")
    _consume(service, "RefundRequired", _refund_required(order.order_id), "y")
    service.process_refunds()
    order2 = order_by_hold(session_factory, "h1")
    assert len(order2.refunds) == 1
    assert order2.refunds[0].reason == "event_cancelled"
    assert len(service._stripe.refund_calls) == 1


def test_consumers_deduplicate_redeliveries(service, outbox, session_factory):
    _seed(service)
    _create_direct(service)
    data = {
        "event_id": "e1",
        "cancellation_id": "c1",
        "cancelled_at": "2026-11-20T12:00:00Z",
        "reason": "double booked",
    }
    # Deliver the same message_id twice (broker redelivery): it must be applied once.
    env = incoming("EventCancelled", data, "x")
    service.handle_event_cancelled(env)
    service.handle_event_cancelled(env)
    outbox.deliver_pending(service._sf())
    order = order_by_hold(session_factory, "h1")
    # Cancelled exactly once; no double refund.
    assert order.booking_status == "CANCELLED"
    assert len(order.refunds) == 0


# --------------------------------------------------------------- refund processor


def test_refund_processor_emits_completed_and_notifies(service, outbox, session_factory):
    _seed(service)
    _create_direct(service)
    _set_booking(service, "h1", "CONFIRMED", "SUCCEEDED")
    # Record a refund obligation for the paid order (e.g. after a cancellation).
    with service._sf() as db:
        o = db.scalar(select(Order).where(Order.hold_id == "h1"))
        service._request_refund(db, o, "event_cancelled")
        db.commit()

    n_completed = len(outbox.by_type("RefundCompleted"))
    n_refunded = len(outbox.by_type("OrderRefunded"))
    service.process_refunds()
    outbox.deliver_pending(service._sf())

    # One Stripe refund was issued; both the contract event and the notification fired.
    assert len(service._stripe.refunds) == 1
    assert len(outbox.by_type("RefundCompleted")) == n_completed + 1
    assert len(outbox.by_type("OrderRefunded")) == n_refunded + 1
    # The refund row is now SUCCEEDED and carries the Stripe refund id.
    order2 = order_by_hold(session_factory, "h1")
    assert order2.refunds[0].status == "SUCCEEDED"
    assert order2.refunds[0].stripe_refund_id == service._stripe.refunds[0].refund_id


def test_deadline_sweep_expires_late_pending_order(service, outbox, session_factory):
    _seed(service)
    _create_direct(service)
    # Push the payment deadline into the past.
    with service._sf() as db:
        o = db.scalar(select(Order).where(Order.hold_id == "h1"))
        o.payment_expires_at = datetime.now(UTC)
        db.commit()
    service.expire_stale_orders(datetime.now(UTC))
    outbox.deliver_pending(service._sf())
    order = order_by_hold(session_factory, "h1")
    assert order.booking_status == "EXPIRED"
    assert order.payment.status == "CANCELLED"


# ------------------------------------------------ late and concurrent payments


def test_success_after_a_declined_card_confirms(service, client, outbox, session_factory):
    # The buyer's first card is declined, they retry on the same intent, and it succeeds.
    _seed(service)
    buy(client, "h1", USER)
    pi = _intent(service)
    service.handle_stripe_event(payment_failed_event(pi, "evt_f"))
    out = service.handle_stripe_event(payment_succeeded_event(pi, "evt_s"))
    outbox.deliver_pending(service._sf())
    assert out == "confirmed"
    order = order_by_hold(session_factory, "h1")
    assert order.payment.status == "SUCCEEDED"
    assert order.booking_status == "CONFIRMED"
    assert len(outbox.by_type("OrderConfirmed")) == 1


def test_success_after_the_deadline_is_refunded(service, client, session_factory):
    # The sweep expired the order and (best-effort) cancelled the intent, but the card
    # went through anyway.
    _seed(service)
    buy(client, "h1", USER)
    with service._sf() as db:
        o = db.scalar(select(Order).where(Order.hold_id == "h1"))
        o.payment_expires_at = datetime.now(UTC)
        db.commit()
    service.expire_stale_orders(datetime.now(UTC))

    resp = _post_webhook(client, "payment_intent.succeeded", {"id": _intent(service)})
    assert resp.status_code == 200  # never a 500 that Stripe would retry for days
    assert json.loads(resp.get_data())["status"] == "late_payment_refund"
    order = order_by_hold(session_factory, "h1")
    assert order.booking_status == "EXPIRED"
    assert order.payment.status == "SUCCEEDED"
    assert [r.reason for r in order.refunds] == ["late_payment"]
    assert service._reservation.confirm_calls == []  # nothing to confirm


def test_success_whose_webhook_was_lost_is_refunded_by_the_sweep(service, client, session_factory):
    # Chaos C8 (2026-11-05): checkout was down when Stripe sent the success webhook, so the
    # sweep saw an unpaid order. Its cancel finds the intent already succeeded: refund now,
    # not hours later when Stripe retries the webhook.
    _seed(service)
    buy(client, "h1", USER)
    pi = _intent(service)
    service._stripe.already_succeeded.add(pi)
    with service._sf() as db:
        o = db.scalar(select(Order).where(Order.hold_id == "h1"))
        o.payment_expires_at = datetime.now(UTC)
        db.commit()
    service.expire_stale_orders(datetime.now(UTC))
    order = order_by_hold(session_factory, "h1")
    assert order.booking_status == "EXPIRED"
    assert order.payment.status == "SUCCEEDED"
    assert [r.reason for r in order.refunds] == ["late_payment"]

    # Stripe's retried webhook arrives later and changes nothing.
    resp = _post_webhook(client, "payment_intent.succeeded", {"id": pi})
    assert json.loads(resp.get_data())["status"] == "already_succeeded"
    service.process_refunds()
    assert len(service._stripe.refunds) == 1
    assert order_by_hold(session_factory, "h1").refunds[0].status == "SUCCEEDED"


def test_success_after_the_event_was_cancelled_is_refunded(service, client, session_factory):
    _seed(service)
    buy(client, "h1", USER)
    _consume(service, "EventCancelled", {"event_id": "e1"}, "x")

    resp = _post_webhook(client, "payment_intent.succeeded", {"id": _intent(service)})
    assert resp.status_code == 200
    order = order_by_hold(session_factory, "h1")
    assert order.booking_status == "CANCELLED"
    assert [r.reason for r in order.refunds] == ["late_payment"]

    service.process_refunds()
    assert order_by_hold(session_factory, "h1").refunds[0].status == "SUCCEEDED"


def test_failed_then_expired_payment_is_cancelled(service, client, session_factory):
    _seed(service)
    buy(client, "h1", USER)
    pi = _intent(service)
    service.handle_stripe_event(payment_failed_event(pi, "evt_f"))
    with service._sf() as db:
        o = db.scalar(select(Order).where(Order.hold_id == "h1"))
        o.payment_expires_at = datetime.now(UTC)
        db.commit()
    service.expire_stale_orders(datetime.now(UTC))
    order = order_by_hold(session_factory, "h1")
    assert order.payment.status == "CANCELLED"
    assert service._stripe.cancelled == [pi]


# ---------------------------------------------------------------- refund lifecycle


def test_pending_refund_is_finished_by_charge_refunded(service, outbox, session_factory):
    _seed(service)
    _create_direct(service)
    _set_booking(service, "h1", "CONFIRMED", "SUCCEEDED")
    _refund(service)
    service._stripe.refund_outcome = "pending"
    service.process_refunds()
    refund = order_by_hold(session_factory, "h1").refunds[0]
    assert refund.status == "PENDING"

    # Stripe's charge.refunded carries the Charge, not the Refund.
    pi = order_by_hold(session_factory, "h1").payment.stripe_payment_intent_id
    out = service.handle_stripe_event(charge_refunded_event(pi))
    outbox.deliver_pending(service._sf())
    assert out == "refund_finalised"
    refund = order_by_hold(session_factory, "h1").refunds[0]
    assert refund.status == "SUCCEEDED"
    assert len(outbox.by_type("RefundCompleted")) == 1
    assert len(outbox.by_type("OrderRefunded")) == 1

    # A replay (new event id, same charge) does not announce it twice.
    service.handle_stripe_event(charge_refunded_event(pi, event_id="evt_r2"))
    outbox.deliver_pending(service._sf())
    assert len(outbox.by_type("RefundCompleted")) == 1


def test_refund_waits_out_a_stripe_outage(service, outbox, session_factory):
    _seed(service)
    _create_direct(service)
    _set_booking(service, "h1", "CONFIRMED", "SUCCEEDED")
    _refund(service)
    service._stripe.refund_error = StripeUnavailableError("timeout")
    service.process_refunds()
    refund = order_by_hold(session_factory, "h1").refunds[0]
    assert refund.status == "REQUESTED"  # not lost in PENDING
    assert "timeout" in refund.last_error

    service._stripe.refund_error = None
    service.process_refunds()
    outbox.deliver_pending(service._sf())
    refund = order_by_hold(session_factory, "h1").refunds[0]
    assert refund.status == "SUCCEEDED"
    assert refund.last_error is None
    # Both attempts used the same idempotency key: one logical refund.
    assert len(set(service._stripe.refund_calls)) == 1
    assert len(outbox.by_type("RefundCompleted")) == 1


@pytest.mark.parametrize("cause", ["failed", "rejected"])
def test_failed_refund_is_reported_once_and_not_retried(service, outbox, session_factory, cause):
    _seed(service)
    _create_direct(service)
    _set_booking(service, "h1", "CONFIRMED", "SUCCEEDED")
    _refund(service)
    if cause == "failed":
        service._stripe.refund_outcome = "failed"
    else:
        service._stripe.refund_error = StripeRejectedError("charge already refunded")
    for _ in range(3):  # three refund-processor runs
        service.process_refunds()
        outbox.deliver_pending(service._sf())
    assert order_by_hold(session_factory, "h1").refunds[0].status == "FAILED"
    assert len(service._stripe.refund_calls) == 1
    assert len(outbox.by_type("RefundFailed")) == 1


def test_one_refund_row_per_order_is_enforced_by_the_schema(service):
    _seed(service)
    order = _create_direct(service)
    _set_booking(service, "h1", "CONFIRMED", "SUCCEEDED")
    _refund(service)
    with service._sf() as db:
        db.add(
            Refund(
                refund_id="r-dup",
                order_id=order.order_id,
                amount=1,
                currency="SGD",
                reason="refund_required",
                idempotency_key="other",
                status="REQUESTED",
            )
        )
        with pytest.raises(Exception, match="(?i)unique"):
            db.commit()


# ------------------------------------------------------------------- helpers


def _create_direct(service) -> Order:
    """Create an order for the already-seeded hold by calling the service directly
    (the route only adds auth + body parsing, so this is equivalent and avoids a second
    app)."""
    order, _secret, _created = service.create_order(
        user_id=USER, hold_id="h1", delivery_email=EMAIL, idempotency_key=None
    )
    return order


def _refund(service, reason: str = "event_cancelled") -> None:
    """Record a refund obligation for the paid order on hold h1."""
    with service._sf() as db:
        o = db.scalar(select(Order).where(Order.hold_id == "h1"))
        service._request_refund(db, o, reason)
        db.commit()


def _refund_required(order_id: str) -> dict:
    """RefundRequired's payload as reservation-service's OutboxWriter writes it."""
    return {
        "hold_id": "h1",
        "order_id": order_id,
        "user_id": USER,
        "event_id": "e1",
        "total_amount": 100_000,
        "currency": "SGD",
        "reason": "EVENT_CANCELLED",
        "required_at": "2026-11-20T12:00:00Z",
    }


def _post_webhook(client, event_type: str, obj: dict, event_id: str = "evt_w"):
    payload = json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}})
    return client.post(
        "/webhooks/stripe",
        data=payload.encode(),
        content_type="application/json",
        headers={"Stripe-Signature": "sig"},
    )
