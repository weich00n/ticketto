package com.ticketing.reservation_service.controller;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.ticketing.reservation_service.config.GatewayHeaders;
import com.ticketing.reservation_service.dto.CreateHoldRequest;
import com.ticketing.reservation_service.dto.HoldCreation;
import com.ticketing.reservation_service.dto.HoldView;
import com.ticketing.reservation_service.dto.ReleaseView;
import com.ticketing.reservation_service.service.ReservationService;

import jakarta.validation.Valid;

/** The buyer's holds. */
@RestController
@RequestMapping("/api/v1/reservations")
public class ReservationController {

	private final ReservationService reservationService;

	public ReservationController(ReservationService reservationService) {
		this.reservationService = reservationService;
	}

	/**
	 * The buyer comes from X-User-Sub, which API Gateway sets from the verified access token and
	 * overwrites if the client tried to send its own. The header is read as optional here so a
	 * missing one becomes 401 UNAUTHENTICATED rather than Spring's own 400.
	 *
	 * <p>An Idempotency-Key makes a retry return the original hold rather than a second one.
	 */
	@PostMapping
	public ResponseEntity<HoldView> createHold(
			@RequestHeader(name = GatewayHeaders.USER_SUB, required = false) String userSub,
			@RequestHeader(name = "Idempotency-Key", required = false) String idempotencyKey,
			@Valid @RequestBody CreateHoldRequest request) {
		HoldCreation created = reservationService.createHold(request, userSub, idempotencyKey);
		// A replay is not a creation, so it answers 200 rather than 201.
		return ResponseEntity.status(created.replayed() ? HttpStatus.OK : HttpStatus.CREATED).body(created.hold());
	}

	/** Buyers see only their own holds, so another buyer's hold is reported as 404. */
	@GetMapping("/{hold_id}")
	public HoldView getHold(
			@RequestHeader(name = GatewayHeaders.USER_SUB, required = false) String userSub,
			@PathVariable("hold_id") String holdId) {
		return reservationService.getHold(holdId, userSub);
	}

	/** Releases a hold early; its tickets return to free immediately. */
	@DeleteMapping("/{hold_id}")
	public ReleaseView releaseHold(
			@RequestHeader(name = GatewayHeaders.USER_SUB, required = false) String userSub,
			@PathVariable("hold_id") String holdId) {
		return reservationService.releaseHold(holdId, userSub);
	}
}
