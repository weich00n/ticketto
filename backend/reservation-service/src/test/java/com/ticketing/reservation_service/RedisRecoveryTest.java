package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.time.Instant;
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

import com.ticketing.reservation_service.config.GatewayHeaders;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.HoldStatus;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.RedisKeys;
import com.ticketing.reservation_service.service.HoldExpiryWorker;

/**
 * Losing Redis must be survivable, because MySQL kept one row per hold all along. This is the
 * acceptance check that a Redis failure does not erase authoritative hold records.
 *
 * <p>Each test wipes exactly this event's live state, the way a flush or a restarted container
 * would, and then asks the service to carry on.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class RedisRecoveryTest {

	private final String eventId = UUID.randomUUID().toString();

	private final String categoryId = UUID.randomUUID().toString();

	@Autowired
	private MockMvc mockMvc;

	@Autowired
	private HoldRepository holdRepository;

	@Autowired
	private HoldExpiryWorker expiryWorker;

	@Autowired
	private CategoryInventoryRepository inventoryRepository;

	@Autowired
	private EventSaleStatusRepository saleStatusRepository;

	@Autowired
	private StringRedisTemplate redis;

	@BeforeEach
	void seedOneHundredTickets() throws Exception {
		mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", eventId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "categories": [ { "category_id": "%s", "total_quantity": 100, "unit_amount": 28800 } ] }
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
	void availabilityIsRebuiltFromMysqlAfterTheLiveStateIsLost() throws Exception {
		createHold(UUID.randomUUID().toString(), 4);
		createHold(UUID.randomUUID().toString(), 3);
		assertThat(available()).isEqualTo(93);

		loseRedis();

		// total 100 − sold 0 − held 7
		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.status").value("ON_SALE"))
				.andExpect(jsonPath("$.categories[0].total").value(100))
				.andExpect(jsonPath("$.categories[0].free").value(93))
				.andExpect(jsonPath("$.categories[0].sold").value(0));
	}

	@Test
	void soldTicketsStaySoldAcrossARedisLoss() throws Exception {
		String holdId = createHold(UUID.randomUUID().toString(), 5);
		mockMvc.perform(post("/internal/v1/reservations/{holdId}/confirm", holdId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "order_id": "%s", "payment_reference": "pi_x" }
						""".formatted(UUID.randomUUID())))
				.andExpect(status().isOk());

		loseRedis();

		// 100 − 5 sold − 0 held: the sold tickets must not reappear as available
		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.categories[0].free").value(95))
				.andExpect(jsonPath("$.categories[0].sold").value(5));
	}

	@Test
	void newHoldsStillWorkAfterARedisLoss() throws Exception {
		createHold(UUID.randomUUID().toString(), 4);
		loseRedis();

		// the buyer should not see a failure just because the live state had to be restored
		createHold(UUID.randomUUID().toString(), 2);

		assertThat(available()).isEqualTo(94);
		assertThat(holdRepository.findByEventId(eventId)).hasSize(2);
	}

	@Test
	void theFiveTicketLimitSurvivesARedisLoss() throws Exception {
		String userSub = UUID.randomUUID().toString();
		String userId = holdRepository.findById(createHold(userSub, 4)).orElseThrow().getUserId();

		loseRedis();
		// reading availability is what triggers the rebuild
		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId)).andExpect(status().isOk());

		// the buyer's spent allowance was restored, so a sixth ticket is still refused
		assertThat(redis.opsForValue().get(RedisKeys.userQuantity(eventId, userId))).isEqualTo("4");
		mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, userSub)
				.contentType(MediaType.APPLICATION_JSON)
				.content(body(userSub, userId, 2)))
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("PURCHASE_LIMIT_EXCEEDED"));
	}

	@Test
	void aLiveHoldStaysConfirmableWithOnlyTheTimeItHadLeft() throws Exception {
		String holdId = createHold(UUID.randomUUID().toString(), 2);
		long ttlBefore = redis.getExpire(RedisKeys.hold(eventId, holdId));

		loseRedis();
		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId)).andExpect(status().isOk());

		// its key is back, and with no more time than it started with
		assertThat(redis.hasKey(RedisKeys.hold(eventId, holdId))).isTrue();
		assertThat(redis.getExpire(RedisKeys.hold(eventId, holdId))).isLessThanOrEqualTo(ttlBefore);
		// so the payment can still be confirmed
		mockMvc.perform(post("/internal/v1/reservations/{holdId}/confirm", holdId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "order_id": "%s", "payment_reference": "pi_after_loss" }
						""".formatted(UUID.randomUUID())))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.status").value("CONFIRMED"));
	}

	@Test
	void anAbandonedHoldCanStillExpireAfterARedisLoss() throws Exception {
		String holdId = createHold(UUID.randomUUID().toString(), 5);
		// the hold is overdue by the time the rebuild happens, as it would be after a long outage
		holdRepository.findById(holdId).ifPresent(hold -> {
			hold.setHoldExpiresAt(Instant.now().minusSeconds(120));
			holdRepository.save(hold);
		});

		loseRedis();
		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId)).andExpect(status().isOk());

		// an overdue hold must not come back confirmable
		assertThat(redis.hasKey(RedisKeys.hold(eventId, holdId))).isFalse();
		// but its expiry entry must, or its tickets would be lost for good
		assertThat(expiryWorker.sweepOnce()).isEqualTo(1);
		assertThat(available()).isEqualTo(100);
		assertThat(holdRepository.findById(holdId)).get()
				.extracting(Hold::getStatus)
				.isEqualTo(HoldStatus.EXPIRED);
	}

	@Test
	void arebuildNeverFlattensACounterThatHoldsHaveAlreadyMoved() throws Exception {
		createHold(UUID.randomUUID().toString(), 5);
		// only the counter is lost; everything else is intact
		redis.delete(RedisKeys.available(eventId, categoryId));

		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId)).andExpect(status().isOk());
		int afterFirst = available();

		// a second read must not recompute over the top of it
		createHold(UUID.randomUUID().toString(), 1);
		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId)).andExpect(status().isOk());

		assertThat(afterFirst).isEqualTo(95);
		assertThat(available()).isEqualTo(94);
	}

	@Test
	void aReaderIsAskedToRetryWhileAnotherRebuildHoldsTheLock() throws Exception {
		createHold(UUID.randomUUID().toString(), 2);
		loseRedis();
		// as if another instance were already rebuilding this event
		redis.opsForValue().set(RedisKeys.rebuildLock(eventId), "1");

		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId))
				.andExpect(status().isServiceUnavailable())
				.andExpect(jsonPath("$.error.code").value("DEPENDENCY_UNAVAILABLE"));

		// once that rebuild finishes, reads work again
		redis.delete(RedisKeys.rebuildLock(eventId));
		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.categories[0].free").value(98));
	}

	@Test
	void anUnknownEventIsStillUnknownAfterARedisLoss() throws Exception {
		loseRedis();

		mockMvc.perform(get("/api/v1/events/{eventId}/availability", UUID.randomUUID().toString()))
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("EVENT_NOT_FOUND"));
	}

	/** Everything Redis knew about this event, gone. */
	private void loseRedis() {
		redis.delete(redis.keys("*{" + eventId + "}*"));
		assertThat(redis.keys("*{" + eventId + "}*")).isEmpty();
	}

	private String createHold(String userSub, int quantity) throws Exception {
		String body = mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, userSub)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "event_id": "%s", "category_id": "%s", "quantity": %d, "admission_token": "%s" }
						""".formatted(eventId, categoryId, quantity,
						TestAdmissionTokens.forNewBuyer(eventId, userSub))))
				.andExpect(status().isCreated())
				.andReturn().getResponse().getContentAsString();
		return com.jayway.jsonpath.JsonPath.read(body, "$.hold_id");
	}

	/** A second hold for a buyer who already has one, so the limit is counted across both. */
	private String body(String userSub, String userId, int quantity) {
		String token = TestAdmissionTokens.builder(eventId, userSub).userId(userId).sign();
		return """
				{ "event_id": "%s", "category_id": "%s", "quantity": %d, "admission_token": "%s" }
				""".formatted(eventId, categoryId, quantity, token);
	}

	private int available() {
		return Integer.parseInt(redis.opsForValue().get(RedisKeys.available(eventId, categoryId)));
	}
}
