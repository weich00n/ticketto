from datetime import timedelta

from sqlalchemy import func, select

from app.correlation import bound_correlation_id
from app.issuance import handle_message, prune_processed_messages, utcnow
from app.messages import parse_message
from app.models import (
    CHECKED_IN,
    INVALIDATED,
    OUTBOX_CANCELLED,
    OUTBOX_PENDING,
    VALID,
    EventState,
    OrderRefund,
    OutboxMessage,
    ProcessedMessage,
    Ticket,
    TicketOrder,
)
from tests.unit.conftest import envelope, event_published, new_id, order_payload


def _tickets(session_factory, **filters):
    with session_factory() as session:
        statement = select(Ticket).order_by(Ticket.ticket_number)
        for name, value in filters.items():
            statement = statement.where(getattr(Ticket, name) == value)
        return session.scalars(statement).all()


def _order(session_factory, order_id):
    """The order and its TicketIssued outbox row (None if it has none)."""
    with session_factory() as session:
        message = session.scalars(
            select(OutboxMessage).where(OutboxMessage.order_id == order_id)
        ).one_or_none()
        return session.get(TicketOrder, order_id), message


def test_order_confirmed_issues_one_valid_ticket_per_admission(deliver, session_factory, signer):
    payload = order_payload(quantity=3)

    assert deliver("OrderConfirmed", payload) == "issued"

    tickets = _tickets(session_factory, order_id=payload["order_id"])
    assert [t.ticket_number for t in tickets] == [1, 2, 3]
    assert len({t.ticket_id for t in tickets}) == 3
    assert all(t.status == VALID and t.user_id == payload["user_id"] for t in tickets)
    for ticket in tickets:
        code = signer.verify(
            signer.payload(ticket.ticket_id, ticket.qr_key_id, ticket.qr_signature)
        )
        assert code.ticket_id == ticket.ticket_id
    order, message = _order(session_factory, payload["order_id"])
    assert order.issuance_status == "ISSUED"
    assert (message.message_type, message.routing_key) == ("TicketIssued", "ticket.issued")
    assert message.status == OUTBOX_PENDING and message.attempts == 0
    assert message.next_attempt_at is not None


def test_redelivered_message_creates_nothing_new(deliver, session_factory):
    payload = order_payload()
    message_id = new_id()

    assert deliver("OrderConfirmed", payload, message_id) == "issued"
    assert deliver("OrderConfirmed", payload, message_id) == "duplicate_message"

    assert len(_tickets(session_factory, order_id=payload["order_id"])) == 2


def test_same_order_under_a_new_message_id_is_not_issued_twice(deliver, session_factory):
    payload = order_payload()

    assert deliver("OrderConfirmed", payload) == "issued"
    assert deliver("OrderConfirmed", payload) == "duplicate_order"

    assert len(_tickets(session_factory, order_id=payload["order_id"])) == 2
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ProcessedMessage)) == 2


def test_confirmation_after_cancellation_is_rejected(deliver, session_factory):
    payload = order_payload()
    deliver("EventCancelled", {"event_id": payload["event_id"], "event_version": 3})

    assert deliver("OrderConfirmed", payload) == "rejected_event_cancelled"

    assert _tickets(session_factory, order_id=payload["order_id"]) == []
    order, message = _order(session_factory, payload["order_id"])
    assert (order.issuance_status, order.rejection_reason) == ("REJECTED", "EVENT_CANCELLED")
    assert message is None  # no tickets, so nothing for notification-service


def test_confirmation_after_refund_is_rejected(deliver, session_factory):
    payload = order_payload()
    deliver("RefundCompleted", {"order_id": payload["order_id"], "event_id": payload["event_id"]})

    assert deliver("OrderConfirmed", payload) == "rejected_order_refunded"

    assert _tickets(session_factory, order_id=payload["order_id"]) == []


def test_cancellation_invalidates_valid_tickets_and_cancels_unpublished_messages(
    deliver, session_factory
):
    first = order_payload()
    second = order_payload(event_id=first["event_id"], quantity=1)
    other_event = order_payload()
    for payload in (first, second, other_event):
        deliver("OrderConfirmed", payload)

    assert deliver("EventCancelled", {"event_id": first["event_id"]}) == "invalidated_3"

    for payload in (first, second):
        tickets = _tickets(session_factory, order_id=payload["order_id"])
        assert {(t.status, t.invalidation_reason) for t in tickets} == {
            (INVALIDATED, "EVENT_CANCELLED")
        }
        order, message = _order(session_factory, payload["order_id"])
        assert order.issuance_status == "INVALIDATED"
        assert message.status == OUTBOX_CANCELLED
    assert {t.status for t in _tickets(session_factory, order_id=other_event["order_id"])} == {
        VALID
    }


def test_checked_in_ticket_keeps_its_record_when_event_is_cancelled(deliver, session_factory):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    with session_factory() as session, session.begin():
        ticket = session.scalars(select(Ticket).where(Ticket.ticket_number == 1)).one()
        ticket.status = CHECKED_IN

    deliver("EventCancelled", {"event_id": payload["event_id"]})

    statuses = [t.status for t in _tickets(session_factory, order_id=payload["order_id"])]
    assert statuses == [CHECKED_IN, INVALIDATED]


def test_duplicate_cancellation_is_harmless(deliver, session_factory):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    cancel = {"event_id": payload["event_id"], "reason": "Venue closure"}

    deliver("EventCancelled", cancel)
    assert deliver("EventCancelled", cancel) == "invalidated_0"

    with session_factory() as session:
        state = session.get(EventState, payload["event_id"])
        assert state.status == "CANCELLED" and state.cancellation_reason == "Venue closure"


def test_refund_invalidates_only_that_order(deliver, session_factory):
    refunded = order_payload()
    kept = order_payload(event_id=refunded["event_id"])
    deliver("OrderConfirmed", refunded)
    deliver("OrderConfirmed", kept)

    outcome = deliver(
        "RefundCompleted",
        {
            "order_id": refunded["order_id"],
            "event_id": refunded["event_id"],
            "refund_id": "re_123",
            "refund_amount": 57600,
            "currency": "SGD",
        },
    )

    assert outcome == "invalidated_2"
    refunded_tickets = _tickets(session_factory, order_id=refunded["order_id"])
    assert {(t.status, t.invalidation_reason) for t in refunded_tickets} == {
        (INVALIDATED, "ORDER_REFUNDED")
    }
    assert {t.status for t in _tickets(session_factory, order_id=kept["order_id"])} == {VALID}


def test_event_snapshot_is_cached_and_older_versions_are_ignored(deliver, session_factory):
    event_id, category_id = new_id(), new_id()

    assert (
        deliver("EventPublished", event_published(event_id, category_id, 2)) == "snapshot_updated"
    )
    stale = event_published(event_id, category_id, 1, title="Old title")
    assert deliver("EventUpdated", stale) == "stale_version"

    with session_factory() as session:
        state = session.get(EventState, event_id)
        assert state.title == "The Midnight Symphony"
        assert state.category_names == {category_id: "CAT 1"}
        assert state.event_version == 2


def test_publication_never_reopens_a_cancelled_event(deliver):
    event_id, category_id = new_id(), new_id()
    deliver("EventCancelled", {"event_id": event_id})
    deliver("EventPublished", event_published(event_id, category_id, 5))

    payload = order_payload(event_id=event_id, category_id=category_id)
    assert deliver("OrderConfirmed", payload) == "rejected_event_cancelled"


def test_missing_delivery_email_still_issues_tickets(deliver, session_factory):
    payload = order_payload()
    del payload["delivery_email"]

    assert deliver("OrderConfirmed", payload) == "issued"

    assert len(_tickets(session_factory, order_id=payload["order_id"])) == 2
    order, message = _order(session_factory, payload["order_id"])
    # Still published: notification-service may have another way to reach the buyer.
    assert order.delivery_email is None and message.status == OUTBOX_PENDING


def test_ticket_issued_carries_the_confirmations_correlation_id(session_factory, signer):
    payload = order_payload()
    body = envelope("OrderConfirmed", payload)
    message = parse_message(body)
    with bound_correlation_id("checkout-req-42"):
        handle_message(session_factory, signer, message)

    _, outbox = _order(session_factory, payload["order_id"])
    assert outbox.correlation_id == "checkout-req-42"


def test_old_processed_message_records_are_pruned(deliver, session_factory):
    payload = order_payload()
    deliver("OrderConfirmed", payload)
    deliver("OrderConfirmed", order_payload())
    with session_factory() as session, session.begin():
        oldest = session.scalars(select(ProcessedMessage)).first()
        oldest.processed_at = utcnow() - timedelta(days=31)

    assert prune_processed_messages(session_factory) == 1

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ProcessedMessage)) == 1


def test_order_without_hold_or_admission_id_is_issued(deliver, session_factory):
    payload = order_payload()
    del payload["hold_id"], payload["admission_id"]

    assert deliver("OrderConfirmed", payload) == "issued"

    order, _ = _order(session_factory, payload["order_id"])
    assert (order.hold_id, order.admission_id) == (None, None)
    assert len(_tickets(session_factory, order_id=payload["order_id"])) == 2


def test_refund_amount_from_checkout_is_recorded(deliver, session_factory):
    payload = order_payload()
    deliver("OrderConfirmed", payload)

    deliver(
        "RefundCompleted",
        {"order_id": payload["order_id"], "event_id": payload["event_id"], "amount": 17600},
    )

    with session_factory() as session:
        assert session.get(OrderRefund, payload["order_id"]).refund_amount == 17600
