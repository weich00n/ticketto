package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

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
import com.ticketing.reservation_service.config.GatewayHeaders;
import com.ticketing.reservation_service.model.CategoryInventoryId;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.HoldStatus;
import com.ticketing.reservation_service.model.OutboxMessage;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.OutboxRepository;
import com.ticketing.reservation_service.repository.RedisKeys;
import com.ticketing.reservation_service.service.OutboxWriter;

/**
 * Cancelling an event mid-sale leaves every hold either released or sold with a refund owed,
 * and never both.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class EventCancellationTest {

	private final String eventId = UUID.randomUUID().toString();

	private final String categoryId = UUID.randomUUID().toString();

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

	@BeforeEach
	void seedFortyTickets() throws Exception {
		seed("ON_SALE").andExpect(status().isOk());
	}

	@AfterEach
	void removeTestData() {
		for (Hold hold : holdRepository.findByEventId(eventId)) {
			outboxRepository.deleteAll(outboxRepository.findByAggregateId(hold.getHoldId()));
		}
		holdRepository.deleteAll(holdRepository.findByEventId(eventId));
		inventoryRepository.findByEventIdOrderByCategoryId(eventId).forEach(inventoryRepository::delete);
		saleStatusRepository.deleteById(eventId);
		redis.delete(redis.keys("*{" + eventId + "}*"));
	}

	@Test
	void unpaidHoldsAreReleasedWithNoMessage() throws Exception {
		String first = createHold(2);
		String second = createHold(3);

		cancel().andExpect(status().isOk())
				.andExpect(jsonPath("$.sale_status").value("CANCELLED"))
				.andExpect(jsonPath("$.holds_cancelled").value(2))
				.andExpect(jsonPath("$.refunds_required").value(0));

		for (String holdId : List.of(first, second)) {
			assertThat(statusOf(holdId)).isEqualTo(HoldStatus.CANCELLED);
			// everyone who cares already hears of it from EventCancelled
			assertThat(messagesFor(holdId)).isEmpty();
		}
		// the tickets came back, and nothing was sold
		assertThat(available()).isEqualTo(40);
		assertThat(sold()).isZero();
	}

	@Test
	void aPaidHoldStaysSoldAndOwesARefund() throws Exception {
		String holdId = createHold(2);
		String orderId = UUID.randomUUID().toString();
		confirm(holdId, orderId).andExpect(status().isOk());

		cancel().andExpect(status().isOk())
				.andExpect(jsonPath("$.holds_cancelled").value(0))
				.andExpect(jsonPath("$.refunds_required").value(1));

		assertThat(statusOf(holdId)).isEqualTo(HoldStatus.CONFIRMED);
		assertThat(sold()).isEqualTo(2);
		List<OutboxMessage> messages = outboxRepository.findByAggregateId(holdId);
		assertThat(messages).extracting(OutboxMessage::getEventType).containsExactly(OutboxWriter.REFUND_REQUIRED);
		String payload = messages.getFirst().getPayload();
		assertThat((String) JsonPath.read(payload, "$.order_id")).isEqualTo(orderId);
		assertThat((Integer) JsonPath.read(payload, "$.total_amount")).isEqualTo(57600);
		assertThat((String) JsonPath.read(payload, "$.reason")).isEqualTo("EVENT_CANCELLED");
	}

	@Test
	void cancellingAgainChangesNothingAndRecordsNothingTwice() throws Exception {
		String unpaid = createHold(1);
		String paid = createHold(1);
		confirm(paid, UUID.randomUUID().toString()).andExpect(status().isOk());
		cancel().andExpect(status().isOk());

		cancel().andExpect(status().isOk())
				.andExpect(jsonPath("$.holds_cancelled").value(0))
				.andExpect(jsonPath("$.refunds_required").value(0));

		assertThat(messagesFor(unpaid)).isEmpty();
		assertThat(messagesFor(paid)).hasSize(1);
		assertThat(available()).isEqualTo(39);
	}

	@Test
	void noNewHoldIsTakenOnceCancelled() throws Exception {
		cancel().andExpect(status().isOk());

		mockMvc.perform(holdRequest(1))
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("EVENT_CANCELLED"));
		// hold.lua reads this key, so it refuses even before MySQL is asked
		assertThat(redis.opsForValue().get(RedisKeys.saleStatus(eventId))).isEqualTo("CANCELLED");
	}

	@Test
	void aPaymentArrivingAfterCancellationIsRefusedSoCheckoutRefunds() throws Exception {
		String holdId = createHold(2);
		cancel().andExpect(status().isOk());

		confirm(holdId, UUID.randomUUID().toString())
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("EVENT_CANCELLED"));

		assertThat(statusOf(holdId)).isEqualTo(HoldStatus.CANCELLED);
		assertThat(sold()).isZero();
		// Checkout owns this refund from the 409; no RefundRequired is owed on top of it
		assertThat(messagesFor(holdId)).isEmpty();
	}

	@Test
	void anOlderPublishMessageCannotReopenACancelledEvent() throws Exception {
		cancel().andExpect(status().isOk());

		seed("ON_SALE")
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("INVENTORY_CONFLICT"));

		assertThat(redis.opsForValue().get(RedisKeys.saleStatus(eventId))).isEqualTo("CANCELLED");
	}

	@Test
	void anUnknownEventCannotBeCancelled() throws Exception {
		mockMvc.perform(post("/internal/v1/events/{eventId}/cancellation", UUID.randomUUID().toString()))
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("EVENT_NOT_FOUND"));
	}

	@Test
	void paymentsRacingCancellationEachEndOneWayOnly() throws Exception {
		// the section 11 shape for S3A: payments landing at the instant the event is cancelled
		int buyers = 8;
		List<String> holdIds = new ArrayList<>();
		for (int i = 0; i < buyers; i++) {
			holdIds.add(createHold(1));
		}

		CyclicBarrier startTogether = new CyclicBarrier(buyers + 1);
		List<Callable<Integer>> work = new ArrayList<>();
		for (String holdId : holdIds) {
			work.add(() -> {
				startTogether.await();
				return confirm(holdId, UUID.randomUUID().toString()).andReturn().getResponse().getStatus();
			});
		}
		work.add(() -> {
			startTogether.await();
			return cancel().andReturn().getResponse().getStatus();
		});
		try (ExecutorService pool = Executors.newFixedThreadPool(work.size())) {
			for (Future<Integer> result : pool.invokeAll(work)) {
				result.get();
			}
		}

		int soldHolds = 0;
		for (String holdId : holdIds) {
			HoldStatus status = statusOf(holdId);
			List<String> messages = messagesFor(holdId);
			if (status == HoldStatus.CONFIRMED) {
				// paid and the event is gone: exactly one refund, never a release as well
				assertThat(messages).containsExactly(OutboxWriter.REFUND_REQUIRED);
				soldHolds++;
			}
			else {
				// released by the cancellation; a payment that lost was refused, so Checkout refunds
				assertThat(status).isEqualTo(HoldStatus.CANCELLED);
				assertThat(messages).isEmpty();
			}
		}
		assertThat(sold()).isEqualTo(soldHolds);
		assertThat(available()).isEqualTo(40 - soldHolds);
	}

	private ResultActions cancel() throws Exception {
		return mockMvc.perform(post("/internal/v1/events/{eventId}/cancellation", eventId));
	}

	private ResultActions seed(String saleStatus) throws Exception {
		return mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", eventId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "sale_status": "%s",
						  "categories": [ { "category_id": "%s", "total_quantity": 40, "unit_amount": 28800 } ] }
						""".formatted(saleStatus, categoryId)));
	}

	private ResultActions confirm(String holdId, String orderId) throws Exception {
		return mockMvc.perform(post("/internal/v1/reservations/{holdId}/confirm", holdId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "order_id": "%s", "payment_reference": "pi_test" }
						""".formatted(orderId)));
	}

	private String createHold(int quantity) throws Exception {
		String body = mockMvc.perform(holdRequest(quantity))
				.andExpect(status().isCreated())
				.andReturn().getResponse().getContentAsString();
		return JsonPath.read(body, "$.hold_id");
	}

	private org.springframework.test.web.servlet.RequestBuilder holdRequest(int quantity) {
		// a different buyer each time, so the five-ticket limit never interferes
		String buyerSub = UUID.randomUUID().toString();
		String token = TestAdmissionTokens.forNewBuyer(eventId, buyerSub);
		return post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, buyerSub)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "event_id": "%s", "category_id": "%s", "quantity": %d, "admission_token": "%s" }
						""".formatted(eventId, categoryId, quantity, token));
	}

	private HoldStatus statusOf(String holdId) {
		return holdRepository.findById(holdId).orElseThrow().getStatus();
	}

	private List<String> messagesFor(String holdId) {
		return outboxRepository.findByAggregateId(holdId).stream().map(OutboxMessage::getEventType).toList();
	}

	private int available() {
		return Integer.parseInt(redis.opsForValue().get(RedisKeys.available(eventId, categoryId)));
	}

	private int sold() {
		return inventoryRepository.findById(new CategoryInventoryId(eventId, categoryId))
				.orElseThrow()
				.getSoldQuantity();
	}
}
