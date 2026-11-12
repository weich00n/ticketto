"""The correctness rules each experiment is judged by (PLAN.md, "Checker").

Pure functions: they take what collect.py read and return violations, so they can be tested
without a database. Run the checker once traffic has stopped and the expiry window has passed,
otherwise holds that are mid-expiry show up as drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Violation:
    rule: str
    code: str
    subject: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.code} {self.subject}: {self.detail}"


# ---------- 1. No overselling ----------


@dataclass(frozen=True)
class CategoryState:
    event_id: str
    category_id: str
    total: int  # category_inventory.total_quantity
    sold: int  # category_inventory.sold_quantity
    confirmed_qty: int  # SUM(quantity) of CONFIRMED holds
    active_qty: int  # SUM(quantity) of ACTIVE holds
    redis_available: int | None  # avail:{event}:{category}; None if the key is missing


@dataclass(frozen=True)
class ActiveHold:
    hold_id: str
    hold_expires_at: datetime


def check_no_oversell(categories: list[CategoryState]) -> list[Violation]:
    """Redis is the live counter, MySQL the durable record; after a fault they must agree.

    Spec, Reservation Consistency: counter = total - sold - SUM(quantity of ACTIVE holds).
    """
    out: list[Violation] = []
    for c in categories:
        subject = f"event {c.event_id} category {c.category_id}"
        if c.sold > c.total:
            out.append(Violation("no-oversell", "OVERSOLD", subject, f"sold {c.sold} > total {c.total}"))
        if c.sold != c.confirmed_qty:
            out.append(Violation("no-oversell", "SOLD_MISMATCH", subject,
                                 f"sold_quantity {c.sold} != confirmed holds {c.confirmed_qty}"))
        if c.redis_available is None:
            out.append(Violation("no-oversell", "REDIS_COUNTER_MISSING", subject,
                                 "no availability counter in Redis (not rebuilt after a restart?)"))
            continue
        if c.redis_available < 0:
            out.append(Violation("no-oversell", "NEGATIVE_AVAILABILITY", subject, f"Redis says {c.redis_available}"))
        expected = c.total - c.sold - c.active_qty
        if c.redis_available != expected:
            out.append(Violation("no-oversell", "COUNTER_DRIFT", subject,
                                 f"Redis {c.redis_available}, expected {expected} "
                                 f"(total {c.total} - sold {c.sold} - active holds {c.active_qty})"))
    return out


def check_holds_expired(active: list[ActiveHold], now: datetime, grace: timedelta) -> list[Violation]:
    """Spec: a periodic scan of the hold records catches expiries missed while Redis was down."""
    return [
        Violation("no-oversell", "STALE_ACTIVE_HOLD", f"hold {h.hold_id}",
                  f"still ACTIVE {now - h.hold_expires_at} after its hold expired")
        for h in active
        if h.hold_expires_at + grace < now
    ]


# ---------- 4. Queue slots freed ----------


@dataclass(frozen=True)
class QueuePool:
    event_id: str
    capacity: int  # queue_configs.active_pool_capacity
    active: dict[str, datetime]  # queue:{event}:active, admission_id -> admission_expires_at


@dataclass(frozen=True)
class ConfirmedAdmission:
    admission_id: str
    confirmed_at: datetime


def check_slots_freed(pool: QueuePool, confirmed: list[ConfirmedAdmission], now: datetime,
                      grace: timedelta) -> list[Violation]:
    """Spec: an active slot is released after booking confirmation or the 11-minute limit, and
    queue capacity recovers. `grace` allows for OrderConfirmed still being on its way."""
    out: list[Violation] = []
    subject = f"event {pool.event_id}"
    if len(pool.active) > pool.capacity:
        out.append(Violation("slots-freed", "POOL_OVER_CAPACITY", subject,
                             f"{len(pool.active)} active admissions, capacity {pool.capacity}"))
    for adm in confirmed:
        if adm.admission_id in pool.active and adm.confirmed_at + grace < now:
            out.append(Violation("slots-freed", "SLOT_NOT_FREED", f"admission {adm.admission_id}",
                                 f"booking confirmed at {adm.confirmed_at:%H:%M:%S} but the slot is still held"))
    for admission_id, expires_at in pool.active.items():
        if expires_at + grace < now:
            out.append(Violation("slots-freed", "EXPIRED_SLOT_NOT_SWEPT", f"admission {admission_id}",
                                 f"expired at {expires_at:%H:%M:%S} but still in the active pool"))
    return out
