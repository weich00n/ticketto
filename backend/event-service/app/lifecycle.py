from datetime import UTC, datetime

from sqlalchemy import func, update

from .models import Event
from .status import EventStatus


def complete_past_events(session_factory, now: datetime | None = None) -> int:
    """Mark published events whose start time has passed as COMPLETED."""
    now = now or datetime.now(UTC)
    with session_factory() as session, session.begin():
        result = session.execute(
            update(Event)
            .where(Event.status == EventStatus.PUBLISHED, Event.starts_at <= now)
            .values(
                status=EventStatus.COMPLETED,
                event_version=Event.event_version + 1,
                updated_at=func.now(),
            )
        )
        return result.rowcount
