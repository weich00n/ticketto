from flask import Blueprint, jsonify
from sqlalchemy import select

from ..errors import ApiError
from ..models import Event, TicketCategory
from ..serializers import category_response
from ..status import EventStatus
from ._common import open_session

blueprint = Blueprint("internal", __name__, url_prefix="/internal/v1/events")


@blueprint.get("/<event_id>/categories/<category_id>")
def get_category(event_id: str, category_id: str):
    with open_session() as session:
        row = session.execute(
            select(Event.status, TicketCategory)
            .join(TicketCategory, TicketCategory.event_id == Event.event_id)
            .where(
                Event.event_id == event_id,
                Event.status != EventStatus.DRAFT,
                TicketCategory.category_id == category_id,
            )
        ).first()
        if row is None:
            raise ApiError(404, "EVENT_NOT_FOUND", "Event or category not found.")
        status, category = row
        return jsonify({"event_id": event_id, "event_status": status} | category_response(category))
