import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pika
from sqlalchemy import select
from sqlalchemy.orm import Session

from .correlation import get_correlation_id
from .models import Event, OutboxMessage
from .schemas import queue_join_opens_at
from .serializers import as_utc, category_response, iso_utc

logger = logging.getLogger(__name__)

EXCHANGE = "event.events"
PRODUCER = "event-service"
SCHEMA_VERSION = 1
# A message the broker keeps refusing is given up after this many tries, so it
# cannot hold back every message behind it. It stays in the table for inspection.
MAX_DELIVERY_ATTEMPTS = 10


def _add_message(
    session: Session, message_type: str, routing_key: str, event: Event, payload: dict[str, Any]
) -> None:
    occurred_at = datetime.now(UTC)
    message_id = str(uuid4())
    correlation_id = get_correlation_id()
    session.add(
        OutboxMessage(
            message_id=message_id,
            message_type=message_type,
            routing_key=routing_key,
            aggregate_id=event.event_id,
            occurred_at=occurred_at,
            correlation_id=None if correlation_id == "-" else correlation_id,
            payload={
                "message_id": message_id,
                "message_type": message_type,
                "schema_version": SCHEMA_VERSION,
                "occurred_at": iso_utc(occurred_at),
                "producer": PRODUCER,
                "payload": payload,
            },
        )
    )


def add_event_published_message(session: Session, event: Event) -> None:
    _add_message(
        session,
        "EventPublished",
        "event.published",
        event,
        {
            "event_id": event.event_id,
            "event_version": event.event_version,
            "title": event.title,
            "venue": event.venue,
            "starts_at": iso_utc(event.starts_at),
            "release_at": iso_utc(event.ticket_release_at),
            "queue_join_opens_at": iso_utc(queue_join_opens_at(as_utc(event.ticket_release_at))),
            "published_at": iso_utc(event.published_at),
            "total_capacity": sum(c.total_quantity for c in event.ticket_categories),
            "ticket_categories": [category_response(c) for c in event.ticket_categories],
        },
    )


def add_event_cancelled_message(session: Session, event: Event) -> None:
    _add_message(
        session,
        "EventCancelled",
        "event.cancelled",
        event,
        {
            "event_id": event.event_id,
            "event_version": event.event_version,
            "cancellation_id": str(uuid4()),
            "cancelled_at": iso_utc(event.cancelled_at),
            "reason": event.cancellation_reason,
        },
    )


def publish_pending_messages(session_factory, rabbitmq_url: str, *, limit: int = 100) -> int:
    """Send pending messages oldest first and mark each one delivered once the broker
    confirms it. Stops at a failure so later messages never overtake it, until that
    message has failed MAX_DELIVERY_ATTEMPTS times and is given up."""
    delivered = 0
    connection = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
    channel = connection.channel()
    channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
    channel.confirm_delivery()
    try:
        with session_factory() as session:
            # SKIP LOCKED lets several workers run without sending a row twice.
            messages = session.scalars(
                select(OutboxMessage)
                .where(
                    OutboxMessage.delivered_at.is_(None),
                    OutboxMessage.attempts < MAX_DELIVERY_ATTEMPTS,
                )
                .order_by(OutboxMessage.occurred_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for message in messages:
                try:
                    channel.basic_publish(
                        exchange=EXCHANGE,
                        routing_key=message.routing_key,
                        body=json.dumps(message.payload),
                        properties=pika.BasicProperties(
                            content_type="application/json",
                            delivery_mode=pika.DeliveryMode.Persistent,
                            message_id=message.message_id,
                            type=message.message_type,
                            correlation_id=message.correlation_id,
                        ),
                        mandatory=True,
                    )
                except pika.exceptions.AMQPConnectionError:
                    # The broker is down, which is not this message's fault.
                    logger.exception("Outbox delivery failed: broker unavailable")
                    break
                except Exception as error:  # pylint: disable=broad-exception-caught
                    message.attempts += 1
                    message.last_error = str(error)[:1000]
                    extra = {"message_id": message.message_id, "attempts": message.attempts}
                    if message.attempts >= MAX_DELIVERY_ATTEMPTS:
                        logger.error(
                            "Outbox message given up; later messages go ahead without it",
                            extra=extra,
                            exc_info=True,
                        )
                        continue
                    logger.exception("Outbox delivery failed", extra=extra)
                    break
                message.delivered_at = datetime.now(UTC)
                message.last_error = None
                delivered += 1
            session.commit()
    finally:
        connection.close()
    return delivered
