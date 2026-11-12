"""Value types returned by the Redis queue repository."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.core.status import EntryStatus
from app.core.timefmt import from_ms, to_iso


@dataclass(frozen=True, slots=True)
class Admission:
    """One active admission. In Redis the times are epoch milliseconds."""

    admission_id: str
    user_id: str
    event_id: str
    admitted_at: datetime
    selection_expires_at: datetime
    admission_expires_at: datetime

    @classmethod
    def from_hash(cls, raw: dict[str, str]) -> Admission:
        return cls(
            admission_id=raw["admission_id"],
            user_id=raw["user_id"],
            event_id=raw["event_id"],
            admitted_at=from_ms(int(raw["admitted_at"])),
            selection_expires_at=from_ms(int(raw["selection_expires_at"])),
            admission_expires_at=from_ms(int(raw["admission_expires_at"])),
        )

    def is_expired(self, now: datetime) -> bool:
        return self.admission_expires_at <= now

    def to_public(self) -> dict[str, str]:
        """The admission object of the contract: exactly six fields, ISO 8601 UTC.

        Used verbatim as the token claims and in every response body.
        """
        return {
            "admission_id": self.admission_id,
            "user_id": self.user_id,
            "event_id": self.event_id,
            "admitted_at": to_iso(self.admitted_at),
            "selection_expires_at": to_iso(self.selection_expires_at),
            "admission_expires_at": to_iso(self.admission_expires_at),
        }


@dataclass(frozen=True, slots=True)
class ReleasedAdmission:
    admission_id: str
    user_id: str | None  # None only if the admission hash had already vanished


class JoinOutcome(StrEnum):
    CREATED = "created"  # new entry (pre-queue before release, waiting after)
    ALREADY_WAITING = "waiting"  # already in the pre-queue or waiting queue
    ALREADY_ADMITTED = "admitted"  # holds an unexpired admission
    CLOSED = "closed"  # queue is CLOSED or CANCELLED (and no admission held)


@dataclass(frozen=True, slots=True)
class JoinResult:
    outcome: JoinOutcome
    admission_id: str | None = None
    closed_status: str | None = None  # "CLOSED" or "CANCELLED" when outcome is CLOSED


@dataclass(frozen=True, slots=True)
class ShuffleResult:
    performed: bool  # False if another run had already shuffled this event
    ordered: int = 0  # members placed in the shuffled order Python passed
    leftovers: int = 0  # members that joined mid-shuffle, appended afterwards


@dataclass(frozen=True, slots=True)
class AdmitResult:
    admitted: list[Admission]
    released: list[ReleasedAdmission]  # expired admissions swept in the same call
    now: datetime  # Redis server time the script ran at


class ReleaseOutcome(StrEnum):
    RELEASED = "released"
    NOT_FOUND = "not_found"  # unknown or already ended: ignore
    EXPIRED = "expired"  # deadline passed but not swept yet: leave it to the sweep
    MISMATCH = "mismatch"  # user_id/event_id differ from the stored admission


@dataclass(frozen=True, slots=True)
class ReleaseResult:
    outcome: ReleaseOutcome
    stored_user_id: str | None = None
    stored_event_id: str | None = None


class LapseOutcome(StrEnum):
    NOT_ADMITTED = "not_admitted"
    KEPT = "kept"  # selection deadline not passed yet
    EXPIRED = "expired"  # admission deadline passed too: left to the sweep
    REQUEUED = "requeued"  # admission ended, buyer at the back of the queue
    EVICTED = "evicted"  # admission ended; queue closed, so not requeued


@dataclass(frozen=True, slots=True)
class LapseResult:
    outcome: LapseOutcome
    admission_id: str | None = None

    @property
    def ended(self) -> bool:
        return self.outcome in (LapseOutcome.REQUEUED, LapseOutcome.EVICTED)


class LeaveOutcome(StrEnum):
    LEFT = "left"
    NOT_IN_QUEUE = "not_in_queue"
    ALREADY_ADMITTED = "already_admitted"


@dataclass(frozen=True, slots=True)
class Entry:
    """A buyer's state in one event's queue."""

    status: EntryStatus
    position: int | None = None  # 1-based; None while in the unordered pre-queue
    admission: Admission | None = None


@dataclass(frozen=True, slots=True)
class QueueCounts:
    waiting: int  # pre-queue + waiting queue
    active: int  # unexpired admissions


@dataclass(frozen=True, slots=True)
class AdmissionPage:
    items: list[Admission]
    next_cursor: str | None
