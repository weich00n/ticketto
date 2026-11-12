"""Adapters between rabbitmq-service's generated contract tests and user-service's real code.

contract_queues: the queues the worker consumes (app.messaging.consumer.QUEUES).

contract_handle: the consumer's real decision for one delivery, `dispose`, running the real
handler (handle_message) on the unit tests' SQLite database: ack, requeue, or dead-letter
(reported as "reject"). OrderConfirmed is only recorded for a buyer who has a profile, and is
retried otherwise (UnknownUser), so the buyer the body names is given one first, as in
production, where the profile exists before the buyer can order.
"""

from __future__ import annotations

import json

import pytest

from app.messaging.consumer import QUEUES, Disposition, dispose
from app.messaging.handlers import handle_message
from app.models import User
from tests.unit import conftest as unit

# The unit tests' database fixtures, re-exported so pytest finds them here.
engine = unit.engine
session_factory = unit.session_factory

OUTCOMES = {
    Disposition.ACK: "ack",
    Disposition.REQUEUE: "requeue",
    Disposition.DEAD_LETTER: "reject",
}


def _ensure_buyer(session_factory, body: bytes) -> None:
    """Give the buyer named by an OrderConfirmed body a profile, if it names one."""
    try:
        payload = json.loads(body).get("payload") or {}
        sub = str(payload["user_id"]).strip().lower()
    except (ValueError, AttributeError, KeyError):
        return  # not something to set up for: the handler decides what to do with it
    with session_factory() as session, session.begin():
        if session.query(User).filter_by(cognito_sub=sub).one_or_none() is None:
            session.add(
                User(
                    user_id="8f14e45f-ceea-4b6a-9a3b-1f2d3c4e5f60",
                    cognito_sub=sub,
                    email="buyer@example.com",
                    name="Contract Buyer",
                    role="buyer",
                )
            )


@pytest.fixture
def contract_queues() -> set[str]:
    return set(QUEUES)


@pytest.fixture
def contract_handle(session_factory):
    def handle(_queue: str, body: bytes, properties: dict[str, object]) -> str:
        _ensure_buyer(session_factory, body)
        disposition = dispose(
            body,
            lambda message: handle_message(session_factory, message),
            correlation_id=properties.get("correlation_id"),
        )
        return OUTCOMES[disposition]

    return handle
