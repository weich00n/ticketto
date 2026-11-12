"""UTC time helpers shared by the API, workers and Redis layer.

All domain timestamps are aware UTC datetimes. On the wire they are ISO 8601 with
a trailing Z and whole seconds (e.g. 2026-11-20T12:00:00Z); in Redis they are
integer milliseconds since the epoch so Lua scripts can compare them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MS = timedelta(milliseconds=1)


def ensure_utc(dt: datetime) -> datetime:
    """Return dt in UTC. Naive datetimes are rejected rather than guessed."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed: {dt!r}")
    return dt.astimezone(UTC)


def to_iso(dt: datetime) -> str:
    """Format as YYYY-MM-DDTHH:MM:SSZ (sub-second precision is dropped)."""
    return ensure_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 timestamp that carries an offset (Z or +hh:mm)."""
    return ensure_utc(datetime.fromisoformat(value))


def to_ms(dt: datetime) -> int:
    return (ensure_utc(dt) - EPOCH) // _ONE_MS


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)
