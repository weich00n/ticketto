package com.ticketing.reservation_service.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.ticketing.reservation_service.dto.AdmissionReservationView;
import com.ticketing.reservation_service.service.ReservationService;

/** Internal route for Queue, asking whether an admission has already been used for a hold. */
@RestController
@RequestMapping("/internal/v1/admissions")
public class InternalAdmissionController {

	private final ReservationService reservationService;

	public InternalAdmissionController(ReservationService reservationService) {
		this.reservationService = reservationService;
	}

	/**
	 * Answers 200 whether or not a hold exists, never 404, so Queue can tell an absent hold from
	 * an unreachable service and keep the slot when it cannot ask.
	 */
	@GetMapping("/{admission_id}/reservation")
	public AdmissionReservationView reservationFor(@PathVariable("admission_id") String admissionId) {
		return reservationService.getAdmissionReservation(admissionId);
	}
}
