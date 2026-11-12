package com.ticketing.reservation_service.dto;

import java.time.Instant;

import com.ticketing.reservation_service.model.SaleStatus;

/**
 * What one cancellation call did. Counts are of what this call changed, so re-running a
 * cancellation that already finished reports zeros.
 */
public record CancellationView(String eventId, SaleStatus saleStatus, int holdsCancelled, int refundsRequired,
		Instant serverTime) {
}
