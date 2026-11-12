package com.ticketing.reservation_service.repository;

import java.time.Instant;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;

import com.ticketing.reservation_service.model.IdempotencyRecord;

public interface IdempotencyRepository extends JpaRepository<IdempotencyRecord, String> {

	/**
	 * Claims a key, failing if it is already taken.
	 *
	 * <p>Written as a real INSERT rather than save(), because save() on an entity whose id is
	 * assigned rather than generated performs a merge: it would quietly overwrite an existing
	 * claim instead of refusing, and two retries would both believe they had won the key.
	 */
	@Transactional
	@Modifying
	@Query(value = "insert into idempotency_record (idempotency_key, request_fingerprint, created_at) "
			+ "values (:idempotencyKey, :requestFingerprint, :createdAt)", nativeQuery = true)
	int insertClaim(@Param("idempotencyKey") String idempotencyKey,
			@Param("requestFingerprint") String requestFingerprint, @Param("createdAt") Instant createdAt);

	/**
	 * Takes over a claim whose request never finished, only if it is still exactly the claim the
	 * caller read: of two retries taking it over together, one wins.
	 */
	@Transactional
	@Modifying(clearAutomatically = true)
	@Query("update IdempotencyRecord r set r.createdAt = :now where r.idempotencyKey = :idempotencyKey "
			+ "and r.holdId is null and r.createdAt = :claimedAt")
	int takeOverClaim(@Param("idempotencyKey") String idempotencyKey, @Param("claimedAt") Instant claimedAt,
			@Param("now") Instant now);

	/** Records which hold the key produced, only while it has none. */
	@Transactional
	@Modifying(clearAutomatically = true)
	@Query("update IdempotencyRecord r set r.holdId = :holdId where r.idempotencyKey = :idempotencyKey")
	int attachHold(@Param("idempotencyKey") String idempotencyKey, @Param("holdId") String holdId);
}
