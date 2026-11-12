package com.ticketing.reservation_service.model;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.IdClass;
import jakarta.persistence.Table;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/**
 * The durable inventory record for one ticket category: how many exist, how many are sold,
 * and what one ticket costs.
 *
 * <p>There is deliberately no held_quantity column. Held tickets are already subtracted from
 * the Redis counter avail:{eventId}:categoryId, which is the only place availability lives.
 */
@Entity
@Table(name = "category_inventory")
@IdClass(CategoryInventoryId.class)
@Getter
@Setter
@NoArgsConstructor
public class CategoryInventory {

	@Id
	@Column(name = "event_id", columnDefinition = "CHAR(36)", nullable = false)
	private String eventId;

	@Id
	@Column(name = "category_id", columnDefinition = "CHAR(36)", nullable = false)
	private String categoryId;

	@Column(name = "total_quantity", nullable = false)
	private int totalQuantity;

	@Column(name = "sold_quantity", nullable = false)
	private int soldQuantity;

	/** Price of one ticket in the smallest currency unit: 28800 is SGD 288.00. */
	@Column(name = "unit_amount", nullable = false)
	private long unitAmount;

	@Column(name = "currency", columnDefinition = "CHAR(3)", nullable = false)
	private String currency;
}
