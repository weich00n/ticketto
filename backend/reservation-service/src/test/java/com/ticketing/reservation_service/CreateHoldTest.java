package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.time.Duration;
import java.time.Instant;
import java.util.UUID;

import org.hamcrest.Matchers;
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
import org.springframework.test.web.servlet.RequestBuilder;
import org.springframework.test.web.servlet.ResultActions;

import com.ticketing.reservation_service.config.GatewayHeaders;
import com.ticketing.reservation_service.model.EventSaleStatus;
import com.ticketing.reservation_service.model.SaleStatus;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.RedisKeys;

/** Creating a hold: the response contract, the price snapshot, the deadlines and the refusals. */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class CreateHoldTest {

	private static final long UNIT_AMOUNT = 28800L;

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
	private StringRedisTemplate redis;

	@BeforeEach
	void seedFiveHundredTickets() throws Exception {
		seed(500);
	}

	@AfterEach
	void removeTestData() {
		holdRepository.deleteAll(holdRepository.findByEventId(eventId));
		inventoryRepository.findByEventIdOrderByCategoryId(eventId).forEach(inventoryRepository::delete);
		saleStatusRepository.deleteById(eventId);
		redis.delete(redis.keys("*{" + eventId + "}*"));
	}

	@Test
	void createsAnActiveHoldWithThePriceSnapshotAndBothDeadlines() throws Exception {
		Buyer buyer = new Buyer();

		String body = hold(buyer, 2)
				.andExpect(status().isCreated())
				.andExpect(jsonPath("$.hold_id").isNotEmpty())
				.andExpect(jsonPath("$.admission_id").isNotEmpty())
				.andExpect(jsonPath("$.status").value("ACTIVE"))
				.andExpect(jsonPath("$.quantity").value(2))
				.andExpect(jsonPath("$.unit_amount").value(UNIT_AMOUNT))
				.andExpect(jsonPath("$.total_amount").value(57600))
				.andExpect(jsonPath("$.currency").value("SGD"))
				.andExpect(jsonPath("$.data").doesNotExist())
				.andReturn().getResponse().getContentAsString();

		// the buyer sees five minutes; the hold itself lives six, the processing buffer. Measured
		// from created_at, not server_time: server_time is read after the Redis and MySQL writes,
		// so it can fall a second later than the moment the deadlines were set.
		String holdId = readString(body, "hold_id");
		Instant createdAt = holdRepository.findById(holdId).orElseThrow().getCreatedAt();
		assertThat(Instant.parse(readString(body, "payment_expires_at")))
				.isEqualTo(createdAt.plus(Duration.ofMinutes(5)));
		assertThat(Instant.parse(readString(body, "hold_expires_at")))
				.isEqualTo(createdAt.plus(Duration.ofMinutes(6)));
		assertThat(Instant.parse(readString(body, "server_time"))).isBetween(createdAt, createdAt.plusSeconds(5));

		assertThat(holdRepository.findById(holdId)).get()
				.satisfies(hold -> {
					assertThat(hold.getUnitAmount()).isEqualTo(UNIT_AMOUNT);
					assertThat(hold.getTotalAmount()).isEqualTo(57600L);
					// the token's user_id and the gateway's sub are the same buyer, and both are kept
					assertThat(hold.getUserId()).isEqualTo(buyer.userSub);
					assertThat(hold.getUserSub()).isEqualTo(buyer.userSub);
				});
		assertThat(available()).isEqualTo(498);
		assertThat(redis.getExpire(RedisKeys.hold(eventId, holdId))).isBetween(300L, 360L);
	}

	@Test
	void theWholeFiveTicketsAreAllowedButTheSixthIsNot() throws Exception {
		Buyer buyer = new Buyer();

		hold(buyer, 5).andExpect(status().isCreated());

		hold(buyer, 1)
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("PURCHASE_LIMIT_EXCEEDED"))
				.andExpect(jsonPath("$.error.message").value(Matchers.containsString("0 more")));

		// the refused request took nothing
		assertThat(available()).isEqualTo(495);
	}

	@Test
	void theLimitHoldsAcrossSeparateHoldsBySameBuyer() throws Exception {
		Buyer buyer = new Buyer();

		hold(buyer, 3).andExpect(status().isCreated());
		hold(buyer, 3)
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("PURCHASE_LIMIT_EXCEEDED"))
				.andExpect(jsonPath("$.error.message").value(Matchers.containsString("2 more")));

		// a different buyer is unaffected
		hold(new Buyer(), 3).andExpect(status().isCreated());
		assertThat(available()).isEqualTo(494);
	}

	@Test
	void aBuyerAlreadyOverTheLimitIsToldZeroMoreNotANegative() throws Exception {
		Buyer buyer = new Buyer();
		// only reachable when a counter has drifted, but the message must still make sense
		redis.opsForValue().set(RedisKeys.userQuantity(eventId, buyer.userSub), "7");

		hold(buyer, 1)
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.message").value(Matchers.containsString("0 more")));
	}

	@Test
	void refusesMoreTicketsThanAreLeft() throws Exception {
		redis.opsForValue().set(RedisKeys.available(eventId, categoryId), "2");

		hold(new Buyer(), 3)
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("INSUFFICIENT_INVENTORY"))
				.andExpect(jsonPath("$.error.message").value(Matchers.containsString("2 left")));

		assertThat(available()).isEqualTo(2);
		assertThat(holdRepository.findByEventId(eventId)).isEmpty();
	}

	@Test
	void refusesASecondHoldOnTheSameAdmissionAndGivesTheTicketsBack() throws Exception {
		Buyer buyer = new Buyer();
		String admissionId = UUID.randomUUID().toString();

		mockMvc.perform(request(buyer, admissionId, 2)).andExpect(status().isCreated());
		assertThat(available()).isEqualTo(498);

		mockMvc.perform(request(buyer, admissionId, 1))
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("ADMISSION_ALREADY_HAS_HOLD"));

		// Redis allocated before MySQL refused, so the undo must have handed the ticket back
		assertThat(available()).isEqualTo(498);
		assertThat(holdRepository.findByEventId(eventId)).hasSize(1);
	}

	@Test
	void refusesAHoldOnACancelledEvent() throws Exception {
		EventSaleStatus sale = saleStatusRepository.findById(eventId).orElseThrow();
		sale.setSaleStatus(SaleStatus.CANCELLED);
		saleStatusRepository.save(sale);

		hold(new Buyer(), 1)
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("EVENT_CANCELLED"));
	}

	@Test
	void refusesAnUnknownCategory() throws Exception {
		Buyer buyer = new Buyer();
		String token = TestAdmissionTokens.forNewBuyer(eventId, buyer.userSub);

		mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, buyer.userSub)
				.contentType(MediaType.APPLICATION_JSON)
				.content(json(eventId, UUID.randomUUID().toString(), 1, token)))
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("CATEGORY_NOT_FOUND"));
	}

	@Test
	void refusesAnUnknownEvent() throws Exception {
		Buyer buyer = new Buyer();
		String unknownEvent = UUID.randomUUID().toString();
		String token = TestAdmissionTokens.forNewBuyer(unknownEvent, buyer.userSub);

		mockMvc.perform(post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, buyer.userSub)
				.contentType(MediaType.APPLICATION_JSON)
				.content(json(unknownEvent, categoryId, 1, token)))
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("EVENT_NOT_FOUND"));
	}

	@Test
	void refusesAQuantityOutsideOneToFive() throws Exception {
		hold(new Buyer(), 0)
				.andExpect(status().isBadRequest())
				.andExpect(jsonPath("$.error.code").value("INVALID_REQUEST"));
		hold(new Buyer(), 6)
				.andExpect(status().isBadRequest())
				.andExpect(jsonPath("$.error.code").value("INVALID_REQUEST"));
	}

	@Test
	void aMissingCounterIsRebuiltRatherThanTreatedAsASellOut() throws Exception {
		redis.delete(RedisKeys.available(eventId, categoryId));

		// The counter is restored from MySQL and the hold goes through, so a buyer never
		// sees a failure merely because the live state was lost.
		hold(new Buyer(), 1).andExpect(status().isCreated());

		assertThat(available()).isEqualTo(499);
	}

	/** One person: a platform user id inside the token, and a Cognito id from the gateway. */
	private static final class Buyer {

		private final String userSub = UUID.randomUUID().toString();
	}

	private ResultActions hold(Buyer buyer, int quantity) throws Exception {
		return mockMvc.perform(request(buyer, UUID.randomUUID().toString(), quantity));
	}

	private RequestBuilder request(Buyer buyer, String admissionId, int quantity) {
		String token = TestAdmissionTokens.builder(eventId, buyer.userSub)
				.admissionId(admissionId)
				.sign();
		return post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, buyer.userSub)
				.contentType(MediaType.APPLICATION_JSON)
				.content(json(eventId, categoryId, quantity, token));
	}

	private static String json(String event, String category, int quantity, String token) {
		return """
				{ "event_id": "%s", "category_id": "%s", "quantity": %d, "admission_token": "%s" }
				""".formatted(event, category, quantity, token);
	}

	private void seed(int total) throws Exception {
		String body = """
				{ "categories": [ { "category_id": "%s", "total_quantity": %d, "unit_amount": %d } ] }
				""".formatted(categoryId, total, UNIT_AMOUNT);
		mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", eventId)
				.contentType(MediaType.APPLICATION_JSON)
				.content(body)).andExpect(status().isOk());
	}

	private int available() {
		return Integer.parseInt(redis.opsForValue().get(RedisKeys.available(eventId, categoryId)));
	}

	private static String readString(String json, String field) {
		return com.jayway.jsonpath.JsonPath.read(json, "$." + field);
	}
}
