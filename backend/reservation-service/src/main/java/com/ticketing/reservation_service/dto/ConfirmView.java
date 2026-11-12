package com.ticketing.reservation_service.dto;

import java.time.Instant;

import com.ticketing.reservation_service.model.HoldStatus;

/**
 * What Checkout learns from confirming.
 *
 * <p>duplicate is false when this call made the confirmation and true when it had already
 * happened, which lets Checkout tell a first success from a retry without either being an
 * error. confirmedAt is the original time in both cases.
 */
public record ConfirmView(String holdId, String orderId, HoldStatus status, int quantity, boolean duplicate,
		Instant confirmedAt) {
}
