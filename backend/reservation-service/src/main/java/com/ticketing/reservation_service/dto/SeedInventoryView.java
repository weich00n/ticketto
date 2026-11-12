package com.ticketing.reservation_service.dto;

import java.time.Instant;
import java.util.List;

import com.ticketing.reservation_service.model.SaleStatus;

/**
 * Result of seeding. created is true when this call wrote the inventory and false when it was
 * a replay that changed nothing, so a caller can tell the two apart.
 */
public record SeedInventoryView(String eventId, SaleStatus saleStatus, boolean created,
		List<CategoryAvailabilityView> categories, Instant serverTime) {
}
