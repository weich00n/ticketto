package com.ticketing.reservation_service.dto;

import java.time.Instant;

import com.ticketing.reservation_service.model.HoldStatus;

/** Result of a buyer releasing their own hold early. */
public record ReleaseView(String holdId, HoldStatus status, Instant serverTime) {
}
