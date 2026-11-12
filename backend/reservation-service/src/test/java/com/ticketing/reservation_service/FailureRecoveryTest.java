package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doReturn;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.reset;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
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
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;

import com.jayway.jsonpath.JsonPath;
import com.ticketing.reservation_service.config.GatewayHeaders;
import com.ticketing.reservation_service.model.CategoryInventoryId;
import com.ticketing.reservation_service.model.EventSaleStatus;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.HoldStatus;
import com.ticketing.reservation_service.model.OutboxMessage;
import com.ticketing.reservation_service.model.SaleStatus;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRedisRepository;
import com.ticketing.reservation_service.repository.HoldRedisRepository.HoldEnding;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.OutboxRepository;
import com.ticketing.reservation_service.repository.RedisKeys;
import com.ticketing.reservation_service.service.HoldExpiryWorker;
import com.ticketing.reservation_service.service.InventoryRebuildService;
import com.ticketing.reservation_service.service.OutboxWriter;

/**
 * A hold whose ending was claimed in Redis but never written to MySQL - the database failed, or
 * the process died in between - still ends exactly once, with its tickets back on sale.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class FailureRecoveryTest {

	private final String eventId = UUID.randomUUID().toString();

	private final String categoryId = UUID.randomUUID().toString();

	private final String userSub = UUID.randomUUID().toString();

	private static final String ORDER_ID = UUID.randomUUID().toString();

	@Autowired
	private MockMvc mockMvc;

	@Autowired
	private HoldExpiryWorker expiryWorker;

	@Autowired
	private HoldRedisRepository holdRedis;

	@MockitoSpyBean
	private HoldRepository holdRepository;

	@MockitoSpyBean
	private InventoryRebuildService rebuildService;

	@MockitoSpyBean
	private CategoryInventoryRepository inventoryRepository;

	@Autowired
	private EventSaleStatusRepository saleStatusRepository;

	@Autowired
	private OutboxRepository outboxRepository;

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
		reset(holdRepository, inventoryRepository, rebuildService);
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
	void aConfirmationWhoseDatabaseWriteFailsCanSimplyBeRetried() throws Exception {
		Hold hold = hold(createHold(2));
		doThrow(new DataAccessResourceFailureException("MySQL went away"))
				.when(inventoryRepository).addSold(anyString(), anyString(), anyInt());
		String orderId = UUID.randomUUID().toString();

		confirm(hold.getHoldId(), orderId).andExpect(status().isServiceUnavailable());
		// MySQL is back
		reset(inventoryRepository);

		// the claim stands, out of the sweep's reach, and nothing is sold yet
		assertThat(hold(hold.getHoldId()).getStatus()).isEqualTo(HoldStatus.ACTIVE);
		assertThat(holdRedis.isIndexedForExpiry(hold)).isFalse();
		assertThat(holdRedis.endingOf(eventId, hold.getHoldId())).isEqualTo(HoldEnding.SOLD_PENDING);
		assertThat(available()).isEqualTo(8);
		assertThat(sold()).isZero();

		confirm(hold.getHoldId(), orderId)
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.status").value("CONFIRMED"));
		assertThat(sold()).isEqualTo(2);
		assertThat(available()).isEqualTo(8);
	}

	@Test
	void aConfirmationThatDiedAfterItsClaimGivesTheTicketsBack() throws Exception {
		Hold hold = hold(createHold(2));
		// the claim lands, and the process dies before MySQL hears of it
		assertThat(holdRedis.confirm(hold, ORDER_ID)).isEqualTo(HoldRedisRepository.Status.OK);
		assertThat(available()).isEqualTo(8);
		pastDeadline(hold);

		assertThat(expiryWorker.reconcileOnce()).isEqualTo(1);
		assertThat(expiryWorker.reconcileOnce()).isZero();

		assertThat(hold(hold.getHoldId()).getStatus()).isEqualTo(HoldStatus.EXPIRED);
		assertThat(available()).isEqualTo(10);
		assertThat(userQuantity()).isZero();
		assertThat(sold()).isZero();
		assertThat(typesFor(hold.getHoldId())).containsExactly(OutboxWriter.RESERVATION_EXPIRED);
	}

	@Test
	void aConfirmationClaimingAHoldWhoseInfoWentMissingCanStillBeUndone() throws Exception {
		Hold hold = hold(createHold(2));
		redis.delete(RedisKeys.holdInfo(eventId, hold.getHoldId()));
		// the claim lands, and the process dies before MySQL hears of it
		assertThat(holdRedis.confirm(hold, ORDER_ID)).isEqualTo(HoldRedisRepository.Status.OK);
		assertThat(holdRedis.endingOf(eventId, hold.getHoldId())).isEqualTo(HoldRedisRepository.HoldEnding.SOLD_PENDING);
		assertThat(redis.getExpire(RedisKeys.holdInfo(eventId, hold.getHoldId()))).isPositive();
		pastDeadline(hold);

		assertThat(expiryWorker.reconcileOnce()).isEqualTo(1);

		assertThat(hold(hold.getHoldId()).getStatus()).isEqualTo(HoldStatus.EXPIRED);
		assertThat(available()).isEqualTo(10);
		assertThat(userQuantity()).isZero();
	}

	@Test
	void aReleaseWhoseRowWasNeverFinishedDoesNotReturnTheTicketsTwice() throws Exception {
		Hold hold = hold(createHold(2));
		// the tickets go back, and MySQL is never told
		assertThat(holdRedis.reclaim(hold)).isTrue();
		assertThat(available()).isEqualTo(10);
		pastDeadline(hold);

		assertThat(expiryWorker.reconcileOnce()).isEqualTo(1);

		assertThat(hold(hold.getHoldId()).getStatus()).isEqualTo(HoldStatus.EXPIRED);
		assertThat(available()).isEqualTo(10);
		assertThat(userQuantity()).isZero();
		assertThat(typesFor(hold.getHoldId())).containsExactly(OutboxWriter.RESERVATION_EXPIRED);
	}

	@Test
	void aHoldNobodyEndedButTheSweepLostIsSweptAgain() throws Exception {
		Hold hold = hold(createHold(2));
		holdRedis.forget(eventId, categoryId, hold.getHoldId());
		pastDeadline(hold);

		assertThat(expiryWorker.reconcileOnce()).isZero();
		assertThat(holdRedis.isIndexedForExpiry(hold)).isTrue();

		assertThat(expiryWorker.sweepOnce()).isEqualTo(1);
		assertThat(hold(hold.getHoldId()).getStatus()).isEqualTo(HoldStatus.EXPIRED);
		assertThat(available()).isEqualTo(10);
	}

	@Test
	void anOrphanedHoldIsReturnedAfterItsKeyHasDied() throws Exception {
		Hold hold = hold(createHold(3));
		// Redis took the tickets but the row never made it, and by the deadline the key is gone
		holdRepository.deleteById(hold.getHoldId());
		redis.delete(RedisKeys.hold(eventId, hold.getHoldId()));
		redis.opsForZSet().add(RedisKeys.expiryIndex(eventId, categoryId), hold.getHoldId(),
				Instant.now().minusSeconds(60).getEpochSecond());
		assertThat(available()).isEqualTo(7);

		assertThat(expiryWorker.sweepOnce()).isEqualTo(1);

		assertThat(available()).isEqualTo(10);
		assertThat(userQuantity()).isZero();
	}

	@Test
	void aStaleHoldAfterRedisWasLostIsRecountedRatherThanGuessed() throws Exception {
		Hold hold = hold(createHold(2));
		redis.delete(redis.keys("*{" + eventId + "}*"));
		pastDeadline(hold);

		assertThat(expiryWorker.reconcileOnce()).isZero();
		// rebuilt from MySQL: the ACTIVE hold still counts as held until the sweep ends it
		assertThat(available()).isEqualTo(8);

		assertThat(expiryWorker.sweepOnce()).isEqualTo(1);
		assertThat(available()).isEqualTo(10);
		assertThat(hold(hold.getHoldId()).getStatus()).isEqualTo(HoldStatus.EXPIRED);
	}

	@Test
	void aHoldWithNoInfoIsFinishedWithoutGuessingAtItsTickets() throws Exception {
		Hold hold = hold(createHold(2));
		// released before hold info existed: tickets back, row never told
		assertThat(holdRedis.reclaim(hold)).isTrue();
		redis.delete(RedisKeys.holdInfo(eventId, hold.getHoldId()));
		pastDeadline(hold);

		assertThat(expiryWorker.reconcileOnce()).isEqualTo(1);

		assertThat(hold(hold.getHoldId()).getStatus()).isEqualTo(HoldStatus.EXPIRED);
		// the counter was there, so no rebuild re-indexed the hold for a second return
		assertThat(available()).isEqualTo(10);
		assertThat(expiryWorker.sweepOnce()).isZero();
		assertThat(available()).isEqualTo(10);
	}

	@Test
	void aRebuildLeavesAReturnedHoldToReconciliation() throws Exception {
		Hold hold = hold(createHold(2));
		assertThat(holdRedis.reclaim(hold)).isTrue();
		// the counter is lost while the row still says ACTIVE
		redis.delete(RedisKeys.available(eventId, categoryId));

		assertThat(rebuildService.rebuild(eventId)).isTrue();

		// its tickets went back before the loss, so they are not counted as held again
		assertThat(available()).isEqualTo(10);
		assertThat(holdRedis.isIndexedForExpiry(hold)).isFalse();
		assertThat(holdRedis.endingOf(eventId, hold.getHoldId())).isEqualTo(HoldEnding.RETURNED);

		pastDeadline(hold);
		assertThat(expiryWorker.reconcileOnce()).isEqualTo(1);
		assertThat(expiryWorker.sweepOnce()).isZero();
		assertThat(available()).isEqualTo(10);
		assertThat(userQuantity()).isZero();
	}

	@Test
	void aRetryAfterReconciliationReturnedTheTicketsIsRefused() throws Exception {
		Hold hold = hold(createHold(2));
		assertThat(holdRedis.confirm(hold, ORDER_ID)).isEqualTo(HoldRedisRepository.Status.OK);
		pastDeadline(hold);
		assertThat(expiryWorker.reconcileOnce()).isEqualTo(1);
		assertThat(available()).isEqualTo(10);

		// Checkout retries only now, after reconciliation has ended the hold
		confirm(hold.getHoldId(), ORDER_ID)
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("HOLD_EXPIRED"));

		assertThat(available()).isEqualTo(10);
		assertThat(sold()).isZero();
	}

	@Test
	void aConfirmationWhoseWriteWasInDoubtIsNeverSweptBack() throws Exception {
		Hold hold = hold(createHold(2));
		// the write timed out, so the claim was kept; meanwhile the hold's deadline passes
		assertThat(holdRedis.confirm(hold, ORDER_ID)).isEqualTo(HoldRedisRepository.Status.OK);
		pastDeadline(hold);

		// the sweep runs before the commit becomes visible: it must not give the tickets back
		assertThat(expiryWorker.sweepOnce()).isZero();
		assertThat(available()).isEqualTo(8);

		// the commit did land after all
		Hold row = hold(hold.getHoldId());
		row.setStatus(HoldStatus.CONFIRMED);
		row.setOrderId(ORDER_ID);
		holdRepository.save(row);
		inventoryRepository.addSold(eventId, categoryId, 2);

		confirm(hold.getHoldId(), ORDER_ID)
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.duplicate").value(true));
		assertThat(available()).isEqualTo(8);
		assertThat(sold()).isEqualTo(2);
	}

	@Test
	void anotherOrderCannotFinishAClaimItDidNotMake() throws Exception {
		Hold hold = hold(createHold(2));
		assertThat(holdRedis.confirm(hold, ORDER_ID)).isEqualTo(HoldRedisRepository.Status.OK);

		// the first order's write may still land, so the other order is told to retry, not refunded
		confirm(hold.getHoldId(), UUID.randomUUID().toString()).andExpect(status().isServiceUnavailable());

		assertThat(hold(hold.getHoldId()).getStatus()).isEqualTo(HoldStatus.ACTIVE);
		assertThat(sold()).isZero();
	}

	@Test
	void aRetryOfAConfirmedHoldOnACancelledEventRecordsTheRefundOnce() throws Exception {
		Hold hold = hold(createHold(2));
		confirm(hold.getHoldId(), ORDER_ID).andExpect(status().isOk());
		// cancelled before the confirmation's own check, which then failed, so no refund was recorded
		EventSaleStatus sale = saleStatusRepository.findById(eventId).orElseThrow();
		sale.setSaleStatus(SaleStatus.CANCELLED);
		saleStatusRepository.save(sale);
		assertThat(typesFor(hold.getHoldId())).doesNotContain(OutboxWriter.REFUND_REQUIRED);

		confirm(hold.getHoldId(), ORDER_ID).andExpect(status().isOk());
		confirm(hold.getHoldId(), ORDER_ID).andExpect(status().isOk());

		assertThat(typesFor(hold.getHoldId())).containsOnlyOnce(OutboxWriter.REFUND_REQUIRED);
	}

	@Test
	void reconciliationNeverReturnsTicketsAConfirmationGotToSellFirst() throws Exception {
		Hold hold = hold(createHold(2));
		pastDeadline(hold);
		Hold staleCopy = hold(hold.getHoldId());
		assertThat(holdRedis.confirm(hold, ORDER_ID)).isEqualTo(HoldRedisRepository.Status.OK);
		// reconciliation read the row as ACTIVE, then the slow confirmation committed
		doReturn(List.of(staleCopy)).when(holdRepository).findByStatusAndHoldExpiresAtBefore(any(), any());
		Hold row = hold(hold.getHoldId());
		row.setStatus(HoldStatus.CONFIRMED);
		holdRepository.save(row);
		inventoryRepository.addSold(eventId, categoryId, 2);

		assertThat(expiryWorker.reconcileOnce()).isZero();

		// sold, so still subtracted: returning them now would sell them twice
		assertThat(available()).isEqualTo(8);
		assertThat(sold()).isEqualTo(2);
		assertThat(hold(hold.getHoldId()).getStatus()).isEqualTo(HoldStatus.CONFIRMED);
	}

	@Test
	void anEventWithManyStuckHoldsIsRebuiltOncePerRun() throws Exception {
		Hold first = hold(createHold(2));
		Hold second = hold(createHold(2));
		redis.delete(redis.keys("*{" + eventId + "}*"));
		pastDeadline(first);
		pastDeadline(second);

		expiryWorker.reconcileOnce();

		verify(rebuildService, times(1)).rebuild(eventId);
		assertThat(available()).isEqualTo(6);
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

	private ResultActions confirm(String holdId, String orderId) throws Exception {
		return mockMvc.perform(post("/internal/v1/reservations/{holdId}/confirm", holdId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "order_id": "%s", "payment_reference": "pi_test" }
						""".formatted(orderId)));
	}

	/** Backdates the row far enough that reconciliation looks at it. */
	private void pastDeadline(Hold hold) {
		Hold row = hold(hold.getHoldId());
		row.setHoldExpiresAt(Instant.now().minusSeconds(300));
		holdRepository.save(row);
		hold.setHoldExpiresAt(row.getHoldExpiresAt());
		if (holdRedis.isIndexedForExpiry(hold)) {
			holdRedis.indexForExpiry(hold);
		}
	}

	private Hold hold(String holdId) {
		return holdRepository.findById(holdId).orElseThrow();
	}

	private int available() {
		return Integer.parseInt(redis.opsForValue().get(RedisKeys.available(eventId, categoryId)));
	}

	private int userQuantity() {
		String value = redis.opsForValue().get(RedisKeys.userQuantity(eventId, userSub));
		return (value == null) ? 0 : Integer.parseInt(value);
	}

	private int sold() {
		return inventoryRepository.findById(new CategoryInventoryId(eventId, categoryId)).orElseThrow()
				.getSoldQuantity();
	}

	private List<String> typesFor(String aggregateId) {
		return outboxRepository.findByAggregateId(aggregateId).stream()
				.map(OutboxMessage::getEventType)
				.toList();
	}
}
