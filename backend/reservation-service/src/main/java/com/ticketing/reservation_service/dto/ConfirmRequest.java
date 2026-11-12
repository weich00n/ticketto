package com.ticketing.reservation_service.dto;

import jakarta.validation.constraints.NotBlank;

/**
 * Body of the internal confirm call, sent by Checkout once a payment webhook has been
 * verified. paymentReference is Stripe's payment identifier, stored for reconciliation.
 */
public record ConfirmRequest(@NotBlank String orderId, @NotBlank String paymentReference) {
}
