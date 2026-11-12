package com.ticketing.reservation_service.model;

import java.time.Instant;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Index;
import jakarta.persistence.Table;
import lombok.Getter;
import lombok.NoArgsConstructor;
import lombok.Setter;

/**
 * A domain event waiting to be published to RabbitMQ, written in the same transaction as the
 * change that caused it so the two cannot disagree.
 *
 * <p>Only the message's payload is stored; {@code OutboxPublisher} wraps it in the broker's
 * envelope from the other columns when it sends it.
 */
@Entity
// The publisher only ever looks for unsent rows, oldest first.
@Table(name = "outbox_message", indexes = @Index(name = "idx_outbox_unpublished", columnList = "published_at, occurred_at"))
@Getter
@Setter
@NoArgsConstructor
public class OutboxMessage {

	@Id
	@Column(name = "message_id", columnDefinition = "CHAR(36)", nullable = false)
	private String messageId;

	@Column(name = "event_type", length = 64, nullable = false)
	private String eventType;

	@Column(name = "schema_version", length = 16, nullable = false)
	private String schemaVersion;

	/** The hold or event this message is about, for ordering and debugging. */
	@Column(name = "aggregate_id", columnDefinition = "CHAR(36)", nullable = false)
	private String aggregateId;

	/** TEXT, not @Lob: under MySQL @Lob maps a String to tinytext, which holds 255 bytes. */
	@Column(name = "payload", columnDefinition = "TEXT", nullable = false)
	private String payload;

	@Column(name = "occurred_at", nullable = false)
	private Instant occurredAt;

	@Column(name = "correlation_id", length = 64)
	private String correlationId;

	/** Null until RabbitMQ has confirmed it stored the message. */
	@Column(name = "published_at")
	private Instant publishedAt;

	/** Sends the broker refused; a broker that is down or silent counts none. */
	@Column(name = "attempts", nullable = false)
	private int attempts;
}
