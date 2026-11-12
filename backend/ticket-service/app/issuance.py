"""Applies consumed messages: issue tickets, invalidate them, cache event details.

Each message is handled in one transaction that also records its message_id, so a
redelivered message changes nothing. OrderConfirmed, EventCancelled and RefundCompleted for
the same event lock that event's `event_states` row first, so they apply one after another
and whichever commits first decides the outcome:

- cancellation first  -> the later confirmation is REJECTED and creates no tickets;
- confirmation first  -> the tickets exist and the cancellation invalidates them.

Issuing tickets also writes a pending TicketIssued to the outbox in the same transaction (the
worker publishes it; see outbox.py), and invalidating them cancels any that hasn't gone out.
"""

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .correlation import bound_correlation_id, get_correlation_id
from .messages import (
    EventCancelled,
    EventSnapshot,
    Message,
    OrderConfirmed,
    Payload,
    RefundCompleted,
)
from .models import (
    EVENT_ACTIVE,
    EVENT_CANCELLED,
    EVENT_STATE_CANCELLED,
    INVALIDATED,
    ISSUED,
    ORDER_INVALIDATED,
    ORDER_REFUNDED,
    OUTBOX_CANCELLED,
    OUTBOX_PENDING,
    REJECTED,
    TICKET_ISSUED,
    TICKET_ISSUED_ROUTING_KEY,
    VALID,
    EventState,
    OrderRefund,
    OutboxMessage,
    ProcessedMessage,
    Ticket,
    TicketOrder,
)
from .qr import QrSigner

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def handle_message(
    session_factory: "sessionmaker[Session]", signer: QrSigner, message: Message
) -> str:
    """Apply one message. Returns a short outcome for logs; raises on transient failure."""
    envelope = message.envelope
    # Envelope correlation_id, else the AMQP property (bound by the consumer), else message_id.
    inherited = get_correlation_id()
    correlation_id = envelope.correlation_id or (inherited if inherited != "-" else None)
    with bound_correlation_id(correlation_id or envelope.message_id):
        with session_factory() as session, session.begin():
            if _already_processed(session, message.message_id):
                outcome = "duplicate_message"
            else:
                handler = _HANDLERS[type(message.payload)]
                outcome = handler(session, signer, message.payload, message)
                session.add(
                    ProcessedMessage(
                        message_id=message.message_id,
                        message_type=message.message_type,
                        outcome=outcome[:32],
                        processed_at=utcnow(),
                    )
                )
        logger.info(
            "message processed",
            extra={
                "message_id": message.message_id,
                "message_type": message.message_type,
                "outcome": outcome,
                "event_id": getattr(message.payload, "event_id", None),
                "order_id": getattr(message.payload, "order_id", None),
            },
        )
        return outcome


def prune_processed_messages(
    session_factory: "sessionmaker[Session]", older_than: timedelta = timedelta(days=30)
) -> int:
    """Message-ID records only guard against redelivery, which happens within minutes.
    Order and ticket uniqueness still prevent duplicates after a record is pruned."""
    with session_factory() as session, session.begin():
        result = session.execute(
            delete(ProcessedMessage).where(ProcessedMessage.processed_at < utcnow() - older_than)
        )
        return result.rowcount or 0


def _already_processed(session: Session, message_id: str) -> bool:
    # A concurrent duplicate that slips past this check fails on the primary key at commit,
    # is redelivered, and is then seen here.
    return session.get(ProcessedMessage, message_id) is not None


def lock_event(session: Session, event_id: str) -> EventState:
    """Lock (creating if needed) the event's row; serialises changes for one event."""
    statement = select(EventState).where(EventState.event_id == event_id).with_for_update()
    state = session.scalar(statement)
    if state is not None:
        return state
    try:
        with session.begin_nested():
            session.add(EventState(event_id=event_id, status=EVENT_ACTIVE))
    except IntegrityError:
        pass  # another transaction created it first; lock theirs below
    state = session.scalar(statement.execution_options(populate_existing=True))
    assert state is not None
    return state


def _invalidate_tickets(session: Session, condition, reason: str, now: datetime) -> int:
    # Conditional on VALID: a ticket already checked in keeps its record of entry, and one
    # already invalidated keeps its first reason. Check-in uses the same condition.
    result = session.execute(
        update(Ticket)
        .where(condition, Ticket.status == VALID)
        .values(status=INVALIDATED, invalidated_at=now, invalidation_reason=reason)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount or 0


def queue_ticket_issued(
    session: Session, order_id: str, now: datetime, correlation_id: str | None = None
) -> OutboxMessage:
    """Write a pending TicketIssued for the order; the worker publishes it (outbox.py).

    The message ID is fixed here, so a publish that is retried after the broker already took
    it reaches notification-service with the same ID and is deduplicated there."""
    if correlation_id is None:
        inherited = get_correlation_id()
        correlation_id = inherited if inherited != "-" else None
    message = OutboxMessage(
        message_id=str(uuid.uuid4()),
        message_type=TICKET_ISSUED,
        routing_key=TICKET_ISSUED_ROUTING_KEY,
        order_id=order_id,
        correlation_id=correlation_id,
        status=OUTBOX_PENDING,
        attempts=0,
        next_attempt_at=now,
        created_at=now,
    )
    session.add(message)
    return message


def _cancel_unpublished(session: Session, order_ids) -> None:
    # Only messages not yet published: one already confirmed by the broker stays PUBLISHED.
    session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.order_id.in_(order_ids), OutboxMessage.status == OUTBOX_PENDING)
        .values(status=OUTBOX_CANCELLED, next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )


def _order_confirmed(session: Session, signer: QrSigner, p: Payload, message: Message) -> str:
    assert isinstance(p, OrderConfirmed)
    state = lock_event(session, p.event_id)
    if session.get(TicketOrder, p.order_id) is not None:
        return "duplicate_order"

    now = utcnow()
    order = TicketOrder(
        order_id=p.order_id,
        user_id=p.user_id,
        event_id=p.event_id,
        category_id=p.category_id,
        hold_id=p.hold_id,
        admission_id=p.admission_id,
        quantity=p.quantity,
        delivery_email=p.delivery_email,
        issuance_status=ISSUED,
        confirmed_at=p.confirmed_at,
        source_message_id=message.message_id,
        received_at=now,
    )
    if state.status == EVENT_STATE_CANCELLED:
        order.issuance_status, order.rejection_reason = REJECTED, EVENT_CANCELLED
    elif session.get(OrderRefund, p.order_id) is not None:
        order.issuance_status, order.rejection_reason = REJECTED, ORDER_REFUNDED
    session.add(order)
    if order.issuance_status == REJECTED:
        logger.warning(
            "confirmation arrived after cancellation or refund; no tickets issued",
            extra={"order_id": p.order_id, "reason": order.rejection_reason},
        )
        return f"rejected_{order.rejection_reason.lower()}"

    session.flush()  # the order row must exist before tickets reference it
    for number in range(1, p.quantity + 1):
        ticket_id = str(uuid.uuid4())
        key_id, signature = signer.sign(ticket_id)
        session.add(
            Ticket(
                ticket_id=ticket_id,
                order_id=p.order_id,
                ticket_number=number,
                user_id=p.user_id,
                event_id=p.event_id,
                category_id=p.category_id,
                status=VALID,
                qr_key_id=key_id,
                qr_signature=signature,
                issued_at=now,
            )
        )
    queue_ticket_issued(session, p.order_id, now)
    if not p.delivery_email:
        logger.warning(
            "no valid delivery email; TicketIssued will carry delivery_email null",
            extra={"order_id": p.order_id},
        )
    return "issued"


def _refund_completed(session: Session, _signer: QrSigner, p: Payload, _message: Message) -> str:
    assert isinstance(p, RefundCompleted)
    lock_event(session, p.event_id)
    now = utcnow()
    if session.get(OrderRefund, p.order_id) is None:
        session.add(
            OrderRefund(
                order_id=p.order_id,
                event_id=p.event_id,
                refund_id=p.refund_id,
                refund_amount=p.refund_amount,
                currency=p.currency,
                completed_at=p.completed_at,
                received_at=now,
            )
        )
    order = session.get(TicketOrder, p.order_id)
    if order is None:
        return "refund_before_order"
    if order.event_id != p.event_id:
        logger.error(
            "RefundCompleted event_id does not match the order; invalidating anyway",
            extra={"order_id": p.order_id, "order_event_id": order.event_id},
        )
    invalidated = _invalidate_tickets(session, Ticket.order_id == p.order_id, ORDER_REFUNDED, now)
    if order.issuance_status == ISSUED:
        order.issuance_status = ORDER_INVALIDATED
    _cancel_unpublished(session, [p.order_id])
    return f"invalidated_{invalidated}"


def _event_cancelled(session: Session, _signer: QrSigner, p: Payload, _message: Message) -> str:
    assert isinstance(p, EventCancelled)
    state = lock_event(session, p.event_id)
    now = utcnow()
    if state.status != EVENT_STATE_CANCELLED:
        state.status = EVENT_STATE_CANCELLED
        state.cancelled_at = p.cancelled_at or now
        state.cancellation_id = p.cancellation_id
        state.cancellation_reason = p.reason
    if p.event_version and (state.event_version is None or p.event_version > state.event_version):
        state.event_version = p.event_version
    invalidated = _invalidate_tickets(session, Ticket.event_id == p.event_id, EVENT_CANCELLED, now)
    session.execute(
        update(TicketOrder)
        .where(TicketOrder.event_id == p.event_id, TicketOrder.issuance_status == ISSUED)
        .values(issuance_status=ORDER_INVALIDATED)
        .execution_options(synchronize_session=False)
    )
    _cancel_unpublished(
        session, select(TicketOrder.order_id).where(TicketOrder.event_id == p.event_id)
    )
    return f"invalidated_{invalidated}"


def _event_snapshot(session: Session, _signer: QrSigner, p: Payload, _message: Message) -> str:
    assert isinstance(p, EventSnapshot)
    state = lock_event(session, p.event_id)
    if (
        p.event_version is not None
        and state.event_version is not None
        and p.event_version <= state.event_version
    ):
        return "stale_version"
    apply_event_details(
        state,
        title=p.title,
        venue=p.venue,
        starts_at=p.starts_at,
        category_names={c.category_id: c.name for c in p.ticket_categories},
    )
    if p.event_version is not None:
        state.event_version = p.event_version
    # Never touches status: an old publication cannot reopen a cancelled event.
    return "snapshot_updated"


def apply_event_details(
    state: EventState,
    *,
    title: str | None,
    venue: str | None,
    starts_at: datetime | None,
    category_names: dict[str, str],
) -> None:
    if title:
        state.title = title
    if venue:
        state.venue = venue
    if starts_at:
        state.starts_at = starts_at
    if category_names:
        state.category_names = {**(state.category_names or {}), **category_names}


_HANDLERS: dict[type, Callable[[Session, QrSigner, Payload, Message], str]] = {
    OrderConfirmed: _order_confirmed,
    RefundCompleted: _refund_completed,
    EventCancelled: _event_cancelled,
    EventSnapshot: _event_snapshot,
}
