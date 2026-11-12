"""Event title and start time from event-service, for events published before this service
started consuming (so their EventPublished was never seen). Best effort: never raises."""

import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .http import HttpError, get_json
from .messages import parse_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EventDetails:
    title: str
    starts_at: datetime | None


class EventClient:
    def __init__(
        self,
        base_url: str | None,
        timeout: float = 2.0,
        fetch: Callable[[str], Any] | None = None,
    ):
        self._base = base_url.rstrip("/") if base_url else None
        self._fetch = fetch or functools.partial(get_json, timeout=timeout)

    def details(self, event_id: str) -> EventDetails | None:
        if not self._base:
            return None
        try:
            body = self._fetch(f"{self._base}/api/v1/events/{event_id}")
        except (HttpError, OSError, ValueError) as error:
            logger.warning(
                "could not fetch event details", extra={"event_id": event_id, "error": repr(error)}
            )
            return None
        if not isinstance(body, dict):
            return None
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        try:
            starts_at = parse_utc(body.get("starts_at"))
        except ValueError:
            starts_at = None
        return EventDetails(title=title.strip()[:160], starts_at=starts_at)
