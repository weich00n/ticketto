package com.ticketing.reservation_service.dto;

import com.ticketing.reservation_service.model.HoldStatus;

/**
 * Whether an admission ever produced a hold, which Queue checks before releasing a
 * selection-timeout slot.
 *
 * <p>This answers 200 either way, never 404, so Queue can tell "no hold exists" apart from
 * "the service is unreachable". Section 6 is explicit that if the check is unavailable Queue
 * must keep the slot rather than release it, and a 404 would blur exactly that distinction.
 *
 * <p>holdId and status are absent when there is no hold.
 */
public record AdmissionReservationView(String admissionId, boolean hasHold, String holdId, HoldStatus status) {

	public static AdmissionReservationView none(String admissionId) {
		return new AdmissionReservationView(admissionId, false, null, null);
	}

	public static AdmissionReservationView of(String admissionId, String holdId, HoldStatus status) {
		return new AdmissionReservationView(admissionId, true, holdId, status);
	}
}
