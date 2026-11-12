package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

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
import com.ticketing.reservation_service.model.HoldStatus;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.RedisKeys;

/** Reading and releasing a hold: who may see it, what status it really has, and who gets refunded. */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class HoldLifecycleTest {

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
	void aBuyerCanReadTheirOwnHold() throws Exception {
		String holdId = createHold(2);

		mockMvc.perform(get("/api/v1/reservations/{holdId}", holdId).header(GatewayHeaders.USER_SUB, userSub))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.hold_id").value(holdId))
				.andExpect(jsonPath("$.status").value("ACTIVE"))
				.andExpect(jsonPath("$.quantity").value(2))
				.andExpect(jsonPath("$.total_amount").value(57600))
				.andExpect(jsonPath("$.payment_expires_at").isNotEmpty())
				.andExpect(jsonPath("$.hold_expires_at").isNotEmpty())
				.andExpect(jsonPath("$.server_time").isNotEmpty());
	}

	@Test
	void anotherBuyersHoldIsNotFoundRatherThanForbidden() throws Exception {
		String holdId = createHold(1);

		// a 403 would confirm the hold exists
		mockMvc.perform(get("/api/v1/reservations/{holdId}", holdId)
				.header(GatewayHeaders.USER_SUB, UUID.randomUUID().toString()))
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("HOLD_NOT_FOUND"));
	}

	@Test
	void anUnknownHoldIsNotFound() throws Exception {
		mockMvc.perform(get("/api/v1/reservations/{holdId}", UUID.randomUUID().toString())
				.header(GatewayHeaders.USER_SUB, userSub))
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("HOLD_NOT_FOUND"));
	}

	@Test
	void readingAHoldWithoutTheGatewayIdentityIsUnauthenticated() throws Exception {
		String holdId = createHold(1);

		mockMvc.perform(get("/api/v1/reservations/{holdId}", holdId))
				.andExpect(status().isUnauthorized())
				.andExpect(jsonPath("$.error.code").value("UNAUTHENTICATED"));
	}

	@Test
	void aLapsedHoldReadsAsExpiredEvenBeforeTheSweepHasRun() throws Exception {
		String holdId = createHold(1);
		// the hold's own key dies on its own; its row still says ACTIVE until a sweep
		redis.delete(RedisKeys.hold(eventId, holdId));

		mockMvc.perform(get("/api/v1/reservations/{holdId}", holdId).header(GatewayHeaders.USER_SUB, userSub))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.status").value("EXPIRED"));

		// reporting is not resolving: the row is untouched, so the sweep still owns the refund
		assertThat(holdRepository.findById(holdId)).get()
				.extracting(hold -> hold.getStatus())
				.isEqualTo(HoldStatus.ACTIVE);
	}

	@Test
	void releasingAHoldReturnsItsTicketsImmediately() throws Exception {
		String holdId = createHold(3);
		assertThat(available()).isEqualTo(7);

		mockMvc.perform(delete("/api/v1/reservations/{holdId}", holdId).header(GatewayHeaders.USER_SUB, userSub))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.hold_id").value(holdId))
				.andExpect(jsonPath("$.status").value("CANCELLED"))
				.andExpect(jsonPath("$.server_time").isNotEmpty());

		assertThat(available()).isEqualTo(10);
		assertThat(holdRepository.findById(holdId)).get()
				.extracting(hold -> hold.getStatus())
				.isEqualTo(HoldStatus.CANCELLED);
		// the hold's own key is gone, so it can no longer be confirmed
		assertThat(redis.hasKey(RedisKeys.hold(eventId, holdId))).isFalse();
		// and the buyer's five-ticket allowance is freed too
		assertThat(redis.opsForValue().get(RedisKeys.userQuantity(eventId, userIdOf(holdId)))).isEqualTo("0");
	}

	@Test
	void releasingTwiceRefundsOnlyOnce() throws Exception {
		String holdId = createHold(3);

		mockMvc.perform(delete("/api/v1/reservations/{holdId}", holdId).header(GatewayHeaders.USER_SUB, userSub))
				.andExpect(status().isOk());
		mockMvc.perform(delete("/api/v1/reservations/{holdId}", holdId).header(GatewayHeaders.USER_SUB, userSub))
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("HOLD_NOT_ACTIVE"));

		assertThat(available()).isEqualTo(10);
	}

	@Test
	void anotherBuyerCannotReleaseSomeoneElsesHold() throws Exception {
		String holdId = createHold(2);

		mockMvc.perform(delete("/api/v1/reservations/{holdId}", holdId)
				.header(GatewayHeaders.USER_SUB, UUID.randomUUID().toString()))
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("HOLD_NOT_FOUND"));

		assertThat(available()).isEqualTo(8);
	}

	@Test
	void aLapsedHoldCannotBeReleasedBecauseTheSweepOwnsItsTickets() throws Exception {
		String holdId = createHold(2);
		redis.delete(RedisKeys.hold(eventId, holdId));

		mockMvc.perform(delete("/api/v1/reservations/{holdId}", holdId).header(GatewayHeaders.USER_SUB, userSub))
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("HOLD_NOT_ACTIVE"));

		// refusing here is what stops a double refund: the tickets are still the sweep's to return
		assertThat(available()).isEqualTo(8);
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

	private String userIdOf(String holdId) {
		return holdRepository.findById(holdId).orElseThrow().getUserId();
	}

	private int available() {
		return Integer.parseInt(redis.opsForValue().get(RedisKeys.available(eventId, categoryId)));
	}
}
