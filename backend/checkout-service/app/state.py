"""The three split state machines from Technical Contract section 9, with their
allowed transitions.

Keeping every legal move in one module (and testing it exhaustively) means no route,
worker or consumer can quietly put an order, payment or refund into an impossible state.
Transitions are guarded here; the caller does the actual write.
"""

from .errors import ApiError, Code

# booking: PENDING_PAYMENT -> CONFIRMING -> CONFIRMED | EXPIRED | CANCELLED
# CONFIRMING may also go to CANCELLED: an event cancellation (or a hold that expires
# between payment and confirmation) takes precedence over an in-flight confirm (TC §11),
# so a paid-but-unconfirmed order is refunded and cancelled, not confirmed.
# CONFIRMED -> CANCELLED when reservation reports a refund is owed (RefundRequired): the
# money goes back, so the booking no longer stands.
# CONFIRMING -> RECONCILIATION when reservation says the hold belongs to another order
# (TC §7: reject and flag, never auto-resolve); only a person moves it on from there.
BOOKING = {
    "PENDING_PAYMENT": {"CONFIRMING", "EXPIRED", "CANCELLED"},
    "CONFIRMING": {"CONFIRMED", "PENDING_PAYMENT", "EXPIRED", "CANCELLED", "RECONCILIATION"},
    "CONFIRMED": {"CANCELLED"},
    "EXPIRED": set(),
    "CANCELLED": set(),
    "RECONCILIATION": set(),
}

# payment: PENDING -> SUCCEEDED | FAILED | CANCELLED
# A success reported by Stripe is a fact, whatever we had concluded before: a declined
# card (FAILED) may be retried on the same intent, and cancelling an intent at the
# deadline is best-effort, so a late success can still arrive (CANCELLED -> SUCCEEDED).
# The booking decides what happens to that money (confirm, or refund).
PAYMENT = {
    "PENDING": {"SUCCEEDED", "FAILED", "CANCELLED"},
    "SUCCEEDED": set(),
    "FAILED": {"SUCCEEDED", "CANCELLED"},
    "CANCELLED": {"SUCCEEDED"},
}

# refund: REQUESTED -> PENDING -> SUCCEEDED | FAILED | REQUIRES_ACTION
# A refund stays REQUESTED until Stripe has answered (a Stripe outage leaves it there
# for the next sweep), and Stripe's answer can already be final (REQUESTED -> SUCCEEDED
# or FAILED). Re-asking Stripe moves a refund from FAILED straight to PENDING again (the
# same idempotency key means it is one logical refund); that is a manual step.
REFUND = {
    "REQUESTED": {"PENDING", "SUCCEEDED", "FAILED"},
    "PENDING": {"SUCCEEDED", "FAILED", "REQUIRES_ACTION"},
    "SUCCEEDED": set(),
    "FAILED": {"PENDING"},
    "REQUIRES_ACTION": set(),
}


def guard(machine: dict[str, set[str]], current: str, target: str, what: str) -> None:
    allowed = machine.get(current)
    if allowed is None:
        raise ApiError(
            500,
            Code.INTERNAL_ERROR,
            f"{what} is in an unknown state: {current}",
        )
    if target not in allowed:
        raise ApiError(
            409,
            Code.ORDER_STATE_CONFLICT,
            f"Cannot move {what} from {current} to {target}.",
        )
