package com.ticketing.reservation_service.repository;

import java.time.Instant;
import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;

import com.ticketing.reservation_service.model.OutboxMessage;

public interface OutboxRepository extends JpaRepository<OutboxMessage, String> {

	/**
	 * Writes a message, failing on a duplicate id.
	 *
	 * <p>A real INSERT rather than save(), for the same reason as the idempotency claim: save() on
	 * an assigned id merges, which would reset published_at on a message already sent and have
	 * the publisher send it a second time. Not INSERT IGNORE either, which downgrades every error
	 * to a warning - a payload too long for its column would be silently cut short.
	 */
	@Transactional
	@Modifying
	@Query(value = "insert into outbox_message (message_id, event_type, schema_version, aggregate_id, "
			+ "payload, occurred_at, correlation_id, attempts) "
			+ "values (:messageId, :eventType, :schemaVersion, :aggregateId, :payload, :occurredAt, :correlationId, 0)",
			nativeQuery = true)
	int insert(@Param("messageId") String messageId, @Param("eventType") String eventType,
			@Param("schemaVersion") String schemaVersion, @Param("aggregateId") String aggregateId,
			@Param("payload") String payload, @Param("occurredAt") Instant occurredAt,
			@Param("correlationId") String correlationId);

	List<OutboxMessage> findByAggregateId(String aggregateId);

	/**
	 * The oldest messages not yet sent, locked until the caller's transaction ends. SKIP LOCKED
	 * lets a second instance of this service take the next rows instead of waiting for, or
	 * sending again, the ones the first is already publishing. Must run inside a transaction.
	 *
	 * <p>Fewest attempts first, so messages the broker keeps refusing drift behind new ones
	 * instead of filling every batch. A message refused {@code maxAttempts} times has been given
	 * up on and is left out.
	 */
	@Query(value = "select * from outbox_message where published_at is null and attempts < :maxAttempts "
			+ "order by attempts, occurred_at, message_id limit :limit for update skip locked", nativeQuery = true)
	List<OutboxMessage> lockUnpublished(@Param("limit") int limit, @Param("maxAttempts") int maxAttempts);
}
