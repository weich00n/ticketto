package com.ticketing.reservation_service.dto;

import java.time.Instant;

import com.ticketing.reservation_service.model.HoldStatus;

/**
 * What the buyer's browser sees for a hold. serverTime lets the frontend run the countdown
 * without trusting the browser clock.
 */
public record HoldView(String holdId, String admissionId, HoldStatus status, int quantity, long unitAmount,
		long totalAmount, String currency, Instant paymentExpiresAt, Instant holdExpiresAt, Instant serverTime) {
}
