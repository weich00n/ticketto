package com.ticketing.reservation_service.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.ticketing.reservation_service.dto.ConfirmRequest;
import com.ticketing.reservation_service.dto.ConfirmView;
import com.ticketing.reservation_service.dto.InternalHoldView;
import com.ticketing.reservation_service.service.ReservationService;

import jakarta.validation.Valid;

/**
 * Internal routes for Checkout. Service-to-service only, on private addresses, so these carry
 * no Authorization header and no X-User-Sub: there is no buyer on the call.
 */
@RestController
@RequestMapping("/internal/v1/reservations")
public class InternalReservationController {

	private final ReservationService reservationService;

	public InternalReservationController(ReservationService reservationService) {
		this.reservationService = reservationService;
	}

	/** What Checkout prices the order from: ownership, quantity, the price snapshot and state. */
	@GetMapping("/{hold_id}")
	public InternalHoldView getHold(@PathVariable("hold_id") String holdId) {
		return reservationService.getHoldForService(holdId);
	}

	/** Called after a verified payment webhook. Safe to call more than once. */
	@PostMapping("/{hold_id}/confirm")
	public ConfirmView confirmHold(@PathVariable("hold_id") String holdId, @Valid @RequestBody ConfirmRequest request) {
		return reservationService.confirmHold(holdId, request);
	}
}
