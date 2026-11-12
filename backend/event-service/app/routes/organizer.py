from datetime import datetime
from functools import wraps
from uuid import uuid4

from flask import Blueprint, Response, g, jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..auth import require_organiser
from ..errors import ApiError, validation_error
from ..images import MAX_IMAGE_BYTES, detect_image_type
from ..models import Event, EventImage, TicketCategory
from ..outbox import add_event_cancelled_message, add_event_published_message
from ..schemas import (
    EVENT_STATUSES,
    QUEUE_JOIN_LEAD,
    CancelInput,
    EventInput,
    TicketCategoryInput,
)
from ..serializers import as_utc, organiser_event_response
from ..status import EventStatus
from ._common import open_session, paginate, parse_body, utc_now

blueprint = Blueprint("organizer", __name__, url_prefix="/api/v1/organizer/events")


def organiser_required(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        g.actor = require_organiser(request)
        return handler(*args, **kwargs)

    return wrapped


def _owned_event(session: Session, event_id: str, *, lock: bool = False) -> Event:
    # Locking the row makes concurrent changes to one event run one after another.
    statement = (
        select(Event)
        .options(selectinload(Event.ticket_categories))
        .where(Event.event_id == event_id, Event.organiser_id == g.actor.organiser_id)
    )
    if lock:
        statement = statement.with_for_update()
    event = session.scalar(statement)
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "Event not found.")
    return event


def _require_status(event: Event, status: EventStatus, action: str) -> None:
    if event.status != status:
        raise ApiError(409, "INVALID_EVENT_STATE", f"Only {status.lower()} events can be {action}.")


def _apply_details(event: Event, event_input: EventInput) -> None:
    event.title = event_input.title
    event.description = event_input.description
    event.venue = event_input.venue
    event.category = event_input.category
    event.starts_at = event_input.starts_at
    event.ticket_release_at = event_input.ticket_release_at


def _new_categories(categories: list[TicketCategoryInput]) -> list[TicketCategory]:
    return [
        TicketCategory(
            category_id=str(uuid4()),
            position=position,
            name=category.name,
            price_amount=category.price_amount,
            currency=category.currency,
            total_quantity=category.total_quantity,
        )
        for position, category in enumerate(categories)
    ]


def _flush_categories(session: Session) -> None:
    # MySQL also treats accented and unaccented names as equal, e.g. "Café" and "Cafe".
    try:
        session.flush()
    except IntegrityError as error:
        message = "Category names must be unique within the event."
        raise validation_error(message, {"ticket_categories": message}) from error


def _snapshot(session: Session, event: Event) -> dict:
    session.flush()
    session.refresh(event)
    return organiser_event_response(event)


def _publish_problems(event: Event, now: datetime) -> dict[str, str]:
    problems = {}
    if as_utc(event.ticket_release_at) < now + QUEUE_JOIN_LEAD:
        minutes = int(QUEUE_JOIN_LEAD.total_seconds() // 60)
        problems["ticket_release_at"] = f"Must be at least {minutes} minutes in the future."
    if as_utc(event.starts_at) <= now:
        problems["starts_at"] = "Must be in the future."
    return problems


@blueprint.post("")
@organiser_required
def create_event():
    event_input = parse_body(EventInput)
    with open_session() as session, session.begin():
        event = Event(
            event_id=str(uuid4()),
            organiser_id=g.actor.organiser_id,
            status=EventStatus.DRAFT,
            event_version=1,
            ticket_categories=_new_categories(event_input.ticket_categories),
        )
        _apply_details(event, event_input)
        session.add(event)
        _flush_categories(session)
        response = _snapshot(session, event)
    return jsonify(response), 201


@blueprint.get("")
@organiser_required
def list_my_events():
    statement = select(Event).where(Event.organiser_id == g.actor.organiser_id)
    status = request.args.get("status")
    if status is not None:
        if status not in EVENT_STATUSES:
            message = f"Must be one of {', '.join(EVENT_STATUSES)}."
            raise validation_error("Invalid status.", {"status": message})
        statement = statement.where(Event.status == status)
    with open_session() as session:
        order_by = (Event.created_at.desc(), Event.event_id)
        return jsonify(paginate(session, statement, order_by, organiser_event_response))


@blueprint.get("/<event_id>")
@organiser_required
def get_my_event(event_id: str):
    with open_session() as session:
        return jsonify(organiser_event_response(_owned_event(session, event_id)))


@blueprint.put("/<event_id>")
@organiser_required
def update_event(event_id: str):
    event_input = parse_body(EventInput)
    with open_session() as session, session.begin():
        event = _owned_event(session, event_id, lock=True)
        _require_status(event, EventStatus.DRAFT, "updated")
        _apply_details(event, event_input)
        event.ticket_categories.clear()
        session.flush()  # delete the old categories before their names are reused
        event.ticket_categories.extend(_new_categories(event_input.ticket_categories))
        event.event_version += 1
        _flush_categories(session)
        response = _snapshot(session, event)
    return jsonify(response)


@blueprint.post("/<event_id>/publish")
@organiser_required
def publish_event(event_id: str):
    with open_session() as session, session.begin():
        event = _owned_event(session, event_id, lock=True)
        _require_status(event, EventStatus.DRAFT, "published")
        now = utc_now()
        problems = _publish_problems(event, now)
        if problems:
            raise validation_error("The event cannot be published yet.", problems)
        event.status = EventStatus.PUBLISHED
        event.event_version += 1
        event.published_at = now
        add_event_published_message(session, event)
        response = _snapshot(session, event)
    return jsonify(response)


@blueprint.post("/<event_id>/cancel")
@organiser_required
def cancel_event(event_id: str):
    cancel_input = parse_body(CancelInput)
    with open_session() as session, session.begin():
        event = _owned_event(session, event_id, lock=True)
        _require_status(event, EventStatus.PUBLISHED, "cancelled")
        event.status = EventStatus.CANCELLED
        event.event_version += 1
        event.cancelled_at = utc_now()
        event.cancellation_reason = cancel_input.reason
        add_event_cancelled_message(session, event)
        response = _snapshot(session, event)
    return jsonify(response)


@blueprint.put("/<event_id>/image")
@organiser_required
def upload_image(event_id: str):
    limit_kb = MAX_IMAGE_BYTES // 1024
    too_large = ApiError(413, "IMAGE_TOO_LARGE", f"The image must be {limit_kb} KB or smaller.")
    if request.content_length and request.content_length > MAX_IMAGE_BYTES:
        raise too_large
    # Read one byte past the limit so a missing or wrong Content-Length can't get around it.
    data = request.stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise too_large
    if not data:
        raise validation_error("The request body must be the image file.")
    image_type = detect_image_type(data)
    if image_type is None or image_type != request.mimetype:
        raise ApiError(415, "UNSUPPORTED_IMAGE_TYPE", "The image must be a JPEG, PNG or WebP file.")
    with open_session() as session, session.begin():
        event = _owned_event(session, event_id, lock=True)
        if event.status not in (EventStatus.DRAFT, EventStatus.PUBLISHED):
            message = "Only draft or published events can have their image changed."
            raise ApiError(409, "INVALID_EVENT_STATE", message)
        image = session.get(EventImage, event_id)
        if image is None:
            session.add(EventImage(event_id=event_id, content_type=image_type, data=data))
        else:
            image.content_type = image_type
            image.data = data
        event.image_version += 1
        response = _snapshot(session, event)
    return jsonify(response)


@blueprint.get("/<event_id>/image")
@organiser_required
def get_my_image(event_id: str):
    with open_session() as session:
        _owned_event(session, event_id)
        image = session.get(EventImage, event_id)
        if image is None:
            raise ApiError(404, "IMAGE_NOT_FOUND", "This event has no image.")
        return Response(
            image.data, mimetype=image.content_type, headers={"Cache-Control": "no-store"}
        )
