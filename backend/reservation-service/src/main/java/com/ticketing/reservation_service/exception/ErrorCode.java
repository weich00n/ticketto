package com.ticketing.reservation_service.exception;

import org.springframework.http.HttpStatus;

/**
 * Every error this service may return, each bound to its HTTP status, so no endpoint can
 * invent a code or pair a code with the wrong status.
 *
 * <p>Note there is no 410: the contract folds expired and cancelled into 409.
 */
public enum ErrorCode {

	INVALID_REQUEST(HttpStatus.BAD_REQUEST, "The request is malformed."),
	UNAUTHENTICATED(HttpStatus.UNAUTHORIZED, "A valid access token is required."),
	INVALID_ADMISSION(HttpStatus.FORBIDDEN, "Admission token is invalid, or not for this buyer and event."),
	SELECTION_DEADLINE_PASSED(HttpStatus.FORBIDDEN, "The selection deadline for this admission has passed."),
	EVENT_NOT_FOUND(HttpStatus.NOT_FOUND, "No such event."),
	CATEGORY_NOT_FOUND(HttpStatus.NOT_FOUND, "That category does not exist for this event."),
	HOLD_NOT_FOUND(HttpStatus.NOT_FOUND, "No such hold."),
	INSUFFICIENT_INVENTORY(HttpStatus.CONFLICT, "Not enough tickets available in this category."),
	PURCHASE_LIMIT_EXCEEDED(HttpStatus.CONFLICT, "This buyer has reached the per-event ticket limit."),
	HOLD_EXPIRED(HttpStatus.CONFLICT, "Your reservation has expired."),
	EVENT_CANCELLED(HttpStatus.CONFLICT, "Event has been cancelled."),
	ADMISSION_ALREADY_HAS_HOLD(HttpStatus.CONFLICT, "This admission already created a hold."),
	ORDER_MISMATCH(HttpStatus.CONFLICT, "This hold is already confirmed for a different order."),
	IDEMPOTENCY_KEY_CONFLICT(HttpStatus.CONFLICT, "That Idempotency-Key was used with a different payload."),
	HOLD_NOT_ACTIVE(HttpStatus.CONFLICT, "This hold is no longer active."),
	DEPENDENCY_UNAVAILABLE(HttpStatus.SERVICE_UNAVAILABLE, "Please retry shortly."),

	/**
	 * Not in the section 10 table. Only seeding raises it - the internal route, or an
	 * EventPublished that contradicts the stored inventory, which is dead-lettered - so this code
	 * never reaches a buyer.
	 */
	INVENTORY_CONFLICT(HttpStatus.CONFLICT, "This event's inventory is already initialised with different values."),

	/** Not in the section 10 table: no endpoint has this path, as opposed to an unknown event or hold. */
	ROUTE_NOT_FOUND(HttpStatus.NOT_FOUND, "No such endpoint."),

	/** Not in the section 10 table: the path exists but not with this HTTP method. */
	METHOD_NOT_ALLOWED(HttpStatus.METHOD_NOT_ALLOWED, "That method is not allowed on this endpoint."),

	/** Not in the section 10 table: the catch-all for a bug on our side. */
	INTERNAL_ERROR(HttpStatus.INTERNAL_SERVER_ERROR, "Something went wrong on our side.");

	private final HttpStatus status;

	private final String defaultMessage;

	ErrorCode(HttpStatus status, String defaultMessage) {
		this.status = status;
		this.defaultMessage = defaultMessage;
	}

	public HttpStatus status() {
		return status;
	}

	public String defaultMessage() {
		return defaultMessage;
	}
}
