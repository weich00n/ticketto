package com.ticketing.reservation_service.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;

/**
 * Body of POST /api/v1/reservations.
 *
 * <p>There is deliberately no user_id field: a buyer-supplied identity is never trusted. The
 * buyer comes from inside the admission token, whose signature is verified and whose buyer is
 * checked against the authenticated caller (AdmissionTokenService).
 */
public record CreateHoldRequest(
		@NotBlank String eventId,
		@NotBlank String categoryId,
		@NotNull @Min(1) @Max(5) Integer quantity,
		@NotBlank String admissionToken) {
}
