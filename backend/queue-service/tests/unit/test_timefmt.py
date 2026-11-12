from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.timefmt import from_ms, parse_iso, to_iso, to_ms

pytestmark = pytest.mark.unit


def test_to_iso_matches_contract_format() -> None:
    assert to_iso(datetime(2026, 11, 20, 12, 0, tzinfo=UTC)) == "2026-11-20T12:00:00Z"


def test_to_iso_drops_subseconds_and_converts_to_utc() -> None:
    plus8 = timezone(timedelta(hours=8))
    dt = datetime(2026, 11, 20, 20, 5, 7, 999_000, tzinfo=plus8)
    assert to_iso(dt) == "2026-11-20T12:05:07Z"


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_iso(datetime(2026, 11, 20, 12, 0))
    with pytest.raises(ValueError, match="naive"):
        to_ms(datetime(2026, 11, 20, 12, 0))
    with pytest.raises(ValueError, match="naive"):
        parse_iso("2026-11-20T12:00:00")


@pytest.mark.parametrize(
    "value", ["2026-11-20T12:00:00Z", "2026-11-20T12:00:00+00:00", "2026-11-20T20:00:00+08:00"]
)
def test_parse_iso_accepts_offsets(value: str) -> None:
    assert parse_iso(value) == datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def test_ms_round_trip_is_exact() -> None:
    dt = datetime(2026, 11, 20, 12, 0, 0, 123_000, tzinfo=UTC)
    assert to_ms(dt) == 1_795_176_000_123
    assert from_ms(to_ms(dt)) == dt
    assert from_ms(0) == datetime(1970, 1, 1, tzinfo=UTC)
