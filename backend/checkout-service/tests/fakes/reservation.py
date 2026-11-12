"""In-memory stand-in for reservation-service's internal API, matching the real
InternalHoldView / ConfirmView / ConfirmRequest shapes exactly, and raising the same
typed errors HttpReservationClient maps reservation's real responses to."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.clients.reservation import (
    ConfirmView,
    HoldCancelledError,
    HoldExpiredError,
    HoldNotFoundError,
    HoldOrderMismatchError,
    HoldView,
    ReservationContractError,
    ReservationError,
)


def _in(seconds: int) -> datetime:
    """A UTC timestamp `seconds` from now, with sub-second precision trimmed."""
    return (datetime.now(UTC).replace(microsecond=0)) + timedelta(seconds=seconds)


class TransientReservationError(ReservationError):
    """A stand-in for a connect/503 failure that the service must retry (not treat as a
    definitive rejection)."""


class FakeReservation:
    # confirm outcomes, each one a real reservation answer:
    #   confirmed        200
    #   expired          409 HOLD_EXPIRED
    #   cancelled        409 EVENT_CANCELLED
    #   mismatch         409 ORDER_MISMATCH
    #   not_found        404 HOLD_NOT_FOUND
    #   event_not_found  404 EVENT_NOT_FOUND (no sale-status row)
    #   bad_request      400 INVALID_REQUEST
    OUTCOMES = (
        "confirmed",
        "expired",
        "cancelled",
        "mismatch",
        "not_found",
        "event_not_found",
        "bad_request",
    )

    def __init__(self) -> None:
        self.holds: dict[str, HoldView] = {}
        self._confirm_outcome: str = "confirmed"
        self.confirm_calls: list[tuple[str, str, str]] = []
        self.transient_failures = 0
        self.get_unavailable = False  # GET answers 503 DEPENDENCY_UNAVAILABLE

    def add_hold(  # pylint: disable=too-many-arguments
        self,
        *,
        hold_id: str,
        user_id: str,
        event_id: str,
        status: str = "ACTIVE",
        category_id: str | None = None,
        admission_id: str | None = None,
        quantity: int = 1,
        unit_amount: int = 100_000,
        currency: str = "SGD",
        order_id: str | None = None,
        payment_deadline_s: int = 300,
        hold_deadline_s: int = 300,
    ) -> HoldView:
        view = HoldView(
            hold_id=hold_id,
            user_id=user_id,
            event_id=event_id,
            category_id=category_id,
            admission_id=admission_id,
            status=status,
            quantity=quantity,
            unit_amount=unit_amount,
            total_amount=unit_amount * quantity,
            currency=currency,
            order_id=order_id,
            payment_expires_at=_in(payment_deadline_s),
            hold_expires_at=_in(hold_deadline_s),
            server_time=_in(0),
        )
        self.holds[hold_id] = view
        return view

    def set_outcome(self, outcome: str) -> None:
        assert outcome in self.OUTCOMES, outcome
        self._confirm_outcome = outcome

    def get_hold(self, hold_id: str) -> HoldView | None:
        if self.get_unavailable:
            raise TransientReservationError("reservation unavailable on hold read: 503")
        return self.holds.get(hold_id)

    def confirm(  # pylint: disable=too-many-return-statements
        self, hold_id: str, order_id: str, payment_reference: str
    ) -> ConfirmView:
        self.confirm_calls.append((hold_id, order_id, payment_reference))
        if not order_id or not payment_reference:
            # ConfirmRequest's @NotBlank fields.
            raise ReservationContractError(hold_id, "unexpected 400 INVALID_REQUEST")
        hold = self.holds.get(hold_id)
        if hold is None or self._confirm_outcome == "not_found":
            raise HoldNotFoundError(hold_id, "HOLD_NOT_FOUND")
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise TransientReservationError("connect refused")
        outcome = self._confirm_outcome
        if outcome == "event_not_found":
            raise HoldNotFoundError(hold_id, "EVENT_NOT_FOUND")
        if outcome == "bad_request":
            raise ReservationContractError(hold_id, "unexpected 400 INVALID_REQUEST")
        if outcome == "expired":
            raise HoldExpiredError(hold_id)
        if outcome == "cancelled":
            raise HoldCancelledError(hold_id)
        if outcome == "mismatch":
            raise HoldOrderMismatchError(hold_id)
        duplicate = hold.order_id is not None and hold.order_id == order_id
        return ConfirmView(
            hold_id=hold_id,
            order_id=order_id,
            status="CONFIRMED",
            quantity=hold.quantity,
            duplicate=duplicate,
            confirmed_at=hold.payment_expires_at,
        )
