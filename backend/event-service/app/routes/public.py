from flask import Blueprint, Response, jsonify, request
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..errors import ApiError
from ..models import Event, EventImage
from ..serializers import event_response
from ..status import PUBLIC_STATUSES, EventStatus
from ._common import open_session, paginate

blueprint = Blueprint("public", __name__, url_prefix="/api/v1/events")


@blueprint.get("")
def list_events():
    statement = select(Event).where(Event.status == EventStatus.PUBLISHED)
    search = request.args.get("search", default="", type=str).strip()
    if search:
        statement = statement.where(
            func.lower(Event.title).contains(search.lower(), autoescape=True)
        )
    with open_session() as session:
        return jsonify(
            paginate(session, statement, (Event.starts_at, Event.event_id), event_response)
        )


@blueprint.get("/<event_id>")
def get_event(event_id: str):
    with open_session() as session:
        event = session.scalar(
            select(Event)
            .options(selectinload(Event.ticket_categories))
            .where(Event.event_id == event_id, Event.status.in_(PUBLIC_STATUSES))
        )
        if event is None:
            raise ApiError(404, "EVENT_NOT_FOUND", "Event not found.")
        return jsonify(event_response(event))


@blueprint.get("/<event_id>/image")
def get_event_image(event_id: str):
    with open_session() as session:
        image = session.scalar(
            select(EventImage)
            .join(Event, Event.event_id == EventImage.event_id)
            .where(Event.event_id == event_id, Event.status.in_(PUBLIC_STATUSES))
        )
        if image is None:
            raise ApiError(404, "IMAGE_NOT_FOUND", "This event has no image.")
        # The URL carries the image version, so a replaced image gets a new address.
        headers = {
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        }
        return Response(image.data, mimetype=image.content_type, headers=headers)
