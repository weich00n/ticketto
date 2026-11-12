package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;

import com.jayway.jsonpath.JsonPath;
import com.ticketing.reservation_service.config.CorrelationIdFilter;
import com.ticketing.reservation_service.config.GatewayHeaders;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.OutboxMessage;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.OutboxRepository;
import com.ticketing.reservation_service.repository.RedisKeys;
import com.ticketing.reservation_service.service.HoldExpiryWorker;
import com.ticketing.reservation_service.service.OutboxWriter;

/**
 * The sad-path messages are recorded when the change they announce happens, once, and the happy
 * path records none. RefundRequired is covered by {@link EventCancellationTest}.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class OutboxMessagesTest {

	private final String eventId = UUID.randomUUID().toString();

	/** Four tickets. */
	private final String bigCategory = UUID.randomUUID().toString();

	/** Two tickets. */
	private final String smallCategory = UUID.randomUUID().toString();

	private final String userSub = UUID.randomUUID().toString();

	@Autowired
	private MockMvc mockMvc;

	@Autowired
	private HoldExpiryWorker expiryWorker;

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

	@BeforeEach
	void seedSixTicketsInTwoCategories() throws Exception {
		mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", eventId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "categories": [
						    { "category_id": "%s", "total_quantity": 4, "unit_amount": 28800 },
						    { "category_id": "%s", "total_quantity": 2, "unit_amount": 8800 } ] }
						""".formatted(bigCategory, smallCategory)))
				.andExpect(status().isOk());
	}

	@AfterEach
	void removeTestData() {
		for (Hold hold : holdRepository.findByEventId(eventId)) {
			outboxRepository.deleteAll(outboxRepository.findByAggregateId(hold.getHoldId()));
		}
		outboxRepository.deleteAll(outboxRepository.findByAggregateId(eventId));
		holdRepository.deleteAll(holdRepository.findByEventId(eventId));
		inventoryRepository.findByEventIdOrderByCategoryId(eventId).forEach(inventoryRepository::delete);
		saleStatusRepository.deleteById(eventId);
		redis.delete(redis.keys("*{" + eventId + "}*"));
	}

	@Test
	void holdingReleasingAndPayingRecordNothing() throws Exception {
		String paid = createHold(bigCategory, 2, "corr-paid");
		confirm(paid, UUID.randomUUID().toString()).andExpect(status().isOk());
		String released = createHold(bigCategory, 1, "corr-released", userSub);
		mockMvc.perform(delete("/api/v1/reservations/{holdId}", released).header(GatewayHeaders.USER_SUB, userSub))
				.andExpect(status().isOk());

		// the happy path is answered over HTTP; no other service consumes a message about it
		assertThat(typesFor(paid)).isEmpty();
		assertThat(typesFor(released)).isEmpty();
		assertThat(typesFor(eventId)).isEmpty();
	}

	@Test
	void anExpiredHoldIsAnnouncedOnceUnderTheRequestThatCreatedIt() throws Exception {
		String holdId = createHold(bigCategory, 2, "corr-expire");
		letTimePass(holdId, bigCategory);

		assertThat(expiryWorker.sweepOnce()).isEqualTo(1);
		assertThat(expiryWorker.sweepOnce()).isZero();

		assertThat(typesFor(holdId)).containsExactly(OutboxWriter.RESERVATION_EXPIRED);
		// the sweep runs outside any request, so the hold's own correlation id is carried on
		assertThat(only(holdId, OutboxWriter.RESERVATION_EXPIRED).getCorrelationId()).isEqualTo("corr-expire");
	}

	@Test
	void holdingTheEventsLastTicketIsNotASellOut() throws Exception {
		// every ticket is held, but each hold may still expire and put its tickets back
		createHold(smallCategory, 2, "corr-small");
		createHold(bigCategory, 4, "corr-big");

		assertThat(typesFor(eventId)).isEmpty();
	}

	@Test
	void confirmingTheEventsLastUnsoldTicketSellsItOut() throws Exception {
		confirm(createHold(smallCategory, 2, "corr-small"), UUID.randomUUID().toString()).andExpect(status().isOk());
		confirm(createHold(bigCategory, 3, "corr-big"), UUID.randomUUID().toString()).andExpect(status().isOk());
		assertThat(typesFor(eventId)).isEmpty();

		String last = createHold(bigCategory, 1, "corr-last");
		assertThat(typesFor(eventId)).isEmpty();
		mockMvc.perform(post("/internal/v1/reservations/{holdId}/confirm", last)
				.header(CorrelationIdFilter.HEADER, "corr-last-paid")
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "order_id": "%s", "payment_reference": "pi_test" }
						""".formatted(UUID.randomUUID())))
				.andExpect(status().isOk());

		assertThat(typesFor(eventId)).containsExactly(OutboxWriter.EVENT_SOLD_OUT);
		OutboxMessage message = only(eventId, OutboxWriter.EVENT_SOLD_OUT);
		assertThat((String) JsonPath.read(message.getPayload(), "$.event_id")).isEqualTo(eventId);
		assertThat(message.getCorrelationId()).isEqualTo("corr-last-paid");
	}

	@Test
	void anExpiredLastHoldNeverSoldTheEventOut() throws Exception {
		confirm(createHold(smallCategory, 2, "corr-small"), UUID.randomUUID().toString()).andExpect(status().isOk());
		String last = createHold(bigCategory, 4, "corr-big");
		letTimePass(last, bigCategory);
		assertThat(expiryWorker.sweepOnce()).isEqualTo(1);

		// the four tickets are back on sale, so Queue must keep admitting
		assertThat(typesFor(eventId)).isEmpty();
	}

	@Test
	void repeatingTheSellingConfirmationAnnouncesItOnce() throws Exception {
		confirm(createHold(smallCategory, 2, "corr-small"), UUID.randomUUID().toString()).andExpect(status().isOk());
		String last = createHold(bigCategory, 4, "corr-big");
		String orderId = UUID.randomUUID().toString();
		confirm(last, orderId).andExpect(status().isOk());
		confirm(last, orderId).andExpect(status().isOk());

		assertThat(typesFor(eventId)).containsExactly(OutboxWriter.EVENT_SOLD_OUT);
	}

	private String createHold(String categoryId, int quantity, String correlationId) throws Exception {
		return createHold(categoryId, quantity, correlationId, UUID.randomUUID().toString());
	}

	private String createHold(String categoryId, int quantity, String correlationId, String buyerSub)
			throws Exception {
		// a different buyer each time unless the test says otherwise, so the five-ticket limit
		// never interferes
		String token = TestAdmissionTokens.forNewBuyer(eventId, buyerSub);
		String body = mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, buyerSub)
				.header(CorrelationIdFilter.HEADER, correlationId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "event_id": "%s", "category_id": "%s", "quantity": %d, "admission_token": "%s" }
						""".formatted(eventId, categoryId, quantity, token)))
				.andExpect(status().isCreated())
				.andReturn().getResponse().getContentAsString();
		return JsonPath.read(body, "$.hold_id");
	}

	private ResultActions confirm(String holdId, String orderId) throws Exception {
		return mockMvc.perform(post("/internal/v1/reservations/{holdId}/confirm", holdId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "order_id": "%s", "payment_reference": "pi_test" }
						""".formatted(orderId)));
	}

	/** Drops the hold's key and backdates its deadline, exactly as six minutes passing would. */
	private void letTimePass(String holdId, String categoryId) {
		redis.delete(RedisKeys.hold(eventId, holdId));
		redis.opsForZSet().add(RedisKeys.expiryIndex(eventId, categoryId), holdId,
				Instant.now().minusSeconds(60).getEpochSecond());
		holdRepository.findById(holdId).ifPresent(hold -> {
			hold.setHoldExpiresAt(Instant.now().minusSeconds(60));
			holdRepository.save(hold);
		});
	}

	private List<String> typesFor(String aggregateId) {
		return outboxRepository.findByAggregateId(aggregateId).stream()
				.map(OutboxMessage::getEventType)
				.toList();
	}

	private OutboxMessage only(String aggregateId, String eventType) {
		List<OutboxMessage> matching = outboxRepository.findByAggregateId(aggregateId).stream()
				.filter(message -> message.getEventType().equals(eventType))
				.toList();
		assertThat(matching).hasSize(1);
		return matching.getFirst();
	}
}
