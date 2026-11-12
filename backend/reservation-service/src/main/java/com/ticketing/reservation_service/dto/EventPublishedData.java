package com.ticketing.reservation_service.dto;

import java.util.List;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;

/**
 * The payload of an EventPublished message (rabbitmq-service contracts/EventPublished.payload.schema.json).
 * Only what this service uses is read; the title and the times are Queue's business.
 */
public record EventPublishedData(@NotBlank String eventId,
		@NotEmpty List<@Valid EventPublishedCategory> ticketCategories) {

	public List<SeedCategoryRequest> seedCategories() {
		return ticketCategories.stream().map(EventPublishedCategory::toSeedCategory).toList();
	}
}
