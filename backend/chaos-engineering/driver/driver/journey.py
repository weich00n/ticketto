"""One buyer's whole purchase (scenario S2), the same calls the frontend makes:

    join the queue -> poll until admitted -> hold 1 ticket -> create the order
    -> pay with Stripe's test card (server side, so Stripe sends the real webhook)
    -> poll the order until it settles -> poll until the ticket is issued

The result is one outcome tag (where the buyer ended up) plus each step's time and failed attempts.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import requests

from .api import STRIPE_API, Gateway, Step, StepFailed, call

SETTLED = {"CONFIRMED", "EXPIRED", "CANCELLED", "RECONCILIATION"}


@dataclass
class Limits:
    queue: float = 900.0     # waiting for admission
    step: float = 60.0       # each retried call
    settle: float = 600.0    # the order reaching a final status (C3 stops reservation for minutes)
    ticket: float = 300.0    # the ticket appearing once confirmed (C6 stops the ticket worker)
    pay_at: datetime | None = None  # hold and order, then pay together (C3: confirm during the outage)
    pay_slots: threading.Semaphore | None = None  # caps Stripe calls at once (test-mode rate limit)


@dataclass
class Result:
    buyer: str
    started_at: str
    outcome: str = "unknown"
    detail: str = ""
    order_id: str | None = None
    hold_id: str | None = None
    order_status: str | None = None
    ticket_issued: bool = False
    steps: dict[str, dict] = field(default_factory=dict)

    def record(self, name: str, step: Step) -> None:
        step.done()
        self.steps[name] = {"ms": step.ms, "errors": step.errors}

    def as_dict(self) -> dict:
        return asdict(self)


def run(gw: Gateway, stripe_key: str, event_id: str, category_id: str, delivery_email: str,
        buyer: str, limits: Limits) -> Result:
    result = Result(buyer=buyer, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    name = "start"
    try:
        name = "queue"
        token = _admission(gw, event_id, result, limits)
        name = "hold"
        _hold(gw, event_id, category_id, token, result, limits)
        name = "order"
        secret = _order(gw, delivery_email, result, limits)
        name = "pay"
        if limits.pay_at:
            time.sleep(max(0.0, (limits.pay_at - datetime.now(timezone.utc)).total_seconds()))
        _pay(stripe_key, secret, result, limits)
        name = "settle"
        _settle(gw, result, limits)
        if result.order_status != "CONFIRMED":
            result.outcome = f"order_{result.order_status.lower()}"
            return result
        name = "ticket"
        _ticket(gw, result, limits)
        result.outcome = "confirmed" if result.ticket_issued else "confirmed_no_ticket"
    except StepFailed as exc:
        result.outcome = f"{name}_failed"
        result.detail = exc.tag
    except Exception as exc:  # a bug in the driver, or an answer it doesn't understand
        result.outcome = f"{name}_crashed"
        result.detail = repr(exc)[:300]
    return result


def _admission(gw: Gateway, event_id: str, result: Result, limits: Limits) -> str:
    step = Step()
    base = gw.url(f"/api/v1/events/{event_id}/queue")
    deadline = time.monotonic() + limits.queue
    try:
        entry = call(gw.session, "POST", base + "/entries", step, budget=limits.step).json()
        while True:
            if entry.get("entry_status") == "ADMITTED" and entry.get("admission_token"):
                return entry["admission_token"]
            if entry.get("entry_status") == "NONE":
                raise StepFailed(f"queue_{entry.get('queue_status', 'closed').lower()}")
            if time.monotonic() > deadline:
                raise StepFailed("queue_timeout")
            time.sleep(entry.get("poll_after_seconds") or 5)
            entry = call(gw.session, "GET", base + "/me", step, budget=limits.step).json()
    finally:
        result.record("queue", step)


def _hold(gw: Gateway, event_id: str, category_id: str, token: str, result: Result, limits: Limits) -> None:
    step = Step()
    try:
        hold = call(gw.session, "POST", gw.url("/api/v1/reservations"), step, budget=limits.step,
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                    json={"event_id": event_id, "category_id": category_id, "quantity": 1,
                          "admission_token": token}).json()
    finally:
        result.record("hold", step)
    result.hold_id = hold["hold_id"]


def _order(gw: Gateway, delivery_email: str, result: Result, limits: Limits) -> str:
    step = Step()
    try:
        order = call(gw.session, "POST", gw.url("/api/v1/orders"), step, budget=limits.step,
                     headers={"Idempotency-Key": f"hold-{result.hold_id}"},
                     json={"hold_id": result.hold_id, "delivery_email": delivery_email}).json()
    finally:
        result.record("order", step)
    result.order_id = order["order_id"]
    result.order_status = order["status"]
    if not order.get("client_secret"):
        raise StepFailed(f"no_client_secret_{order['status'].lower()}")
    return order["client_secret"]


def _pay(stripe_key: str, client_secret: str, result: Result, limits: Limits) -> None:
    step = Step()
    intent_id = client_secret.split("_secret_")[0]
    try:
        with limits.pay_slots or contextlib.nullcontext(), requests.Session() as stripe:
            stripe.auth = (stripe_key, "")
            call(stripe, "POST", f"{STRIPE_API}/payment_intents/{intent_id}/confirm", step,
                 budget=limits.step, ok=(200,),
                 headers={"Idempotency-Key": f"chaos-confirm-{result.order_id}"},
                 data={"payment_method": "pm_card_visa"})
    finally:
        result.record("pay", step)


def _settle(gw: Gateway, result: Result, limits: Limits) -> None:
    step = Step()
    deadline = time.monotonic() + limits.settle
    try:
        while True:
            order = call(gw.session, "GET", gw.url(f"/api/v1/orders/{result.order_id}"), step,
                         budget=limits.step, ok=(200,)).json()
            result.order_status = order["status"]
            if order["status"] in SETTLED:
                return
            if time.monotonic() > deadline:
                raise StepFailed(f"stuck_{order['status'].lower()}")
            time.sleep(3)
    finally:
        result.record("settle", step)


def _ticket(gw: Gateway, result: Result, limits: Limits) -> None:
    step = Step()
    deadline = time.monotonic() + limits.ticket
    try:
        while time.monotonic() < deadline:
            page = call(gw.session, "GET", gw.url("/api/v1/tickets"), step, budget=limits.step,
                        ok=(200,), params={"page_size": 100}).json()
            if any(t.get("order_id") == result.order_id for t in page.get("items", [])):
                result.ticket_issued = True
                return
            time.sleep(5)
    finally:
        result.record("ticket", step)
