"""Queue lifecycle and polling rules. Pure functions: no I/O, no clock reads."""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class QueueStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    OPEN = "OPEN"
    ADMITTING = "ADMITTING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset({QueueStatus.CLOSED, QueueStatus.CANCELLED})


class CloseReason(StrEnum):
    """Why a CLOSED queue closed (CLOSED covers both)."""

    SOLD_OUT = "SOLD_OUT"
    SALE_ENDED = "SALE_ENDED"


class EntryStatus(StrEnum):
    WAITING = "WAITING"
    ADMITTED = "ADMITTED"
    NONE = "NONE"


# Fixed by the domain rules (not configurable).
POOL_DIVISOR = 5

PRE_RELEASE_POLL_SECONDS = 30
PRE_RELEASE_JITTER_SECONDS = 5
NEAR_FRONT_POLL_SECONDS = 3
MID_QUEUE_POLL_SECONDS = 10
FAR_BACK_POLL_SECONDS = 30
MID_QUEUE_CAPACITY_MULTIPLE = 10


class Schedule(Protocol):
    @property
    def status(self) -> QueueStatus: ...
    @property
    def queue_opens_at(self) -> datetime: ...
    @property
    def release_at(self) -> datetime: ...
    @property
    def sale_ends_at(self) -> datetime | None: ...


class RandInt(Protocol):
    def randint(self, a: int, b: int) -> int: ...


def active_pool_capacity(total_capacity: int) -> int:
    """max(1, floor(total_capacity / 5)), e.g. 304 -> 60, 3 -> 1."""
    return max(1, total_capacity // POOL_DIVISOR)


def effective_status(cfg: Schedule, now: datetime) -> QueueStatus:
    """Terminal statuses are stored; OPEN, ADMITTING and the end of the sale
    (CLOSED once sale_ends_at passes) are derived from time."""
    if cfg.status in TERMINAL_STATUSES:
        return cfg.status
    if cfg.sale_ends_at is not None and now >= cfg.sale_ends_at:
        return QueueStatus.CLOSED
    if now < cfg.queue_opens_at:
        return QueueStatus.SCHEDULED
    if now < cfg.release_at:
        return QueueStatus.OPEN
    return QueueStatus.ADMITTING


def close_reason(
    sale_ends_at: datetime | None, closed_at: datetime | None, now: datetime
) -> CloseReason:
    """For a CLOSED queue: the sale ended if sale_ends_at was reached no later than
    the moment it closed (closed_at, or now if the close isn't persisted yet);
    otherwise it sold out first."""
    if sale_ends_at is not None and sale_ends_at <= (closed_at or now):
        return CloseReason.SALE_ENDED
    return CloseReason.SOLD_OUT


def poll_after_seconds(
    *,
    queue_status: QueueStatus,
    entry_status: EntryStatus,
    position: int | None,
    capacity: int,
    now: datetime,
    release_at: datetime,
    rng: RandInt,
) -> int | None:
    """Seconds until the client should poll again; None means stop polling."""
    if entry_status is not EntryStatus.WAITING or queue_status in TERMINAL_STATUSES:
        return None
    if position is None:
        # Pre-queue (order not decided yet): wait until release, spread with jitter.
        until_release = max(0, math.ceil((release_at - now).total_seconds()))
        base = min(PRE_RELEASE_POLL_SECONDS, until_release)
        return base + rng.randint(0, PRE_RELEASE_JITTER_SECONDS)
    if position <= capacity:
        return NEAR_FRONT_POLL_SECONDS
    if position <= MID_QUEUE_CAPACITY_MULTIPLE * capacity:
        return MID_QUEUE_POLL_SECONDS
    return FAR_BACK_POLL_SECONDS
