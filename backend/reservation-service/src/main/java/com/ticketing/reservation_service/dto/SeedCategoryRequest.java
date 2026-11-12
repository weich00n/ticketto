package com.ticketing.reservation_service.dto;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;

/**
 * One category in a seed request. unitAmount is in the smallest currency unit, so 28800 is
 * SGD 288.00. currency may be omitted and defaults to SGD.
 */
public record SeedCategoryRequest(
		@NotBlank String categoryId,
		@NotNull @Min(1) Integer totalQuantity,
		@NotNull @Min(0) Long unitAmount,
		String currency) {
}
