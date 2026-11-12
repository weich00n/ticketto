package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
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
import com.ticketing.reservation_service.model.IdempotencyRecord;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.IdempotencyRepository;
import com.ticketing.reservation_service.repository.RedisKeys;

/** Retrying a create: the same hold comes back, with the same deadlines, and only once. */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class IdempotencyTest {

	private final String eventId = UUID.randomUUID().toString();

	private final String categoryId = UUID.randomUUID().toString();

	private final String userSub = UUID.randomUUID().toString();

	private final List<String> keysUsed = new ArrayList<>();

	@Autowired
	private MockMvc mockMvc;

	@Autowired
	private HoldRepository holdRepository;

	@Autowired
	private IdempotencyRepository idempotencyRepository;

	@Autowired
	private CategoryInventoryRepository inventoryRepository;

	@Autowired
	private EventSaleStatusRepository saleStatusRepository;

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
		keysUsed.forEach(idempotencyRepository::deleteById);
		holdRepository.deleteAll(holdRepository.findByEventId(eventId));
		inventoryRepository.findByEventIdOrderByCategoryId(eventId).forEach(inventoryRepository::delete);
		saleStatusRepository.deleteById(eventId);
		redis.delete(redis.keys("*{" + eventId + "}*"));
	}

	@Test
	void replayingTheSameRequestReturnsTheSameHoldAndTheSameDeadlines() throws Exception {
		String key = key();

		String first = create(key, 2)
				.andExpect(status().isCreated())
				.andReturn().getResponse().getContentAsString();
		String second = create(key, 2)
				// a replay is not a creation
				.andExpect(status().isOk())
				.andReturn().getResponse().getContentAsString();

		assertThat(field(second, "hold_id")).isEqualTo(field(first, "hold_id"));
		// the deadlines must not move, or retrying would buy the buyer more time
		assertThat(field(second, "payment_expires_at")).isEqualTo(field(first, "payment_expires_at"));
		assertThat(field(second, "hold_expires_at")).isEqualTo(field(first, "hold_expires_at"));
		// and only one hold was ever made
		assertThat(holdRepository.findByEventId(eventId)).hasSize(1);
		assertThat(available()).isEqualTo(8);
	}

	@Test
	void reusingAKeyForADifferentRequestIsRefused() throws Exception {
		String key = key();
		create(key, 2).andExpect(status().isCreated());

		// same key, different quantity
		create(key, 3)
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("IDEMPOTENCY_KEY_CONFLICT"));

		assertThat(holdRepository.findByEventId(eventId)).hasSize(1);
		assertThat(available()).isEqualTo(8);
	}

	@Test
	void anotherBuyerCannotReuseSomeoneElsesKey() throws Exception {
		String key = key();
		create(key, 2).andExpect(status().isCreated());

		// the fingerprint covers the buyer, so the same key from someone else is a conflict
		String otherSub = UUID.randomUUID().toString();
		mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, otherSub)
				.header("Idempotency-Key", key)
				.contentType(MediaType.APPLICATION_JSON)
				.content(body(otherSub, 2)))
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("IDEMPOTENCY_KEY_CONFLICT"));
	}

	@Test
	void differentKeysStillCreateDifferentHolds() throws Exception {
		String firstHold = field(create(key(), 2).andExpect(status().isCreated())
				.andReturn().getResponse().getContentAsString(), "hold_id");
		String secondHold = field(create(key(), 2).andExpect(status().isCreated())
				.andReturn().getResponse().getContentAsString(), "hold_id");

		assertThat(firstHold).isNotEqualTo(secondHold);
		assertThat(available()).isEqualTo(6);
	}

	@Test
	void aRequestWithoutAKeyIsUnaffected() throws Exception {
		mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, userSub)
				.contentType(MediaType.APPLICATION_JSON)
				.content(body(userSub, 1)))
				.andExpect(status().isCreated());

		assertThat(idempotencyRepository.count()).isZero();
	}

	@Test
	void aKeyIsFreedWhenNoHoldCameOfIt() throws Exception {
		String key = key();
		// too many tickets, so the create fails after the key was claimed
		redis.opsForValue().set(RedisKeys.available(eventId, categoryId), "1");
		create(key, 4)
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("INSUFFICIENT_INVENTORY"));

		// the buyer must be able to genuinely retry rather than be stuck on a dead key
		assertThat(idempotencyRepository.findById(key)).isEmpty();
		redis.opsForValue().set(RedisKeys.available(eventId, categoryId), "10");
		create(key, 4).andExpect(status().isCreated());
	}

	@Test
	void aClaimLeftByARequestThatDiedIsTakenOverByItsRetry() throws Exception {
		String key = key();
		create(key, 2).andExpect(status().isCreated());
		// as a task stopped mid-create would leave it: claimed long ago, no hold recorded
		IdempotencyRecord claim = idempotencyRepository.findById(key).orElseThrow();
		claim.setHoldId(null);
		claim.setCreatedAt(Instant.now().minus(Duration.ofMinutes(2)));
		idempotencyRepository.save(claim);

		create(key, 2).andExpect(status().isCreated());

		assertThat(idempotencyRepository.findById(key).orElseThrow().getHoldId()).isNotNull();
	}

	@Test
	void aRecentClaimWithNoHoldYetIsStillInFlight() throws Exception {
		String key = key();
		create(key, 2).andExpect(status().isCreated());
		IdempotencyRecord claim = idempotencyRepository.findById(key).orElseThrow();
		claim.setHoldId(null);
		idempotencyRepository.save(claim);

		create(key, 2)
				.andExpect(status().isServiceUnavailable())
				.andExpect(jsonPath("$.error.code").value("DEPENDENCY_UNAVAILABLE"));
	}

	@Test
	void anOverlongKeyIsRejected() throws Exception {
		mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, userSub)
				.header("Idempotency-Key", "k".repeat(200))
				.contentType(MediaType.APPLICATION_JSON)
				.content(body(userSub, 1)))
				.andExpect(status().isBadRequest())
				.andExpect(jsonPath("$.error.code").value("INVALID_REQUEST"));
	}

	@Test
	void eightSimultaneousRetriesCreateExactlyOneHold() throws Exception {
		String key = key();
		int tabs = 8;
		CyclicBarrier startTogether = new CyclicBarrier(tabs);
		AtomicInteger created = new AtomicInteger();
		AtomicInteger replayed = new AtomicInteger();
		AtomicInteger askedToRetry = new AtomicInteger();

		try (ExecutorService pool = Executors.newFixedThreadPool(tabs)) {
			List<Future<?>> attempts = new ArrayList<>();
			for (int i = 0; i < tabs; i++) {
				attempts.add(pool.submit(() -> {
					startTogether.await();
					int status = mockMvc.perform(post("/api/v1/reservations")
							.header(GatewayHeaders.USER_SUB, userSub)
							.header("Idempotency-Key", key)
							.contentType(MediaType.APPLICATION_JSON)
							.content(body(userSub, 2)))
							.andReturn().getResponse().getStatus();
					switch (status) {
						case 201 -> created.incrementAndGet();
						case 200 -> replayed.incrementAndGet();
						// honest answer while the first create is still in flight
						case 503 -> askedToRetry.incrementAndGet();
						default -> throw new AssertionError("Unexpected status " + status);
					}
					return null;
				}));
			}
			for (Future<?> attempt : attempts) {
				attempt.get();
			}
		}

		assertThat(created.get()).isEqualTo(1);
		assertThat(replayed.get() + askedToRetry.get()).isEqualTo(tabs - 1);
		// the point of all of it: one hold, two tickets
		assertThat(holdRepository.findByEventId(eventId)).hasSize(1);
		assertThat(available()).isEqualTo(8);
	}

	private String key() {
		String key = UUID.randomUUID().toString();
		keysUsed.add(key);
		return key;
	}

	private ResultActions create(String idempotencyKey, int quantity) throws Exception {
		return mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, userSub)
				.header("Idempotency-Key", idempotencyKey)
				.contentType(MediaType.APPLICATION_JSON)
				.content(body(userSub, quantity)));
	}

	private String body(String sub, int quantity) {
		return """
				{ "event_id": "%s", "category_id": "%s", "quantity": %d, "admission_token": "%s" }
				""".formatted(eventId, categoryId, quantity, TestAdmissionTokens.forNewBuyer(eventId, sub));
	}

	private static String field(String json, String name) {
		return com.jayway.jsonpath.JsonPath.read(json, "$." + name);
	}

	private int available() {
		return Integer.parseInt(redis.opsForValue().get(RedisKeys.available(eventId, categoryId)));
	}
}
