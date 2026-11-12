package com.ticketing.reservation_service.dto;

/**
 * The section 10 error body, returned by every endpoint:
 *
 * <pre>
 * { "error": { "code": "HOLD_EXPIRED", "message": "..." }, "correlation_id": "uuid" }
 * </pre>
 */
public record ErrorResponse(ErrorBody error, String correlationId) {

	public record ErrorBody(String code, String message) {
	}

	public static ErrorResponse of(String code, String message, String correlationId) {
		return new ErrorResponse(new ErrorBody(code, message), correlationId);
	}
}
