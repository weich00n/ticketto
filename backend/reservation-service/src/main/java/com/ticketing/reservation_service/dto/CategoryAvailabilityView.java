package com.ticketing.reservation_service.dto;

/**
 * One category's numbers. total and sold come from MySQL; free is read straight off the
 * Redis counter and is the authoritative live figure. Held quantity is not reported.
 */
public record CategoryAvailabilityView(String categoryId, int total, int free, int sold) {
}
