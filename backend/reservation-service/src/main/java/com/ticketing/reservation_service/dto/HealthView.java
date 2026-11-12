package com.ticketing.reservation_service.dto;

/** The load balancer's probe response: { "status": "UP" }. */
public record HealthView(String status) {
}
