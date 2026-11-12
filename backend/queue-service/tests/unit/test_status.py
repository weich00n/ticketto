from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.core.status import (
    CloseReason,
    EntryStatus,
    QueueStatus,
    active_pool_capacity,
    close_reason,
    effective_status,
    poll_after_seconds,
)

pytestmark = pytest.mark.unit

RELEASE = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
OPENS = RELEASE - timedelta(minutes=15)


@dataclass
class Cfg:
    status: QueueStatus
    queue_opens_at: datetime = OPENS
    release_at: datetime = RELEASE
    sale_ends_at: datetime | None = None


class FixedRng:
    def __init__(self, value: int) -> None:
        self.value = value

    def randint(self, a: int, b: int) -> int:
        assert (a, b) == (0, 5)
        return self.value


@pytest.mark.parametrize(
    ("total", "expected"),
    [(304, 60), (3, 1), (0, 1), (4, 1), (5, 1), (9, 1), (10, 2), (1000, 200)],
)
def test_active_pool_capacity(total: int, expected: int) -> None:
    assert active_pool_capacity(total) == expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (OPENS - timedelta(milliseconds=1), QueueStatus.SCHEDULED),
        (OPENS, QueueStatus.OPEN),
        (RELEASE - timedelta(milliseconds=1), QueueStatus.OPEN),
        (RELEASE, QueueStatus.ADMITTING),
        (RELEASE + timedelta(days=1), QueueStatus.ADMITTING),
    ],
)
def test_effective_status_derived_from_time(now: datetime, expected: QueueStatus) -> None:
    for stored in (QueueStatus.SCHEDULED, QueueStatus.OPEN, QueueStatus.ADMITTING):
        assert effective_status(Cfg(stored), now) is expected


@pytest.mark.parametrize("terminal", [QueueStatus.CLOSED, QueueStatus.CANCELLED])
@pytest.mark.parametrize("now", [OPENS - timedelta(hours=1), OPENS, RELEASE + timedelta(hours=1)])
def test_terminal_status_overrides_time(terminal: QueueStatus, now: datetime) -> None:
    assert effective_status(Cfg(terminal), now) is terminal


def _poll(
    *,
    queue_status: QueueStatus = QueueStatus.ADMITTING,
    entry_status: EntryStatus = EntryStatus.WAITING,
    position: int | None = None,
    capacity: int = 60,
    now: datetime = RELEASE,
    rng: FixedRng | random.Random | None = None,
) -> int | None:
    return poll_after_seconds(
        queue_status=queue_status,
        entry_status=entry_status,
        position=position,
        capacity=capacity,
        now=now,
        release_at=RELEASE,
        rng=rng or FixedRng(0),
    )


def test_pre_release_polls_every_30s_plus_jitter() -> None:
    now = RELEASE - timedelta(minutes=10)
    assert _poll(queue_status=QueueStatus.OPEN, now=now, rng=FixedRng(0)) == 30
    assert _poll(queue_status=QueueStatus.OPEN, now=now, rng=FixedRng(5)) == 35


def test_pre_release_poll_capped_at_seconds_until_release() -> None:
    assert _poll(queue_status=QueueStatus.OPEN, now=RELEASE - timedelta(seconds=12)) == 12
    # Partial seconds round up so the client never polls before release.
    assert _poll(queue_status=QueueStatus.OPEN, now=RELEASE - timedelta(seconds=11.2)) == 12
    assert (
        _poll(queue_status=QueueStatus.OPEN, now=RELEASE - timedelta(seconds=12), rng=FixedRng(3))
        == 15
    )


def test_prequeue_after_release_before_shuffle_polls_soon() -> None:
    assert _poll(position=None, now=RELEASE + timedelta(milliseconds=300)) == 0


def test_pre_release_jitter_stays_in_range() -> None:
    rng = random.Random(1234)
    now = RELEASE - timedelta(minutes=10)
    values = {_poll(queue_status=QueueStatus.OPEN, now=now, rng=rng) for _ in range(500)}
    assert values == {30, 31, 32, 33, 34, 35}


@pytest.mark.parametrize(
    ("position", "expected"),
    [(1, 3), (60, 3), (61, 10), (600, 10), (601, 30), (100_000, 30)],
)
def test_waiting_poll_by_position(position: int, expected: int) -> None:
    assert _poll(position=position, capacity=60) == expected


@pytest.mark.parametrize("entry_status", [EntryStatus.ADMITTED, EntryStatus.NONE])
def test_not_waiting_stops_polling(entry_status: EntryStatus) -> None:
    assert _poll(entry_status=entry_status, position=None) is None


@pytest.mark.parametrize("queue_status", [QueueStatus.CLOSED, QueueStatus.CANCELLED])
def test_terminal_queue_stops_polling(queue_status: QueueStatus) -> None:
    assert _poll(queue_status=queue_status, position=5) is None


# --- end of sale --------------------------------------------------------------------------

SALE_ENDS = RELEASE + timedelta(days=7)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (SALE_ENDS - timedelta(milliseconds=1), QueueStatus.ADMITTING),
        (SALE_ENDS, QueueStatus.CLOSED),
        (SALE_ENDS + timedelta(days=30), QueueStatus.CLOSED),
    ],
)
def test_sale_end_closes_the_queue(now: datetime, expected: QueueStatus) -> None:
    for stored in (QueueStatus.SCHEDULED, QueueStatus.OPEN, QueueStatus.ADMITTING):
        assert effective_status(Cfg(stored, sale_ends_at=SALE_ENDS), now) is expected


def test_stored_terminal_status_wins_over_sale_end() -> None:
    cfg = Cfg(QueueStatus.CANCELLED, sale_ends_at=SALE_ENDS)
    assert effective_status(cfg, SALE_ENDS + timedelta(hours=1)) is QueueStatus.CANCELLED


def test_no_sale_end_means_admitting_forever() -> None:
    assert effective_status(Cfg(QueueStatus.ADMITTING), RELEASE + timedelta(days=3650)) is (
        QueueStatus.ADMITTING
    )


@pytest.mark.parametrize(
    ("sale_ends_at", "closed_at", "now", "expected"),
    [
        # Sold out before the sale's end.
        (SALE_ENDS, SALE_ENDS - timedelta(hours=1), SALE_ENDS + timedelta(days=1), "SOLD_OUT"),
        # Closed exactly at the end: the sale ended.
        (SALE_ENDS, SALE_ENDS, SALE_ENDS + timedelta(days=1), "SALE_ENDED"),
        # Admitter hasn't persisted the close yet: judged by now.
        (SALE_ENDS, None, SALE_ENDS + timedelta(milliseconds=1), "SALE_ENDED"),
        # No end configured: always a sell-out.
        (None, RELEASE, RELEASE + timedelta(days=1), "SOLD_OUT"),
        (None, None, RELEASE, "SOLD_OUT"),
    ],
)
def test_close_reason(
    sale_ends_at: datetime | None, closed_at: datetime | None, now: datetime, expected: str
) -> None:
    assert close_reason(sale_ends_at, closed_at, now) is CloseReason(expected)
