package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.UUID;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.context.ActiveProfiles;

import com.ticketing.reservation_service.model.CategoryInventoryId;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.RedisKeys;

/**
 * Seeding and availability against the real MySQL and Redis from docker-compose.
 *
 * <p>Each test uses a fresh random event id and cleans up after itself, so runs do not collide.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class InventoryIntegrationTest {

	private final String eventId = UUID.randomUUID().toString();

	private final String standardCategory = UUID.randomUUID().toString();

	private final String premiumCategory = UUID.randomUUID().toString();

	@Autowired
	private MockMvc mockMvc;

	@Autowired
	private CategoryInventoryRepository inventoryRepository;

	@Autowired
	private EventSaleStatusRepository saleStatusRepository;

	@Autowired
	private StringRedisTemplate redis;

	@AfterEach
	void removeTestData() {
		inventoryRepository.findByEventIdOrderByCategoryId(eventId).forEach(inventoryRepository::delete);
		saleStatusRepository.deleteById(eventId);
		redis.delete(RedisKeys.available(eventId, standardCategory));
		redis.delete(RedisKeys.available(eventId, premiumCategory));
		redis.delete(RedisKeys.saleStatus(eventId));
	}

	@Test
	void seedingAnEventThenReadingAvailabilityReportsTheRightNumbers() throws Exception {
		seed(500, 1500).andExpect(status().isOk())
				.andExpect(jsonPath("$.created").value(true))
				.andExpect(jsonPath("$.sale_status").value("ON_SALE"));

		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.event_id").value(eventId))
				.andExpect(jsonPath("$.status").value("ON_SALE"))
				// UTC ISO-8601, whole seconds
				.andExpect(jsonPath("$.server_time").value(org.hamcrest.Matchers.endsWith("Z")))
				.andExpect(jsonPath("$.categories.length()").value(2))
				.andExpect(jsonPath("$.categories[?(@.category_id=='" + standardCategory + "')].total").value(500))
				.andExpect(jsonPath("$.categories[?(@.category_id=='" + standardCategory + "')].free").value(500))
				.andExpect(jsonPath("$.categories[?(@.category_id=='" + standardCategory + "')].sold").value(0))
				.andExpect(jsonPath("$.categories[?(@.category_id=='" + premiumCategory + "')].total").value(1500))
				.andExpect(jsonPath("$.categories[?(@.category_id=='" + premiumCategory + "')].free").value(1500))
				// no data envelope anywhere
				.andExpect(jsonPath("$.data").doesNotExist());
	}

	@Test
	void seedingWritesTheRedisKeysAvailabilityAndTheHoldScriptRead() throws Exception {
		seed(500, 1500).andExpect(status().isOk());

		assertThat(redis.opsForValue().get(RedisKeys.available(eventId, standardCategory))).isEqualTo("500");
		assertThat(redis.opsForValue().get(RedisKeys.available(eventId, premiumCategory))).isEqualTo("1500");
		assertThat(redis.opsForValue().get(RedisKeys.saleStatus(eventId))).isEqualTo("ON_SALE");
	}

	@Test
	void soldTicketsComeFromMysqlWhileFreeComesFromTheCounter() throws Exception {
		seed(500, 1500).andExpect(status().isOk());

		// A confirmation moves these two together; here we set them by hand to prove
		// availability reads each number from the right place and subtracts nothing.
		markSold(standardCategory, 170);
		redis.opsForValue().set(RedisKeys.available(eventId, standardCategory), "312");

		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.categories[?(@.category_id=='" + standardCategory + "')].total").value(500))
				.andExpect(jsonPath("$.categories[?(@.category_id=='" + standardCategory + "')].free").value(312))
				.andExpect(jsonPath("$.categories[?(@.category_id=='" + standardCategory + "')].sold").value(170));
	}

	@Test
	void replayingTheSameSeedChangesNothing() throws Exception {
		seed(500, 1500).andExpect(status().isOk()).andExpect(jsonPath("$.created").value(true));

		// a live hold has already moved the counter; the replay must not reset it
		redis.opsForValue().set(RedisKeys.available(eventId, standardCategory), "498");

		seed(500, 1500).andExpect(status().isOk()).andExpect(jsonPath("$.created").value(false));

		assertThat(redis.opsForValue().get(RedisKeys.available(eventId, standardCategory))).isEqualTo("498");
		assertThat(inventoryRepository.findByEventIdOrderByCategoryId(eventId)).hasSize(2);
	}

	@Test
	void seedingTheSameEventWithDifferentTotalsIsRefused() throws Exception {
		seed(500, 1500).andExpect(status().isOk());

		seed(600, 1500)
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("INVENTORY_CONFLICT"))
				.andExpect(jsonPath("$.correlation_id").isNotEmpty());

		CategoryInventoryId id = new CategoryInventoryId(eventId, standardCategory);
		assertThat(inventoryRepository.findById(id)).get()
				.extracting(row -> row.getTotalQuantity())
				.isEqualTo(500);
	}

	@Test
	void anUnknownEventIsNotFound() throws Exception {
		mockMvc.perform(get("/api/v1/events/{eventId}/availability", UUID.randomUUID().toString()))
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("EVENT_NOT_FOUND"))
				.andExpect(jsonPath("$.correlation_id").isNotEmpty());
	}

	@Test
	void aMissingCounterIsRebuiltFromMysqlRatherThanReportedAsASellOut() throws Exception {
		seed(500, 1500).andExpect(status().isOk());
		redis.delete(RedisKeys.available(eventId, standardCategory));

		// Rebuilt from MySQL: total 500 − sold 0 − held 0. Reporting zero free here would tell buyers the
		// event had sold out when the truth is that Redis lost its counter.
		mockMvc.perform(get("/api/v1/events/{eventId}/availability", eventId))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.categories[?(@.category_id=='" + standardCategory + "')].free").value(500));
	}

	private org.springframework.test.web.servlet.ResultActions seed(int standardTotal, int premiumTotal)
			throws Exception {
		String body = """
				{
				  "sale_status": "ON_SALE",
				  "categories": [
				    { "category_id": "%s", "total_quantity": %d, "unit_amount": 28800, "currency": "SGD" },
				    { "category_id": "%s", "total_quantity": %d, "unit_amount": 8800 }
				  ]
				}
				""".formatted(standardCategory, standardTotal, premiumCategory, premiumTotal);
		return mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", eventId)
				.contentType(MediaType.APPLICATION_JSON)
				.content(body));
	}

	private void markSold(String categoryId, int soldQuantity) {
		inventoryRepository.findById(new CategoryInventoryId(eventId, categoryId)).ifPresent(row -> {
			row.setSoldQuantity(soldQuantity);
			inventoryRepository.save(row);
		});
	}
}
