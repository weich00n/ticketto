import uuid
from functools import wraps
from typing import Any

from flask import Blueprint, Response, current_app, g, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .auth import check_in_caller, require_user
from .checkin import check_in, invalidated
from .errors import ApiError, order_not_found, ticket_not_found, validation_error
from .issuance import queue_ticket_issued, utcnow
from .models import (
    INVALIDATED,
    ISSUED,
    OUTBOX_PENDING,
    OUTBOX_STATUSES,
    TICKET_ISSUED,
    TICKET_STATUSES,
    VALID,
    EventState,
    OutboxMessage,
    Ticket,
    TicketOrder,
)
from .qr import QrSigner, render_png
from .serializers import as_utc, order_response, outbox_message_response, ticket_response

health = Blueprint("health", __name__)
buyer = Blueprint("buyer", __name__, url_prefix="/api/v1/tickets")
internal = Blueprint("internal", __name__, url_prefix="/internal/v1")


def _session() -> Session:
    return current_app.extensions["session_factory"]()


def _signer() -> QrSigner:
    return current_app.extensions["qr_signer"]


def _uuid(value: str | None, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError) as error:
        raise validation_error(f"Invalid {field}.", {field: "Must be a UUID."}) from error


def _int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    if not raw.isdecimal() or not raw.isascii() or not minimum <= int(raw) <= maximum:
        message = f"Must be a whole number from {minimum} to {maximum}."
        raise validation_error(f"Invalid {name}.", {name: message})
    return int(raw)


def _choice_arg(name: str, choices: tuple[str, ...], default: str | None = None) -> str | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    if raw not in choices:
        raise validation_error(f"Invalid {name}.", {name: f"Must be one of {', '.join(choices)}."})
    return raw


def _parse[M: BaseModel](model: type[M]) -> M:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise validation_error("A JSON object request body is required.")
    try:
        return model.model_validate(body)
    except ValidationError as error:
        details: dict[str, str] = {}
        for issue in error.errors():
            field = ".".join(str(part) for part in issue["loc"]) or "body"
            message = issue["msg"].removeprefix("Value error, ")
            details[field] = f"{details[field]} {message}" if field in details else message
        raise validation_error("The request is invalid.", details) from error


def _auth_mode() -> str:
    return current_app.extensions["settings"].auth_mode


def user_required(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        g.caller = require_user(request, _auth_mode())
        return handler(*args, **kwargs)

    return wrapped


def check_in_access(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        settings = current_app.extensions["settings"]
        g.caller = check_in_caller(request, settings.auth_mode, settings.check_in_groups)
        return handler(*args, **kwargs)

    return wrapped


def _ticket_rows(*conditions):
    """Tickets with the order quantity and event snapshot they are shown with."""
    return (
        select(Ticket, TicketOrder.quantity, EventState)
        .join(TicketOrder, TicketOrder.order_id == Ticket.order_id)
        .outerjoin(EventState, EventState.event_id == Ticket.event_id)
        .where(*conditions)
    )


def _latest_ticket_issued(session: Session, order_id: str) -> OutboxMessage | None:
    return session.scalars(
        select(OutboxMessage)
        .where(OutboxMessage.order_id == order_id, OutboxMessage.message_type == TICKET_ISSUED)
        .order_by(OutboxMessage.id.desc())
        .limit(1)
    ).first()


def _own_ticket(session: Session, ticket_id: str):
    statement = _ticket_rows(Ticket.ticket_id == ticket_id, Ticket.user_id == g.caller.user_id)
    row = session.execute(statement).first()
    if row is None:  # another buyer's ticket is "not found", so its existence isn't revealed
        raise ticket_not_found()
    return row


# ---------- health ----------


@health.get("/health")
def liveness():
    return jsonify({"status": "ok", "service": "ticket-service"})


@health.get("/ready")
def readiness():
    try:
        with _session() as session:
            session.execute(text("SELECT 1"))
    except Exception:  # pylint: disable=broad-exception-caught
        current_app.logger.exception("Readiness check failed")
        return jsonify({"status": "not_ready", "failed": ["database"]}), 503
    return jsonify({"status": "ready"})


# ---------- buyer ----------


@buyer.get("")
@user_required
def list_my_tickets():
    conditions: list[Any] = [Ticket.user_id == g.caller.user_id]
    if event_id := request.args.get("event_id"):
        conditions.append(Ticket.event_id == _uuid(event_id, "event_id"))
    if order_id := request.args.get("order_id"):
        conditions.append(Ticket.order_id == _uuid(order_id, "order_id"))
    if status := _choice_arg("status", TICKET_STATUSES):
        conditions.append(Ticket.status == status)
    page = _int_arg("page", 1, 1, 10_000)
    page_size = _int_arg("page_size", 20, 1, 100)

    with _session() as session:
        total = session.scalar(select(func.count(Ticket.ticket_id)).where(*conditions)) or 0
        rows = session.execute(
            _ticket_rows(*conditions)
            .order_by(Ticket.issued_at.desc(), Ticket.order_id, Ticket.ticket_number)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        items = [ticket_response(*row) for row in rows]
    return jsonify({"items": items, "page": page, "page_size": page_size, "total_items": total})


@buyer.get("/<ticket_id>")
@user_required
def get_my_ticket(ticket_id: str):
    ticket_id = _uuid(ticket_id, "ticket_id")
    with _session() as session:
        return jsonify(ticket_response(*_own_ticket(session, ticket_id)))


@buyer.get("/<ticket_id>/qr")
@user_required
def get_my_ticket_qr(ticket_id: str):
    ticket_id = _uuid(ticket_id, "ticket_id")
    scale = _int_arg("scale", 8, 2, 20)
    with _session() as session:
        ticket = _own_ticket(session, ticket_id)[0]
    if ticket.status == INVALIDATED:
        raise invalidated(ticket)
    png = render_png(
        QrSigner.payload(ticket.ticket_id, ticket.qr_key_id, ticket.qr_signature), scale
    )
    response = Response(png, mimetype="image/png")
    response.headers["Cache-Control"] = "private, no-store"
    return response


# ---------- staff ----------


class CheckInInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    qr_payload: str = Field(min_length=1, max_length=500)
    event_id: str = Field(min_length=1, max_length=64)


@buyer.post("/check-in")
@check_in_access
def check_in_ticket():
    body = _parse(CheckInInput)
    event_id = _uuid(body.event_id, "event_id")
    with _session() as session:
        result = check_in(session, _signer(), body.qr_payload, event_id, g.caller.username)
    return jsonify(result)


# ---------- internal ----------


@internal.get("/orders/<order_id>/tickets")
def get_order_tickets(order_id: str):
    order_id = _uuid(order_id, "order_id")
    with _session() as session:
        order = session.get(TicketOrder, order_id)
        if order is None:
            raise order_not_found()
        tickets = session.scalars(
            select(Ticket).where(Ticket.order_id == order_id).order_by(Ticket.ticket_number)
        ).all()
        return jsonify(
            order_response(order, list(tickets), _latest_ticket_issued(session, order_id))
        )


@internal.get("/outbox-messages")
def list_outbox_messages():
    status = _choice_arg("status", OUTBOX_STATUSES, default=OUTBOX_PENDING)
    limit = _int_arg("limit", 50, 1, 200)
    with _session() as session:
        messages = session.scalars(
            select(OutboxMessage)
            .where(OutboxMessage.status == status)
            .order_by(OutboxMessage.id.desc())
            .limit(limit)
        ).all()
        items = [outbox_message_response(m) for m in messages]
    return jsonify({"items": items, "count": len(items)})


@internal.post("/orders/<order_id>/ticket-issued/republish")
def republish_ticket_issued(order_id: str):
    """Publish the order's TicketIssued again, e.g. so notification-service resends the email.

    A new message ID, so it isn't deduplicated as the earlier one. If one is still waiting to
    be published, that one is returned and made due now instead of adding another."""
    order_id = _uuid(order_id, "order_id")
    with _session() as session, session.begin():
        order = session.get(TicketOrder, order_id, with_for_update=True)  # one republish at a time
        if order is None:
            raise order_not_found()
        valid = session.scalar(
            select(func.count(Ticket.ticket_id)).where(
                Ticket.order_id == order_id, Ticket.status == VALID
            )
        )
        if order.issuance_status != ISSUED or not valid:
            raise ApiError(
                409,
                "TICKET_ISSUED_NOT_REPUBLISHABLE",
                "This order has no valid tickets: it was rejected, refunded or cancelled, "
                "or every ticket has been used.",
                {"issuance_status": order.issuance_status, "valid_tickets": valid or 0},
            )
        now = utcnow()
        pending = session.scalars(
            select(OutboxMessage).where(
                OutboxMessage.order_id == order_id,
                OutboxMessage.message_type == TICKET_ISSUED,
                OutboxMessage.status == OUTBOX_PENDING,
            )
        ).first()
        if pending is not None:
            # Waiting to retry: try now. (If a worker is publishing it this very moment, another
            # copy may go out; it has the same message_id, so notification-service drops it.)
            if pending.next_attempt_at is None or as_utc(pending.next_attempt_at) > now:
                pending.next_attempt_at = now
            message_id = pending.message_id
        else:
            message_id = queue_ticket_issued(session, order_id, now).message_id
    return jsonify({"order_id": order_id, "message_id": message_id, "status": OUTBOX_PENDING}), 202
