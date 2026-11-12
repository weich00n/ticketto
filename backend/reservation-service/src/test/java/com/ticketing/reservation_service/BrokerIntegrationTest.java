package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.Map;
import java.util.UUID;
import java.util.function.BooleanSupplier;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageBuilder;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitAdmin;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.test.context.ActiveProfiles;

import com.jayway.jsonpath.JsonPath;
import com.ticketing.reservation_service.config.RabbitConfig;
import com.ticketing.reservation_service.messaging.OutboxPublisher;
import com.ticketing.reservation_service.model.SaleStatus;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.OutboxRepository;

/**
 * The same messages as EventMessagesTest and OutboxPublisherTest, but through a real RabbitMQ
 * with Ticketto's topology instead of calling the listener directly or mocking RabbitTemplate.
 * This is what those tests cannot see: that our queues are bound to Event Service's exchange,
 * that a rejected message really reaches our dead-letter queue, and that the broker confirms
 * and routes what the outbox sends.
 *
 * <p>The topology in src/test/resources/rabbitmq/definitions.json is a copy of rabbitmq-service's
 * topology/definitions.json, loaded through the management API before the context starts (the
 * listeners refuse to start if their queues are missing). When rabbitmq-service changes its
 * topology, copy the file again.
 *
 * <p>Needs a broker with the management plugin: a plain rabbitmq:*-management container in CI and
 * docker compose, or rabbitmq-service's own local broker, whose admin login is the default here.
 */
@SpringBootTest(properties = "spring.rabbitmq.listener.simple.auto-startup=true")
@ActiveProfiles("test")
class BrokerIntegrationTest {

	private static final Duration TIMEOUT = Duration.ofSeconds(15);

	private final String eventId = UUID.randomUUID().toString();

	private final String categoryId = UUID.randomUUID().toString();

	private final String aggregateId = UUID.randomUUID().toString();

	/** Stands in for Checkout's queue, so the test never takes messages from a shared broker's. */
	private final String probeQueue = "reservation-service.test-" + UUID.randomUUID();

	@Autowired
	private RabbitTemplate rabbitTemplate;

	@Autowired
	private ConnectionFactory connectionFactory;

	@Autowired
	private OutboxPublisher publisher;

	@Autowired
	private OutboxRepository outboxRepository;

	@Autowired
	private CategoryInventoryRepository inventoryRepository;

	@Autowired
	private EventSaleStatusRepository saleStatusRepository;

	@Autowired
	private StringRedisTemplate redis;

	private RabbitAdmin admin;

	@BeforeAll
	static void loadTickettoTopology() throws Exception {
		String managementUrl = env("RABBITMQ_MANAGEMENT_URL", "http://localhost:15672");
		String login = env("RABBITMQ_USERNAME", "ticketto") + ":" + env("RABBITMQ_PASSWORD", "ticketto-local");
		byte[] definitions;
		try (InputStream in = BrokerIntegrationTest.class.getResourceAsStream("/rabbitmq/definitions.json")) {
			definitions = in.readAllBytes();
		}
		HttpRequest request = HttpRequest.newBuilder(URI.create(managementUrl + "/api/definitions"))
				.header("Authorization",
						"Basic " + Base64.getEncoder().encodeToString(login.getBytes(StandardCharsets.UTF_8)))
				.header("Content-Type", "application/json")
				.POST(HttpRequest.BodyPublishers.ofByteArray(definitions))
				.build();

		// The broker's ports can open before the management API is ready to take definitions.
		// HTTP/1.1: the JDK client's default h2c upgrade makes the management API hang up.
		HttpClient http = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build();
		Instant deadline = Instant.now().plus(Duration.ofSeconds(60));
		while (true) {
			try {
				HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
				if (response.statusCode() / 100 == 2) {
					return;
				}
				if (Instant.now().isAfter(deadline)) {
					throw new IllegalStateException("Broker refused the topology: HTTP " + response.statusCode()
							+ " " + response.body());
				}
			}
			catch (IOException ex) {
				if (Instant.now().isAfter(deadline)) {
					throw new IllegalStateException("No RabbitMQ management API at " + managementUrl, ex);
				}
			}
			Thread.sleep(1000);
		}
	}

	@BeforeEach
	void bindProbeQueue() {
		admin = new RabbitAdmin(connectionFactory);
		// The vhost defaults to quorum queues; a throwaway queue need not be replicated.
		admin.declareQueue(new Queue(probeQueue, true, false, false, Map.of("x-queue-type", "classic")));
		admin.declareBinding(new Binding(probeQueue, Binding.DestinationType.QUEUE, RabbitConfig.EXCHANGE,
				"reservation.expired", null));
	}

	@AfterEach
	void removeTestData() {
		admin.deleteQueue(probeQueue);
		outboxRepository.deleteAll(outboxRepository.findByAggregateId(aggregateId));
		inventoryRepository.findByEventIdOrderByCategoryId(eventId).forEach(inventoryRepository::delete);
		saleStatusRepository.findById(eventId).ifPresent(saleStatusRepository::delete);
		redis.delete(redis.keys("*{" + eventId + "}*"));
	}

	@Test
	void eventPublishedOnEventServicesExchangeReachesOurListener() {
		rabbitTemplate.send("event.events", "event.published", envelope("EventPublished", """
				{ "event_id": "%s", "event_version": 2, "title": "Autumn Live",
				  "starts_at": "2026-11-20T12:00:00Z", "release_at": "2026-10-20T12:00:00Z",
				  "queue_join_opens_at": "2026-10-20T11:45:00Z", "published_at": "2026-10-06T10:05:00.123456Z",
				  "total_capacity": 500,
				  "ticket_categories": [ { "category_id": "%s", "name": "CAT 1", "price_amount": 8800,
				                           "currency": "SGD", "total_quantity": 500 } ] }
				""".formatted(eventId, categoryId)));

		waitFor(() -> saleStatusRepository.findById(eventId).isPresent());
		assertThat(saleStatusRepository.findById(eventId).orElseThrow().getSaleStatus()).isEqualTo(SaleStatus.ON_SALE);
		assertThat(inventoryRepository.findByEventIdOrderByCategoryId(eventId)).singleElement()
				.satisfies(category -> assertThat(category.getUnitAmount()).isEqualTo(8800));
	}

	@Test
	void aMessageThatCanNeverWorkLandsInOurDeadLetterQueue() {
		String messageId = UUID.randomUUID().toString();
		rabbitTemplate.send("event.events", "event.published", MessageBuilder
				.withBody("not json".getBytes(StandardCharsets.UTF_8))
				.setMessageId(messageId)
				.build());

		Message deadLettered = receive(RabbitConfig.EVENT_PUBLISHED_QUEUE + ".dlq", messageId);
		assertThat(new String(deadLettered.getBody(), StandardCharsets.UTF_8)).isEqualTo("not json");
	}

	@Test
	void theBrokerConfirmsAndRoutesWhatTheOutboxSends() {
		// Dated 2001 so it is the oldest unsent row and leads the batch.
		String messageId = UUID.randomUUID().toString();
		outboxRepository.insert(messageId, "ReservationExpired", "1", aggregateId,
				"{\"hold_id\":\"%s\",\"quantity\":2}".formatted(aggregateId),
				Instant.parse("2001-01-01T00:00:00Z"), "corr-broker");

		publisher.publishBatch();

		// Marked sent only on the broker's confirm, and only if the message was routed somewhere.
		assertThat(outboxRepository.findById(messageId).orElseThrow().getPublishedAt()).isNotNull();
		Message message = receive(probeQueue, messageId);
		assertThat(message.getMessageProperties().getType()).isEqualTo("ReservationExpired");
		assertThat(message.getMessageProperties().getCorrelationId()).isEqualTo("corr-broker");
		String body = new String(message.getBody(), StandardCharsets.UTF_8);
		assertThat((String) JsonPath.read(body, "$.producer")).isEqualTo(RabbitConfig.PRODUCER);
		assertThat((String) JsonPath.read(body, "$.payload.hold_id")).isEqualTo(aggregateId);
	}

	/** The message with this id from the queue, skipping any others already waiting there. */
	private Message receive(String queue, String messageId) {
		Instant deadline = Instant.now().plus(TIMEOUT);
		while (Instant.now().isBefore(deadline)) {
			Message message = rabbitTemplate.receive(queue, 1000);
			if (message != null && messageId.equals(message.getMessageProperties().getMessageId())) {
				return message;
			}
		}
		throw new AssertionError("Message " + messageId + " never reached " + queue);
	}

	private static void waitFor(BooleanSupplier condition) {
		Instant deadline = Instant.now().plus(TIMEOUT);
		while (!condition.getAsBoolean()) {
			if (Instant.now().isAfter(deadline)) {
				throw new AssertionError("Timed out after " + TIMEOUT);
			}
			try {
				Thread.sleep(200);
			}
			catch (InterruptedException ex) {
				Thread.currentThread().interrupt();
				throw new AssertionError(ex);
			}
		}
	}

	/** The broker's envelope; the correlation id travels only as the AMQP property. */
	private static Message envelope(String messageType, String payload) {
		String messageId = UUID.randomUUID().toString();
		return MessageBuilder.withBody("""
				{ "message_id": "%s", "message_type": "%s", "schema_version": 1,
				  "occurred_at": "2026-10-06T10:05:00Z", "producer": "event-service", "payload": %s }
				""".formatted(messageId, messageType, payload).getBytes(StandardCharsets.UTF_8))
				.setMessageId(messageId)
				.setCorrelationId("corr-broker")
				.build();
	}

	private static String env(String name, String fallback) {
		String value = System.getenv(name);
		return (value == null || value.isBlank()) ? fallback : value;
	}
}
