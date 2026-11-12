package com.ticketing.reservation_service.model;

/** Whether an event is still selling. Mirrored into Redis as sale:{eventId}. */
public enum SaleStatus {
	ON_SALE,
	CANCELLED
}
