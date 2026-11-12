package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

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

import com.ticketing.reservation_service.config.GatewayHeaders;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.HoldStatus;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.RedisKeys;
import com.ticketing.reservation_service.service.HoldExpiryWorker;

/**
 * The expiry sweep: unpaid holds give their tickets back, and no hold is ever refunded twice.
 *
 * <p>Rather than waiting six minutes, each test moves a hold's deadline into the past exactly as
 * time would have: the hold's own key is dropped, its expiry score is backdated, and the sweep is
 * then asked to run.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class HoldExpiryTest {

	private final String eventId = UUID.randomUUID().toString();

	private final String categoryId = UUID.randomUUID().toString();

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
	void anAbandonedHoldGivesItsTicketsBack() throws Exception {
		String holdId = createHold(4);
		assertThat(available()).isEqualTo(6);

		letTimePass(holdId);
		assertThat(expiryWorker.sweepOnce()).isEqualTo(1);

		assertThat(available()).isEqualTo(10);
		assertThat(holdRepository.findById(holdId)).get()
				.extracting(Hold::getStatus)
				.isEqualTo(HoldStatus.EXPIRED);
		// the buyer's allowance is freed, so they may queue again
		assertThat(redis.opsForValue().get(RedisKeys.userQuantity(eventId, userIdOf(holdId)))).isEqualTo("0");
		// and nothing is left behind to sweep again
		assertThat(redis.opsForZSet().size(RedisKeys.expiryIndex(eventId, categoryId))).isZero();
	}

	@Test
	void sweepingRepeatedlyRefundsOnlyOnce() throws Exception {
		String holdId = createHold(4);
		letTimePass(holdId);

		assertThat(expiryWorker.sweepOnce()).isEqualTo(1);
		assertThat(expiryWorker.sweepOnce()).isZero();
		assertThat(expiryWorker.sweepOnce()).isZero();

		assertThat(available()).isEqualTo(10);
	}

	@Test
	void aHoldThatIsNotYetDueIsLeftAlone() throws Exception {
		String holdId = createHold(4);

		assertThat(expiryWorker.sweepOnce()).isZero();

		assertThat(available()).isEqualTo(6);
		assertThat(holdRepository.findById(holdId)).get()
				.extracting(Hold::getStatus)
				.isEqualTo(HoldStatus.ACTIVE);
	}

	@Test
	void aHoldTheBuyerAlreadyReleasedIsNotRefundedAgainBySweeping() throws Exception {
		String holdId = createHold(4);

		mockMvc.perform(delete("/api/v1/reservations/{holdId}", holdId).header(GatewayHeaders.USER_SUB, userSub))
				.andExpect(status().isOk());
		assertThat(available()).isEqualTo(10);

		// time passes; the sweep must not pay the same tickets back a second time
		backdateRow(holdId);
		assertThat(expiryWorker.sweepOnce()).isZero();

		assertThat(available()).isEqualTo(10);
		assertThat(holdRepository.findById(holdId)).get()
				.extracting(Hold::getStatus)
				.isEqualTo(HoldStatus.CANCELLED);
	}

	@Test
	void aConfirmedHoldKeepsItsTicketsWhenTheSweepReachesIt() throws Exception {
		String holdId = createHold(4);
		// A verified payment writes this status; here it is set directly so the race the sweep
		// must survive is tested on its own.
		holdRepository.transitionStatus(holdId, HoldStatus.ACTIVE, HoldStatus.CONFIRMED);

		letTimePass(holdId);
		assertThat(expiryWorker.sweepOnce()).isZero();

		// sold tickets must not come back as available
		assertThat(available()).isEqualTo(6);
		assertThat(holdRepository.findById(holdId)).get()
				.extracting(Hold::getStatus)
				.isEqualTo(HoldStatus.CONFIRMED);
		assertThat(redis.opsForZSet().size(RedisKeys.expiryIndex(eventId, categoryId))).isZero();
	}

	@Test
	void anOrphanedHoldWithNoRowGivesItsTicketsBack() throws Exception {
		String holdId = createHold(3);
		assertThat(available()).isEqualTo(7);

		// Redis took the tickets but the row never made it, and the undo could not run either
		holdRepository.deleteById(holdId);
		backdateScore(holdId);

		assertThat(expiryWorker.sweepOnce()).isEqualTo(1);
		assertThat(available()).isEqualTo(10);
	}

	@Test
	void anOrphanWhoseInfoIsAlsoGoneIsLeftForTheRebuild() throws Exception {
		String holdId = createHold(3);

		// nothing left anywhere to say how many tickets this hold took: no row, no key, and its
		// info has expired too
		holdRepository.deleteById(holdId);
		letTimePass(holdId);
		redis.delete(RedisKeys.holdInfo(eventId, holdId));

		assertThat(expiryWorker.sweepOnce()).isZero();

		// the counter is deliberately not guessed at; InventoryRebuildService rebuilds it from MySQL
		assertThat(available()).isEqualTo(7);
		assertThat(redis.opsForZSet().size(RedisKeys.expiryIndex(eventId, categoryId))).isZero();
	}

	@Test
	void severalDueHoldsAreAllResolvedInOneSweep() throws Exception {
		// three buyers, since one alone could not hold six tickets
		String first = createHold(2);
		String second = createHold(3, UUID.randomUUID().toString());
		String third = createHold(1, UUID.randomUUID().toString());
		assertThat(available()).isEqualTo(4);

		letTimePass(first);
		letTimePass(second);
		letTimePass(third);

		assertThat(expiryWorker.sweepOnce()).isEqualTo(3);
		assertThat(available()).isEqualTo(10);
	}

	@Test
	void aHoldThatFailsEverySweepDoesNotStopTheOthersExpiring() throws Exception {
		String holdId = createHold(2);
		letTimePass(holdId);
		// an orphan whose info cannot be read, due first so it comes before the real hold
		String broken = UUID.randomUUID().toString();
		redis.opsForHash().put(RedisKeys.holdInfo(eventId, broken), "quantity", "not a number");
		redis.opsForHash().put(RedisKeys.holdInfo(eventId, broken), "user_id", userSub);
		redis.opsForZSet().add(RedisKeys.expiryIndex(eventId, categoryId), broken,
				Instant.now().minusSeconds(120).getEpochSecond());

		assertThat(expiryWorker.sweepOnce()).isEqualTo(1);

		assertThat(available()).isEqualTo(10);
		assertThat(holdRepository.findById(holdId)).get()
				.extracting(Hold::getStatus)
				.isEqualTo(HoldStatus.EXPIRED);
	}

	@Test
	void aBuyerReleasingWhileTheSweepRunsRefundsExactlyOnce() throws Exception {
		String holdId = createHold(4);
		// The sharp case: the expiry score has come due while the hold's own key is still alive,
		// so the sweep and the buyer's DELETE both believe they may act. Only one may.
		backdateScore(holdId);
		backdateRow(holdId);

		AtomicInteger releasesAccepted = new AtomicInteger();
		AtomicInteger sweepsThatResolved = new AtomicInteger();
		CyclicBarrier startTogether = new CyclicBarrier(2);

		try (ExecutorService pool = Executors.newFixedThreadPool(2)) {
			Future<?> release = pool.submit(() -> {
				startTogether.await();
				if (mockMvc.perform(delete("/api/v1/reservations/{holdId}", holdId)
						.header(GatewayHeaders.USER_SUB, userSub))
						.andReturn().getResponse().getStatus() == 200) {
					releasesAccepted.incrementAndGet();
				}
				return null;
			});
			Future<?> sweep = pool.submit(() -> {
				startTogether.await();
				sweepsThatResolved.addAndGet(expiryWorker.sweepOnce());
				return null;
			});
			release.get();
			sweep.get();
		}

		// exactly one of the two acted, whichever won the claim
		assertThat(releasesAccepted.get() + sweepsThatResolved.get()).isEqualTo(1);
		// and the tickets came back exactly once, never twice
		assertThat(available()).isEqualTo(10);
	}

	@Test
	void manySimultaneousReleasesRefundExactlyOnce() throws Exception {
		String holdId = createHold(5);
		assertThat(available()).isEqualTo(5);

		int tabs = 8;
		CyclicBarrier startTogether = new CyclicBarrier(tabs);
		AtomicInteger accepted = new AtomicInteger();
		AtomicInteger refused = new AtomicInteger();

		try (ExecutorService pool = Executors.newFixedThreadPool(tabs)) {
			List<Future<?>> attempts = new ArrayList<>();
			for (int i = 0; i < tabs; i++) {
				attempts.add(pool.submit(() -> {
					startTogether.await();
					int status = mockMvc.perform(delete("/api/v1/reservations/{holdId}", holdId)
							.header(GatewayHeaders.USER_SUB, userSub))
							.andReturn().getResponse().getStatus();
					if (status == 200) {
						accepted.incrementAndGet();
					}
					else if (status == 409) {
						refused.incrementAndGet();
					}
					return null;
				}));
			}
			for (Future<?> attempt : attempts) {
				attempt.get();
			}
		}

		assertThat(accepted.get()).isEqualTo(1);
		assertThat(refused.get()).isEqualTo(tabs - 1);
		assertThat(available()).isEqualTo(10);
	}

	/** Exactly what six minutes would do: the hold's key dies and its expiry comes due. */
	private void letTimePass(String holdId) {
		redis.delete(RedisKeys.hold(eventId, holdId));
		backdateScore(holdId);
		backdateRow(holdId);
	}

	private void backdateScore(String holdId) {
		redis.opsForZSet().add(RedisKeys.expiryIndex(eventId, categoryId), holdId,
				Instant.now().minusSeconds(60).getEpochSecond());
	}

	private void backdateRow(String holdId) {
		holdRepository.findById(holdId).ifPresent(hold -> {
			hold.setHoldExpiresAt(Instant.now().minusSeconds(60));
			holdRepository.save(hold);
		});
	}

	private String createHold(int quantity) throws Exception {
		return createHold(quantity, userSub);
	}

	private String createHold(int quantity, String buyerSub) throws Exception {
		String token = TestAdmissionTokens.forNewBuyer(eventId, buyerSub);
		String body = mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, buyerSub)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "event_id": "%s", "category_id": "%s", "quantity": %d, "admission_token": "%s" }
						""".formatted(eventId, categoryId, quantity, token)))
				.andExpect(status().isCreated())
				.andReturn().getResponse().getContentAsString();
		return com.jayway.jsonpath.JsonPath.read(body, "$.hold_id");
	}

	private String userIdOf(String holdId) {
		return holdRepository.findById(holdId).orElseThrow().getUserId();
	}

	private int available() {
		return Integer.parseInt(redis.opsForValue().get(RedisKeys.available(eventId, categoryId)));
	}
}
