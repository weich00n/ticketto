"""Venue check-in: verify the signed QR payload, then VALID -> CHECKED_IN exactly once."""

import hmac
import logging
import uuid
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .errors import ApiError, invalid_qr_code, ticket_not_found
from .issuance import utcnow
from .models import CHECKED_IN, VALID, CheckIn, EventState, Ticket, TicketOrder
from .qr import InvalidQrCode, QrSigner
from .serializers import iso_utc

logger = logging.getLogger(__name__)


def already_checked_in(ticket: Ticket) -> ApiError:
    return ApiError(
        409,
        "TICKET_ALREADY_CHECKED_IN",
        "This ticket has already been used.",
        {"checked_in_at": iso_utc(ticket.checked_in_at)},
    )


def invalidated(ticket: Ticket) -> ApiError:
    return ApiError(
        409,
        "TICKET_INVALIDATED",
        "This ticket is no longer valid.",
        {"invalidation_reason": ticket.invalidation_reason},
    )


def check_in(
    session: Session, signer: QrSigner, qr_payload: str, event_id: str, staff: str | None
) -> dict[str, Any]:
    try:
        code = signer.verify(qr_payload)
    except InvalidQrCode as error:
        logger.warning("check-in rejected: invalid QR code", extra={"reason": str(error)})
        raise invalid_qr_code() from error

    ticket = session.get(Ticket, code.ticket_id)
    if ticket is None:
        raise ticket_not_found()
    # The payload must be the one issued for this ticket, not merely signed by a known key.
    if ticket.qr_key_id != code.key_id or not hmac.compare_digest(
        ticket.qr_signature, code.signature
    ):
        raise invalid_qr_code()
    if ticket.event_id != event_id:
        raise ApiError(
            409,
            "TICKET_EVENT_MISMATCH",
            "This ticket is for a different event.",
            {"ticket_event_id": ticket.event_id},
        )
    if ticket.status == CHECKED_IN:
        raise already_checked_in(ticket)
    if ticket.status != VALID:
        raise invalidated(ticket)

    now = utcnow()
    # Atomic: of any number of concurrent scans, and of a scan racing a cancellation or
    # refund, only one statement can change the row away from VALID.
    result = session.execute(
        update(Ticket)
        .where(Ticket.ticket_id == ticket.ticket_id, Ticket.status == VALID)
        .values(status=CHECKED_IN, checked_in_at=now, checked_in_by=staff)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        session.rollback()
        raise _current_conflict(session, ticket.ticket_id)
    try:
        session.add(
            CheckIn(
                check_in_id=str(uuid.uuid4()),
                ticket_id=ticket.ticket_id,
                event_id=ticket.event_id,
                checked_in_at=now,
                checked_in_by=staff,
            )
        )
        session.commit()
    except IntegrityError as error:  # second guard: one check-in row per ticket
        session.rollback()
        raise _current_conflict(session, ticket.ticket_id) from error

    order = session.get(TicketOrder, ticket.order_id)
    state = session.get(EventState, ticket.event_id)
    logger.info(
        "ticket checked in",
        extra={"ticket_id": ticket.ticket_id, "event_id": ticket.event_id, "staff": staff},
    )
    return {
        "ticket_id": ticket.ticket_id,
        "event_id": ticket.event_id,
        "category_id": ticket.category_id,
        "category_name": ((state.category_names or {}) if state else {}).get(ticket.category_id),
        "ticket_number": ticket.ticket_number,
        "tickets_in_order": order.quantity if order else None,
        "status": CHECKED_IN,
        "checked_in_at": iso_utc(now),
    }


def _current_conflict(session: Session, ticket_id: str) -> ApiError:
    ticket = session.get(Ticket, ticket_id, populate_existing=True)
    if ticket is None:
        return ticket_not_found()
    if ticket.status == CHECKED_IN:
        return already_checked_in(ticket)
    return invalidated(ticket)
