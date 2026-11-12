from dataclasses import dataclass
from typing import Any

from .correlation import get_correlation_id


@dataclass
class ApiError(Exception):
    status_code: int
    code: str
    message: str
    details: dict[str, str] | None = None

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error, "correlation_id": get_correlation_id()}


def validation_error(message: str, details: dict[str, str] | None = None) -> ApiError:
    return ApiError(400, "VALIDATION_ERROR", message, details)


# The business-state codes from Technical Contract section 10, each paired with the one
# HTTP status the contract assigns to it, so no route can invent a code or pair one with
# the wrong status. (Section 10 folds expired and cancelled into 409.)
class Code:
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    HOLD_NOT_FOUND = "HOLD_NOT_FOUND"
    HOLD_EXPIRED = "HOLD_EXPIRED"
    EVENT_CANCELLED = "EVENT_CANCELLED"
    ORDER_STATE_CONFLICT = "ORDER_STATE_CONFLICT"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    STRIPE_UNAVAILABLE = "STRIPE_UNAVAILABLE"
    RESERVATION_UNAVAILABLE = "RESERVATION_UNAVAILABLE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
