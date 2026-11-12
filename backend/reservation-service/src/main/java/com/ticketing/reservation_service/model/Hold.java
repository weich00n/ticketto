package com.ticketing.reservation_service.model;

import java.time.Instant;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/**
 * One row per hold. This is the durable memory of what happened.
 *
 * <p>Checkout confirms minutes later carrying only a hold_id, and may retry. By then the
 * Redis key may be gone, so only this row can tell "already confirmed" (success) apart from
 * "never existed" (Checkout refunds).
 *
 * <p>The price is snapshotted onto the row at creation, so the amount billed cannot drift if
 * the category price changes mid-sale.
 */
@Entity
@Table(name = "hold",
		uniqueConstraints = @UniqueConstraint(name = "uk_hold_admission_id", columnNames = "admission_id"))
@Getter
@Setter
@NoArgsConstructor
public class Hold {

	@Id
	@Column(name = "hold_id", columnDefinition = "CHAR(36)", nullable = false)
	private String holdId;

	/** The platform user id, taken from the admission token Queue signed. */
	@Column(name = "user_id", columnDefinition = "CHAR(36)", nullable = false)
	private String userId;

	/**
	 * The buyer's Cognito id, as API Gateway authenticated them. Stored because that is the only
	 * identity an incoming request carries, so it is what ownership checks can compare against.
	 */
	@Column(name = "user_sub", columnDefinition = "CHAR(36)")
	private String userSub;

	@Column(name = "event_id", columnDefinition = "CHAR(36)", nullable = false)
	private String eventId;

	@Column(name = "category_id", columnDefinition = "CHAR(36)", nullable = false)
	private String categoryId;

	/** At most one hold per admission, enforced by the unique constraint above. */
	@Column(name = "admission_id", columnDefinition = "CHAR(36)", nullable = false)
	private String admissionId;

	@Column(name = "quantity", nullable = false)
	private int quantity;

	@Column(name = "unit_amount", nullable = false)
	private long unitAmount;

	@Column(name = "total_amount", nullable = false)
	private long totalAmount;

	@Column(name = "currency", columnDefinition = "CHAR(3)", nullable = false)
	private String currency;

	@Enumerated(EnumType.STRING)
	// VARCHAR rather than a native MySQL ENUM, so adding a state later is not a schema migration
	@Column(name = "status", columnDefinition = "VARCHAR(16)", nullable = false)
	private HoldStatus status;

	/** Set only once Checkout confirms. */
	@Column(name = "order_id", columnDefinition = "CHAR(36)")
	private String orderId;

	/** Stripe's payment identifier, kept so records can be reconciled against Stripe. */
	@Column(name = "payment_reference", length = 128)
	private String paymentReference;

	/** When the confirmation was accepted, so a retry is answered with the original time. */
	@Column(name = "confirmed_at")
	private Instant confirmedAt;

	@Column(name = "correlation_id", length = 64)
	private String correlationId;

	@Column(name = "created_at", nullable = false)
	private Instant createdAt;

	/** What the buyer sees counting down: created + 5 minutes. */
	@Column(name = "payment_expires_at", nullable = false)
	private Instant paymentExpiresAt;

	/** When the Redis key dies: created + 6 minutes. A hold is confirmable until then. */
	@Column(name = "hold_expires_at", nullable = false)
	private Instant holdExpiresAt;
}
