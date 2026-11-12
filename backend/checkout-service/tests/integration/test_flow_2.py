"""E2E against the REAL reservation-service: the S2B flow (checkout confirms the hold
over HTTP) and the S2C handoff (checkout publishes OrderConfirmed to the broker).

Unlike test_broker.py, which talks to the reservation stub, this needs:
  - reservation-service running with HS256 admission tokens, e.g. from its repo:
        ADMISSION_TOKEN_ALGORITHM=HS256 ADMISSION_TOKEN_SECRET=<secret> \\
        docker compose --profile app up -d --build
  - checkout's API + worker started with RESERVATION_BASE_URL pointing at it
  - checkout's MySQL and the shared broker reachable from where pytest runs

and these variables (it is skipped otherwise):
    IT_RESERVATION_URL        e.g. http://127.0.0.1:8081
    IT_ADMISSION_TOKEN_SECRET the same secret reservation-service was started with
    IT_SERVICE_URL / IT_DATABASE_URL / IT_RABBITMQ_URL  as for test_broker.py

    pytest tests/integration/test_flow_2.py -m broker

Stripe's network is never called: the order's payment intent id is filled in straight
in MySQL when no Stripe key is configured, and the success webhook is hand-signed.
Only reservation's real endpoints are used (no stand-ins):
  POST /internal/v1/events/{event_id}/inventory   seed a category
  POST /api/v1/reservations                       buyer creates a hold (admission token)
  GET  /internal/v1/reservations/{hold_id}         what checkout reads
  POST /internal/v1/reservations/{hold_id}/confirm what checkout calls
"""

import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pika
import pytest
import requests
from sqlalchemy import create_engine, make_url, select
from sqlalchemy.orm import sessionmaker

from app.models import Order, Payment
from tests.integration.test_broker import sign_event, wait_for

pytestmark = [
    pytest.mark.broker,
    pytest.mark.skipif(
        not (os.environ.get("IT_RESERVATION_URL") and os.environ.get("IT_ADMISSION_TOKEN_SECRET")),
        reason="needs a real reservation-service (IT_RESERVATION_URL, IT_ADMISSION_TOKEN_SECRET)",
    ),
]

CHECKOUT_API = os.environ.get("IT_SERVICE_URL", "http://127.0.0.1:3002")
RESERVATION_API = os.environ.get("IT_RESERVATION_URL", "")
ADMISSION_SECRET = os.environ.get("IT_ADMISSION_TOKEN_SECRET", "")
DATABASE_URL = os.environ.get(
    "IT_DATABASE_URL",
    "mysql+pymysql://ticketto:ticketto-local@127.0.0.1:3308/checkout_service",  # gitleaks:allow
)
RABBITMQ_URL = os.environ.get(
    "IT_RABBITMQ_URL",
    "amqp://ticketto:ticketto-local@127.0.0.1:5673/%2F",  # gitleaks:allow
)
USER_ID = f"live-e2e-{uuid.uuid4().hex[:8]}"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def admission_token(event_id: str) -> str:
    """An HS256 admission token with the claims AdmissionTokenService requires."""
    now = datetime.now(UTC)
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "admission_id": str(uuid.uuid4()),
        "user_id": USER_ID,
        "user_sub": USER_ID,
        "event_id": event_id,
        "selection_expires_at": _iso(now + timedelta(minutes=10)),
        "admission_expires_at": _iso(now + timedelta(minutes=30)),
    }
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    signature = hmac.new(ADMISSION_SECRET.encode(), signing_input.encode(), hashlib.sha256)
    return f"{signing_input}.{_b64(signature.digest())}"


@pytest.fixture(scope="module")
def db():
    url = make_url(DATABASE_URL)
    engine = create_engine(
        url, connect_args={"charset": "utf8mb4", "init_command": "SET time_zone = '+00:00'"}
    )
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(scope="module")
def broker_conn():
    conn = pika.BlockingConnection(pika.URLParameters(RABBITMQ_URL))
    yield conn
    conn.close()


def _ensure_intent(db, hold_id: str) -> tuple[str, str]:
    """(order_id, payment_intent_id). Without a Stripe key the order is committed but
    its intent is not created (POST answers 503), so give it one directly."""
    with db() as s:
        order = s.scalar(select(Order).where(Order.hold_id == hold_id))
        assert order is not None, "checkout did not store the order"
        payment = s.scalar(select(Payment).where(Payment.order_id == order.order_id))
        if not payment.stripe_payment_intent_id:
            payment.stripe_payment_intent_id = f"pi_e2e_{uuid.uuid4().hex[:12]}"
            s.commit()
        return order.order_id, payment.stripe_payment_intent_id


def test_s2b_confirm_against_real_reservation_and_s2c_handoff(  # pylint: disable=too-many-locals  # one linear e2e story
    db, broker_conn
):
    event_id = str(uuid.uuid4())
    category_id = str(uuid.uuid4())

    # Sniff OrderConfirmed on checkout.events before anything is published.
    ch = broker_conn.channel()
    sniffer = f"ci-sniff-{uuid.uuid4().hex[:8]}"
    ch.queue_declare(sniffer, exclusive=True, auto_delete=True)
    ch.queue_bind(sniffer, "checkout.events", routing_key="order.confirmed")

    # 1. Seed the event's inventory in reservation (stand-in for EventPublished).
    r = requests.post(
        f"{RESERVATION_API}/internal/v1/events/{event_id}/inventory",
        json={
            "categories": [
                {
                    "category_id": category_id,
                    "total_quantity": 10,
                    "unit_amount": 8800,
                    "currency": "SGD",
                }
            ]
        },
        timeout=10,
    )
    assert r.status_code == 200, r.text

    # 2. The buyer creates a hold with an admission token.
    r = requests.post(
        f"{RESERVATION_API}/api/v1/reservations",
        headers={"X-User-Sub": USER_ID},
        json={
            "event_id": event_id,
            "category_id": category_id,
            "quantity": 2,
            "admission_token": admission_token(event_id),
        },
        timeout=10,
    )
    assert r.status_code in (200, 201), r.text
    hold_id = r.json()["hold_id"]

    # 3. Checkout prices the order from reservation's InternalHoldView.
    r = requests.post(
        f"{CHECKOUT_API}/api/v1/orders",
        headers={"X-User-Sub": USER_ID},
        json={"hold_id": hold_id, "delivery_email": "buyer@example.com"},
        timeout=10,
    )
    assert r.status_code in (200, 201, 503), r.text  # 503 = no Stripe key configured
    order_id, intent_id = _ensure_intent(db, hold_id)
    order = requests.get(
        f"{CHECKOUT_API}/api/v1/orders/{order_id}", headers={"X-User-Sub": USER_ID}, timeout=10
    ).json()
    assert order["quantity"] == 2
    assert order["total_amount"] == 17600  # from the hold's price snapshot, not the browser
    assert order["status"] == "PENDING_PAYMENT"

    # 4. Stripe says the payment succeeded -> checkout confirms the hold over HTTP.
    body, sig = sign_event(
        {
            "id": f"evt_{uuid.uuid4().hex[:12]}",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": intent_id}},
        }
    )
    r = requests.post(
        f"{CHECKOUT_API}/webhooks/stripe",
        data=body,
        headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirmed"

    # S2B: reservation has the hold CONFIRMED for exactly this order.
    held = requests.get(f"{RESERVATION_API}/internal/v1/reservations/{hold_id}", timeout=10)
    assert held.status_code == 200
    assert held.json()["status"] == "CONFIRMED"
    assert held.json()["order_id"] == order_id

    # Confirm is idempotent for the same order (what checkout's retry sweep relies on).
    again = requests.post(
        f"{RESERVATION_API}/internal/v1/reservations/{hold_id}/confirm",
        json={"order_id": order_id, "payment_reference": intent_id},
        timeout=10,
    )
    assert again.status_code == 200
    assert again.json()["duplicate"] is True

    # S2C: checkout published OrderConfirmed in the broker envelope.
    seen: list[dict] = []

    def published() -> bool:
        method, props, raw = ch.basic_get(sniffer, auto_ack=True)
        if method is None:
            return False
        envelope = json.loads(raw)
        if envelope["payload"].get("order_id") == order_id:
            assert props.type == "OrderConfirmed"
            seen.append(envelope)
            return True
        return False

    try:
        wait_for(published, what="OrderConfirmed on checkout.events")
    finally:
        ch.queue_delete(sniffer)
        ch.close()
    envelope = seen[0]
    assert envelope["message_type"] == "OrderConfirmed"
    assert envelope["producer"] == "checkout-service"
    assert envelope["payload"]["hold_id"] == hold_id
    assert envelope["payload"]["event_id"] == event_id
    assert envelope["payload"]["quantity"] == 2
