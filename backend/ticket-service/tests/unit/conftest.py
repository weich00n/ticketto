import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine

from app.config import Settings
from app.db import Base
from app.issuance import handle_message
from app.main import create_app
from app.messages import parse_message
from app.qr import QrSigner

BUYER = "2f9c7d10-6a3b-4e55-8c21-0f4a9b7e1d33"
OTHER_BUYER = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'ticket-service.db'}",
        rabbitmq_url="amqp://unused",
        qr_signing_keys={"k1": "k1-secret-" + "x" * 40, "k0": "k0-secret-" + "y" * 40},
        qr_active_key_id="k1",
        outbox_retry_base_seconds=5,
        outbox_retry_max_seconds=300,
    )


@pytest.fixture()
def app(settings):
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    app = create_app(settings)
    app.config.update(TESTING=True)
    yield app
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def gateway_client(app, settings):  # pylint: disable=unused-argument  # app creates the schema
    """A second app on the same database with API Gateway authentication turned on."""
    gateway_app = create_app(type(settings)(**{**settings.__dict__, "auth_mode": "gateway"}))
    gateway_app.config.update(TESTING=True)
    return gateway_app.test_client()


@pytest.fixture()
def session_factory(app):
    return app.extensions["session_factory"]


@pytest.fixture()
def signer(app) -> QrSigner:
    return app.extensions["qr_signer"]


def buyer_headers(user_id: str = BUYER) -> dict[str, str]:
    """No authentication yet: the buyer is named by X-User-Id (AUTH_MODE=none)."""
    return {"X-User-Id": user_id}


def staff_headers() -> dict[str, str]:
    return {"X-User-Id": "gate-1"}


def gateway_buyer_headers(user_id: str = BUYER) -> dict[str, str]:
    """What API Gateway sends once Cognito is in front (AUTH_MODE=gateway)."""
    return {
        "X-User-Sub": user_id,
        "X-User-Username": f"{user_id[:8]}@example.com",
        "X-User-Groups": "[buyer]",
    }


def gateway_staff_headers(group: str = "organiser") -> dict[str, str]:
    """A check-in caller behind API Gateway: the team's Cognito role is `organiser`."""
    return {
        "X-User-Sub": "5c4b3a29-1807-4f6e-9d5c-4b3a29180706",
        "X-User-Username": "gate-1",
        "X-User-Groups": f"[{group}]",
    }


def envelope(message_type: str, payload: dict[str, Any], message_id: str | None = None) -> bytes:
    return json.dumps(
        {
            "message_id": message_id or new_id(),
            "message_type": message_type,
            "schema_version": 1,
            "occurred_at": iso(datetime.now(UTC)),
            "producer": "test",
            "payload": payload,
        }
    ).encode()


def order_payload(**overrides) -> dict[str, Any]:
    payload = {
        "order_id": new_id(),
        "hold_id": new_id(),
        "admission_id": new_id(),
        "user_id": BUYER,
        "event_id": new_id(),
        "category_id": new_id(),
        "quantity": 2,
        "delivery_email": "buyer@example.com",
        "confirmed_at": iso(datetime.now(UTC)),
    }
    payload.update(overrides)
    return payload


def event_published(event_id: str, category_id: str, version: int = 2, **overrides):
    starts = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)
    payload = {
        "event_id": event_id,
        "event_version": version,
        "title": "The Midnight Symphony",
        "starts_at": iso(starts),
        "release_at": iso(starts - timedelta(days=20)),
        "total_capacity": 300,
        "ticket_categories": [
            {"category_id": category_id, "name": "CAT 1", "price_amount": 8800, "currency": "SGD"}
        ],
    }
    payload.update(overrides)
    return payload


Deliver = Callable[..., str]


@pytest.fixture()
def deliver(session_factory, signer) -> Deliver:
    """Apply a message the way the consumer does. Returns the handler's outcome."""

    def apply(message_type: str, payload: dict[str, Any], message_id: str | None = None) -> str:
        message = parse_message(envelope(message_type, payload, message_id))
        assert message is not None
        return handle_message(session_factory, signer, message)

    return apply
