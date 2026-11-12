"""The HTTP reservation client against reservation-service's real wire format: snake_case
bodies (SNAKE_CASE naming strategy) with null fields omitted (non_null inclusion)."""

import json

import pytest
import requests

from app.clients import reservation
from app.clients.reservation import (
    HoldCancelledError,
    HoldExpiredError,
    HoldNotActiveError,
    HoldNotFoundError,
    HoldOrderMismatchError,
    HttpReservationClient,
    ReservationContractError,
    ReservationError,
)
from app.correlation import correlation_id_var

# InternalHoldView as Jackson writes it for an ACTIVE hold: no order_id yet.
HOLD = {
    "hold_id": "h-1",
    "user_id": "u-1",
    "event_id": "e-1",
    "category_id": "c-1",
    "admission_id": "a-1",
    "status": "ACTIVE",
    "quantity": 2,
    "unit_amount": 5000,
    "total_amount": 10000,
    "currency": "SGD",
    "payment_expires_at": "2026-11-20T12:05:00Z",
    "hold_expires_at": "2026-11-20T12:05:00Z",
    "server_time": "2026-11-20T12:00:00Z",
}


class _Resp:
    def __init__(self, status: int, body: dict | None) -> None:
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode() if body is not None else b""

    def json(self):
        return self._body


@pytest.fixture
def calls(monkeypatch):
    """Records requests and replays the queued responses in order."""
    log: list[dict] = []
    replies: list[_Resp] = []

    def fake_request(  # pylint: disable=redefined-outer-name
        method, url, json=None, headers=None, timeout=None
    ):
        log.append(
            {"method": method, "url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        return replies.pop(0)

    monkeypatch.setattr(reservation.requests, "request", fake_request)
    return log, replies


def test_get_hold_parses_snake_case(calls):
    log, replies = calls
    replies.append(_Resp(200, HOLD))
    hold = HttpReservationClient("http://res:8081/").get_hold("h-1")
    assert log[0]["method"] == "GET"
    assert log[0]["url"] == "http://res:8081/internal/v1/reservations/h-1"
    assert hold.hold_id == "h-1"
    assert hold.user_id == "u-1"
    assert hold.admission_id == "a-1"
    assert hold.quantity == 2
    assert hold.unit_amount == 5000
    assert hold.total_amount == 10000
    assert hold.order_id is None  # omitted by non_null inclusion
    assert hold.payment_expires_at.tzinfo is not None


def test_get_hold_tolerates_omitted_optional_fields(calls):
    _, replies = calls
    body = {k: v for k, v in HOLD.items() if k not in ("category_id", "admission_id")}
    replies.append(_Resp(200, body))
    hold = HttpReservationClient("http://res:8081").get_hold("h-1")
    assert hold.category_id is None
    assert hold.admission_id is None


def test_get_hold_not_found_is_none(calls):
    _, replies = calls
    replies.append(_Resp(404, {"error": {"code": "HOLD_NOT_FOUND", "message": "x"}}))
    assert HttpReservationClient("http://res:8081").get_hold("h-1") is None


def test_confirm_sends_snake_case_body_and_parses_view(calls):
    log, replies = calls
    replies.append(
        _Resp(
            200,
            {
                "hold_id": "h-1",
                "order_id": "o-1",
                "status": "CONFIRMED",
                "quantity": 2,
                "duplicate": True,
                "confirmed_at": "2026-11-20T12:01:00Z",
            },
        )
    )
    view = HttpReservationClient("http://res:8081").confirm("h-1", "o-1", "pi_123")
    assert log[0]["method"] == "POST"
    assert log[0]["url"] == "http://res:8081/internal/v1/reservations/h-1/confirm"
    # ConfirmRequest(order_id, payment_reference), both @NotBlank on reservation's side.
    assert log[0]["json"] == {"order_id": "o-1", "payment_reference": "pi_123"}
    assert view.order_id == "o-1"
    assert view.duplicate is True


@pytest.mark.parametrize(
    ("code", "error"),
    [("HOLD_EXPIRED", HoldExpiredError), ("ORDER_MISMATCH", HoldOrderMismatchError)],
)
def test_confirm_maps_conflict_codes(calls, code, error):
    _, replies = calls
    replies.append(_Resp(409, {"error": {"code": code, "message": "x"}, "correlation_id": "c"}))
    with pytest.raises(error):
        HttpReservationClient("http://res:8081").confirm("h-1", "o-1", "pi_123")


def test_connection_failure_is_transient(monkeypatch):
    def boom(*_args, **_kwargs):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(reservation.requests, "request", boom)
    with pytest.raises(ReservationError):
        HttpReservationClient("http://res:8081").get_hold("h-1")


# ---------------------------------------------------------------------------
# Every response reservation-service's two internal endpoints can return
# (ReservationService, ErrorCode, GlobalExceptionHandler), each mapped on purpose.
# Bodies are exactly what GlobalExceptionHandler writes.


def _error(code: str) -> dict:
    return {"error": {"code": code, "message": "x"}, "correlation_id": "c-1"}


CONFIRM_VIEW = {
    "hold_id": "h-1",
    "order_id": "o-1",
    "status": "CONFIRMED",
    "quantity": 2,
    "duplicate": False,
    "confirmed_at": "2026-11-20T12:01:00Z",
}


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (404, _error("HOLD_NOT_FOUND"), HoldNotFoundError),
        (404, _error("EVENT_NOT_FOUND"), HoldNotFoundError),
        (409, _error("HOLD_EXPIRED"), HoldExpiredError),
        (409, _error("EVENT_CANCELLED"), HoldCancelledError),
        (409, _error("ORDER_MISMATCH"), HoldOrderMismatchError),
        (409, _error("HOLD_NOT_ACTIVE"), HoldNotActiveError),
        (400, _error("INVALID_REQUEST"), ReservationContractError),
        (503, _error("DEPENDENCY_UNAVAILABLE"), ReservationError),
        (500, _error("INTERNAL_ERROR"), ReservationError),
    ],
)
def test_confirm_maps_every_reservation_response(calls, status, body, expected):
    _, replies = calls
    replies.append(_Resp(status, body))
    with pytest.raises(expected):
        HttpReservationClient("http://res:8081").confirm("h-1", "o-1", "pi_123")


def test_confirm_404_keeps_the_reason_code(calls):
    _, replies = calls
    replies.append(_Resp(404, _error("EVENT_NOT_FOUND")))
    with pytest.raises(HoldNotFoundError) as info:
        HttpReservationClient("http://res:8081").confirm("h-1", "o-1", "pi_123")
    assert info.value.code == "EVENT_NOT_FOUND"


def test_confirm_first_success_is_not_a_duplicate(calls):
    _, replies = calls
    replies.append(_Resp(200, CONFIRM_VIEW))
    view = HttpReservationClient("http://res:8081").confirm("h-1", "o-1", "pi_123")
    assert view.duplicate is False
    assert view.status == "CONFIRMED"
    assert view.confirmed_at.tzinfo is not None


def test_confirm_malformed_success_body_is_a_contract_error(calls):
    _, replies = calls
    replies.append(_Resp(200, {k: v for k, v in CONFIRM_VIEW.items() if k != "duplicate"}))
    with pytest.raises(ReservationContractError):
        HttpReservationClient("http://res:8081").confirm("h-1", "o-1", "pi_123")


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (503, _error("DEPENDENCY_UNAVAILABLE"), ReservationError),
        (500, _error("INTERNAL_ERROR"), ReservationError),
        (400, _error("INVALID_REQUEST"), ReservationContractError),
    ],
)
def test_get_hold_maps_every_error_response(calls, status, body, expected):
    _, replies = calls
    replies.append(_Resp(status, body))
    with pytest.raises(expected):
        HttpReservationClient("http://res:8081").get_hold("h-1")


@pytest.mark.parametrize("status", ["ACTIVE", "EXPIRED", "CONFIRMED", "CANCELLED"])
def test_get_hold_reports_every_hold_status_as_200(calls, status):
    # An ended hold is still a 200 from the real service (observedStatus), not a 409.
    _, replies = calls
    body = dict(HOLD, status=status)
    if status == "CONFIRMED":
        body["order_id"] = "o-1"
    replies.append(_Resp(200, body))
    hold = HttpReservationClient("http://res:8081").get_hold("h-1")
    assert hold.status == status
    assert hold.is_active is (status == "ACTIVE")
    assert hold.order_id == ("o-1" if status == "CONFIRMED" else None)


def test_get_hold_malformed_body_is_a_contract_error(calls):
    _, replies = calls
    replies.append(_Resp(200, {k: v for k, v in HOLD.items() if k != "server_time"}))
    with pytest.raises(ReservationContractError):
        HttpReservationClient("http://res:8081").get_hold("h-1")


def test_correlation_id_is_forwarded_on_both_calls(calls):
    log, replies = calls
    replies.append(_Resp(200, HOLD))
    replies.append(_Resp(200, CONFIRM_VIEW))
    token = correlation_id_var.set("corr-123")
    try:
        client = HttpReservationClient("http://res:8081")
        client.get_hold("h-1")
        client.confirm("h-1", "o-1", "pi_123")
    finally:
        correlation_id_var.reset(token)
    assert [c["headers"] for c in log] == [{"X-Correlation-Id": "corr-123"}] * 2


def test_no_correlation_header_outside_a_request(calls):
    log, replies = calls
    replies.append(_Resp(200, HOLD))
    HttpReservationClient("http://res:8081").get_hold("h-1")
    assert log[0]["headers"] == {}
