"""Each rule must pass on a consistent state and catch the inconsistency it exists for."""

from datetime import datetime, timedelta, timezone

from checker.invariants import (
    ActiveHold,
    CategoryState,
    ConfirmedAdmission,
    QueuePool,
    check_holds_expired,
    check_no_oversell,
    check_slots_freed,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
GRACE = timedelta(minutes=2)


def category(**changes) -> CategoryState:
    base = dict(event_id="e", category_id="c", total=100, sold=10, confirmed_qty=10, active_qty=5,
                redis_available=85)
    return CategoryState(**{**base, **changes})


def codes(violations) -> set[str]:
    return {v.code for v in violations}


# ---------- no overselling ----------

def test_consistent_inventory_passes():
    assert check_no_oversell([category()]) == []


def test_sold_beyond_total_is_oversold():
    assert "OVERSOLD" in codes(check_no_oversell([category(total=10, sold=11, confirmed_qty=11, active_qty=0,
                                                          redis_available=0)]))


def test_redis_counter_out_of_step_with_mysql_is_drift():
    # e.g. a hold returned to Redis twice after a reboot
    assert codes(check_no_oversell([category(redis_available=90)])) == {"COUNTER_DRIFT"}


def test_missing_counter_after_restart_is_reported():
    assert codes(check_no_oversell([category(redis_available=None)])) == {"REDIS_COUNTER_MISSING"}


def test_sold_quantity_must_match_confirmed_holds():
    assert "SOLD_MISMATCH" in codes(check_no_oversell([category(confirmed_qty=9)]))


def test_hold_past_expiry_and_grace_is_stale():
    holds = [ActiveHold("fresh", NOW + timedelta(minutes=3)),
             ActiveHold("late-but-in-grace", NOW - timedelta(minutes=1)),
             ActiveHold("stale", NOW - timedelta(minutes=5))]
    assert [v.subject for v in check_holds_expired(holds, NOW, GRACE)] == ["hold stale"]


# ---------- slots freed ----------

def test_released_slots_pass():
    pool = QueuePool("e", capacity=2, active={"a2": NOW + timedelta(minutes=5)})
    confirmed = [ConfirmedAdmission("a1", NOW - timedelta(minutes=5))]
    assert check_slots_freed(pool, confirmed, NOW, GRACE) == []


def test_confirmed_booking_still_holding_its_slot():
    pool = QueuePool("e", capacity=2, active={"a1": NOW + timedelta(minutes=5)})
    confirmed = [ConfirmedAdmission("a1", NOW - timedelta(minutes=5))]
    assert codes(check_slots_freed(pool, confirmed, NOW, GRACE)) == {"SLOT_NOT_FREED"}


def test_recent_confirmation_gets_grace_for_the_message():
    pool = QueuePool("e", capacity=2, active={"a1": NOW + timedelta(minutes=5)})
    confirmed = [ConfirmedAdmission("a1", NOW - timedelta(seconds=30))]
    assert check_slots_freed(pool, confirmed, NOW, GRACE) == []


def test_pool_over_capacity_and_unswept_expiry():
    pool = QueuePool("e", capacity=1, active={"a1": NOW - timedelta(minutes=10), "a2": NOW + timedelta(minutes=1)})
    assert codes(check_slots_freed(pool, [], NOW, GRACE)) == {"POOL_OVER_CAPACITY", "EXPIRED_SLOT_NOT_SWEPT"}
