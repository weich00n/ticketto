package com.ticketing.reservation_service.dto;

/**
 * A created hold, and whether it had already been created by an earlier identical request.
 *
 * <p>The controller turns this into 201 for a new hold and 200 for a replay, so a retry reads
 * as "nothing new happened" rather than as a second creation.
 */
public record HoldCreation(HoldView hold, boolean replayed) {
}
