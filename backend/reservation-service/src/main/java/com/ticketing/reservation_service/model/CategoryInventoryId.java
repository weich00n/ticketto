package com.ticketing.reservation_service.model;

import java.io.Serializable;

import lombok.AllArgsConstructor;
import lombok.EqualsAndHashCode;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/** Composite key for {@link CategoryInventory}: one row per event and category. */
@Getter
@Setter
@NoArgsConstructor
@AllArgsConstructor
@EqualsAndHashCode
public class CategoryInventoryId implements Serializable {

	private String eventId;

	private String categoryId;
}
