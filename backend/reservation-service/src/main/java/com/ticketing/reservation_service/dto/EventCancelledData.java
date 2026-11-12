package com.ticketing.reservation_service.dto;

import jakarta.validation.constraints.NotBlank;

/**
 * The payload of an EventCancelled message (rabbitmq-service contracts/EventCancelled.payload.schema.json).
 * Only the event matters here: every hold on it is resolved, whatever the reason.
 */
public record EventCancelledData(@NotBlank String eventId) {
}
