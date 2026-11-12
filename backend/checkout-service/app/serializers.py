"""Timestamp and amount formatting shared by every outbound JSON body.

Technical Contract section 2: ISO-8601 UTC with a Z suffix, and money as integer
minor units always paired with a currency.
"""

from datetime import UTC, datetime


def iso_utc(value: datetime) -> str:
    """Format a datetime as ISO 8601 UTC with a Z suffix, e.g. 2026-11-20T12:00:00Z."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def as_utc(value: datetime) -> datetime:
    """Coerce to UTC (naive datetimes from MySQL are already UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
