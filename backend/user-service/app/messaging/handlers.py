"""Apply consumed messages to the users table.

OrderConfirmed adds the event to the buyer's purchased_event_ids (a comma-separated list),
unless the event was cancelled first. EventCancelled records the event in cancelled_events
and leaves users' lists alone, so notification-service can still find who to email.

Both are idempotent by their effect (adding an event already listed, or recording a
cancellation already recorded, changes nothing), so a redelivered message is harmless and
no processed-message table is needed. Each message is one transaction; the consumer acks
only after it commits.
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.messaging.envelope import EventCancelled, Message, OrderConfirmed
from app.models import CancelledEvent, User

logger = logging.getLogger(__name__)


class UnknownUser(Exception):
    """No profile has this Cognito sub yet. Retried: the profile may be created shortly."""


def purchased_events(user: User) -> list[str]:
    return [e for e in (user.purchased_event_ids or "").split(",") if e]


def _order_confirmed(session: Session, payload: OrderConfirmed) -> str:
    if session.get(CancelledEvent, payload.event_id) is not None:
        return "event_cancelled"
    # Lock the row: two orders for the same buyer must not both read the old list.
    user = session.scalar(select(User).where(User.cognito_sub == payload.user_id).with_for_update())
    if user is None:
        raise UnknownUser(payload.user_id)
    events = purchased_events(user)
    if payload.event_id in events:
        return "already_recorded"
    user.purchased_event_ids = ",".join([*events, payload.event_id])
    return "purchase_recorded"


def _event_cancelled(session: Session, payload: EventCancelled) -> str:
    if session.get(CancelledEvent, payload.event_id) is not None:
        return "already_cancelled"
    session.add(CancelledEvent(event_id=payload.event_id))
    return "cancellation_recorded"


def handle_message(session_factory: "sessionmaker[Session]", message: Message) -> str:
    """Apply one message. Returns a short outcome for logs; raises on transient failure."""
    payload = message.payload
    try:
        with session_factory() as session, session.begin():
            if isinstance(payload, OrderConfirmed):
                outcome = _order_confirmed(session, payload)
            elif isinstance(payload, EventCancelled):
                outcome = _event_cancelled(session, payload)
            else:
                outcome = "ignored"
    except IntegrityError:
        if not isinstance(payload, EventCancelled):
            raise
        outcome = "already_cancelled"  # a concurrent delivery recorded it first
    logger.info(
        "message processed",
        extra={
            "message_id": message.message_id,
            "message_type": message.message_type,
            "outcome": outcome,
            "event_id": getattr(payload, "event_id", None),
        },
    )
    return outcome
