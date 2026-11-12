"""Publishes the outbox: TicketIssued, for notification-service to email the buyer's tickets.

Issuance writes one pending `outbox_messages` row per order in the same transaction as the
tickets (issuance.queue_ticket_issued). The worker claims due rows (SKIP LOCKED, so several
workers never publish the same row at once), builds the message from the order's tickets that
are still valid, and publishes it to Ticket's exchange as rabbitmq-service's MESSAGING.md asks:
persistent, with publisher confirms and `mandatory`, and the message_id, type and
correlation_id properties set. A row is marked PUBLISHED only once the broker has confirmed it.

If the broker refuses or can't be reached, the row stays PENDING and is retried with backoff,
for as long as it takes. A crash mid-publish leaves the row claimed only until its lease
expires; the retry reuses the same message_id, so notification-service can deduplicate it.
An order whose tickets were invalidated before its message went out is CANCELLED instead.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import pika
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .correlation import bound_correlation_id
from .event_details import ensure_event_details
from .issuance import utcnow
from .models import (
    ISSUED,
    OUTBOX_CANCELLED,
    OUTBOX_PENDING,
    OUTBOX_PUBLISHED,
    VALID,
    EventState,
    OutboxMessage,
    Ticket,
    TicketOrder,
)
from .qr import QrSigner
from .serializers import iso_utc

logger = logging.getLogger(__name__)

EXCHANGE = "ticket.events"  # Ticket's own exchange on the team broker
PRODUCER = "ticket-service"
SCHEMA_VERSION = 1
# A claimed row is not picked up again until its lease expires. Each publish takes at most
# ~2 s for an Event Service lookup plus the broker's confirm, so a batch fits well inside it.
CLAIM_LEASE = timedelta(minutes=2)
BATCH_SIZE = 20


class Publisher(Protocol):
    def publish(
        self,
        routing_key: str,
        body: bytes,
        *,
        message_id: str,
        message_type: str,
        correlation_id: str | None,
    ) -> None: ...


class RabbitPublisher:
    """Publishes to `ticket.events` over one blocking connection, reopened after any error."""

    def __init__(self, rabbitmq_url: str):
        self._url = rabbitmq_url
        self._connection: pika.BlockingConnection | None = None
        self._channel = None

    def _open_channel(self):
        if self._channel is not None and self._channel.is_open:
            return self._channel
        self.close()
        self._connection = pika.BlockingConnection(pika.URLParameters(self._url))
        channel = self._connection.channel()
        # Allowed for Ticket's user, and a no-op on the team broker, which creates the exchange
        # at boot. Exactly these settings: anything else closes the channel (406).
        channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
        channel.confirm_delivery()
        self._channel = channel
        return channel

    def publish(
        self,
        routing_key: str,
        body: bytes,
        *,
        message_id: str,
        message_type: str,
        correlation_id: str | None,
    ) -> None:
        """Returns once the broker has stored the message; raises otherwise."""
        properties = pika.BasicProperties(
            message_id=message_id,
            type=message_type,
            correlation_id=correlation_id,
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,  # survives a broker restart
        )
        try:
            self._open_channel().basic_publish(
                EXCHANGE, routing_key, body, properties=properties, mandatory=True
            )
        except Exception:
            self.close()  # start from a fresh connection next time
            raise

    def keepalive(self) -> None:
        """Answer the broker's heartbeats between publishes, or drop a dead connection."""
        if self._connection is None or not self._connection.is_open:
            return
        try:
            self._connection.process_data_events(time_limit=0)
        except Exception:  # pylint: disable=broad-exception-caught
            self.close()

    def close(self) -> None:
        connection, self._connection, self._channel = self._connection, None, None
        if connection is not None and connection.is_open:
            try:
                connection.close()
            except Exception:  # pylint: disable=broad-exception-caught
                logger.debug("closing the publisher connection failed", exc_info=True)


@dataclass(frozen=True)
class _Claim:
    message_id: str
    message_type: str
    routing_key: str
    order_id: str
    correlation_id: str | None
    attempts: int
    created_at: datetime


def claim_due(session_factory: "sessionmaker[Session]", now: datetime, limit: int) -> list[_Claim]:
    with session_factory() as session, session.begin():
        rows = session.scalars(
            select(OutboxMessage)
            .where(OutboxMessage.status == OUTBOX_PENDING, OutboxMessage.next_attempt_at <= now)
            .order_by(OutboxMessage.next_attempt_at, OutboxMessage.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        claims = []
        for row in rows:
            row.attempts += 1
            row.last_attempt_at = now
            row.next_attempt_at = now + CLAIM_LEASE  # lease; replaced by the outcome
            claims.append(
                _Claim(
                    message_id=row.message_id,
                    message_type=row.message_type,
                    routing_key=row.routing_key,
                    order_id=row.order_id,
                    correlation_id=row.correlation_id,
                    attempts=row.attempts,
                    created_at=row.created_at,
                )
            )
        return claims


def _set(session_factory, message_id: str, *conditions, **values) -> None:
    with session_factory() as session, session.begin():
        session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.message_id == message_id, *conditions)
            .values(**values)
            .execution_options(synchronize_session=False)
        )


def ticket_issued_payload(
    order: TicketOrder, tickets: list[Ticket], state: EventState | None
) -> dict[str, Any]:
    """The payload agreed with notification-service. Event details Ticket doesn't know yet
    (no EventPublished, Event Service unreachable) are null rather than guessed."""
    names = (state.category_names or {}) if state else {}
    return {
        "order_id": order.order_id,
        "user_id": order.user_id,
        "event_id": order.event_id,
        "delivery_email": order.delivery_email,
        "title": state.title if state else None,
        "venue": state.venue if state else None,
        "starts_at": iso_utc(state.starts_at) if state else None,
        "category_name": names.get(order.category_id),
        "tickets": [
            {
                "ticket_id": t.ticket_id,
                "ticket_number": t.ticket_number,
                "qr_payload": QrSigner.payload(t.ticket_id, t.qr_key_id, t.qr_signature),
            }
            for t in tickets
        ],
    }


def envelope(claim: _Claim, payload: dict[str, Any]) -> dict[str, Any]:
    """The team broker's envelope (rabbitmq-service contracts/envelope.schema.json). It allows
    no other fields, so the correlation ID travels only as the AMQP property."""
    return {
        "message_id": claim.message_id,
        "message_type": claim.message_type,
        "schema_version": SCHEMA_VERSION,
        "occurred_at": iso_utc(claim.created_at),
        "producer": PRODUCER,
        "payload": payload,
    }


def retry_delay(settings: Settings, attempts: int) -> timedelta:
    """Base, doubling per attempt, capped at the maximum."""
    seconds = settings.outbox_retry_base_seconds * 2 ** min(attempts - 1, 30)
    return timedelta(seconds=min(seconds, settings.outbox_retry_max_seconds))


def deliver(
    claim: _Claim,
    *,
    session_factory: "sessionmaker[Session]",
    settings: Settings,
    publisher: Publisher,
) -> str:
    with session_factory() as session:
        order = session.get(TicketOrder, claim.order_id)
        event_id = order.event_id if order else None
        issued = order is not None and order.issuance_status == ISSUED
    if not issued:
        _cancel(session_factory, claim, "order not issued")
        return "cancelled"

    try:
        ensure_event_details(session_factory, settings.event_service_url, event_id)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("filling in event details failed; publishing without them")
    with session_factory() as session:
        order = session.get(TicketOrder, claim.order_id)
        tickets = session.scalars(
            select(Ticket)
            .where(Ticket.order_id == claim.order_id, Ticket.status == VALID)
            .order_by(Ticket.ticket_number)
        ).all()
        state = session.get(EventState, event_id)
        payload = ticket_issued_payload(order, list(tickets), state) if tickets else None
    if payload is None:
        _cancel(session_factory, claim, "no valid tickets")
        return "cancelled"

    try:
        publisher.publish(
            claim.routing_key,
            json.dumps(envelope(claim, payload), separators=(",", ":")).encode(),
            message_id=claim.message_id,
            message_type=claim.message_type,
            correlation_id=claim.correlation_id,
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        message = f"{type(error).__name__}: {error}"[:1000]
        delay = retry_delay(settings, claim.attempts)
        # Conditional on PENDING: if the tickets were invalidated meanwhile, stay CANCELLED.
        _set(
            session_factory,
            claim.message_id,
            OutboxMessage.status == OUTBOX_PENDING,
            next_attempt_at=utcnow() + delay,
            last_error=message,
        )
        logger.warning(
            "publishing failed; will retry",
            extra={
                "message_id": claim.message_id,
                "message_type": claim.message_type,
                "order_id": claim.order_id,
                "attempts": claim.attempts,
                "retry_in_seconds": delay.total_seconds(),
                "error": message,
            },
        )
        return "retry"

    # Unconditional: the broker has it, so the record says so even if the order was
    # invalidated while publishing (notification-service also hears about that).
    _set(
        session_factory,
        claim.message_id,
        status=OUTBOX_PUBLISHED,
        published_at=utcnow(),
        next_attempt_at=None,
        last_error=None,
    )
    logger.info(
        "message published",
        extra={
            "message_id": claim.message_id,
            "message_type": claim.message_type,
            "order_id": claim.order_id,
            "tickets": len(payload["tickets"]),
        },
    )
    return "published"


def _cancel(session_factory, claim: _Claim, reason: str) -> None:
    _set(
        session_factory,
        claim.message_id,
        OutboxMessage.status == OUTBOX_PENDING,
        status=OUTBOX_CANCELLED,
        next_attempt_at=None,
    )
    logger.info(
        "message cancelled before publishing",
        extra={"message_id": claim.message_id, "order_id": claim.order_id, "reason": reason},
    )


def run_once(
    session_factory: "sessionmaker[Session]", settings: Settings, publisher: Publisher
) -> int:
    """Publish the messages that are due now. Returns how many were attempted."""
    claims = claim_due(session_factory, utcnow(), BATCH_SIZE)
    for claim in claims:
        with bound_correlation_id(claim.correlation_id or claim.message_id):
            try:
                deliver(
                    claim,
                    session_factory=session_factory,
                    settings=settings,
                    publisher=publisher,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                # e.g. the database went away; the lease expires and the row is retried.
                logger.exception(
                    "publishing crashed",
                    extra={"message_id": claim.message_id, "order_id": claim.order_id},
                )
    return len(claims)
