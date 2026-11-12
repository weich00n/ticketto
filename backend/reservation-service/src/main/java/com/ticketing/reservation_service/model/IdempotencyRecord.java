package com.ticketing.reservation_service.model;

import java.time.Instant;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/**
 * One row per Idempotency-Key, which is what makes a retried create return the original hold
 * instead of a second one.
 *
 * <p>The key is the primary key on purpose. Inserting the row is how a request claims the key:
 * two retries arriving together cannot both succeed at that insert, so only one of them goes on
 * to create a hold. The same trick as the expiry index claim, in MySQL instead of Redis.
 *
 * <p>holdId stays null between claiming the key and the hold existing, and that gap is
 * meaningful: a second caller seeing it knows a create is in flight rather than finished.
 */
@Entity
@Table(name = "idempotency_record")
@Getter
@Setter
@NoArgsConstructor
public class IdempotencyRecord {

	@Id
	@Column(name = "idempotency_key", length = 128, nullable = false)
	private String idempotencyKey;

	/**
	 * A digest of the request that claimed this key. A retry must carry the same one, so the same
	 * key cannot be reused for a different hold.
	 */
	@Column(name = "request_fingerprint", length = 64, nullable = false)
	private String requestFingerprint;

	/** Null until the hold exists. */
	@Column(name = "hold_id", columnDefinition = "CHAR(36)")
	private String holdId;

	@Column(name = "created_at", nullable = false)
	private Instant createdAt;
}
