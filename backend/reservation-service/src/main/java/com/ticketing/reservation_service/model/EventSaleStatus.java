package com.ticketing.reservation_service.model;

import java.time.Instant;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/**
 * One row per event this service knows about. Its existence is what makes an event "known":
 * no row means EVENT_NOT_FOUND.
 */
@Entity
@Table(name = "event_sale_status")
@Getter
@Setter
@NoArgsConstructor
public class EventSaleStatus {

	@Id
	@Column(name = "event_id", columnDefinition = "CHAR(36)", nullable = false)
	private String eventId;

	@Enumerated(EnumType.STRING)
	@Column(name = "sale_status", columnDefinition = "VARCHAR(16)", nullable = false)
	private SaleStatus saleStatus;

	@Column(name = "created_at", nullable = false)
	private Instant createdAt;

	@Column(name = "updated_at", nullable = false)
	private Instant updatedAt;
}
