package com.ticketing.reservation_service.messaging;

import java.nio.charset.StandardCharsets;
import java.util.Set;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.amqp.AmqpRejectAndDontRequeueException;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.stereotype.Component;

import com.ticketing.reservation_service.config.CorrelationIdFilter;
import com.ticketing.reservation_service.config.RabbitConfig;
import com.ticketing.reservation_service.dto.CancellationView;
import com.ticketing.reservation_service.dto.EventCancelledData;
import com.ticketing.reservation_service.dto.EventPublishedData;
import com.ticketing.reservation_service.exception.ApiException;
import com.ticketing.reservation_service.exception.ErrorCode;
import com.ticketing.reservation_service.service.EventCancellationService;
import com.ticketing.reservation_service.service.InventoryService;

import jakarta.validation.ConstraintViolation;
import jakarta.validation.Validator;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * Event Service's messages in, the way a controller takes HTTP requests in: unwrap and check
 * the message, then hand it to the same service the internal routes use. No business logic.
 *
 * <p>Delivery is at least once, so either message may arrive twice. Both are safe to repeat as
 * they stand - initialising the same inventory again changes nothing, and a second cancellation
 * finds nothing left to cancel - so no record of seen message ids is kept.
 *
 * <p>What happens on failure:
 * <ul>
 * <li>returning normally acknowledges the message and the broker deletes it</li>
 * <li>a message that can never succeed - not JSON, the wrong type, missing fields, or inventory
 * that contradicts what is stored - is rejected without retrying and lands in the broker's
 * dead-letter queue for someone to look at</li>
 * <li>anything else, such as MySQL or Redis being down, is retried in-process a few times and
 * then dead-lettered too (spring.rabbitmq.listener.simple.retry.*)</li>
 * </ul>
 */
@Component
public class EventMessageListener {

	private static final Logger log = LoggerFactory.getLogger(EventMessageListener.class);

	/** Errors that mean the message itself is wrong, so retrying it could never help. */
	private static final Set<ErrorCode> PERMANENT = Set.of(ErrorCode.INVALID_REQUEST, ErrorCode.INVENTORY_CONFLICT);

	private final InventoryService inventoryService;

	private final EventCancellationService cancellationService;

	private final JsonMapper jsonMapper;

	private final Validator validator;

	public EventMessageListener(InventoryService inventoryService, EventCancellationService cancellationService,
			JsonMapper jsonMapper, Validator validator) {
		this.inventoryService = inventoryService;
		this.cancellationService = cancellationService;
		this.jsonMapper = jsonMapper;
		this.validator = validator;
	}

	@RabbitListener(queues = RabbitConfig.EVENT_PUBLISHED_QUEUE)
	public void onEventPublished(Message message) {
		logReceived(message);
		EventPublishedData data = unwrap(message, "EventPublished", EventPublishedData.class);
		handle(message, () -> {
			if (inventoryService.initialiseFromEventPublished(data)) {
				log.info("EventPublished: event {} is on sale with {} categories", data.eventId(),
						data.ticketCategories().size());
			}
			else {
				log.info("EventPublished ignored: event {} is already cancelled", data.eventId());
			}
		});
	}

	@RabbitListener(queues = RabbitConfig.EVENT_CANCELLED_QUEUE)
	public void onEventCancelled(Message message) {
		logReceived(message);
		EventCancelledData data = unwrap(message, "EventCancelled", EventCancelledData.class);
		handle(message, () -> {
			CancellationView result = cancellationService.cancelFromEventCancelled(data.eventId());
			log.info("EventCancelled: event {}, {} hold(s) cancelled, {} refund(s) owed", data.eventId(),
					result.holdsCancelled(), result.refundsRequired());
		});
	}

	/**
	 * Logged before anything is checked, so a message that is then rejected is on record too. The
	 * body only at DEBUG: it is someone else's text, and a message can carry buyer and order ids.
	 */
	private static void logReceived(Message message) {
		MessageProperties props = message.getMessageProperties();
		log.info("Received from {} (routing key {}): message_id={} type={} correlation_id={}",
				props.getConsumerQueue(), props.getReceivedRoutingKey(), props.getMessageId(), props.getType(),
				props.getCorrelationId());
		if (log.isDebugEnabled()) {
			log.debug("Message {} body: {}", props.getMessageId(), new String(message.getBody(), StandardCharsets.UTF_8));
		}
	}

	/**
	 * Runs the work under the message's correlation id, so its logs - and any message it causes,
	 * such as a RefundRequired - can be traced back to the request that started it. An id that
	 * would not fit the outbox's column is not carried on; the hold's own id is used instead.
	 */
	private void handle(Message message, Runnable work) {
		String correlationId = message.getMessageProperties().getCorrelationId();
		if (CorrelationIdFilter.isUsable(correlationId)) {
			MDC.put(CorrelationIdFilter.MDC_KEY, correlationId);
		}
		try {
			work.run();
		}
		catch (ApiException ex) {
			if (PERMANENT.contains(ex.getCode())) {
				throw reject(message, ex.getMessage());
			}
			throw ex;
		}
		finally {
			MDC.remove(CorrelationIdFilter.MDC_KEY);
		}
	}

	/**
	 * The broker's envelope (rabbitmq-service contracts/envelope.schema.json), and its payload
	 * checked against what this service needs from it. The correlation id is the AMQP property;
	 * the envelope does not carry one.
	 */
	private <T> T unwrap(Message message, String expectedType, Class<T> dataType) {
		JsonNode envelope;
		try {
			envelope = jsonMapper.readTree(new String(message.getBody(), StandardCharsets.UTF_8));
		}
		catch (JacksonException ex) {
			throw reject(message, "body is not JSON");
		}
		if (!expectedType.equals(envelope.path("message_type").asString(null))) {
			throw reject(message, "expected message_type " + expectedType);
		}
		if (envelope.path("schema_version").asInt(0) != 1) {
			throw reject(message, "unsupported schema_version " + envelope.path("schema_version"));
		}
		if (!envelope.path("payload").isObject()) {
			throw reject(message, "envelope has no payload");
		}

		T data;
		try {
			data = jsonMapper.treeToValue(envelope.get("payload"), dataType);
		}
		catch (JacksonException ex) {
			throw reject(message, "payload does not match " + expectedType + ": " + ex.getOriginalMessage());
		}
		Set<ConstraintViolation<T>> violations = validator.validate(data);
		if (!violations.isEmpty()) {
			ConstraintViolation<T> first = violations.iterator().next();
			throw reject(message, "payload." + first.getPropertyPath() + " " + first.getMessage());
		}
		return data;
	}

	private static AmqpRejectAndDontRequeueException reject(Message message, String reason) {
		String messageId = message.getMessageProperties().getMessageId();
		log.error("Dead-lettering message {}: {}", messageId, reason);
		return new AmqpRejectAndDontRequeueException("Message " + messageId + " rejected: " + reason);
	}
}
