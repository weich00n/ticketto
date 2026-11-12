"""A local-development stand-in for reservation-service's internal API.

The local compose stack runs this instead of the Java service (which needs Redis,
MySQL and Queue's signing key). This dependency-free stdlib server implements the two
internal endpoints checkout calls (see app/clients/reservation.py), with the same
bodies, status codes and error shape as the real service:

    GET  /internal/v1/reservations/{hold_id}          -> InternalHoldView
    POST /internal/v1/reservations/{hold_id}/confirm  -> ConfirmView

Every hold belongs to `local-dev-user` (the GET carries no buyer, so the stub cannot
echo one): send `X-User-Sub: local-dev-user` to buy. Confirmations are remembered in
memory, so a repeat for the same order answers `duplicate: true` and a different order
gets 409 ORDER_MISMATCH, exactly like the real service.

The hold id's prefix picks one of the real service's other answers, so each checkout
path can be exercised by hand:

    expired-...    GET status EXPIRED;   confirm 409 HOLD_EXPIRED
    cancelled-...  GET status CANCELLED; confirm 409 EVENT_CANCELLED
    gone-...       GET 404 HOLD_NOT_FOUND; confirm 404 HOLD_NOT_FOUND
    noevent-...    GET ACTIVE;           confirm 404 EVENT_NOT_FOUND
    busy-...       GET and confirm 503 DEPENDENCY_UNAVAILABLE
    anything else  ACTIVE, and confirm succeeds

A blank order_id or payment_reference answers 400 INVALID_REQUEST (@NotBlank).
No auth, no persistence. Local development only.

    python scripts/dev/reservation_stub.py        # listens on :8081
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCAL_USER = "local-dev-user"
LOCAL_EVENT = "5f0c1e4a-0000-4000-8000-00000000e001"
LOCAL_CATEGORY = "5f0c1e4a-0000-4000-8000-00000000c001"

_lock = threading.Lock()
_confirmed: dict[str, dict] = {}  # hold_id -> {"order_id", "confirmed_at"}


def _in(seconds: int) -> str:
    """A UTC ISO-8601 timestamp `seconds` from now (the shape reservation returns)."""
    return ((datetime.now(UTC).replace(microsecond=0)) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _error(code: str, message: str) -> dict:
    # GlobalExceptionHandler's body.
    return {"error": {"code": code, "message": message}, "correlation_id": str(uuid.uuid4())}


def _status_for(hold_id: str) -> str:
    if hold_id in _confirmed:
        return "CONFIRMED"
    if hold_id.startswith("expired-"):
        return "EXPIRED"
    if hold_id.startswith("cancelled-"):
        return "CANCELLED"
    return "ACTIVE"


def _hold(hold_id: str) -> dict:
    # snake_case with null fields left out, exactly as reservation-service's Jackson
    # settings serialise InternalHoldView (order_id is absent until confirmed).
    body = {
        "hold_id": hold_id,
        "user_id": LOCAL_USER,
        "event_id": LOCAL_EVENT,
        "category_id": LOCAL_CATEGORY,
        "admission_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"admission:{hold_id}")),
        "status": _status_for(hold_id),
        "quantity": 1,
        "unit_amount": 100000,
        "total_amount": 100000,
        "currency": "SGD",
        "payment_expires_at": _in(900),
        "hold_expires_at": _in(900),
        "server_time": _in(0),
    }
    if hold_id in _confirmed:
        body["order_id"] = _confirmed[hold_id]["order_id"]
    return body


def _confirm(hold_id: str, req: dict) -> tuple[int, dict]:  # pylint: disable=too-many-return-statements
    order_id = req.get("order_id")
    payment_reference = req.get("payment_reference")
    if not (isinstance(order_id, str) and order_id.strip()):
        return 400, _error("INVALID_REQUEST", "orderId must not be blank.")
    if not (isinstance(payment_reference, str) and payment_reference.strip()):
        return 400, _error("INVALID_REQUEST", "paymentReference must not be blank.")
    if hold_id.startswith("gone-"):
        return 404, _error("HOLD_NOT_FOUND", "No such hold.")
    if hold_id.startswith("busy-"):
        return 503, _error("DEPENDENCY_UNAVAILABLE", "Please retry shortly.")
    with _lock:
        done = _confirmed.get(hold_id)
        if done is not None:  # checked first, like asSettledConfirmation
            if done["order_id"] != order_id:
                return 409, _error(
                    "ORDER_MISMATCH", "This hold is already confirmed for a different order."
                )
            return 200, {
                "hold_id": hold_id,
                "order_id": order_id,
                "status": "CONFIRMED",
                "quantity": 1,
                "duplicate": True,
                "confirmed_at": done["confirmed_at"],
            }
        if hold_id.startswith("noevent-"):
            return 404, _error("EVENT_NOT_FOUND", "No such event.")
        if hold_id.startswith("cancelled-"):
            return 409, _error("EVENT_CANCELLED", "Event has been cancelled.")
        if hold_id.startswith("expired-"):
            return 409, _error("HOLD_EXPIRED", "Your reservation has expired.")
        confirmed_at = _in(0)
        _confirmed[hold_id] = {"order_id": order_id, "confirmed_at": confirmed_at}
    return 200, {
        "hold_id": hold_id,
        "order_id": order_id,
        "status": "CONFIRMED",
        "quantity": 1,
        "duplicate": False,
        "confirmed_at": confirmed_at,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        cid = self.headers.get("X-Correlation-Id")
        if cid:
            self.send_header("X-Correlation-Id", cid)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # pylint: disable=invalid-name  # http.server's API
        # /internal/v1/reservations/{hold_id}
        parts = self.path.split("/")
        if len(parts) == 5 and parts[1] == "internal" and parts[3] == "reservations":
            hold_id = parts[4]
            if hold_id.startswith("gone-"):
                self._send(404, _error("HOLD_NOT_FOUND", "No such hold."))
            elif hold_id.startswith("busy-"):
                self._send(503, _error("DEPENDENCY_UNAVAILABLE", "Please retry shortly."))
            else:
                self._send(200, _hold(hold_id))
        else:
            self._send(404, _error("NOT_FOUND", "no such route"))

    def do_POST(self) -> None:  # pylint: disable=invalid-name  # http.server's API
        # /internal/v1/reservations/{hold_id}/confirm
        parts = self.path.split("/")
        if (
            len(parts) == 6
            and parts[1] == "internal"
            and parts[3] == "reservations"
            and parts[5] == "confirm"
        ):
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._send(400, _error("INVALID_REQUEST", "The request body is not valid JSON."))
                return
            self._send(*_confirm(parts[4], req if isinstance(req, dict) else {}))
        else:
            self._send(404, _error("NOT_FOUND", "no such route"))

    def log_message(self, *args) -> None:  # keep the local stack's console quiet
        pass


def main() -> None:
    port = int(os.environ.get("RESERVATION_PORT", "8081"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
