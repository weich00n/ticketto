package com.ticketing.reservation_service.repository;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;

import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.HoldStatus;

public interface HoldRepository extends JpaRepository<Hold, String> {

	/** The hold an admission produced, if it ever made one. At most one exists by constraint. */
	Optional<Hold> findByAdmissionId(String admissionId);

	/** Every hold for one event, which EventCancellationService walks when an event is cancelled. */
	List<Hold> findByEventId(String eventId);

	/**
	 * Moves a hold out of ACTIVE, and answers how many rows it changed: one if this caller made
	 * the transition, zero if the hold had already ended.
	 *
	 * <p>The WHERE clause is the guard. It cannot replace reclaim.lua's claim - that is what
	 * decides who returns the tickets - but it does mean a hold's final status is written once
	 * and never overwritten by a slower caller.
	 */
	@Transactional
	@Modifying(clearAutomatically = true)
	@Query("update Hold h set h.status = :newStatus where h.holdId = :holdId and h.status = :expectedStatus")
	int transitionStatus(@Param("holdId") String holdId, @Param("expectedStatus") HoldStatus expectedStatus,
			@Param("newStatus") HoldStatus newStatus);

	/**
	 * Records a confirmation, but only on a hold that is still ACTIVE.
	 *
	 * <p>This is the idempotency arbiter. One row changed means this call is the confirmation;
	 * zero means someone got there first, and the stored row then says whether that was the
	 * same order retrying or a different one entirely.
	 */
	@Transactional
	@Modifying(clearAutomatically = true)
	@Query("update Hold h set h.status = :newStatus, h.orderId = :orderId, "
			+ "h.paymentReference = :paymentReference, h.confirmedAt = :confirmedAt "
			+ "where h.holdId = :holdId and h.status = :expectedStatus")
	int confirmIfActive(@Param("holdId") String holdId, @Param("expectedStatus") HoldStatus expectedStatus,
			@Param("newStatus") HoldStatus newStatus, @Param("orderId") String orderId,
			@Param("paymentReference") String paymentReference, @Param("confirmedAt") Instant confirmedAt);

	/**
	 * What each buyer has held or bought for one event, which is what the five-ticket limit
	 * counts. Overdue ACTIVE holds are included because, like their tickets, their allowance is
	 * only released when the sweep reclaims them.
	 */
	@Query("select h.userId as userId, sum(h.quantity) as quantity from Hold h where h.eventId = :eventId "
			+ "and (h.status = :confirmedStatus or h.status = :activeStatus) group by h.userId")
	List<BuyerQuantity> sumQuantityPerBuyer(@Param("eventId") String eventId,
			@Param("activeStatus") HoldStatus activeStatus, @Param("confirmedStatus") HoldStatus confirmedStatus);

	/** Every hold for one event in a given state. */
	List<Hold> findByEventIdAndStatus(String eventId, HoldStatus status);

	/** One buyer's running total for an event. */
	interface BuyerQuantity {

		String getUserId();

		long getQuantity();

	}

	/** Holds that should already have ended, used to check the sweep's work. */
	List<Hold> findByStatusAndHoldExpiresAtBefore(HoldStatus status, Instant cutoff);
}
