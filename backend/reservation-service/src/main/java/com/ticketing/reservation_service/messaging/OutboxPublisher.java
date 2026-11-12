package com.ticketing.reservation_service.messaging;

import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Duration;
import java.time.temporal.ChronoUnit;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageBuilder;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

import com.ticketing.reservation_service.config.RabbitConfig;
import com.ticketing.reservation_service.model.OutboxMessage;
import com.ticketing.reservation_service.repository.OutboxRepository;

import tools.jackson.databind.json.JsonMapper;

/**
 * Sends the messages {@code OutboxWriter} recorded, in the broker's envelope, to this service's
 * reservation.events exchange with each type's dotted routing key.
 *
 * <p>A row is marked sent only once the broker confirms it has stored the message. If the
 * publish fails, or the confirmation never comes, the row stays unsent and the next run tries
 * it again. So a message can go out twice - a confirmation lost on the way back - but never
 * zero times; that is why every message carries a stable message_id for consumers to
 * deduplicate on.
 *
 * <p>A message the broker refuses or cannot route, or that cannot be built, is skipped and tried
 * again next run, behind the others: one bad message must not hold up every RefundRequired and
 * ReservationExpired after it. Nothing depends on order across messages. After
 * {@link #MAX_ATTEMPTS} refusals it is given up on: logged at ERROR and left unsent, for a manual
 * replay (reset attempts to 0). A batch stops only when the broker itself is unreachable or silent,
 * since then every later send would fail too, each after its own confirm timeout; that counts no
 * attempt, so an outage cannot use up a message's attempts.
 */
@Component
public class OutboxPublisher {

	private static final Logger log = LoggerFactory.getLogger(OutboxPublisher.class);

	private static final int BATCH_SIZE = 100;

	/** Refusals before a message is given up on. Retried once per run until then. */
	public static final int MAX_ATTEMPTS = 10;

	/** How long to wait for the broker to confirm one message before treating it as failed. */
	private static final Duration CONFIRM_TIMEOUT = Duration.ofSeconds(5);

	private final OutboxRepository outboxRepository;

	private final RabbitTemplate rabbitTemplate;

	private final JsonMapper jsonMapper;

	private final TransactionTemplate transactions;

	private final Clock clock;

	public OutboxPublisher(OutboxRepository outboxRepository, RabbitTemplate rabbitTemplate, JsonMapper jsonMapper,
			TransactionTemplate transactions, Clock clock) {
		this.outboxRepository = outboxRepository;
		this.rabbitTemplate = rabbitTemplate;
		this.jsonMapper = jsonMapper;
		this.transactions = transactions;
		this.clock = clock;
	}

	/** The interval is set absurdly high under the test profile, which has no broker. */
	@Scheduled(fixedDelayString = "${reservation.outbox-publish-seconds}s",
			initialDelayString = "${reservation.outbox-publish-seconds}s")
	public void publish() {
		try {
			int sent;
			do {
				sent = publishBatch();
			}
			while (sent == BATCH_SIZE);
		}
		catch (RuntimeException ex) {
			// Never let a bad run kill the schedule; the next one will try again.
			log.error("Outbox publish failed", ex);
		}
	}

	/**
	 * Sends up to one batch, oldest first. The rows stay locked while they are sent, so another
	 * instance of this service skips them rather than sending them too.
	 *
	 * @return how many messages the broker confirmed
	 */
	public int publishBatch() {
		Integer sent = transactions.execute(status -> {
			List<OutboxMessage> due = outboxRepository.lockUnpublished(BATCH_SIZE, MAX_ATTEMPTS);
			int confirmed = 0;
			for (OutboxMessage row : due) {
				SendResult result = send(row);
				if (result == SendResult.SENT) {
					row.setPublishedAt(clock.instant().truncatedTo(ChronoUnit.SECONDS));
					confirmed++;
					continue;
				}
				if (result == SendResult.BROKER_UNAVAILABLE) {
					break;
				}
				row.setAttempts(row.getAttempts() + 1);
				if (row.getAttempts() >= MAX_ATTEMPTS) {
					log.error("Giving up on {} {} after {} refused attempts; it stays unsent for a manual replay",
							row.getEventType(), row.getMessageId(), row.getAttempts());
				}
			}
			return confirmed;
		});
		return (sent == null) ? 0 : sent;
	}

	private enum SendResult {
		/** The broker confirmed it stored the message. */
		SENT,
		/** This message was refused, unroutable or unbuildable; the next one may still go. */
		REFUSED,
		/** No answer from the broker, so no later message would get one either. */
		BROKER_UNAVAILABLE
	}

	private SendResult send(OutboxMessage row) {
		String routingKey;
		Message message;
		try {
			routingKey = RabbitConfig.routingKey(row.getEventType());
			message = toMessage(row);
		}
		catch (RuntimeException ex) {
			log.error("Could not build {} {}: {}", row.getEventType(), row.getMessageId(), ex.toString());
			return SendResult.REFUSED;
		}
		CorrelationData confirmation = new CorrelationData(row.getMessageId());
		try {
			rabbitTemplate.send(RabbitConfig.EXCHANGE, routingKey, message, confirmation);
			CorrelationData.Confirm confirm = confirmation.getFuture()
					.get(CONFIRM_TIMEOUT.toMillis(), TimeUnit.MILLISECONDS);
			if (!confirm.isAck()) {
				log.warn("Broker refused {} {}: {}", row.getEventType(), row.getMessageId(), confirm.getReason());
				return SendResult.REFUSED;
			}
			if (confirmation.getReturned() != null) {
				// Routed nowhere, not even to the broker's catch-all for unrouted messages, so the
				// topology is not what this service expects.
				log.warn("Broker could not route {} {}: {}", row.getEventType(), row.getMessageId(),
						confirmation.getReturned().getReplyText());
				return SendResult.REFUSED;
			}
			MessageProperties props = message.getMessageProperties();
			// The body only at DEBUG: RefundRequired and ReservationExpired carry buyer and order ids.
			log.info("Published to {} (routing key {}): message_id={} type={} correlation_id={}",
					RabbitConfig.EXCHANGE, routingKey, props.getMessageId(), props.getType(), props.getCorrelationId());
			if (log.isDebugEnabled()) {
				log.debug("Message {} body: {}", props.getMessageId(),
						new String(message.getBody(), StandardCharsets.UTF_8));
			}
			return SendResult.SENT;
		}
		catch (InterruptedException ex) {
			Thread.currentThread().interrupt();
			return SendResult.BROKER_UNAVAILABLE;
		}
		catch (Exception ex) {
			log.warn("Could not publish {} {}: {}", row.getEventType(), row.getMessageId(), ex.toString());
			return SendResult.BROKER_UNAVAILABLE;
		}
	}

	/**
	 * The broker's envelope (rabbitmq-service contracts/envelope.schema.json) around the stored
	 * payload. The correlation id is not part of it: it travels only as the AMQP property, beside
	 * message_id and type, as the broker's conventions require.
	 */
	Message toMessage(OutboxMessage row) {
		// The fallback is never expected: OutboxWriter always finds one.
		String correlationId = (row.getCorrelationId() != null) ? row.getCorrelationId() : row.getMessageId();

		Map<String, Object> envelope = new LinkedHashMap<>();
		envelope.put("message_id", row.getMessageId());
		envelope.put("message_type", row.getEventType());
		envelope.put("schema_version", Integer.parseInt(row.getSchemaVersion()));
		envelope.put("occurred_at", row.getOccurredAt().toString());
		envelope.put("producer", RabbitConfig.PRODUCER);
		envelope.put("payload", jsonMapper.readTree(row.getPayload()));

		return MessageBuilder.withBody(jsonMapper.writeValueAsString(envelope).getBytes(StandardCharsets.UTF_8))
				.setContentType(MessageProperties.CONTENT_TYPE_JSON)
				.setContentEncoding(StandardCharsets.UTF_8.name())
				.setDeliveryMode(MessageDeliveryMode.PERSISTENT)
				.setMessageId(row.getMessageId())
				.setType(row.getEventType())
				.setCorrelationId(correlationId)
				.build();
	}
}
