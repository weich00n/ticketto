package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.time.Instant;
import java.util.UUID;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;

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

import com.ticketing.reservation_service.config.GatewayHeaders;
import com.ticketing.reservation_service.model.CategoryInventoryId;
import com.ticketing.reservation_service.model.EventSaleStatus;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.HoldStatus;
import com.ticketing.reservation_service.model.SaleStatus;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.RedisKeys;
import com.ticketing.reservation_service.service.HoldExpiryWorker;

/**
 * Confirmation: a paid hold becomes sold exactly once, a retry is a success rather than an
 * error, and a clashing order is refused.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class ConfirmationTest {

	private final String eventId = UUID.randomUUID().toString();

	private final String categoryId = UUID.randomUUID().toString();

	private final String userSub = UUID.randomUUID().toString();

	@Autowired
	private MockMvc mockMvc;

	@Autowired
	private HoldRepository holdRepository;

	@Autowired
	private CategoryInventoryRepository inventoryRepository;

	@Autowired
	private EventSaleStatusRepository saleStatusRepository;

	@Autowired
	private HoldExpiryWorker expiryWorker;

	@Autowired
	private StringRedisTemplate redis;

	@BeforeEach
	void seedTenTickets() throws Exception {
		mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", eventId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "categories": [ { "category_id": "%s", "total_quantity": 10, "unit_amount": 28800 } ] }
						""".formatted(categoryId)))
				.andExpect(status().isOk());
	}

	@AfterEach
	void removeTestData() {
		holdRepository.deleteAll(holdRepository.findByEventId(eventId));
		inventoryRepository.findByEventIdOrderByCategoryId(eventId).forEach(inventoryRepository::delete);
		saleStatusRepository.deleteById(eventId);
		redis.delete(redis.keys("*{" + eventId + "}*"));
	}

	@Test
	void confirmingSellsTheTicketsAndLeavesAvailabilityWhereItWas() throws Exception {
		String holdId = createHold(3);
		String orderId = UUID.randomUUID().toString();
		assertThat(available()).isEqualTo(7);

		confirm(holdId, orderId, "pi_3QabcdEFGH12345")
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.hold_id").value(holdId))
				.andExpect(jsonPath("$.order_id").value(orderId))
				.andExpect(jsonPath("$.status").value("CONFIRMED"))
				.andExpect(jsonPath("$.quantity").value(3))
				.andExpect(jsonPath("$.duplicate").value(false))
				.andExpect(jsonPath("$.confirmed_at").isNotEmpty());

		// held became sold: the counter does not move, because those tickets left it already
		assertThat(available()).isEqualTo(7);
		assertThat(sold()).isEqualTo(3);
		assertThat(holdRepository.findById(holdId)).get().satisfies(hold -> {
			assertThat(hold.getStatus()).isEqualTo(HoldStatus.CONFIRMED);
			assertThat(hold.getOrderId()).isEqualTo(orderId);
			assertThat(hold.getPaymentReference()).isEqualTo("pi_3QabcdEFGH12345");
			assertThat(hold.getConfirmedAt()).isNotNull();
		});
		// a confirmed hold must not look confirmable any more
		assertThat(redis.hasKey(RedisKeys.hold(eventId, holdId))).isFalse();
		// and the sweep must never see it again
		assertThat(redis.opsForZSet().size(RedisKeys.expiryIndex(eventId, categoryId))).isZero();
	}

	@Test
	void confirmingTwiceSucceedsBothTimesAndSellsTheTicketsOnce() throws Exception {
		String holdId = createHold(2);
		String orderId = UUID.randomUUID().toString();

		confirm(holdId, orderId, "pi_first")
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.duplicate").value(false));
		String secondBody = confirm(holdId, orderId, "pi_first")
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.duplicate").value(true))
				.andExpect(jsonPath("$.status").value("CONFIRMED"))
				.andReturn().getResponse().getContentAsString();

		// sold_quantity moved exactly once
		assertThat(sold()).isEqualTo(2);
		// and the retry reports the original confirmation time, not a new one
		Instant confirmedAt = holdRepository.findById(holdId).orElseThrow().getConfirmedAt();
		assertThat(Instant.parse(com.jayway.jsonpath.JsonPath.read(secondBody, "$.confirmed_at")))
				.isEqualTo(confirmedAt);
	}

	@Test
	void adifferentOrderConfirmingTheSameHoldIsRefused() throws Exception {
		String holdId = createHold(2);

		confirm(holdId, UUID.randomUUID().toString(), "pi_first").andExpect(status().isOk());
		confirm(holdId, UUID.randomUUID().toString(), "pi_second")
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("ORDER_MISMATCH"));

		assertThat(sold()).isEqualTo(2);
	}

	@Test
	void anUnknownHoldIsNotFound() throws Exception {
		confirm(UUID.randomUUID().toString(), UUID.randomUUID().toString(), "pi_x")
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("HOLD_NOT_FOUND"));
	}

	@Test
	void aLapsedHoldCannotBeConfirmedSoCheckoutCanRefund() throws Exception {
		String holdId = createHold(2);
		// the hold's own key dies; nothing else has run yet
		redis.delete(RedisKeys.hold(eventId, holdId));

		confirm(holdId, UUID.randomUUID().toString(), "pi_late")
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("HOLD_EXPIRED"));

		assertThat(sold()).isZero();
	}

	@Test
	void aHoldTheBuyerReleasedCannotBeConfirmed() throws Exception {
		String holdId = createHold(2);
		mockMvc.perform(delete("/api/v1/reservations/{holdId}", holdId).header(GatewayHeaders.USER_SUB, userSub))
				.andExpect(status().isOk());

		confirm(holdId, UUID.randomUUID().toString(), "pi_late")
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("HOLD_EXPIRED"));

		assertThat(sold()).isZero();
		assertThat(available()).isEqualTo(10);
	}

	@Test
	void aConfirmationAfterTheEventIsCancelledIsRefused() throws Exception {
		String holdId = createHold(2);
		EventSaleStatus sale = saleStatusRepository.findById(eventId).orElseThrow();
		sale.setSaleStatus(SaleStatus.CANCELLED);
		saleStatusRepository.save(sale);

		confirm(holdId, UUID.randomUUID().toString(), "pi_x")
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("EVENT_CANCELLED"));

		assertThat(sold()).isZero();
	}

	@Test
	void aMissingOrderIdIsRejected() throws Exception {
		String holdId = createHold(1);

		mockMvc.perform(post("/internal/v1/reservations/{holdId}/confirm", holdId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("{\"payment_reference\": \"pi_x\"}"))
				.andExpect(status().isBadRequest())
				.andExpect(jsonPath("$.error.code").value("INVALID_REQUEST"));
	}

	@Test
	void confirmationRacingExpiryYieldsOneOutcome() throws Exception {
		String holdId = createHold(4);
		String orderId = UUID.randomUUID().toString();
		// the sharp case from section 11: the hold is due for expiry at the same moment the
		// payment lands, and its key is still alive so both callers believe they may act
		redis.opsForZSet().add(RedisKeys.expiryIndex(eventId, categoryId), holdId,
				Instant.now().minusSeconds(60).getEpochSecond());
		holdRepository.findById(holdId).ifPresent(hold -> {
			hold.setHoldExpiresAt(Instant.now().minusSeconds(60));
			holdRepository.save(hold);
		});

		AtomicInteger confirmed = new AtomicInteger();
		AtomicInteger expired = new AtomicInteger();
		CyclicBarrier startTogether = new CyclicBarrier(2);

		try (ExecutorService pool = Executors.newFixedThreadPool(2)) {
			Future<?> confirmation = pool.submit(() -> {
				startTogether.await();
				if (confirm(holdId, orderId, "pi_race").andReturn().getResponse().getStatus() == 200) {
					confirmed.incrementAndGet();
				}
				return null;
			});
			Future<?> sweep = pool.submit(() -> {
				startTogether.await();
				expired.addAndGet(expiryWorker.sweepOnce());
				return null;
			});
			confirmation.get();
			sweep.get();
		}

		// exactly one of them acted
		assertThat(confirmed.get() + expired.get()).isEqualTo(1);
		Hold hold = holdRepository.findById(holdId).orElseThrow();
		if (confirmed.get() == 1) {
			// sold, so the tickets stay gone from availability
			assertThat(hold.getStatus()).isEqualTo(HoldStatus.CONFIRMED);
			assertThat(sold()).isEqualTo(4);
			assertThat(available()).isEqualTo(6);
		}
		else {
			// expired, so the tickets came back and nothing was sold
			assertThat(hold.getStatus()).isEqualTo(HoldStatus.EXPIRED);
			assertThat(sold()).isZero();
			assertThat(available()).isEqualTo(10);
		}
	}

	@Test
	void theSweepLeavesAConfirmedHoldSold() throws Exception {
		String holdId = createHold(4);
		confirm(holdId, UUID.randomUUID().toString(), "pi_x").andExpect(status().isOk());

		assertThat(expiryWorker.sweepOnce()).isZero();

		assertThat(sold()).isEqualTo(4);
		assertThat(available()).isEqualTo(6);
		assertThat(holdRepository.findById(holdId)).get()
				.extracting(Hold::getStatus)
				.isEqualTo(HoldStatus.CONFIRMED);
	}

	@Test
	void aBuyersAllowanceStaysSpentAfterBuying() throws Exception {
		String holdId = createHold(5);
		confirm(holdId, UUID.randomUUID().toString(), "pi_x").andExpect(status().isOk());

		// the five-ticket limit counts held AND bought, so buying must not free the allowance
		String userId = holdRepository.findById(holdId).orElseThrow().getUserId();
		assertThat(redis.opsForValue().get(RedisKeys.userQuantity(eventId, userId))).isEqualTo("5");
	}

	private ResultActions confirm(String holdId, String orderId, String paymentReference) throws Exception {
		return mockMvc.perform(post("/internal/v1/reservations/{holdId}/confirm", holdId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "order_id": "%s", "payment_reference": "%s" }
						""".formatted(orderId, paymentReference)));
	}

	private String createHold(int quantity) throws Exception {
		String token = TestAdmissionTokens.forNewBuyer(eventId, userSub);
		String body = mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, userSub)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "event_id": "%s", "category_id": "%s", "quantity": %d, "admission_token": "%s" }
						""".formatted(eventId, categoryId, quantity, token)))
				.andExpect(status().isCreated())
				.andReturn().getResponse().getContentAsString();
		return com.jayway.jsonpath.JsonPath.read(body, "$.hold_id");
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
