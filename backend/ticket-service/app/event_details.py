"""Fills in event details missing from messages by asking Event Service once.

EventPublished does not currently carry `venue`, and Ticket may have started after an event
was published. Before publishing TicketIssued, the worker fetches the public event from Event
Service's private address and caches what was missing. Best effort: if Event Service is
unreachable, the message still goes out with whatever details are known.
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from .correlation import get_correlation_id
from .issuance import apply_event_details, lock_event
from .messages import parse_utc
from .models import EventState

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 2.0


def fetch_event(base_url: str, event_id: str) -> dict | None:
    request = urllib.request.Request(
        f"{base_url}/api/v1/events/{event_id}",
        headers={"Accept": "application/json", "X-Correlation-Id": get_correlation_id()},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            body = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        logger.warning(
            "could not fetch event details", extra={"event_id": event_id, "error": repr(error)}
        )
        return None
    return body if isinstance(body, dict) else None


def ensure_event_details(
    session_factory: "sessionmaker[Session]", base_url: str | None, event_id: str
) -> None:
    if not base_url:
        return
    with session_factory() as session:
        state = session.get(EventState, event_id)
        if state is not None and state.title and state.venue and state.starts_at:
            return
    event = fetch_event(base_url, event_id)
    if not event:
        return
    categories = {
        str(c.get("category_id")): str(c.get("name"))[:80]
        for c in event.get("ticket_categories") or []
        if isinstance(c, dict) and c.get("category_id") and c.get("name")
    }
    starts_at: datetime | None = parse_utc(event.get("starts_at"))
    with session_factory() as session, session.begin():
        state = lock_event(session, event_id)
        apply_event_details(
            state,
            title=(str(event["title"])[:160] if event.get("title") else None),
            venue=(str(event["venue"])[:200] if event.get("venue") else None),
            starts_at=starts_at,
            category_names=categories,
        )
