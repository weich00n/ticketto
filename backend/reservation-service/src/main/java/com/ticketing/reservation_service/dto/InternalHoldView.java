package com.ticketing.reservation_service.dto;

import java.time.Instant;

import com.ticketing.reservation_service.model.HoldStatus;

/**
 * What Checkout prices an order from. The frontend never decides the payable amount, so these
 * are the authoritative figures: the price snapshot taken when the hold was made.
 *
 * <p>Richer than the buyer's own view, because Checkout also needs the ids it builds the order
 * with. orderId is absent until a confirmation has happened.
 */
public record InternalHoldView(String holdId, String userId, String eventId, String categoryId, String admissionId,
		HoldStatus status, int quantity, long unitAmount, long totalAmount, String currency, String orderId,
		Instant paymentExpiresAt, Instant holdExpiresAt, Instant serverTime) {
}
