package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doThrow;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.amqp.AmqpConnectException;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.core.ReturnedMessage;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import com.jayway.jsonpath.JsonPath;
import com.ticketing.reservation_service.messaging.OutboxPublisher;
import com.ticketing.reservation_service.model.OutboxMessage;
import com.ticketing.reservation_service.repository.OutboxRepository;

/**
 * The relay from outbox_message to the broker, against a stand-in RabbitTemplate: a row is
 * marked sent only once the broker confirms it, and goes out to reservation.events with its
 * dotted routing key, in the broker's envelope (rabbitmq-service contracts/envelope.schema.json).
 *
 * <p>The test rows are dated in 2001 so they are the oldest unsent rows and lead the batch.
 */
@SpringBootTest
@ActiveProfiles("test")
class OutboxPublisherTest {

	private final String aggregateId = UUID.randomUUID().toString();

	private final List<Message> sent = new ArrayList<>();

	/** "exchange routing.key", one per send, in the order they were sent. */
	private final List<String> destinations = new ArrayList<>();

	@MockitoBean
	private RabbitTemplate rabbitTemplate;

	@Autowired
	private OutboxPublisher publisher;

	@Autowired
	private OutboxRepository outboxRepository;

	@BeforeEach
	void brokerConfirmsEverything() {
		confirmWith(true);
	}

	@AfterEach
	void removeTestData() {
		outboxRepository.deleteAll(outboxRepository.findByAggregateId(aggregateId));
	}

	@Test
	void aConfirmedMessageIsMarkedSentAndGoesOutInTheEnvelope() {
		String id = record("ReservationExpired", "{\"hold_id\":\"h-1\",\"quantity\":2}", "corr-1");

		publisher.publishBatch();

		assertThat(row(id).getPublishedAt()).isNotNull();
		Message message = sentWithId(id);
		assertThat(message.getMessageProperties().getType()).isEqualTo("ReservationExpired");
		assertThat(message.getMessageProperties().getCorrelationId()).isEqualTo("corr-1");
		assertThat(message.getMessageProperties().getContentType()).isEqualTo("application/json");
		assertThat(message.getMessageProperties().getDeliveryMode()).isEqualTo(MessageDeliveryMode.PERSISTENT);

		String body = new String(message.getBody(), StandardCharsets.UTF_8);
		assertThat((String) JsonPath.read(body, "$.message_id")).isEqualTo(id);
		assertThat((String) JsonPath.read(body, "$.message_type")).isEqualTo("ReservationExpired");
		assertThat((Integer) JsonPath.read(body, "$.schema_version")).isEqualTo(1);
		assertThat((String) JsonPath.read(body, "$.occurred_at")).isEqualTo("2001-01-01T00:00:00Z");
		assertThat((String) JsonPath.read(body, "$.producer")).isEqualTo("reservation-service");
		// the stored payload is nested as JSON, not as an escaped string
		assertThat((Integer) JsonPath.read(body, "$.payload.quantity")).isEqualTo(2);
		// the envelope schema allows nothing else; the correlation id is only the AMQP property
		Map<String, Object> envelope = JsonPath.read(body, "$");
		assertThat(envelope).containsOnlyKeys("message_id", "message_type", "schema_version", "occurred_at",
				"producer", "payload");
	}

	@Test
	void eachMessageGoesToOurExchangeWithItsDottedRoutingKey() {
		record("ReservationExpired", "{}", "corr-1");
		record("RefundRequired", "{}", "corr-2");
		record("EventSoldOut", "{}", "corr-3");

		publisher.publishBatch();

		assertThat(destinations).containsSubsequence("reservation.events reservation.expired",
				"reservation.events refund.required", "reservation.events event.sold_out");
	}

	@Test
	void aSentMessageIsNotSentAgain() {
		String id = record("EventSoldOut", "{}", "corr-1");
		publisher.publishBatch();
		sent.clear();

		publisher.publishBatch();

		assertThat(sent).noneMatch(message -> id.equals(message.getMessageProperties().getMessageId()));
	}

	@Test
	void aRefusedMessageIsSkippedSoTheOnesBehindItStillGoOut() {
		String first = record("RefundRequired", "{}", "corr-1");
		String second = record("RefundRequired", "{}", "corr-2");
		refuse(first);

		assertThat(publisher.publishBatch()).isEqualTo(1);

		assertThat(row(first).getPublishedAt()).isNull();
		assertThat(row(first).getAttempts()).isEqualTo(1);
		assertThat(row(second).getPublishedAt()).isNotNull();

		confirmWith(true);
		publisher.publishBatch();
		assertThat(row(first).getPublishedAt()).isNotNull();
	}

	@Test
	void aRefusedMessageGoesBehindNewOnesNextTime() {
		String refused = record("RefundRequired", "{}", "corr-1");
		refuse(refused);
		publisher.publishBatch();
		String newer = record("ReservationExpired", "{}", "corr-2");
		sent.clear();

		confirmWith(true);
		publisher.publishBatch();

		List<String> order = sent.stream().map(message -> message.getMessageProperties().getMessageId()).toList();
		assertThat(order).containsSubsequence(newer, refused);
	}

	@Test
	void anUnroutableMessageIsSkippedToo() {
		String first = record("RefundRequired", "{}", "corr-1");
		String second = record("RefundRequired", "{}", "corr-2");
		doAnswer(invocation -> {
			Message message = invocation.getArgument(2);
			sent.add(message);
			CorrelationData confirmation = invocation.getArgument(3);
			if (first.equals(message.getMessageProperties().getMessageId())) {
				confirmation.setReturned(new ReturnedMessage(message, 312, "NO_ROUTE", invocation.getArgument(0),
						invocation.getArgument(1)));
			}
			confirmation.getFuture().complete(new CorrelationData.Confirm(true, null));
			return null;
		}).when(rabbitTemplate).send(anyString(), anyString(), any(Message.class), any(CorrelationData.class));

		publisher.publishBatch();

		assertThat(row(first).getPublishedAt()).isNull();
		assertThat(row(second).getPublishedAt()).isNotNull();
	}

	@Test
	void anUnreachableBrokerLeavesTheMessageForNextTimeAndStopsTheBatch() {
		doThrow(new AmqpConnectException(new java.net.ConnectException("refused"))).when(rabbitTemplate)
				.send(anyString(), anyString(), any(Message.class), any(CorrelationData.class));
		String id = record("ReservationExpired", "{}", "corr-1");
		String next = record("ReservationExpired", "{}", "corr-2");

		publisher.publish();

		assertThat(row(id).getPublishedAt()).isNull();
		// an outage is not the message's fault, so it uses up none of its attempts
		assertThat(row(id).getAttempts()).isZero();
		// no point waiting on the broker again for the rest
		assertThat(row(next).getAttempts()).isZero();
	}

	@Test
	void aMessageRefusedMaxAttemptsTimesIsGivenUpOn() {
		String refused = record("RefundRequired", "{}", "corr-1");
		refuse(refused);
		for (int i = 0; i < OutboxPublisher.MAX_ATTEMPTS; i++) {
			publisher.publishBatch();
		}
		assertThat(row(refused).getAttempts()).isEqualTo(OutboxPublisher.MAX_ATTEMPTS);
		sent.clear();

		confirmWith(true);
		publisher.publishBatch();

		assertThat(sent).noneMatch(message -> refused.equals(message.getMessageProperties().getMessageId()));
		assertThat(row(refused).getPublishedAt()).isNull();
		assertThat(row(refused).getAttempts()).isEqualTo(OutboxPublisher.MAX_ATTEMPTS);
	}

	private void confirmWith(boolean ack) {
		doAnswer(invocation -> {
			Message message = invocation.getArgument(2);
			sent.add(message);
			destinations.add(invocation.getArgument(0) + " " + invocation.getArgument(1));
			CorrelationData confirmation = invocation.getArgument(3);
			confirmation.getFuture().complete(new CorrelationData.Confirm(ack, ack ? null : "nack"));
			return null;
		}).when(rabbitTemplate).send(anyString(), anyString(), any(Message.class), any(CorrelationData.class));
	}

	/** The broker refuses this one message and confirms every other. */
	private void refuse(String messageId) {
		doAnswer(invocation -> {
			Message message = invocation.getArgument(2);
			sent.add(message);
			boolean ack = !messageId.equals(message.getMessageProperties().getMessageId());
			CorrelationData confirmation = invocation.getArgument(3);
			confirmation.getFuture().complete(new CorrelationData.Confirm(ack, ack ? null : "nack"));
			return null;
		}).when(rabbitTemplate).send(anyString(), anyString(), any(Message.class), any(CorrelationData.class));
	}

	/** Seconds apart, so the two rows of a test keep their order. */
	private String record(String eventType, String payload, String correlationId) {
		String id = UUID.randomUUID().toString();
		Instant occurredAt = Instant.parse("2001-01-01T00:00:00Z")
				.plusSeconds(outboxRepository.findByAggregateId(aggregateId).size());
		outboxRepository.insert(id, eventType, "1", aggregateId, payload, occurredAt, correlationId);
		return id;
	}

	private OutboxMessage row(String id) {
		return outboxRepository.findById(id).orElseThrow();
	}

	private Message sentWithId(String id) {
		return sent.stream()
				.filter(message -> id.equals(message.getMessageProperties().getMessageId()))
				.findFirst()
				.orElseThrow();
	}
}
