import pytest

from app.errors import ApiError
from app.state import BOOKING, PAYMENT, REFUND, guard


def test_booking_happy_path():
    guard(BOOKING, "PENDING_PAYMENT", "CONFIRMING", "booking")
    guard(BOOKING, "CONFIRMING", "CONFIRMED", "booking")


def test_booking_can_expire_or_cancel_from_pending():
    guard(BOOKING, "PENDING_PAYMENT", "EXPIRED", "booking")
    guard(BOOKING, "PENDING_PAYMENT", "CANCELLED", "booking")


def test_confirmed_can_only_be_cancelled_by_a_refund():
    guard(BOOKING, "CONFIRMED", "CANCELLED", "booking")
    with pytest.raises(ApiError) as e:
        guard(BOOKING, "CONFIRMED", "EXPIRED", "booking")
    assert e.value.status_code == 409
    assert e.value.code == "ORDER_STATE_CONFLICT"


def test_order_mismatch_parks_the_booking_for_reconciliation():
    guard(BOOKING, "CONFIRMING", "RECONCILIATION", "booking")
    for target in ("CONFIRMED", "CANCELLED", "CONFIRMING"):
        with pytest.raises(ApiError):
            guard(BOOKING, "RECONCILIATION", target, "booking")


def test_closed_bookings_stay_closed():
    for closed in ("EXPIRED", "CANCELLED"):
        with pytest.raises(ApiError):
            guard(BOOKING, closed, "CONFIRMING", "booking")


def test_payment_happy_path():
    guard(PAYMENT, "PENDING", "SUCCEEDED", "payment")


def test_stripe_success_is_recorded_after_a_decline_or_a_cancel():
    # A declined card can be retried; a best-effort cancel can lose the race.
    guard(PAYMENT, "FAILED", "SUCCEEDED", "payment")
    guard(PAYMENT, "CANCELLED", "SUCCEEDED", "payment")


def test_succeeded_payment_is_terminal():
    for target in ("FAILED", "CANCELLED", "PENDING"):
        with pytest.raises(ApiError):
            guard(PAYMENT, "SUCCEEDED", target, "payment")


def test_refund_can_settle_on_stripes_first_answer():
    guard(REFUND, "REQUESTED", "SUCCEEDED", "refund")
    guard(REFUND, "REQUESTED", "FAILED", "refund")


def test_refund_retry_allowed():
    guard(REFUND, "REQUESTED", "PENDING", "refund")
    guard(REFUND, "PENDING", "FAILED", "refund")
    # A failed refund can be re-asked (same idempotency key) without re-REQUESTING.
    guard(REFUND, "FAILED", "PENDING", "refund")


def test_unknown_state_is_internal_error():
    with pytest.raises(ApiError) as e:
        guard(BOOKING, "NAN", "CONFIRMED", "booking")
    assert e.value.status_code == 500


def test_terminal_refund_states_reject_transitions():
    for s in ("SUCCEEDED", "REQUIRES_ACTION"):
        with pytest.raises(ApiError):
            guard(REFUND, s, "PENDING", "refund")
