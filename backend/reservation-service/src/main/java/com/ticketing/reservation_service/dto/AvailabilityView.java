package com.ticketing.reservation_service.dto;

import java.time.Instant;
import java.util.List;

import com.ticketing.reservation_service.model.SaleStatus;

/** Response of GET /api/v1/events/{event_id}/availability. */
public record AvailabilityView(String eventId, SaleStatus status, Instant serverTime,
		List<CategoryAvailabilityView> categories) {
}
