"""Live-broker integration tests (the `broker` marker).

These run against the REAL local stack (`docker compose up --build`): the gunicorn
API, the `python -m app.worker` worker, real MySQL, and the shared broker with its
topology in place, plus the reservation stub. Nothing in this suite touches Stripe's
network:

- the payment-success webhook is hand-signed (stdlib HMAC over the webhook secret,
  exactly what `stripe.Webhook.construct_event` verifies in the API);
- orders are seeded straight into MySQL in the state each test needs;
- the only external calls the service makes are to the reservation stub.

What they prove that the in-process `test_flows.py` cannot:

- the outbox relay reads rows from MySQL and publishes the broker envelope to the
  `checkout.events` exchange with publisher confirms (asserted via the outbox row's
  `delivered_at` and, for consumer-less messages, via `ticketto.unrouted`);
- the worker's pika consumers actually drain the `checkout-service.*` queues and the
  service applies the effects;
- message-id deduplication holds over real broker redelivery;
- the webhook path end-to-end (hand-signed Stripe event -> state change -> relay).

Run it (stack already up), from the compose tests service:

    docker compose run --rm tests -- \
        pytest tests/integration/test_broker.py -m broker -p no:cacheprovider

...or from the host venv against the compose stack's host ports:

    IT_SERVICE_URL=http://127.0.0.1:3002 \
    IT_DATABASE_URL=mysql+pymysql://ticketto:ticketto-local@127.0.0.1:3308/checkout_service \
    IT_RABBITMQ_URL=amqp://ticketto:ticketto-local@127.0.0.1:5673/%2F \
    pytest tests/integration/test_broker.py -m broker

`STRIPE_WEBHOOK_SECRET` must match the value the API process was started with; when
unset, both here and in compose fall back to the same throwaway local test secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pika
import pytest
import requests
from sqlalchemy import create_engine, make_url, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.messaging.outbox import build_envelope
from app.models import Order, OutboxMessage, Payment

pytestmark = pytest.mark.broker

# The readiness/drain loops below probe the live stack; a failed probe is expected
# and retried, not fatal.
# pylint: disable=broad-exception-caught

SERVICE_URL = os.environ.get("IT_SERVICE_URL", "http://127.0.0.1:3002")
# Throwaway local test secret (not a real Stripe credential). The compose stack
# defaults the API and worker to the same value when no .env value is given.
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_test_local")
# The ticketto-rabbitmq image has no guest user; the admin is ticketto/ticketto-local
# (local-only credentials, matching docker-compose.yml).
DEFAULT_BROKER_URL = "amqp://ticketto:ticketto-local@127.0.0.1:5673/%2F"  # gitleaks:allow

RUN = str(time.time()).replace(".", "")
# The reservation stub answers every hold for its one fixed user.
USER_ID = "local-dev-user"
# Keep seeded deadlines well inside the stub's 15-minute payment window so the
# worker's deadline sweep cannot expire a test order mid-run.
EXPIRY = timedelta(minutes=10)

SCHEMA_VERSION = 1

# --------------------------------------------------------------------- fixtures


def _engine():
    url = make_url(
        os.environ.get(
            "IT_DATABASE_URL",
            # Local compose stack credentials (host port 3308); gitleaks:allow
            "mysql+pymysql://ticketto:ticketto-local@127.0.0.1:3308/checkout_service",
        )
    )
    if url.get_backend_name() == "sqlite":
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    # Same connect settings as the app's MySQL engine (app/config.py).
    return create_engine(
        url,
        connect_args={"charset": "utf8mb4", "init_command": "SET time_zone = '+00:00'"},
        pool_pre_ping=True,
    )


@pytest.fixture(scope="session")
def db():
    """A sessionmaker factory: tests call it to get a session (one per test)."""
    yield sessionmaker(bind=_engine(), expire_on_commit=False)


@pytest.fixture(scope="session")
def broker_conn():
    url = os.environ.get("IT_RABBITMQ_URL", DEFAULT_BROKER_URL)
    conn = pika.BlockingConnection(pika.URLParameters(url))
    yield conn
    conn.close()


# --------------------------------------------------------------------- helpers


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def up() -> None:
    """Wait until the API's /ready passes (DB + broker both reachable)."""
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            r = requests.get(f"{SERVICE_URL}/ready", timeout=3)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    pytest.fail(f"service never became ready at {SERVICE_URL}/ready")


def sign_event(event: dict) -> tuple[bytes, str]:
    """Hand-sign a Stripe webhook event the way Stripe does:
    v1 = HMAC-SHA256(secret, "{t}.{body}"), header `t={t},v1={v1}` (no space)."""
    body = json.dumps(event, separators=(",", ":")).encode()
    t = str(int(time.time()))
    v1 = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode(), t.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return body, f"t={t},v1={v1}"


def post_webhook(event: dict) -> requests.Response:
    body, sig = sign_event(event)
    return requests.post(
        f"{SERVICE_URL}/webhooks/stripe",
        data=body,  # raw bytes: json= would re-serialise and break the signature
        headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
        timeout=10,
    )


def get_order(order_id: str) -> dict:
    r = requests.get(
        f"{SERVICE_URL}/api/v1/orders/{order_id}",
        headers={"X-User-Sub": USER_ID},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()


def seed_order(  # pylint: disable=too-many-arguments
    db,
    *,
    order_id: str,
    hold_id: str,
    event_id: str,
    booking_status: str,
    payment_status: str,
    payment_intent_id: str | None,
) -> None:
    """Insert an order with its payment row directly, so a consumer or webhook can act
    on a known state without driving the full payment flow (and without calling Stripe)."""
    now = datetime.now(UTC)
    o = Order(
        order_id=order_id,
        hold_id=hold_id,
        user_id=USER_ID,
        event_id=event_id,
        quantity=1,
        unit_amount=100_000,
        total_amount=100_000,
        currency="SGD",
        booking_status=booking_status,
        payment_expires_at=now + EXPIRY,
        hold_expires_at=now + EXPIRY,
        confirmed_at=now if booking_status == "CONFIRMED" else None,
    )
    o.payment = Payment(
        payment_id=str(uuid.uuid4()),
        order_id=order_id,
        stripe_payment_intent_id=payment_intent_id,
        status=payment_status,
        amount=100_000,
        currency="SGD",
        succeeded_at=now if payment_status == "SUCCEEDED" else None,
    )
    with db() as session:
        session.add(o)
        session.commit()


# Where each consumed message comes from: (exchange, routing key, producer), as in
# rabbitmq-service topology/messages.yaml.
SOURCES = {
    "EventCancelled": ("event.events", "event.cancelled", "event-service"),
    "ReservationExpired": ("reservation.events", "reservation.expired", "reservation-service"),
    "RefundRequired": ("reservation.events", "refund.required", "reservation-service"),
}


def inject(
    conn: pika.BlockingConnection,
    message_type: str,
    payload: dict,
    message_id: str | None = None,
) -> str:
    """Publish one real broker envelope, exactly as its publisher would. Returns the
    message_id so a test can redeliver the same one."""
    exchange, routing_key, producer = SOURCES[message_type]
    mid = message_id or str(uuid.uuid4())
    body = json.dumps(
        {
            "message_id": mid,
            "message_type": message_type,
            "schema_version": SCHEMA_VERSION,
            "occurred_at": _now_iso(),
            "producer": producer,
            "payload": payload,
        },
        separators=(",", ":"),
    ).encode()
    props = pika.BasicProperties(
        message_id=mid,
        type=message_type,
        correlation_id=str(uuid.uuid4()),
        content_type="application/json",
        delivery_mode=pika.DeliveryMode.Persistent,
    )
    ch = conn.channel()
    try:
        ch.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=body,
            properties=props,
            mandatory=True,
        )
    finally:
        ch.close()
    return mid


def _declare_unrouted(conn: pika.BlockingConnection):
    """Declare a fresh exclusive queue bound to the `ticketto.unrouted` fanout.

    A fresh queue only receives messages published AFTER it is bound, so the test
    declares it first, triggers the publish it is waiting for, then drains.
    """
    ch = conn.channel()
    queue = f"ci-unrouted-{uuid.uuid4().hex[:12]}"
    ch.queue_declare(queue, exclusive=True, auto_delete=True)
    ch.queue_bind(queue, "ticketto.unrouted")
    ch.basic_qos(prefetch_count=50)
    return ch, queue


def _drain_unrouted(  # pylint: disable=too-many-arguments
    _conn: pika.BlockingConnection,
    ch,
    queue: str,
    *,
    match=None,
    wanted_types: frozenset[str] | None = None,
    timeout: float = 45.0,
) -> list[dict]:
    """Drain the sniffer until every wanted event type has been seen (among the
    envelopes matching `match`). The sniffer queue is deleted afterwards."""
    got: list[dict] = []
    seen: set[str] = set()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if wanted_types is not None and wanted_types <= seen:
                break
            # pika's basic_get is always non-blocking (no `nowait` argument).
            method, _props, body = ch.basic_get(queue)
            if method is None:
                time.sleep(0.5)
                continue
            env = json.loads(body)
            if match is None or match(env):
                got.append(env)
                seen.add(env["message_type"])
    finally:
        try:
            ch.queue_delete(queue)
        finally:
            ch.close()
    if wanted_types is not None:
        missing = wanted_types - seen
        assert not missing, f"never saw {sorted(missing)} in ticketto.unrouted (saw {sorted(seen)})"
    return got


def _refund_required(order_id: str, tag: str) -> dict:
    """A RefundRequired payload as reservation-service's OutboxWriter writes it."""
    return {
        "hold_id": f"hold-ci-{RUN}-{tag}",
        "order_id": order_id,
        "user_id": USER_ID,
        "event_id": f"evt-ci-{RUN}-{tag}",
        "total_amount": 100_000,
        "currency": "SGD",
        "reason": "EVENT_CANCELLED",
        "required_at": _now_iso(),
    }


def wait_for(condition, timeout: float = 45.0, what: str = "") -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(1)
    pytest.fail(f"timed out waiting for {what or 'condition'}")


# --------------------------------------------------------------------- the tests


@pytest.mark.usefixtures("broker_conn")  # needs the live broker, though it no longer reads it
def test_webhook_confirms_and_relay_publishes(db) -> None:
    """The full happy path over the live stack: a hand-signed payment_intent.succeeded
    webhook marks the payment SUCCEEDED, confirms the booking through the reservation
    stub, the order becomes CONFIRMED, and the relay publishes its OrderConfirmed row to
    checkout.events. No PaymentCompleted is staged any more (it had no consumer).

    OrderConfirmed is deliberately NOT asserted against ticketto.unrouted: in the
    full group topology it is consumed by the other services' queues (it is
    consumer-less only in the checkout-scoped CI topology), so its delivery is
    asserted through the outbox row's delivered_at instead.
    """
    up()
    order_id = f"order-ci-{RUN}-webhook"
    intent_id = f"pi-ci-{RUN}-webhook"
    seed_order(
        db,
        order_id=order_id,
        hold_id=f"hold-ci-{RUN}-webhook",
        event_id=f"evt-ci-{RUN}-webhook",
        booking_status="PENDING_PAYMENT",
        payment_status="PENDING",
        payment_intent_id=intent_id,
    )
    r = post_webhook(
        {
            "id": f"evt-ci-{RUN}-succeeded",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": intent_id}},
        }
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirmed"

    body = get_order(order_id)
    assert body["status"] == "CONFIRMED"
    assert body["payment"]["status"] == "SUCCEEDED"

    # The relay must have published the outbox row it staged in the same commit.
    def all_delivered() -> bool:
        with db() as s:
            rows = s.scalars(
                select(OutboxMessage).where(OutboxMessage.aggregate_id == order_id)
            ).all()
            return all(r.delivered_at is not None for r in rows) and len(rows) >= 1

    wait_for(all_delivered, what=f"the outbox rows for {order_id} to be delivered")
    with db() as s:
        rows = s.scalars(select(OutboxMessage).where(OutboxMessage.aggregate_id == order_id)).all()
    types = [r.event_type for r in rows]
    assert types.count("OrderConfirmed") == 1
    assert types.count("PaymentCompleted") == 0


def test_event_cancelled_cancels_pending_order(broker_conn, db) -> None:
    """The worker's EventCancelled consumer cancels an unpaid PENDING_PAYMENT booking
    (nothing paid, so no refund obligation)."""
    up()
    order_id = f"order-ci-{RUN}-cancelled"
    event_id = f"evt-ci-{RUN}-cancelled"
    seed_order(
        db,
        order_id=order_id,
        hold_id=f"hold-ci-{RUN}-cancelled",
        event_id=event_id,
        booking_status="PENDING_PAYMENT",
        payment_status="PENDING",
        payment_intent_id=None,
    )
    inject(
        broker_conn,
        "EventCancelled",
        {
            "event_id": event_id,
            "cancellation_id": str(uuid.uuid4()),
            "cancelled_at": _now_iso(),
            "reason": "test",
        },
    )
    wait_for(
        lambda: get_order(order_id)["status"] == "CANCELLED",
        what=f"{order_id} to be CANCELLED",
    )
    body = get_order(order_id)
    assert body["refunds"] == []  # nothing was paid, nothing to refund
    assert body["payment"]["status"] == "CANCELLED"  # an unpaid payment can no longer be taken


def test_reservation_expired_expires_pending_order(broker_conn, db) -> None:
    """The worker's ReservationExpired consumer expires an unpaid booking and cancels
    its (unpaid) payment so a late card can never be charged."""
    up()
    order_id = f"order-ci-{RUN}-expired"
    seed_order(
        db,
        order_id=order_id,
        hold_id=f"hold-ci-{RUN}-expired",
        event_id=f"evt-ci-{RUN}-expired",
        booking_status="PENDING_PAYMENT",
        payment_status="PENDING",
        payment_intent_id=None,
    )
    inject(
        broker_conn,
        "ReservationExpired",
        {
            "hold_id": f"hold-ci-{RUN}-expired",
            "admission_id": str(uuid.uuid4()),
            "user_id": USER_ID,
            "event_id": f"evt-ci-{RUN}-expired",
            "category_id": str(uuid.uuid4()),
            "quantity": 1,
            "expired_at": _now_iso(),
        },
    )
    wait_for(
        lambda: get_order(order_id)["status"] == "EXPIRED",
        what=f"{order_id} to be EXPIRED",
    )
    body = get_order(order_id)
    assert body["payment"]["status"] == "CANCELLED"
    assert body["refunds"] == []


def test_refund_required_on_confirmed_order_records_obligation(broker_conn, db) -> None:
    """The worker's RefundRequired consumer records a refund obligation on a CONFIRMED
    order that is paid, and cancels the booking (the money is going back). The worker's
    refund processor will later attempt the Stripe refund and fail gracefully with no
    test-mode key (covered by the unit tests); the consumer's own effect is the
    obligation row."""
    up()
    order_id = f"order-ci-{RUN}-refund"
    seed_order(
        db,
        order_id=order_id,
        hold_id=f"hold-ci-{RUN}-refund",
        event_id=f"evt-ci-{RUN}-refund",
        booking_status="CONFIRMED",
        payment_status="SUCCEEDED",
        payment_intent_id=f"pi-ci-{RUN}-refund",
    )
    inject(
        broker_conn,
        "RefundRequired",
        _refund_required(order_id, "refund"),
    )
    wait_for(
        lambda: any(r["reason"] == "refund_required" for r in get_order(order_id)["refunds"]),
        what=f"a refund obligation on {order_id}",
    )
    body = get_order(order_id)
    assert body["status"] == "CANCELLED"
    refunds = [r for r in body["refunds"] if r["reason"] == "refund_required"]
    assert len(refunds) == 1
    assert refunds[0]["status"] in ("REQUESTED", "PENDING")


def test_redelivery_applied_once(broker_conn, db) -> None:
    """At-least-once delivery: publishing the same message_id twice (a broker
    redelivery) must produce exactly one refund obligation - the message-id dedupe
    row is committed in the same transaction as the business effect."""
    up()
    order_id = f"order-ci-{RUN}-redel"
    seed_order(
        db,
        order_id=order_id,
        hold_id=f"hold-ci-{RUN}-redel",
        event_id=f"evt-ci-{RUN}-redel",
        booking_status="CONFIRMED",
        payment_status="SUCCEEDED",
        payment_intent_id=f"pi-ci-{RUN}-redel",
    )
    data = _refund_required(order_id, "redel")
    mid = inject(broker_conn, "RefundRequired", data)
    inject(
        broker_conn,
        "RefundRequired",
        data,
        message_id=mid,
    )
    wait_for(
        lambda: any(r["reason"] == "refund_required" for r in get_order(order_id)["refunds"]),
        what=f"a refund obligation on {order_id}",
    )
    refunds = [r for r in get_order(order_id)["refunds"] if r["reason"] == "refund_required"]
    assert len(refunds) == 1, f"redelivered message produced {len(refunds)} refunds"


def test_outbox_relay_publishes_staged_message_to_unrouted(broker_conn, db) -> None:
    """The relay publishes whatever the outbox table holds: a row staged directly in
    MySQL (as every state change stages its message in the same commit) is published to
    checkout.events and, being consumer-less, parked in ticketto.unrouted - and the row
    is marked delivered."""
    up()
    order_id = f"order-ci-{RUN}-relay"
    # A message type no queue is bound to, so the alternate exchange parks it.
    env = build_envelope("RelayProbe", {"order_id": order_id})
    sniffer_ch, sniffer_q = _declare_unrouted(broker_conn)
    with db() as s:
        s.add(
            OutboxMessage(
                message_id=env["message_id"],
                event_type=env["message_type"],
                routing_key="relay.probe",
                aggregate_id=order_id,
                payload=env,
                correlation_id=str(uuid.uuid4()),
                occurred_at=datetime.now(UTC),
            )
        )
        s.commit()

    got = _drain_unrouted(
        broker_conn,
        sniffer_ch,
        sniffer_q,
        match=lambda e: e["message_id"] == env["message_id"],
        wanted_types=frozenset({"RelayProbe"}),
    )
    assert got, "relay did not publish the staged message"

    with db() as s:
        row = s.scalar(select(OutboxMessage).where(OutboxMessage.message_id == env["message_id"]))
        assert row is not None and row.delivered_at is not None, (
            "outbox row was never marked delivered"
        )


def test_unusable_refund_required_is_dead_lettered_at_once(broker_conn) -> None:
    """A RefundRequired checkout can never act on (no order_id) goes straight to
    checkout-service.RefundRequired.dlq - rejected without requeue, not redelivered five
    times first - and its x-death header names the queue it failed in."""
    up()
    payload = _refund_required(f"order-ci-{RUN}-poison", "poison")
    del payload["order_id"]
    mid = inject(broker_conn, "RefundRequired", payload)

    found: dict = {}

    def in_dlq() -> bool:
        # Peek without acking (each get moves on to the next message); closing the
        # channel puts every other message back. Only our own message is acked away.
        ch = broker_conn.channel()
        try:
            for _ in range(500):
                method, props, body = ch.basic_get("checkout-service.RefundRequired.dlq")
                if method is None:
                    return False
                if json.loads(body)["message_id"] == mid:
                    ch.basic_ack(method.delivery_tag)
                    found["death"] = (props.headers or {}).get("x-death") or []
                    return True
            return False
        finally:
            ch.close()

    wait_for(in_dlq, what=f"{mid} in checkout-service.RefundRequired.dlq")
    death = found["death"][0]
    assert death["queue"] == "checkout-service.RefundRequired"
    assert death["reason"] == "rejected"
    assert death.get("count", 1) == 1  # rejected once, not after five redeliveries
