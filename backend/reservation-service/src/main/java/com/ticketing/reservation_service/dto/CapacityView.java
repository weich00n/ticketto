package com.ticketing.reservation_service.dto;

import java.time.Instant;
import java.util.List;

import com.ticketing.reservation_service.model.SaleStatus;

/**
 * Response of the internal capacity call, which Queue uses to size admission batches.
 *
 * <p>ready is false until inventory has been initialised, so Queue can tell "this event is not
 * ready to sell yet" from "this event has nothing left".
 */
public record CapacityView(String eventId, boolean ready, SaleStatus saleStatus, Instant serverTime,
		List<CategoryCapacityView> categories) {
}
