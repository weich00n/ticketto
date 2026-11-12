package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.UUID;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.amqp.AmqpRejectAndDontRequeueException;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageBuilder;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;

import com.jayway.jsonpath.JsonPath;
import com.ticketing.reservation_service.config.GatewayHeaders;
import com.ticketing.reservation_service.messaging.EventMessageListener;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.OutboxMessage;
import com.ticketing.reservation_service.model.SaleStatus;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.OutboxRepository;
import com.ticketing.reservation_service.service.OutboxWriter;

/**
 * Event Service's two messages, handed to the listeners as the broker would hand them over:
 * they initialise and cancel exactly as the internal routes do, survive being delivered twice or
 * out of order, and send a message that can never work to the dead-letter queue.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class EventMessagesTest {

	private final String eventId = UUID.randomUUID().toString();

	private final String categoryId = UUID.randomUUID().toString();

	private final String userSub = UUID.randomUUID().toString();

	@Autowired
	private EventMessageListener listener;

	@Autowired
	private MockMvc mockMvc;

	@Autowired
	private HoldRepository holdRepository;

	@Autowired
	private CategoryInventoryRepository inventoryRepository;

	@Autowired
	private EventSaleStatusRepository saleStatusRepository;

	@Autowired
	private OutboxRepository outboxRepository;

	@Autowired
	private StringRedisTemplate redis;

	@AfterEach
	void removeTestData() {
		for (Hold hold : holdRepository.findByEventId(eventId)) {
			outboxRepository.deleteAll(outboxRepository.findByAggregateId(hold.getHoldId()));
		}
		holdRepository.deleteAll(holdRepository.findByEventId(eventId));
		inventoryRepository.findByEventIdOrderByCategoryId(eventId).forEach(inventoryRepository::delete);
		saleStatusRepository.findById(eventId).ifPresent(saleStatusRepository::delete);
		redis.delete(redis.keys("*{" + eventId + "}*"));
	}

	@Test
	void eventPublishedPutsTheEventOnSaleWithItsPrices() throws Exception {
		listener.onEventPublished(eventPublished(500));

		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.status").value("ON_SALE"))
				.andExpect(jsonPath("$.categories[0].category_id").value(categoryId))
				.andExpect(jsonPath("$.categories[0].total").value(500))
				.andExpect(jsonPath("$.categories[0].free").value(500));
		assertThat(inventoryRepository.findByEventIdOrderByCategoryId(eventId).getFirst().getUnitAmount())
				.isEqualTo(8800);
	}

	@Test
	void aRedeliveredEventPublishedChangesNothing() throws Exception {
		Message message = eventPublished(4);
		listener.onEventPublished(message);
		createHold(3);

		listener.onEventPublished(message);

		// the replay did not reset the counter the hold had moved
		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId))
				.andExpect(jsonPath("$.categories[0].free").value(1));
	}

	@Test
	void eventCancelledOvertakingEventPublishedKeepsTheEventCancelled() throws Exception {
		listener.onEventCancelled(eventCancelled());
		listener.onEventPublished(eventPublished(500));

		assertThat(saleStatusRepository.findById(eventId).orElseThrow().getSaleStatus())
				.isEqualTo(SaleStatus.CANCELLED);
		assertThat(inventoryRepository.findByEventIdOrderByCategoryId(eventId)).isEmpty();
	}

	@Test
	void aLateEventPublishedNeverReopensACancelledEvent() throws Exception {
		Message published = eventPublished(4);
		listener.onEventPublished(published);
		listener.onEventCancelled(eventCancelled());

		listener.onEventPublished(published);

		assertThat(saleStatusRepository.findById(eventId).orElseThrow().getSaleStatus())
				.isEqualTo(SaleStatus.CANCELLED);
	}

	@Test
	void eventCancelledReleasesUnpaidHoldsAndOwesRefundsUnderTheMessagesCorrelationId() throws Exception {
		listener.onEventPublished(eventPublished(4));
		String paid = createHold(2);
		mockMvc.perform(post("/internal/v1/reservations/{holdId}/confirm", paid)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "order_id": "%s", "payment_reference": "pi_test" }
						""".formatted(UUID.randomUUID())))
				.andExpect(status().isOk());
		createHold(1);

		Message cancelled = eventCancelled();
		listener.onEventCancelled(cancelled);
		listener.onEventCancelled(cancelled);

		List<OutboxMessage> refunds = outboxRepository.findByAggregateId(paid);
		assertThat(refunds).extracting(OutboxMessage::getEventType).containsExactly(OutboxWriter.REFUND_REQUIRED);
		assertThat(refunds.getFirst().getCorrelationId()).isEqualTo("corr-cancel");
		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId))
				.andExpect(jsonPath("$.status").value("CANCELLED"))
				.andExpect(jsonPath("$.categories[0].free").value(2))
				.andExpect(jsonPath("$.categories[0].sold").value(2));
	}

	@Test
	void aMessageThatCanNeverWorkIsDeadLettered() {
		assertThatThrownBy(() -> listener.onEventPublished(raw("not json")))
				.isInstanceOf(AmqpRejectAndDontRequeueException.class);
		assertThatThrownBy(() -> listener.onEventPublished(envelope("EventCancelled", """
				{ "event_id": "%s", "cancellation_id": "%s" }
				""".formatted(eventId, UUID.randomUUID()), "corr")))
				.isInstanceOf(AmqpRejectAndDontRequeueException.class)
				.hasMessageContaining("expected message_type EventPublished");
		assertThatThrownBy(() -> listener.onEventPublished(envelope("EventPublished", """
				{ "event_id": "%s", "ticket_categories": [] }
				""".formatted(eventId), "corr")))
				.isInstanceOf(AmqpRejectAndDontRequeueException.class)
				.hasMessageContaining("ticketCategories must not be empty");

		assertThat(saleStatusRepository.findById(eventId)).isEmpty();
	}

	@Test
	void anEventPublishedThatContradictsTheStoredInventoryIsDeadLettered() {
		listener.onEventPublished(eventPublished(4));

		assertThatThrownBy(() -> listener.onEventPublished(eventPublished(10)))
				.isInstanceOf(AmqpRejectAndDontRequeueException.class);
	}

	private String createHold(int quantity) throws Exception {
		String body = mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, userSub)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "event_id": "%s", "category_id": "%s", "quantity": %d, "admission_token": "%s" }
						""".formatted(eventId, categoryId, quantity, TestAdmissionTokens.forNewBuyer(eventId, userSub))))
				.andExpect(status().isCreated())
				.andReturn().getResponse().getContentAsString();
		return JsonPath.read(body, "$.hold_id");
	}

	/** Shaped like rabbitmq-service's contracts/examples/EventPublished.json. */
	private Message eventPublished(int totalQuantity) {
		return envelope("EventPublished", """
				{ "event_id": "%s", "event_version": 2, "title": "Autumn Live",
				  "starts_at": "2026-11-20T12:00:00Z", "release_at": "2026-10-20T12:00:00Z",
				  "queue_join_opens_at": "2026-10-20T11:45:00Z", "published_at": "2026-10-06T10:05:00.123456Z",
				  "total_capacity": %d,
				  "ticket_categories": [ { "category_id": "%s", "name": "CAT 1", "price_amount": 8800,
				                           "currency": "SGD", "total_quantity": %d } ] }
				""".formatted(eventId, totalQuantity, categoryId, totalQuantity), "corr-publish");
	}

	/** Shaped like rabbitmq-service's contracts/examples/EventCancelled.json. */
	private Message eventCancelled() {
		return envelope("EventCancelled", """
				{ "event_id": "%s", "event_version": 3, "cancellation_id": "%s",
				  "cancelled_at": "2026-10-06T11:00:00Z", "reason": "Event cancelled by organiser." }
				""".formatted(eventId, UUID.randomUUID()), "corr-cancel");
	}

	/** The broker's envelope; the correlation id travels only as the AMQP property. */
	private Message envelope(String messageType, String payload, String correlationId) {
		Message message = raw("""
				{ "message_id": "%s", "message_type": "%s", "schema_version": 1,
				  "occurred_at": "2026-10-06T10:05:00Z", "producer": "event-service", "payload": %s }
				""".formatted(UUID.randomUUID(), messageType, payload));
		message.getMessageProperties().setCorrelationId(correlationId);
		return message;
	}

	private static Message raw(String body) {
		return MessageBuilder.withBody(body.getBytes(StandardCharsets.UTF_8))
				.setMessageId(UUID.randomUUID().toString())
				.build();
	}
}
