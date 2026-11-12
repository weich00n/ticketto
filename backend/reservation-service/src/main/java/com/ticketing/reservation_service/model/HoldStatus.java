package com.ticketing.reservation_service.model;

/** The four hold states from Technical Contract section 9. Never HELD or SOLD. */
public enum HoldStatus {
	ACTIVE,
	CONFIRMED,
	EXPIRED,
	CANCELLED
}
