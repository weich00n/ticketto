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
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.RedisKeys;

/** The internal routes Queue and Checkout drive this service through. */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class InternalEndpointsTest {

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

	// --- GET /internal/v1/reservations/{hold_id} : what Checkout bills from ---

	@Test
	void checkoutSeesThePriceSnapshotAndEveryIdItNeeds() throws Exception {
		String holdId = createHold(2);

		mockMvc.perform(get("/internal/v1/reservations/{holdId}", holdId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.hold_id").value(holdId))
				.andExpect(jsonPath("$.user_id").isNotEmpty())
				.andExpect(jsonPath("$.event_id").value(eventId))
				.andExpect(jsonPath("$.category_id").value(categoryId))
				.andExpect(jsonPath("$.admission_id").isNotEmpty())
				.andExpect(jsonPath("$.status").value("ACTIVE"))
				.andExpect(jsonPath("$.quantity").value(2))
				.andExpect(jsonPath("$.unit_amount").value(28800))
				.andExpect(jsonPath("$.total_amount").value(57600))
				.andExpect(jsonPath("$.currency").value("SGD"))
				.andExpect(jsonPath("$.payment_expires_at").isNotEmpty())
				.andExpect(jsonPath("$.hold_expires_at").isNotEmpty())
				.andExpect(jsonPath("$.server_time").isNotEmpty())
				// no order yet, and nulls are omitted rather than sent
				.andExpect(jsonPath("$.order_id").doesNotExist());
	}

	@Test
	void checkoutSeesTheOrderOnceConfirmed() throws Exception {
		String holdId = createHold(2);
		String orderId = UUID.randomUUID().toString();
		mockMvc.perform(post("/internal/v1/reservations/{holdId}/confirm", holdId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "order_id": "%s", "payment_reference": "pi_x" }
						""".formatted(orderId)))
				.andExpect(status().isOk());

		mockMvc.perform(get("/internal/v1/reservations/{holdId}", holdId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.status").value("CONFIRMED"))
				.andExpect(jsonPath("$.order_id").value(orderId));
	}

	@Test
	void aLapsedHoldReadsAsExpiredToCheckoutToo() throws Exception {
		String holdId = createHold(2);
		redis.delete(RedisKeys.hold(eventId, holdId));

		mockMvc.perform(get("/internal/v1/reservations/{holdId}", holdId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.status").value("EXPIRED"));
	}

	@Test
	void anUnknownHoldIsNotFoundInternallyAsWell() throws Exception {
		mockMvc.perform(get("/internal/v1/reservations/{holdId}", UUID.randomUUID().toString()))
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("HOLD_NOT_FOUND"));
	}

	// --- GET /internal/v1/events/{event_id}/capacity : what Queue sizes batches with ---

	@Test
	void queueSeesCapacityWithHeldDerivedFromTheCounter() throws Exception {
		createHold(3);

		mockMvc.perform(get("/internal/v1/events/{eventId}/capacity", eventId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.event_id").value(eventId))
				.andExpect(jsonPath("$.ready").value(true))
				.andExpect(jsonPath("$.sale_status").value("ON_SALE"))
				.andExpect(jsonPath("$.server_time").isNotEmpty())
				.andExpect(jsonPath("$.categories[0].category_id").value(categoryId))
				.andExpect(jsonPath("$.categories[0].total").value(10))
				.andExpect(jsonPath("$.categories[0].available").value(7))
				.andExpect(jsonPath("$.categories[0].held").value(3))
				.andExpect(jsonPath("$.categories[0].sold").value(0));
	}

	@Test
	void heldBecomesZeroAndSoldRisesOnceAHoldIsPaidFor() throws Exception {
		String holdId = createHold(3);
		mockMvc.perform(post("/internal/v1/reservations/{holdId}/confirm", holdId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "order_id": "%s", "payment_reference": "pi_x" }
						""".formatted(UUID.randomUUID())))
				.andExpect(status().isOk());

		mockMvc.perform(get("/internal/v1/events/{eventId}/capacity", eventId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.categories[0].available").value(7))
				.andExpect(jsonPath("$.categories[0].held").value(0))
				.andExpect(jsonPath("$.categories[0].sold").value(3));
	}

	@Test
	void heldIsClampedRatherThanReportedNegative() throws Exception {
		// the three numbers are read at slightly different moments, so the arithmetic can go
		// negative under load; a negative held would be nonsense to Queue
		redis.opsForValue().set(RedisKeys.available(eventId, categoryId), "12");

		mockMvc.perform(get("/internal/v1/events/{eventId}/capacity", eventId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.categories[0].held").value(0));
	}

	@Test
	void anEventWithNoInventoryYetIsReportedAsNotReady() throws Exception {
		String freshEvent = UUID.randomUUID().toString();
		try {
			// a sale status row with no categories: known, but not ready to sell
			mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", freshEvent)
					.contentType(MediaType.APPLICATION_JSON)
					.content("""
							{ "categories": [ { "category_id": "%s", "total_quantity": 1, "unit_amount": 100 } ] }
							""".formatted(UUID.randomUUID())))
					.andExpect(status().isOk());
			inventoryRepository.findByEventIdOrderByCategoryId(freshEvent).forEach(inventoryRepository::delete);

			mockMvc.perform(get("/internal/v1/events/{eventId}/capacity", freshEvent))
					.andExpect(status().isOk())
					.andExpect(jsonPath("$.ready").value(false))
					.andExpect(jsonPath("$.categories").isEmpty());
		}
		finally {
			saleStatusRepository.deleteById(freshEvent);
			redis.delete(redis.keys("*{" + freshEvent + "}*"));
		}
	}

	@Test
	void capacityForAnUnknownEventIsNotFound() throws Exception {
		mockMvc.perform(get("/internal/v1/events/{eventId}/capacity", UUID.randomUUID().toString()))
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("EVENT_NOT_FOUND"));
	}

	// --- GET /internal/v1/admissions/{admission_id}/reservation : Queue's slot check ---

	@Test
	void queueFindsTheHoldAnAdmissionProduced() throws Exception {
		String holdId = createHold(1);
		String admissionId = holdRepository.findById(holdId).orElseThrow().getAdmissionId();

		mockMvc.perform(get("/internal/v1/admissions/{admissionId}/reservation", admissionId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.admission_id").value(admissionId))
				.andExpect(jsonPath("$.has_hold").value(true))
				.andExpect(jsonPath("$.hold_id").value(holdId))
				.andExpect(jsonPath("$.status").value("ACTIVE"));
	}

	@Test
	void anAdmissionWithNoHoldAnswersTwoHundredRatherThanNotFound() throws Exception {
		// Queue must be able to tell "no hold" from "service unreachable", so this is never a 404
		mockMvc.perform(get("/internal/v1/admissions/{admissionId}/reservation", UUID.randomUUID().toString()))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.has_hold").value(false))
				.andExpect(jsonPath("$.hold_id").doesNotExist())
				.andExpect(jsonPath("$.status").doesNotExist());
	}

	@Test
	void queueStillSeesAnAdmissionThatSelectedAndThenReleased() throws Exception {
		String holdId = createHold(1);
		String admissionId = holdRepository.findById(holdId).orElseThrow().getAdmissionId();
		mockMvc.perform(delete("/api/v1/reservations/{holdId}", holdId).header(GatewayHeaders.USER_SUB, userSub))
				.andExpect(status().isOk());

		// the buyer did use their slot, so Queue must not treat this as "never selected"
		mockMvc.perform(get("/internal/v1/admissions/{admissionId}/reservation", admissionId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.has_hold").value(true))
				.andExpect(jsonPath("$.status").value("CANCELLED"));
	}

	@Test
	void everyInternalResponseIsFlatSnakeCaseJson() throws Exception {
		String holdId = createHold(1);
		String admissionId = holdRepository.findById(holdId).orElseThrow().getAdmissionId();

		for (String path : new String[] { "/internal/v1/reservations/" + holdId,
				"/internal/v1/events/" + eventId + "/capacity",
				"/internal/v1/admissions/" + admissionId + "/reservation" }) {
			String body = mockMvc.perform(get(path))
					.andExpect(status().isOk())
					.andExpect(jsonPath("$.data").doesNotExist())
					.andReturn().getResponse().getContentAsString();
			assertThat(body).doesNotContain("holdId").doesNotContain("eventId").doesNotContain("saleStatus");
		}
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
}
