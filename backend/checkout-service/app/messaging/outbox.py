"""The transactional outbox for checkout's broker messages.

A message is stored in the same commit as the state change it represents, so a crash can
never produce "state changed, message lost" or "message sent, state unchanged". The
relay (worker) publishes each undelivered row to checkout.events with the message's
dotted routing key; a row is marked delivered only once the broker has confirmed it,
and a failure records attempts/last_error so it is retried. A message the broker keeps
refusing is given up on after MAX_ATTEMPTS, so it can never hold up the ones behind it.

The stored payload is the full broker envelope (rabbitmq-service
contracts/envelope.schema.json):
    {message_id, message_type, schema_version, occurred_at, producer, payload}
additionalProperties is false, so any extra key here would break the contract. The
correlation id is not in the body: it is sent as the AMQP correlation_id property.

Message ids are derived from (message type, aggregate), so recording the same fact twice
stores one row, and a consumer that sees it again can tell it is a duplicate.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

import pika
from pika.exceptions import AMQPError, NackError, UnroutableError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..correlation import get_correlation_id
from ..models import OutboxMessage
from ..serializers import iso_utc
from .events import ROUTING_KEYS

# checkout-service's own exchange on the shared broker (one topic exchange per publisher).
EXCHANGE = "checkout.events"
PRODUCER = "checkout-service"
SCHEMA_VERSION = 1

# Failed sends before a message is given up on: logged at ERROR and left undelivered, for
# a manual replay (reset attempts to 0). Retried once per relay cycle until then.
MAX_ATTEMPTS = 10

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def message_id_for(message_type: str, aggregate_id: str) -> str:
    """The stable id of the one message of this type about this aggregate."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{PRODUCER}:{message_type}:{aggregate_id}"))


def build_envelope(message_type: str, payload: dict, message_id: str | None = None) -> dict:
    """Build a broker envelope around a payload."""
    return {
        "message_id": message_id or str(uuid.uuid4()),
        "message_type": message_type,
        "schema_version": SCHEMA_VERSION,
        "occurred_at": iso_utc(_now()),
        "producer": PRODUCER,
        "payload": payload,
    }


class Outbox:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def write(
        self,
        db: Session,
        *,
        message_type: str,
        payload: dict,
        aggregate_id: str,
        correlation_id: str | None = None,
    ) -> OutboxMessage:
        """Stage a message. Call inside the caller's transaction and commit with it -
        the row exists iff the state change committed.

        `aggregate_id` is what the message is about (the order, or the refund for refund
        messages); a second write for the same type and aggregate returns the first row.
        """
        message_id = message_id_for(message_type, aggregate_id)
        existing = db.scalar(select(OutboxMessage).where(OutboxMessage.message_id == message_id))
        if existing is not None:
            return existing
        cid = correlation_id or _current_correlation_id() or str(uuid.uuid4())
        msg = OutboxMessage(
            message_id=message_id,
            event_type=message_type,
            routing_key=ROUTING_KEYS[message_type],
            aggregate_id=aggregate_id,
            payload=build_envelope(message_type, payload, message_id),
            correlation_id=cid,
            occurred_at=_now(),
        )
        db.add(msg)
        return msg

    def _connect(self) -> pika.BlockingConnection:
        # pika.BlockingConnection takes Parameters/URLParameters, not a raw URL
        # string (a str makes pika 1.4's connection workflow raise
        # AttributeError). Consumers wrap it the same way.
        return pika.BlockingConnection(pika.URLParameters(self.settings.rabbitmq_url))

    def deliver_pending(self, db: Session, limit: int = 100) -> int:
        """Publish undelivered outbox rows, oldest first. Returns how many were sent.

        Publisher confirms are on, so basic_publish returns only once the broker has
        stored the message, and raises if it was nacked or (mandatory) unroutable. Only
        then is the row marked delivered. The exchange is not declared: the broker owns it.

        A message that fails on its own - nacked, unroutable, unbuildable - counts an
        attempt and is skipped, so the messages behind it still go; later messages about
        the same aggregate wait behind it, to keep their order. After MAX_ATTEMPTS it is
        given up on. A broker or connection failure counts no attempt, since every message
        would fail alike and an outage must not use them up: it ends the cycle, and the
        next one retries.
        """
        # `db.scalars()` executes the Select, so .order_by()/.limit() must be
        # applied to the Select first (a ScalarResult has no such methods).
        rows = db.scalars(
            select(OutboxMessage)
            .filter(OutboxMessage.delivered_at.is_(None))
            .filter(OutboxMessage.attempts < MAX_ATTEMPTS)
            .order_by(OutboxMessage.id)
            .limit(limit)
        ).all()
        if not rows:
            return 0
        connection = self._connect()
        channel = connection.channel()
        channel.confirm_delivery()
        sent = 0
        # Aggregates with an earlier message unsent in this cycle.
        held: set[str] = set()
        try:
            for msg in rows:
                if msg.aggregate_id in held:
                    continue
                try:
                    self._publish(channel, msg)
                except (NackError, UnroutableError) as exc:
                    _record_refusal(db, msg, exc)
                    held.add(msg.aggregate_id)
                    continue
                except AMQPError as exc:
                    msg.last_error = _describe(exc)
                    db.commit()
                    log.warning(
                        "outbox: broker unavailable, retrying next cycle",
                        extra={"message_id": msg.message_id, "error": msg.last_error},
                    )
                    break
                # Anything else is this message's own fault (e.g. an unserialisable payload).
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    _record_refusal(db, msg, exc)
                    held.add(msg.aggregate_id)
                    continue
                msg.delivered_at = _now()
                msg.attempts = msg.attempts + 1
                db.commit()
                sent += 1
        finally:
            connection.close()
        return sent

    @staticmethod
    def _publish(channel, msg: OutboxMessage) -> None:
        body = str.encode(_dump(msg.payload))
        props = pika.BasicProperties(
            message_id=msg.message_id,
            type=msg.event_type,
            correlation_id=msg.correlation_id or msg.message_id,
            content_type="application/json",
            delivery_mode=pika.DeliveryMode.Persistent,
        )
        channel.basic_publish(
            exchange=EXCHANGE,
            routing_key=msg.routing_key,
            body=body,
            properties=props,
            mandatory=True,
        )


def _record_refusal(db: Session, msg: OutboxMessage, exc: Exception) -> None:
    """Count a failed send of this one message; on the last allowed attempt, give up."""
    msg.attempts = msg.attempts + 1
    msg.last_error = _describe(exc)
    db.commit()
    extra = {
        "message_id": msg.message_id,
        "message_type": msg.event_type,
        "aggregate_id": msg.aggregate_id,
        "attempts": msg.attempts,
        "error": msg.last_error,
    }
    if msg.attempts >= MAX_ATTEMPTS:
        log.error(
            "outbox: giving up on message after %d failed attempts", msg.attempts, extra=extra
        )
    else:
        log.warning("outbox: message refused, will retry", extra=extra)


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _current_correlation_id() -> str | None:
    # The context var defaults to "-" outside a request or message.
    cid = get_correlation_id()
    return cid if cid and cid != "-" else None


def _dump(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), default=str)
