package com.ticketing.reservation_service.dto;

import java.util.List;

import com.ticketing.reservation_service.model.SaleStatus;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotEmpty;

/**
 * Body of POST /internal/v1/events/{event_id}/inventory. saleStatus may be omitted and
 * defaults to ON_SALE.
 */
public record SeedInventoryRequest(SaleStatus saleStatus, @NotEmpty List<@Valid SeedCategoryRequest> categories) {
}
