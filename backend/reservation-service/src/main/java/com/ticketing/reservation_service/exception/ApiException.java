package com.ticketing.reservation_service.exception;

/**
 * Any failure a caller is meant to see. The code carries its own HTTP status, and
 * GlobalExceptionHandler turns it into the section 10 error body.
 */
public class ApiException extends RuntimeException {

	private final ErrorCode code;

	public ApiException(ErrorCode code) {
		this(code, code.defaultMessage());
	}

	public ApiException(ErrorCode code, String message) {
		super(message);
		this.code = code;
	}

	public ErrorCode getCode() {
		return code;
	}
}
