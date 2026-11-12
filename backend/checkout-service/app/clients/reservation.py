"""Client for reservation-service (Java, :8081).

Checkout makes exactly two internal calls (the TC keeps internal calls to two):
  GET  /internal/v1/reservations/{hold_id}        -> InternalHoldView
  POST /internal/v1/reservations/{hold_id}/confirm (order_id, payment_reference) -> ConfirmView

Both are service-to-service on private addresses, so they carry no auth header. The
Java service applies a global snake_case JSON strategy (with null fields omitted) in
both directions. This module maps every response those two endpoints can return to a
value object or a typed exception, so the service layer decides each outcome on purpose.

What reservation-service can answer (ReservationService, ErrorCode, GlobalExceptionHandler):

  GET  200 (status ACTIVE | EXPIRED | CONFIRMED | CANCELLED), 404 HOLD_NOT_FOUND,
       503 DEPENDENCY_UNAVAILABLE, 500 INTERNAL_ERROR
  POST 200 (duplicate true/false), 400 INVALID_REQUEST, 404 HOLD_NOT_FOUND,
       404 EVENT_NOT_FOUND, 409 ORDER_MISMATCH | HOLD_EXPIRED | EVENT_CANCELLED,
       503 DEPENDENCY_UNAVAILABLE, 500 INTERNAL_ERROR

Error bodies are {"error": {"code", "message"}, "correlation_id"}. The caller's
X-Correlation-Id is forwarded so one id spans checkout's and reservation's logs.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import requests

from ..correlation import HEADER as CORRELATION_HEADER
from ..correlation import get_correlation_id

# --- errors ------------------------------------------------------------------


class ReservationError(Exception):
    """A transient failure (connect, timeout, 5xx incl. 503 DEPENDENCY_UNAVAILABLE).

    Never a business answer: the caller retries (or answers 503) and assumes nothing.
    """


class ReservationContractError(Exception):
    """Reservation answered something this client does not expect: a 400
    INVALID_REQUEST (checkout sent a bad request), an unknown 4xx, or a success body
    missing a field. A bug on one side of the contract, never a reason to refund."""

    def __init__(self, hold_id: str, detail: str) -> None:
        super().__init__(f"{detail}: {hold_id}")
        self.hold_id = hold_id
        self.detail = detail


class HoldNotFoundError(Exception):
    """404 on confirm: HOLD_NOT_FOUND, or EVENT_NOT_FOUND when reservation has no
    sale-status row for the hold's event. Either way reservation's records do not match
    an order checkout created from that very hold, so a person must look at it."""

    def __init__(self, hold_id: str, code: str = "HOLD_NOT_FOUND") -> None:
        super().__init__(f"{code}: {hold_id}")
        self.hold_id = hold_id
        self.code = code


class HoldExpiredError(Exception):
    """HOLD_EXPIRED (409): the payment deadline passed."""

    def __init__(self, hold_id: str) -> None:
        super().__init__(hold_id)
        self.hold_id = hold_id


class HoldCancelledError(Exception):
    """EVENT_CANCELLED (409): the event was cancelled, so the hold can't be confirmed."""

    def __init__(self, hold_id: str) -> None:
        super().__init__(hold_id)
        self.hold_id = hold_id


class HoldOrderMismatchError(Exception):
    """ORDER_MISMATCH (409): this hold is already confirmed for a different order."""

    def __init__(self, hold_id: str) -> None:
        super().__init__(hold_id)
        self.hold_id = hold_id


class HoldNotActiveError(Exception):
    """Any other 409 (e.g. HOLD_NOT_ACTIVE): a definitive "this hold can't be confirmed"."""

    def __init__(self, hold_id: str, code: str) -> None:
        super().__init__(f"{code}: {hold_id}")
        self.hold_id = hold_id
        self.code = code


# --- value types -------------------------------------------------------------


@dataclass(frozen=True)
class HoldView:  # pylint: disable=too-many-instance-attributes  # mirrors InternalHoldView
    """The authoritative figures checkout prices an order from (InternalHoldView)."""

    hold_id: str
    user_id: str
    event_id: str
    category_id: str | None
    admission_id: str | None
    status: str
    quantity: int
    unit_amount: int  # minor units, SGD
    total_amount: int  # minor units, SGD
    currency: str
    order_id: str | None
    payment_expires_at: datetime
    hold_expires_at: datetime
    server_time: datetime

    @property
    def is_active(self) -> bool:
        return self.status == "ACTIVE"


@dataclass(frozen=True)
class ConfirmView:
    """What checkout learns from confirming (ConfirmView)."""

    hold_id: str
    order_id: str
    status: str
    quantity: int
    duplicate: bool
    confirmed_at: datetime


class ReservationClient(Protocol):
    def get_hold(self, hold_id: str) -> HoldView | None: ...
    def confirm(self, hold_id: str, order_id: str, payment_reference: str) -> ConfirmView: ...


# --- helpers -----------------------------------------------------------------


def _parse_dt(value: Any) -> datetime:
    """ISO 8601 from reservation's JSON, with or without a Z suffix."""
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _hold_view(d: dict) -> HoldView:
    return HoldView(
        hold_id=d["hold_id"],
        user_id=d["user_id"],
        event_id=d["event_id"],
        category_id=d.get("category_id"),
        admission_id=d.get("admission_id"),
        status=d["status"],
        quantity=int(d["quantity"]),
        unit_amount=int(d["unit_amount"]),
        total_amount=int(d["total_amount"]),
        currency=d["currency"],
        order_id=d.get("order_id"),
        payment_expires_at=_parse_dt(d["payment_expires_at"]),
        hold_expires_at=_parse_dt(d["hold_expires_at"]),
        server_time=_parse_dt(d["server_time"]),
    )


def _confirm_view(d: dict) -> ConfirmView:
    return ConfirmView(
        hold_id=d["hold_id"],
        order_id=d["order_id"],
        status=d["status"],
        quantity=int(d["quantity"]),
        duplicate=bool(d["duplicate"]),
        confirmed_at=_parse_dt(d["confirmed_at"]),
    )


# --- implementation ------------------------------------------------------------


class HttpReservationClient:
    """The real client: synchronous requests against the reservation base URL."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_hold(self, hold_id: str) -> HoldView | None:
        resp = self._request("GET", f"/internal/v1/reservations/{hold_id}", body=None)
        status = resp.status_code
        if status == 404:
            return None
        if status >= 500 or status in (401, 403):
            # 401/403 never come from reservation itself (internal routes have no auth);
            # they mean something in between refused us, which is an outage, not an answer.
            raise ReservationError(f"reservation unavailable on hold read: {status}")
        # The real GET only answers 200/404/5xx: an EXPIRED or CANCELLED hold is a 200
        # with that status. These 409 branches keep the client honest if that changes.
        code = self._error_code(resp)
        if status == 409 and code == "HOLD_EXPIRED":
            raise HoldExpiredError(hold_id)
        if status == 409 and code == "EVENT_CANCELLED":
            raise HoldCancelledError(hold_id)
        if status == 409:
            raise HoldNotActiveError(hold_id, code or "HOLD_NOT_ACTIVE")
        if status != 200:
            raise ReservationContractError(hold_id, f"unexpected {status} {code or ''}".strip())
        return self._parse(hold_id, resp, _hold_view)

    def confirm(self, hold_id: str, order_id: str, payment_reference: str) -> ConfirmView:
        resp = self._request(
            "POST",
            f"/internal/v1/reservations/{hold_id}/confirm",
            # ConfirmRequest(order_id, payment_reference), both @NotBlank.
            body={"order_id": order_id, "payment_reference": payment_reference},
        )
        status = resp.status_code
        if status >= 500 or status in (401, 403):
            # Includes 503 DEPENDENCY_UNAVAILABLE, which reservation also answers when
            # another confirmation for this hold is in flight: retry, never refund.
            raise ReservationError(f"reservation unavailable on confirm: {status}")
        code = self._error_code(resp)
        if status == 404:
            raise HoldNotFoundError(hold_id, code or "HOLD_NOT_FOUND")
        if status == 409 and code == "HOLD_EXPIRED":
            raise HoldExpiredError(hold_id)
        if status == 409 and code == "EVENT_CANCELLED":
            raise HoldCancelledError(hold_id)
        if status == 409 and code == "ORDER_MISMATCH":
            raise HoldOrderMismatchError(hold_id)
        if status == 409:
            raise HoldNotActiveError(hold_id, code or "HOLD_NOT_ACTIVE")
        if status != 200:
            # 400 INVALID_REQUEST: our request broke ConfirmRequest's validation.
            raise ReservationContractError(hold_id, f"unexpected {status} {code or ''}".strip())
        return self._parse(hold_id, resp, _confirm_view)

    @staticmethod
    def _parse(hold_id: str, resp: "requests.Response", build):
        try:
            return build(resp.json())
        except (ValueError, KeyError, TypeError) as exc:
            raise ReservationContractError(
                hold_id, f"malformed success body ({type(exc).__name__}: {exc})"
            ) from exc

    @staticmethod
    def _error_code(resp: "requests.Response") -> str | None:
        if not resp.content:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        error = body.get("error", {}) if isinstance(body, dict) else {}
        return error.get("code") if isinstance(error, dict) else None

    def _request(self, method: str, path: str, body: dict | None) -> "requests.Response":
        headers = {}
        cid = get_correlation_id()
        if cid and cid != "-":
            headers[CORRELATION_HEADER] = cid
        try:
            return requests.request(
                method,
                self.base_url + path,
                json=body,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ReservationError(str(exc)) from exc
