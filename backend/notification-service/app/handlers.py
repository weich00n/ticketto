"""Turn consumed messages into queued emails (rows in `notifications`).

Each handler asks user-service who to email *before* opening its transaction, then writes
the emails, the event state and the message's processed_messages row in one transaction.
The consumer acks only after that commits, so a crash or a user-service outage leaves the
message on its queue to be tried again, and a redelivered message changes nothing.

  EventPublished  one email per user with new-event emails on, unless the event was
                  cancelled (the cancellation may arrive first) or already announced
  EventCancelled  one email per buyer; new-event emails not sent yet are cancelled
  OrderRefunded   one email to the buyer, looked up by Cognito sub
  RefundFailed    the same, saying the refund didn't go through
  TicketIssued    one email with the order's QR codes, to the order's delivery email, unless
                  the event was cancelled or the order refunded. A pending one is cancelled
                  if either happens later

`dedupe_key` is unique, so the same person never gets the same email twice, even when a
publisher sends the same fact again under a new message_id. TicketIssued is the exception:
ticket-service's republish endpoint uses a new message_id to send a buyer their tickets again.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .correlation import bound_correlation_id
from .email_delivery import utcnow, without_qr_codes
from .messages import (
    EventCancelled,
    EventPublished,
    Message,
    OrderRefunded,
    RefundFailed,
    TicketIssued,
)
from .models import (
    KIND_EVENT_CANCELLED,
    KIND_EVENT_PUBLISHED,
    KIND_ORDER_REFUNDED,
    KIND_REFUND_FAILED,
    KIND_TICKET_ISSUED,
    STATUS_CANCELLED,
    STATUS_PENDING,
    Event,
    Notification,
    ProcessedMessage,
)
from .user_client import Recipient, UserNotFound, UserServiceUnavailable

logger = logging.getLogger(__name__)

FALLBACK_TITLE = "your event"
_KEY_CHUNK = 500


def iso_z(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def format_money(amount: int, currency: str) -> str:
    """Integer minor units: 8800 -> 'SGD 88.00'."""
    return f"{currency} {amount // 100:,}.{amount % 100:02d}"


def lock_event(session: Session, event_id: str) -> Event:
    """Lock (creating if needed) the event's row; serialises changes for one event."""
    statement = select(Event).where(Event.event_id == event_id).with_for_update()
    event = session.scalar(statement)
    if event is not None:
        return event
    try:
        with session.begin_nested():
            session.add(Event(event_id=event_id))
    except IntegrityError:
        pass  # another transaction created it first; lock theirs below
    event = session.scalar(statement.execution_options(populate_existing=True))
    assert event is not None
    return event


def _existing_keys(session: Session, keys: list[str]) -> set[str]:
    existing: set[str] = set()
    for start in range(0, len(keys), _KEY_CHUNK):
        chunk = keys[start : start + _KEY_CHUNK]
        existing.update(
            session.scalars(
                select(Notification.dedupe_key).where(Notification.dedupe_key.in_(chunk))
            )
        )
    return existing


def _queue(  # pylint: disable=too-many-arguments  # keyword-only, one per column group
    session: Session,
    *,
    kind: str,
    message: Message,
    event_id: str | None,
    recipients: Iterable[Recipient],
    context: dict,
    dedupe_key: Callable[[Recipient], str],
    now: datetime,
) -> int:
    """Add one PENDING email per recipient whose dedupe_key isn't queued yet."""
    wanted: dict[str, Recipient] = {}
    for recipient in recipients:
        wanted.setdefault(dedupe_key(recipient), recipient)
    existing = _existing_keys(session, list(wanted))
    added = 0
    for key, recipient in wanted.items():
        if key in existing:
            continue
        session.add(
            Notification(
                kind=kind,
                dedupe_key=key,
                source_message_id=message.message_id,
                event_id=event_id,
                user_id=recipient.user_id,
                cognito_sub=recipient.cognito_sub.lower() if recipient.cognito_sub else None,
                recipient=recipient.email,
                context={**context, "name": recipient.name},
                status=STATUS_PENDING,
                attempts=0,
                next_attempt_at=now,
            )
        )
        added += 1
    return added


def _record(session: Session, message: Message, outcome: str, now: datetime) -> None:
    session.add(
        ProcessedMessage(
            message_id=message.message_id,
            message_type=message.message_type,
            outcome=outcome[:32],
            processed_at=now,
        )
    )


def _event_key(kind: str, event_id: str) -> Callable[[Recipient], str]:
    return lambda r: f"{kind}:{event_id}:{r.user_id}"


def _cancel_ticket_emails(session: Session, event_id: str, order_id: str | None = None) -> None:
    """Cancel the event's pending ticket emails, or just one order's: those QR codes are no
    longer any use, and the row has no more need of them."""
    rows = session.scalars(
        select(Notification).where(
            Notification.event_id == event_id,
            Notification.kind == KIND_TICKET_ISSUED,
            Notification.status == STATUS_PENDING,
        )
    )
    for row in rows:
        if order_id is None or row.context.get("order_id") == order_id:
            row.status, row.next_attempt_at = STATUS_CANCELLED, None
            row.context = without_qr_codes(row.context)


# ---------- EventPublished ----------


def _needs_announcement(session_factory: "sessionmaker[Session]", event_id: str) -> bool:
    with session_factory() as session:
        event = session.get(Event, event_id)
        return event is None or (event.cancelled_at is None and event.announced_at is None)


def _event_published(session_factory, users, _events, message: Message) -> str:
    p = message.payload
    assert isinstance(p, EventPublished)
    # Cancellation and announcement only ever move forward, so a "no" here stays a "no".
    recipients = users.opted_in() if _needs_announcement(session_factory, p.event_id) else None
    now = utcnow()
    with session_factory() as session, session.begin():
        event = lock_event(session, p.event_id)
        event.title, event.starts_at = p.title, p.starts_at
        if event.cancelled_at is not None:
            outcome = "event_cancelled"
        elif event.announced_at is not None:
            outcome = "already_announced"
        else:
            if recipients is None:  # the state went backwards: retry rather than skip
                raise RuntimeError(f"event {p.event_id} lost its announced/cancelled state")
            event.announced_at = now
            context = {
                "title": p.title,
                "starts_at": iso_z(p.starts_at),
                "release_at": iso_z(p.release_at),
                "queue_join_opens_at": iso_z(p.queue_join_opens_at),
            }
            added = _queue(
                session,
                kind=KIND_EVENT_PUBLISHED,
                message=message,
                event_id=p.event_id,
                recipients=recipients,
                context=context,
                dedupe_key=_event_key(KIND_EVENT_PUBLISHED, p.event_id),
                now=now,
            )
            outcome = f"queued_{added}"
        _record(session, message, outcome, now)
    return outcome


# ---------- EventCancelled ----------


def _cached_event(session_factory, event_id: str) -> Event | None:
    with session_factory() as session:
        return session.get(Event, event_id)


def _event_cancelled(session_factory, users, events, message: Message) -> str:
    p = message.payload
    assert isinstance(p, EventCancelled)
    cached = _cached_event(session_factory, p.event_id)
    details = events.details(p.event_id) if cached is None or not cached.title else None
    recipients = users.buyers(p.event_id)
    now = utcnow()
    with session_factory() as session, session.begin():
        event = lock_event(session, p.event_id)
        if details is not None and not event.title:
            event.title, event.starts_at = details.title, details.starts_at
        if event.cancelled_at is None:
            event.cancelled_at = p.cancelled_at or now
        # Nobody should be told about an event that is already off.
        session.execute(
            update(Notification)
            .where(
                Notification.event_id == p.event_id,
                Notification.kind == KIND_EVENT_PUBLISHED,
                Notification.status == STATUS_PENDING,
            )
            .values(status=STATUS_CANCELLED, next_attempt_at=None)
            .execution_options(synchronize_session=False)
        )
        _cancel_ticket_emails(session, p.event_id)
        context = {
            "title": event.title or FALLBACK_TITLE,
            "starts_at": iso_z(event.starts_at),
            "reason": p.reason,
        }
        added = _queue(
            session,
            kind=KIND_EVENT_CANCELLED,
            message=message,
            event_id=p.event_id,
            recipients=recipients,
            context=context,
            dedupe_key=_event_key(KIND_EVENT_CANCELLED, p.event_id),
            now=now,
        )
        outcome = f"queued_{added}"
        _record(session, message, outcome, now)
    return outcome


# ---------- OrderRefunded and RefundFailed ----------


def _save_details(session: Session, event_id: str, details) -> str | None:
    """Keep event-service's title for later emails; returns the event's title."""
    event = lock_event(session, event_id)
    if not event.title:
        event.title, event.starts_at = details.title, details.starts_at
    return event.title


def _queue_refund_email(  # pylint: disable=too-many-arguments  # the handlers' plus the kind
    session_factory, users, events, message: Message, *, kind: str
) -> str:
    p = message.payload
    assert isinstance(p, OrderRefunded)
    recipient = users.by_sub(p.user_id)  # UserNotFound / UserServiceUnavailable: retried
    # by-sub doesn't return the sub; the message's user_id is it (already lower case).
    recipient = replace(recipient, cognito_sub=p.user_id)
    cached = _cached_event(session_factory, p.event_id)
    details = events.details(p.event_id) if cached is None or not cached.title else None
    now = utcnow()
    with session_factory() as session, session.begin():
        title = cached.title if cached is not None else None
        if details is not None:
            title = _save_details(session, p.event_id, details)
        if kind == KIND_ORDER_REFUNDED:
            _cancel_ticket_emails(session, p.event_id, p.order_id)
        context = {
            "title": title or FALLBACK_TITLE,
            "amount": format_money(p.amount, p.currency),
            "reason": p.reason,
            "order_id": p.order_id,
        }
        added = _queue(
            session,
            kind=kind,
            message=message,
            event_id=p.event_id,
            recipients=[recipient],
            context=context,
            dedupe_key=lambda _r: f"{kind}:{p.refund_id or p.order_id}",
            now=now,
        )
        outcome = f"queued_{added}"
        _record(session, message, outcome, now)
    return outcome


def _order_refunded(session_factory, users, events, message: Message) -> str:
    return _queue_refund_email(session_factory, users, events, message, kind=KIND_ORDER_REFUNDED)


def _refund_failed(session_factory, users, events, message: Message) -> str:
    p = message.payload
    assert isinstance(p, RefundFailed)
    outcome = _queue_refund_email(session_factory, users, events, message, kind=KIND_REFUND_FAILED)
    # checkout-service won't retry, so this needs a person. The reason is in its refund row.
    logger.error("refund failed", extra={"refund_id": p.refund_id, "order_id": p.order_id})
    return outcome


# ---------- TicketIssued ----------


def _ticket_recipient(users, p: TicketIssued) -> Recipient:
    """With a delivery_email, user-service only adds the name on the ticket, so its outage or
    a missing profile can't hold up tickets. Without one, both are retried."""
    try:
        profile = users.by_sub(p.user_id)
    except (UserNotFound, UserServiceUnavailable):
        if not p.delivery_email:
            raise
        return Recipient(user_id=p.user_id, email=p.delivery_email, name="", cognito_sub=p.user_id)
    # by-sub doesn't return the sub.
    return replace(profile, email=p.delivery_email or profile.email, cognito_sub=p.user_id)


def _order_was_refunded(session: Session, event_id: str, order_id: str) -> bool:
    contexts = session.scalars(
        select(Notification.context).where(
            Notification.event_id == event_id, Notification.kind == KIND_ORDER_REFUNDED
        )
    )
    return any(context.get("order_id") == order_id for context in contexts)


def _ticket_context(p: TicketIssued, event: Event) -> dict:
    return {
        "title": p.title or event.title or FALLBACK_TITLE,
        "venue": p.venue,
        "starts_at": iso_z(p.starts_at or event.starts_at),
        "category_name": p.category_name,
        "order_id": p.order_id,
        "tickets": [ticket.model_dump() for ticket in p.tickets],
    }


def _ticket_issued(session_factory, users, events, message: Message) -> str:
    p = message.payload
    assert isinstance(p, TicketIssued)
    recipient = _ticket_recipient(users, p)
    cached = _cached_event(session_factory, p.event_id)
    title_known = p.title or (cached is not None and cached.title)
    details = None if title_known else events.details(p.event_id)
    now = utcnow()
    with session_factory() as session, session.begin():
        event = lock_event(session, p.event_id)  # a cancellation can't slip in after this check
        if not event.title and (p.title or details is not None):
            source = p if p.title else details
            event.title, event.starts_at = source.title, source.starts_at
        if event.cancelled_at is not None:
            outcome = "event_cancelled"
        elif _order_was_refunded(session, p.event_id, p.order_id):
            outcome = "order_refunded"
        else:
            added = _queue(
                session,
                kind=KIND_TICKET_ISSUED,
                message=message,
                event_id=p.event_id,
                recipients=[recipient],
                context=_ticket_context(p, event),
                dedupe_key=lambda _r: f"{KIND_TICKET_ISSUED}:{message.message_id}",
                now=now,
            )
            outcome = f"queued_{added}"
        _record(session, message, outcome, now)
    return outcome


_HANDLERS = {
    EventPublished: _event_published,
    EventCancelled: _event_cancelled,
    OrderRefunded: _order_refunded,
    RefundFailed: _refund_failed,
    TicketIssued: _ticket_issued,
}


def _already_processed(session_factory, message_id: str) -> bool:
    with session_factory() as session:
        return session.get(ProcessedMessage, message_id) is not None


def handle_message(
    session_factory: "sessionmaker[Session]", users, events, message: Message
) -> str:
    """Apply one message. Returns a short outcome for logs; raises on transient failure."""
    with bound_correlation_id(message.correlation_id or message.message_id):
        if message.payload is None:
            outcome = "ignored"
        elif _already_processed(session_factory, message.message_id):
            outcome = "duplicate_message"
        else:
            outcome = _HANDLERS[type(message.payload)](session_factory, users, events, message)
        logger.info(
            "message processed",
            extra={
                "message_id": message.message_id,
                "message_type": message.message_type,
                "outcome": outcome,
                "event_id": getattr(message.payload, "event_id", None),
            },
        )
        return outcome


def prune_processed_messages(
    session_factory: "sessionmaker[Session]",
    older_than: timedelta = timedelta(days=30),
    limit: int = 1000,
) -> int:
    """Message-ID records only guard against redelivery, which happens within minutes; the
    unique dedupe_key still stops a repeat email after a record is pruned."""
    cutoff = utcnow() - older_than
    with session_factory() as session, session.begin():
        ids = list(
            session.scalars(
                select(ProcessedMessage.message_id)
                .where(ProcessedMessage.processed_at < cutoff)
                .limit(limit)
            )
        )
        if ids:
            session.execute(delete(ProcessedMessage).where(ProcessedMessage.message_id.in_(ids)))
        return len(ids)
