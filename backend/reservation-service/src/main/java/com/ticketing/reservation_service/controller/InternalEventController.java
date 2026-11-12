package com.ticketing.reservation_service.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.ticketing.reservation_service.dto.CancellationView;
import com.ticketing.reservation_service.dto.CapacityView;
import com.ticketing.reservation_service.dto.SeedInventoryRequest;
import com.ticketing.reservation_service.dto.SeedInventoryView;
import com.ticketing.reservation_service.service.EventCancellationService;
import com.ticketing.reservation_service.service.InventoryService;

import jakarta.validation.Valid;

/**
 * Internal routes, never buyer-accessible. Seeding and cancelling do by HTTP what the
 * EventPublished and EventCancelled messages do through the broker, through the same services.
 * They are kept for tests and for running the service without a broker.
 */
@RestController
@RequestMapping("/internal/v1/events")
public class InternalEventController {

	private final InventoryService inventoryService;

	private final EventCancellationService cancellationService;

	public InternalEventController(InventoryService inventoryService,
			EventCancellationService cancellationService) {
		this.inventoryService = inventoryService;
		this.cancellationService = cancellationService;
	}

	/** Called by Queue to size admission batches. */
	@GetMapping("/{event_id}/capacity")
	public CapacityView capacity(@PathVariable("event_id") String eventId) {
		return inventoryService.getCapacity(eventId);
	}

	@PostMapping("/{event_id}/inventory")
	public SeedInventoryView seedInventory(@PathVariable("event_id") String eventId,
			@Valid @RequestBody SeedInventoryRequest request) {
		return inventoryService.seedInventory(eventId, request);
	}

	/** Safe to repeat; a repeat reports zeros because there is nothing left to change. */
	@PostMapping("/{event_id}/cancellation")
	public CancellationView cancel(@PathVariable("event_id") String eventId) {
		return cancellationService.cancel(eventId);
	}
}
