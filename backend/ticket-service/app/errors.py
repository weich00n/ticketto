from dataclasses import dataclass
from typing import Any

from .correlation import get_correlation_id


@dataclass
class ApiError(Exception):
    status_code: int
    code: str
    message: str
    details: dict[str, Any] | None = None

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error, "correlation_id": get_correlation_id()}


def validation_error(message: str, details: dict[str, str] | None = None) -> ApiError:
    return ApiError(400, "VALIDATION_ERROR", message, details)


def ticket_not_found() -> ApiError:
    return ApiError(404, "TICKET_NOT_FOUND", "Ticket not found.")


def order_not_found() -> ApiError:
    return ApiError(404, "ORDER_NOT_FOUND", "No confirmed order with this ID has been received.")


def invalid_qr_code() -> ApiError:
    return ApiError(400, "INVALID_QR_CODE", "This QR code is not a valid Ticketto ticket.")
