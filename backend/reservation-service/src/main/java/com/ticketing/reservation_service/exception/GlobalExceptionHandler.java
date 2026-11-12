package com.ticketing.reservation_service.exception;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.util.StringUtils;
import org.springframework.validation.FieldError;
import org.springframework.web.HttpRequestMethodNotSupportedException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.servlet.NoHandlerFoundException;
import org.springframework.web.servlet.resource.NoResourceFoundException;

import com.ticketing.reservation_service.config.CorrelationIdFilter;
import com.ticketing.reservation_service.dto.ErrorResponse;

/** Turns every failure into the one error body described in section 10. */
@RestControllerAdvice
public class GlobalExceptionHandler {

	private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

	@ExceptionHandler(ApiException.class)
	public ResponseEntity<ErrorResponse> handleApiException(ApiException ex) {
		return build(ex.getCode(), ex.getMessage());
	}

	/** A @Valid DTO was rejected: report the first offending field. */
	@ExceptionHandler(MethodArgumentNotValidException.class)
	public ResponseEntity<ErrorResponse> handleValidation(MethodArgumentNotValidException ex) {
		String message = ex.getBindingResult().getFieldErrors().stream()
				.findFirst()
				.map(GlobalExceptionHandler::describe)
				.orElse(ErrorCode.INVALID_REQUEST.defaultMessage());
		return build(ErrorCode.INVALID_REQUEST, message);
	}

	/** Unparseable or missing JSON body. */
	@ExceptionHandler(HttpMessageNotReadableException.class)
	public ResponseEntity<ErrorResponse> handleUnreadableBody(HttpMessageNotReadableException ex) {
		return build(ErrorCode.INVALID_REQUEST, "The request body is missing or is not valid JSON.");
	}

	/**
	 * MySQL or Redis could not be reached. The caller should retry rather than treat this as a
	 * business outcome, which is exactly what 503 DEPENDENCY_UNAVAILABLE means.
	 */
	@ExceptionHandler(DataAccessException.class)
	public ResponseEntity<ErrorResponse> handleDependencyFailure(DataAccessException ex) {
		log.error("A data dependency failed", ex);
		return build(ErrorCode.DEPENDENCY_UNAVAILABLE, ErrorCode.DEPENDENCY_UNAVAILABLE.defaultMessage());
	}

	/**
	 * No controller has this path: which of the two Spring raises depends on whether static
	 * resource handling is on. Without this the catch-all below would answer 500.
	 */
	@ExceptionHandler({ NoResourceFoundException.class, NoHandlerFoundException.class })
	public ResponseEntity<ErrorResponse> handleUnknownRoute(Exception ex) {
		return build(ErrorCode.ROUTE_NOT_FOUND, ErrorCode.ROUTE_NOT_FOUND.defaultMessage());
	}

	@ExceptionHandler(HttpRequestMethodNotSupportedException.class)
	public ResponseEntity<ErrorResponse> handleWrongMethod(HttpRequestMethodNotSupportedException ex) {
		ResponseEntity<ErrorResponse> response = build(ErrorCode.METHOD_NOT_ALLOWED,
				ErrorCode.METHOD_NOT_ALLOWED.defaultMessage());
		return ResponseEntity.status(response.getStatusCode())
				.headers(ex.getHeaders()) // Allow: the methods this path does take
				.body(response.getBody());
	}

	@ExceptionHandler(Exception.class)
	public ResponseEntity<ErrorResponse> handleUnexpected(Exception ex) {
		// Anything else Spring MVC refused before reaching a controller - an unsupported
		// Content-Type, a missing parameter - is a malformed request, not a fault on our side.
		if (ex instanceof org.springframework.web.ErrorResponse refused
				&& refused.getStatusCode().is4xxClientError()) {
			String detail = refused.getBody().getDetail();
			return build(ErrorCode.INVALID_REQUEST,
					StringUtils.hasText(detail) ? detail : ErrorCode.INVALID_REQUEST.defaultMessage());
		}
		log.error("Unhandled failure", ex);
		return build(ErrorCode.INTERNAL_ERROR, ErrorCode.INTERNAL_ERROR.defaultMessage());
	}

	private static ResponseEntity<ErrorResponse> build(ErrorCode code, String message) {
		ErrorResponse body = ErrorResponse.of(code.name(), message, CorrelationIdFilter.current());
		return ResponseEntity.status(code.status()).body(body);
	}

	private static String describe(FieldError error) {
		return error.getField() + " " + error.getDefaultMessage() + ".";
	}
}
