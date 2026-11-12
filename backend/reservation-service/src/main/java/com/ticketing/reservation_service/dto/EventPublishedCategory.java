package com.ticketing.reservation_service.dto;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;

/**
 * One of an EventPublished message's ticket_categories. Event Service calls the price
 * price_amount; it is the same smallest-currency-unit figure a seed request calls unit_amount.
 */
public record EventPublishedCategory(
		@NotBlank String categoryId,
		@NotNull @Min(1) Integer totalQuantity,
		@NotNull @Min(0) Long priceAmount,
		// The schema's const: prices are only ever SGD in the MVP. Absent is read as SGD.
		@Pattern(regexp = "SGD", message = "must be SGD") String currency) {

	public SeedCategoryRequest toSeedCategory() {
		return new SeedCategoryRequest(categoryId, totalQuantity, priceAmount, currency);
	}
}
