"""The checkout business logic: order lifecycle, the confirmation flow, and refunds.

This is the heart of the service. Every method owns its own DB transaction and writes
any resulting messages to the outbox in the same commit, so "state changed" and "message
staged" are atomic. The callers (webhook route, consumer threads, worker sweeps) are thin.

The confirmation flow implements Technical Contract section 7: a verified payment
succeeds, we ask reservation to confirm the hold, and we handle each of the five outcome
rows. A transient reservation failure is a requeue, never an assumed failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .clients.reservation import (
    HoldCancelledError,
    HoldExpiredError,
    HoldNotActiveError,
    HoldNotFoundError,
    HoldOrderMismatchError,
    HoldView,
    ReservationClient,
    ReservationContractError,
    ReservationError,
)
from .clients.stripe import (
    StripeClient,
    StripeEvent,
    StripeRejectedError,
    StripeUnavailableError,
)
from .config import Settings
from .errors import ApiError, Code, validation_error
from .messaging.envelope import PoisonMessage
from .messaging.events import (
    EVENT_ORDER_CONFIRMED,
    EVENT_ORDER_REFUNDED,
    EVENT_REFUND_COMPLETED,
    EVENT_REFUND_FAILED,
    order_confirmed_data,
    order_refunded_data,
    refund_completed_data,
    refund_failed_data,
)
from .messaging.outbox import Outbox
from .messaging.payloads import (
    event_cancelled_fields,
    refund_required_fields,
    reservation_expired_fields,
)
from .models import (
    IdempotencyKey,
    Order,
    Payment,
    ProcessedMessage,
    ProcessedStripeEvent,
    Refund,
)
from .state import BOOKING, PAYMENT, REFUND, guard

log = logging.getLogger(__name__)

ACTIVE = "ACTIVE"


class CheckoutService:
    def __init__(
        self,
        settings: Settings,
        session_factory,
        outbox: Outbox,
        reservation: ReservationClient,
        stripe: StripeClient,
    ) -> None:
        self._settings = settings
        self._sf = session_factory
        self._outbox = outbox
        self._reservation = reservation
        self._stripe = stripe

    # ------------------------------------------------------------------ orders

    def create_order(  # pylint: disable=too-many-branches
        self,
        *,
        user_id: str,
        hold_id: str,
        delivery_email: str,
        idempotency_key: str | None,
    ) -> tuple[Order, str | None, bool]:
        """Create (or return) the order for a hold.

        Quantity and amounts come from the hold, never from the client. Returns
        (order, client_secret, created); a returned unpaid order always carries a client
        secret, creating its payment intent first if an earlier attempt could not.
        """
        hold_id = hold_id.strip()
        if not hold_id:
            raise validation_error("hold_id is required.")

        hold = self._read_hold(hold_id)
        if hold is None or hold.user_id != user_id:
            # TC: a buyer sees only their own orders; a foreign hold is 404, not 403 - and
            # ownership is checked before status, so its state doesn't leak either.
            raise ApiError(404, Code.HOLD_NOT_FOUND, "No such reservation.")

        with self._sf() as db:
            # Idempotent create: one order per hold. Checked before the hold's status,
            # because once this order is paid reservation reports the hold CONFIRMED (and
            # an unpaid one EXPIRED): a repeat of the request still gets its order back.
            # Order's payment/refunds relationships are eager (selectin) on the model, so
            # the returned object serialises cleanly.
            existing = db.scalar(select(Order).where(Order.hold_id == hold_id))
            if existing is not None:
                return existing, self._ensure_intent(db, existing), False

            if not hold.is_active:
                if hold.status == "EXPIRED":
                    raise ApiError(409, Code.HOLD_EXPIRED, "Your reservation has expired.")
                if hold.status == "CANCELLED":
                    raise ApiError(409, Code.EVENT_CANCELLED, "This event has been cancelled.")
                raise ApiError(
                    409, Code.ORDER_STATE_CONFLICT, "This hold can no longer be purchased."
                )

            # Idempotency-Key: same key + same body -> original; different body -> conflict.
            if idempotency_key:
                request_hash = _body_hash(hold_id, delivery_email)
                row = db.get(IdempotencyKey, idempotency_key)
                if row is not None:
                    if row.request_hash != request_hash:
                        raise ApiError(
                            409,
                            Code.IDEMPOTENCY_KEY_CONFLICT,
                            "This Idempotency-Key was used with a different payload.",
                        )
                    if row.order_id:
                        order = db.get(Order, row.order_id)
                        return order, self._ensure_intent(db, order), False
                else:
                    db.add(
                        IdempotencyKey(
                            key=idempotency_key, user_id=user_id, request_hash=request_hash
                        )
                    )

            order = Order(
                order_id=str(uuid.uuid4()),
                hold_id=hold.hold_id,
                admission_id=hold.admission_id,
                user_id=user_id,
                event_id=hold.event_id,
                category_id=hold.category_id,
                quantity=hold.quantity,
                unit_amount=hold.unit_amount,
                total_amount=hold.total_amount,
                currency=hold.currency,
                delivery_email=delivery_email,
                booking_status="PENDING_PAYMENT",
                payment_expires_at=hold.payment_expires_at,
                hold_expires_at=hold.hold_expires_at,
            )
            payment = Payment(
                payment_id=str(uuid.uuid4()),
                order_id=order.order_id,
                status="PENDING",
                amount=order.total_amount,
                currency=order.currency,
            )
            db.add(order)
            db.add(payment)
            if idempotency_key:
                db.execute(_update_idem_order(idempotency_key, order.order_id))
            db.commit()
            db.refresh(order)

            # The order is committed before the intent exists, so a Stripe failure here
            # never loses it; a retry of this request finds it and creates the intent then.
            return order, self._ensure_intent(db, order), True

    def _read_hold(self, hold_id: str) -> HoldView | None:
        """Read the hold, turning every reservation failure into a documented API error.

        The real GET answers 200/404/5xx; the 409 mappings cover the client's defensive
        branches. A contract error is our problem, so it is logged loudly and the buyer
        gets a 409 rather than a misleading 503 that invites endless retries.
        """
        try:
            return self._reservation.get_hold(hold_id)
        except ReservationError as exc:
            log.warning("reservation unavailable on hold read", extra={"error": str(exc)})
            raise ApiError(
                503,
                Code.RESERVATION_UNAVAILABLE,
                "Reservations are temporarily unavailable. Please retry.",
            ) from exc
        except HoldExpiredError as exc:
            raise ApiError(409, Code.HOLD_EXPIRED, "Your reservation has expired.") from exc
        except HoldCancelledError as exc:
            raise ApiError(409, Code.EVENT_CANCELLED, "This event has been cancelled.") from exc
        except (HoldNotActiveError, ReservationContractError) as exc:
            log.error(
                "unexpected reservation answer on hold read",
                extra={"hold_id": hold_id, "error": str(exc)},
            )
            raise ApiError(
                409, Code.ORDER_STATE_CONFLICT, "This hold can no longer be purchased."
            ) from exc

    def get_order(self, order_id: str, user_id: str) -> Order:
        with self._sf() as db:
            order = db.get(Order, order_id)
            if order is None or order.user_id != user_id:
                # foreign or unknown -> 404 (TC: not visible to the caller)
                raise ApiError(404, Code.ORDER_NOT_FOUND, "No such order.")
            return order

    def list_orders(self, user_id: str, page: int, page_size: int) -> tuple[list[Order], int]:
        with self._sf() as db:
            total = (
                db.scalar(select(func.count(Order.order_id)).where(Order.user_id == user_id)) or 0
            )
            rows = (
                db.scalars(
                    select(Order)
                    .where(Order.user_id == user_id)
                    .order_by(Order.created_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
            return list(rows), total

    # ------------------------------------------------------------ confirmation

    def handle_stripe_event(self, event: StripeEvent) -> str:
        """Process a verified Stripe webhook. Returns a short outcome tag for logging."""
        with self._sf() as db:
            if db.get(ProcessedStripeEvent, event.event_id) is not None:
                return "duplicate"
            if event.event_type == "payment_intent.succeeded":
                return self._confirm_on_payment(db, event)
            if event.event_type == "payment_intent.payment_failed":
                return self._payment_failed(db, event)
            if event.event_type == "charge.refunded":
                return self._charge_refunded(db, event)
            return "ignored"

    def _confirm_on_payment(self, db: Session, event: StripeEvent) -> str:
        order = self._order_by_intent(db, event.object_id)
        if order is None:
            return "unknown"
        if order.payment.status == "SUCCEEDED":
            # A replayed success: don't re-emit, but make sure a confirm was attempted.
            db.add(
                ProcessedStripeEvent(stripe_event_id=event.event_id, event_type=event.event_type)
            )
            db.commit()
            return "already_succeeded"

        # Stripe has the money: record that whatever we had concluded about this payment
        # (a declined attempt the buyer retried, or an intent we could not cancel in time).
        guard(PAYMENT, order.payment.status, "SUCCEEDED", "payment")
        order.payment.status = "SUCCEEDED"
        order.payment.succeeded_at = datetime.now(UTC)
        db.add(ProcessedStripeEvent(stripe_event_id=event.event_id, event_type=event.event_type))
        if order.booking_status != "PENDING_PAYMENT":
            # The booking already ended (deadline passed, or the event was cancelled while
            # the buyer was paying): there is nothing to confirm, so the money goes back.
            log.warning(
                "payment succeeded after the booking ended; refunding",
                extra={"order_id": order.order_id, "booking_status": order.booking_status},
            )
            self._request_refund(db, order, REASON_LATE_PAYMENT)
            db.commit()
            return "late_payment_refund"
        guard(BOOKING, order.booking_status, "CONFIRMING", "booking")
        order.booking_status = "CONFIRMING"
        db.commit()

        outcome = self._attempt_confirm(db, order)
        return outcome

    def _attempt_confirm(self, db: Session, order: Order) -> str:
        """Ask reservation to confirm the hold for this paid order and apply the outcome.

        TC §7 confirmation outcomes:
          confirmed (or already confirmed for this order) -> CONFIRMED, emit OrderConfirmed
          expired / cancelled / event cancelled           -> refund, CANCELLED, OrderRefunded
          different order                                  -> reject, flag for reconciliation
          hold or event unknown to reservation (404)       -> flag for reconciliation
          request rejected as malformed (400) / bad body   -> stays CONFIRMING, logged
          transient reservation failure                    -> requeue (stays CONFIRMING)
        """
        payment = order.payment
        payment_ref = payment.stripe_payment_intent_id
        if not payment_ref:
            # ConfirmRequest.payment_reference is @NotBlank: sending "" would come back as
            # 400. A paid order without an intent id is inconsistent data, so park it.
            return self._reconcile(db, order, "paid order has no payment intent id")
        try:
            view = self._reservation.confirm(order.hold_id, order.order_id, payment_ref)
        except HoldOrderMismatchError:
            # This hold is confirmed for a different order: reject and flag, never auto-fix.
            # RECONCILIATION takes the order out of the confirm-retry sweep; a person
            # decides what happens to the payment.
            self._reconcile(db, order, "order mismatch on confirm")
            return "mismatch"
        except HoldNotFoundError as exc:
            # 404 HOLD_NOT_FOUND / EVENT_NOT_FOUND: reservation has no record matching the
            # hold this order was priced from. Not an expiry, so no automatic refund: the
            # buyer has paid and a person decides (same as a mismatch).
            return self._reconcile(db, order, f"reservation answered {exc.code} on confirm")
        except ReservationContractError as exc:
            # Our request (or reservation's answer) broke the contract. Refunding would be
            # wrong; stay CONFIRMING so the retry sweep succeeds once the bug is fixed.
            log.error(
                "reservation contract error on confirm",
                extra={"order_id": order.order_id, "hold_id": order.hold_id, "error": str(exc)},
            )
            return "contract_error"
        except (HoldExpiredError, HoldCancelledError, HoldNotActiveError):
            # The payment succeeded but the hold is no longer confirmable: refund it.
            self._request_refund(db, order, REASON_CONFIRM_REJECTED)
            guard(BOOKING, order.booking_status, "CANCELLED", "booking")
            order.booking_status = "CANCELLED"
            db.commit()
            return "refund_required"
        # Transient reservation failure: do NOT assume failure. Keep the booking
        # CONFIRMING so the confirm-retry sweep re-attempts; the payment is recorded
        # SUCCEEDED, so the confirm is retried - the exception propagates to the caller
        # (requeue), and no state change is applied.

        # Confirmed (or a safe duplicate) -> booking CONFIRMED, emit OrderConfirmed.
        if view.duplicate:
            pass  # already confirmed earlier; still ensure state + events are consistent
        guard(BOOKING, order.booking_status, "CONFIRMED", "booking")
        order.booking_status = "CONFIRMED"
        order.confirmed_at = view.confirmed_at
        self._outbox.write(
            db,
            message_type=EVENT_ORDER_CONFIRMED,
            payload=order_confirmed_data(order),
            aggregate_id=order.order_id,
        )
        db.commit()
        return "confirmed"

    def retry_confirms(self, limit: int = 50) -> int:
        """Sweep orders stuck in CONFIRMING with a succeeded payment and re-attempt the
        confirm. This is what makes a transient reservation failure recoverable."""
        with self._sf() as db:
            orders = list(
                db.scalars(
                    select(Order)
                    .where(Order.booking_status == "CONFIRMING")
                    .where(Payment.status == "SUCCEEDED")
                    .join(Payment, Payment.order_id == Order.order_id)
                    .limit(limit)
                )
            )
            done = 0
            for order in orders:
                # Each order stands alone: one that fails in an unexpected way must not
                # stop the sweep for the rest (it would otherwise abort every cycle).
                try:
                    self._attempt_confirm(db, order)
                    done += 1
                except ReservationError:
                    db.rollback()
                    continue
                except ApiError as exc:
                    db.rollback()
                    log.warning(
                        "confirm retry rejected",
                        extra={"order_id": order.order_id, "code": exc.code},
                    )
                except Exception:  # pylint: disable=broad-exception-caught  # isolate one order
                    db.rollback()
                    log.exception("confirm retry failed", extra={"order_id": order.order_id})
            return done

    def _reconcile(self, db: Session, order: Order, reason: str) -> str:
        """Park a CONFIRMING order for a person (TC §7: reject and flag, never auto-fix).

        RECONCILIATION takes the order out of the confirm-retry sweep and the deadline
        sweep; nothing is refunded or announced automatically.
        """
        log.error(
            "order needs reconciliation",
            extra={"order_id": order.order_id, "hold_id": order.hold_id, "reason": reason},
        )
        guard(BOOKING, order.booking_status, "RECONCILIATION", "booking")
        order.booking_status = "RECONCILIATION"
        db.commit()
        return "reconciliation"

    def _payment_failed(self, db: Session, event: StripeEvent) -> str:
        order = self._order_by_intent(db, event.object_id)
        if order is None:
            return "unknown"
        db.add(ProcessedStripeEvent(stripe_event_id=event.event_id, event_type=event.event_type))
        if order.payment.status == "PENDING":
            guard(PAYMENT, "PENDING", "FAILED", "payment")
            order.payment.status = "FAILED"
            order.payment.failed_at = datetime.now(UTC)
            # The buyer may retry with a fresh intent; the booking stays payable until
            # the deadline sweep expires it.
            db.commit()
            return "payment_failed"
        db.commit()
        return "already_settled"

    def _charge_refunded(self, db: Session, event: StripeEvent) -> str:
        """A refund completed on Stripe's side: finalise the order's refund row.

        The event's object is the Charge (ch_...), not the Refund, so the order is found
        through the charge's payment intent; an order has at most one refund.
        """
        db.add(ProcessedStripeEvent(stripe_event_id=event.event_id, event_type=event.event_type))
        charge = event.object
        order = self._order_by_intent(db, charge.get("payment_intent") or "")
        refund = (
            db.scalar(select(Refund).where(Refund.order_id == order.order_id))
            if order is not None
            else None
        )
        if refund is None or not charge.get("refunded"):
            db.commit()
            return "no_matching_refund"
        if refund.status not in ("REQUESTED", "PENDING"):
            # Already final (or needs a person); a webhook must not 500 over it.
            if refund.status != "SUCCEEDED":
                log.warning(
                    "charge refunded but refund is not open",
                    extra={"refund_id": refund.refund_id, "status": refund.status},
                )
            db.commit()
            return "refund_already_final"
        refund.status = "SUCCEEDED"
        refund.stripe_refund_id = refund.stripe_refund_id or _first_refund_id(charge)
        self._emit_refund_completed(db, order, refund)
        db.commit()
        return "refund_finalised"

    # ----------------------------------------------------------------- refunds

    def process_refunds(self, limit: int = 50) -> int:
        """Issue Stripe refunds for outstanding refund obligations and finalise them.

        A refund stays REQUESTED until Stripe has answered, so an outage is retried on
        the next sweep; retries reuse the same idempotency key so one logical refund
        never becomes two. Only a successful refund emits RefundCompleted (and the
        notification OrderRefunded); a failed refund emits RefundFailed once and is left
        for a person (Stripe would give the same answer to the same request again).
        """
        with self._sf() as db:
            refunds = list(
                db.scalars(
                    select(Refund)
                    .where(Refund.status == "REQUESTED")
                    .order_by(Refund.created_at)
                    .limit(limit)
                )
            )
            processed = 0
            for refund in refunds:
                order = db.get(Order, refund.order_id)
                if order is None or not order.payment:
                    continue
                if (
                    order.payment.status != "SUCCEEDED"
                    or not order.payment.stripe_payment_intent_id
                ):
                    continue
                self._issue_refund(db, order, refund)
                processed += 1
            return processed

    def _issue_refund(self, db: Session, order: Order, refund: Refund) -> None:
        try:
            result = self._stripe.create_refund(
                payment_intent_id=order.payment.stripe_payment_intent_id,
                amount=refund.amount,
                reason=_stripe_reason(refund.reason),
                idempotency_key=refund.idempotency_key,
            )
        except StripeUnavailableError as exc:
            # Stripe never answered: stay REQUESTED so the next sweep asks again.
            refund.last_error = f"{type(exc).__name__}: {exc}"
            db.commit()
            return
        except StripeRejectedError as exc:
            guard(REFUND, refund.status, "FAILED", "refund")
            refund.status = "FAILED"
            refund.last_error = f"{type(exc).__name__}: {exc}"
            self._emit_refund_failed(db, order, refund)
            db.commit()
            return
        refund.stripe_refund_id = result.refund_id or refund.stripe_refund_id
        refund.last_error = None
        if result.status == "succeeded":
            guard(REFUND, refund.status, "SUCCEEDED", "refund")
            refund.status = "SUCCEEDED"
            self._emit_refund_completed(db, order, refund)
        elif result.status == "pending":
            # charge.refunded finalises it.
            guard(REFUND, refund.status, "PENDING", "refund")
            refund.status = "PENDING"
        else:  # failed / canceled / requires_action
            guard(REFUND, refund.status, "FAILED", "refund")
            refund.status = "FAILED"
            refund.last_error = f"stripe refund {result.status}"
            self._emit_refund_failed(db, order, refund)
        db.commit()

    # ------------------------------------------------------------- expirations

    def expire_stale_orders(self, now: datetime, limit: int = 200) -> int:
        """The payment-deadline sweep: expire orders past their deadline (TC §11).

        A PENDING_PAYMENT order whose payment_expires_at has passed is EXPIRED, and its
        unconfirmed intent is cancelled so a late card cannot be charged. A CONFIRMING
        order that has never confirmed is also expired (the confirm will be retried by
        the confirm sweep only while a payment is SUCCEEDED).
        """
        with self._sf() as db:
            orders = list(
                db.scalars(
                    select(Order)
                    .where(Order.payment_expires_at <= now)
                    .where(Order.booking_status.in_(["PENDING_PAYMENT", "CONFIRMING"]))
                    .limit(limit)
                )
            )
            expired = 0
            for order in orders:
                if _payment_succeeded(order):
                    continue  # paid; the confirm/retry path owns it
                guard(BOOKING, order.booking_status, "EXPIRED", "booking")
                order.booking_status = "EXPIRED"
                self._cancel_unpaid_payment(db, order)
                expired += 1
            db.commit()
            return expired

    def _cancel_unpaid_payment(self, db: Session, order: Order) -> None:
        """An unpaid payment can no longer be taken: cancel its intent and mark it
        CANCELLED. If Stripe says the intent already succeeded, the buyer paid and we
        never heard: record the payment and refund it now, instead of waiting for a
        webhook that may take hours to be retried. Any other cancel failure is
        best-effort; a success that still gets through is refunded by the webhook (see
        _confirm_on_payment)."""
        payment = order.payment
        if payment is None or payment.status not in ("PENDING", "FAILED"):
            return
        intent_id = payment.stripe_payment_intent_id
        if intent_id and self._cancel_intent_best_effort(intent_id):
            guard(PAYMENT, payment.status, "SUCCEEDED", "payment")
            payment.status = "SUCCEEDED"
            payment.succeeded_at = datetime.now(UTC)
            log.warning(
                "payment succeeded before its webhook arrived; refunding",
                extra={"order_id": order.order_id, "booking_status": order.booking_status},
            )
            self._request_refund(db, order, REASON_LATE_PAYMENT)
            return
        guard(PAYMENT, payment.status, "CANCELLED", "payment")
        payment.status = "CANCELLED"

    def _cancel_intent_best_effort(self, payment_intent_id: str) -> bool:
        """True when the intent had already succeeded."""
        try:
            return self._stripe.cancel_payment_intent(payment_intent_id)
        # Expiring the order is the point; a dead Stripe connection must not block it.
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            log.warning(
                "cancel intent failed (best-effort)",
                extra={"payment_intent_id": payment_intent_id, "error": str(exc)},
            )
            return False

    # ------------------------------------------------- consumer entry points

    def _dedupe(self, db: Session, envelope) -> bool:
        """Record the message_id as processed in the caller's transaction.

        Because it is committed (or rolled back) with the business change, a redelivery
        is a no-op only if the first delivery actually succeeded - a failed commit makes
        the next delivery reprocess, which is what at-least-once wants.
        """
        if db.get(ProcessedMessage, envelope.message_id) is not None:
            return True
        db.add(ProcessedMessage(message_id=envelope.message_id, event_type=envelope.message_type))
        return False

    def handle_event_cancelled(self, envelope) -> None:
        event_id = event_cancelled_fields(envelope.payload)  # PoisonMessage if unusable
        with self._sf() as db:
            if self._dedupe(db, envelope):
                db.commit()
                return
            orders = list(
                db.scalars(
                    select(Order)
                    .where(Order.event_id == event_id)
                    .where(Order.booking_status.in_(["PENDING_PAYMENT", "CONFIRMING", "CONFIRMED"]))
                )
            )
            for order in orders:
                # Cancellation takes precedence over an older payment success (TC §11):
                # refund any payment that went through, then cancel the booking. An
                # unpaid intent is cancelled so the buyer can no longer pay for it.
                #
                # CONFIRMED orders are refunded here too rather than waiting for
                # reservation's RefundRequired (decision D2, 2026-10-22). Reservation also
                # publishes RefundRequired for them; whichever message arrives first creates
                # the refund and the other is a no-op, because an order has at most one.
                if _payment_succeeded(order):
                    self._request_refund(db, order, REASON_EVENT_CANCELLED)
                else:
                    self._cancel_unpaid_payment(db, order)
                if order.booking_status != "CANCELLED":
                    guard(BOOKING, order.booking_status, "CANCELLED", "booking")
                    order.booking_status = "CANCELLED"
            db.commit()

    def handle_reservation_expired(self, envelope) -> None:
        """Reservation publishes this when a hold's TTL passes (HoldExpiryWorker), which
        expires the order early. The payment-deadline sweep (expire_stale_orders) still
        expires unpaid orders on its own, so a lost or late message changes nothing."""
        hold_id = reservation_expired_fields(envelope.payload)  # PoisonMessage if unusable
        with self._sf() as db:
            if self._dedupe(db, envelope):
                db.commit()
                return
            order = db.scalar(select(Order).where(Order.hold_id == hold_id))
            # Only a booking still waiting on payment/confirmation can expire; a final one,
            # or one parked for reconciliation, is left as it is (no requeue loop).
            if order is None or order.booking_status not in ("PENDING_PAYMENT", "CONFIRMING"):
                db.commit()
                return
            # A payment that never completed must not be chargeable: expire + cancel intent.
            if not _payment_succeeded(order):
                guard(BOOKING, order.booking_status, "EXPIRED", "booking")
                order.booking_status = "EXPIRED"
                self._cancel_unpaid_payment(db, order)
            db.commit()

    def handle_refund_required(self, envelope) -> None:
        fields = refund_required_fields(envelope.payload)  # PoisonMessage if unusable
        with self._sf() as db:
            if self._dedupe(db, envelope):
                db.commit()
                return
            order = db.get(Order, fields.order_id)
            if order is None:
                log.warning(
                    "RefundRequired for an unknown order; ignored",
                    extra={"order_id": fields.order_id, "hold_id": fields.hold_id},
                )
                db.commit()
                return
            if fields.hold_id != order.hold_id:
                # Reservation names a different hold than the one this order was priced
                # from: the records disagree. Dead-letter it for a person, change nothing.
                raise PoisonMessage(
                    f"RefundRequired hold_id {fields.hold_id} does not match order "
                    f"{order.order_id} (hold {order.hold_id})"
                )
            if (fields.total_amount, fields.currency) != (order.total_amount, order.currency):
                # Checkout's record of what was charged is authoritative for the refund.
                log.warning(
                    "RefundRequired amount differs from the order; refunding the order amount",
                    extra={
                        "order_id": order.order_id,
                        "message_amount": fields.total_amount,
                        "message_currency": fields.currency,
                        "order_amount": order.total_amount,
                        "order_currency": order.currency,
                    },
                )
            if _payment_succeeded(order):
                self._request_refund(db, order, REASON_REFUND_REQUIRED)
            # The money is going back, so the booking no longer stands - a CONFIRMED one
            # included. A booking that already ended, or awaits reconciliation, is left.
            if order.booking_status in ("PENDING_PAYMENT", "CONFIRMING", "CONFIRMED"):
                guard(BOOKING, order.booking_status, "CANCELLED", "booking")
                order.booking_status = "CANCELLED"
            db.commit()

    # ------------------------------------------------------------ shared bits

    def _request_refund(self, db: Session, order: Order, reason: str) -> Refund | None:
        """Record the refund obligation for a paid order (TC §7).

        An order has one payment, so it has at most one (full) refund: a second cause -
        an event cancelled mid-confirmation, then reservation's RefundRequired for the
        same hold - finds the first refund and records nothing new. `reason` is the
        first cause.
        """
        existing = db.scalar(select(Refund).where(Refund.order_id == order.order_id))
        if existing is not None or not _payment_succeeded(order):
            return existing
        refund = Refund(
            refund_id=str(uuid.uuid4()),
            order_id=order.order_id,
            amount=order.total_amount,
            currency=order.currency,
            reason=reason,
            idempotency_key=f"{order.order_id}:refund",
            status="REQUESTED",
        )
        db.add(refund)
        # Note: OrderRefunded is NOT emitted here. It is a success notification and is
        # published by _emit_refund_completed once Stripe has actually refunded (the
        # request merely records the obligation). See the refund decision, 2026-10-19.
        return refund

    def _emit_refund_completed(self, db: Session, order: Order, refund: Refund) -> None:
        self._outbox.write(
            db,
            message_type=EVENT_REFUND_COMPLETED,
            payload=refund_completed_data(order, refund),
            aggregate_id=refund.refund_id,
        )
        self._outbox.write(
            db,
            message_type=EVENT_ORDER_REFUNDED,
            payload=order_refunded_data(order, refund),
            aggregate_id=refund.refund_id,
        )

    def _emit_refund_failed(self, db: Session, order: Order, refund: Refund) -> None:
        self._outbox.write(
            db,
            message_type=EVENT_REFUND_FAILED,
            payload=refund_failed_data(order, refund),
            aggregate_id=refund.refund_id,
        )

    def _order_by_intent(self, db: Session, payment_intent_id: str) -> Order | None:
        return db.scalar(
            select(Order)
            .join(Payment, Payment.order_id == Order.order_id)
            .where(Payment.stripe_payment_intent_id == payment_intent_id)
        )

    def _ensure_intent(self, db: Session, order: Order) -> str | None:
        """The client secret the buyer pays an unpaid order with.

        Creates the payment intent if an earlier attempt failed before storing one;
        Stripe's idempotency key is the order id, so this can never make a second intent.
        A declined card (FAILED) can be retried on the same intent until the deadline. A
        paid or closed order has nothing left to pay, so it gets no secret.
        """
        payment = order.payment
        if payment is None or payment.status not in ("PENDING", "FAILED"):
            return None
        if order.booking_status != "PENDING_PAYMENT":
            return None
        if payment.stripe_payment_intent_id:
            try:
                return self._stripe.get_payment_intent(
                    payment.stripe_payment_intent_id
                ).client_secret
            except StripeUnavailableError:
                # Stripe is down; we still return the existing order (200) so the
                # client can keep polling. The secret is empty until it recovers.
                return ""
        try:
            created = self._stripe.create_payment_intent(
                order_id=order.order_id,
                hold_id=order.hold_id,
                amount=order.total_amount,
                currency=order.currency,
            )
        except StripeUnavailableError as exc:
            log.error(
                "stripe unavailable creating intent",
                extra={"order_id": order.order_id, "error": str(exc)},
            )
            raise ApiError(
                503, Code.STRIPE_UNAVAILABLE, "Payment could not be started. Please retry."
            ) from exc
        payment.stripe_payment_intent_id = created.payment_intent_id
        db.commit()
        return created.client_secret


# --------------------------------------------------------------------- helpers

REASON_EVENT_CANCELLED = "event_cancelled"
REASON_EXPIRED_PAYMENT = "expired_payment"
REASON_REFUND_REQUIRED = "refund_required"
REASON_CONFIRM_REJECTED = "confirm_rejected"
REASON_LATE_PAYMENT = "late_payment"


def _payment_succeeded(order: Order) -> bool:
    return bool(order.payment and order.payment.status == "SUCCEEDED")


def _first_refund_id(charge: dict) -> str | None:
    # Present only when the webhook's API version includes the charge's refunds list.
    refunds = (charge.get("refunds") or {}).get("data") or []
    return refunds[0].get("id") if refunds else None


def _stripe_reason(reason: str) -> str:
    # Stripe Refund.create accepts only 'duplicate', 'fraudulent' and 'requested_by_customer'
    # (the 'request_cancel' family is for PaymentIntent.cancel). None of our reasons is
    # a duplicate or fraud, so all map to requested_by_customer; our own reason stays on
    # the refund row.
    return {
        "event_cancelled": "requested_by_customer",
        "expired_payment": "requested_by_customer",
        "refund_required": "requested_by_customer",
        "confirm_rejected": "requested_by_customer",
        "late_payment": "requested_by_customer",
    }.get(reason, "requested_by_customer")


def _body_hash(hold_id: str, delivery_email: str) -> str:
    raw = json.dumps({"hold_id": hold_id, "delivery_email": delivery_email}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _update_idem_order(key: str, order_id: str):
    return update(IdempotencyKey).where(IdempotencyKey.key == key).values(order_id=order_id)
