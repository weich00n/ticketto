package com.ticketing.reservation_service.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.ticketing.reservation_service.dto.AvailabilityView;
import com.ticketing.reservation_service.service.InventoryService;

/** Live category availability, for the waiting room and the selection screen. */
@RestController
@RequestMapping("/api/v1/events")
public class AvailabilityController {

	private final InventoryService inventoryService;

	public AvailabilityController(InventoryService inventoryService) {
		this.inventoryService = inventoryService;
	}

	@GetMapping("/{event_id}/availability")
	public AvailabilityView availability(@PathVariable("event_id") String eventId) {
		return inventoryService.getAvailability(eventId);
	}
}
