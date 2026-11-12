package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.CyclicBarrier;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.function.Function;
import java.util.function.Supplier;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.test.context.ActiveProfiles;

import com.ticketing.reservation_service.config.GatewayHeaders;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.RedisKeys;

/**
 * The guarantee the whole design exists for: buyers racing for the last tickets over real HTTP,
 * against real Redis and MySQL.
 *
 * <p>Every thread is held at a barrier and released together, so the requests overlap inside the
 * Lua script rather than queueing politely behind one another. The JDK's HttpClient is used
 * directly: it is thread-safe and adds nothing between the test and the server.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class HoldConcurrencyTest {

	private static final HttpClient HTTP = HttpClient.newHttpClient();

	private final String eventId = UUID.randomUUID().toString();

	private final String categoryId = UUID.randomUUID().toString();

	@LocalServerPort
	private int port;

	@Autowired
	private HoldRepository holdRepository;

	@Autowired
	private CategoryInventoryRepository inventoryRepository;

	@Autowired
	private EventSaleStatusRepository saleStatusRepository;

	@Autowired
	private StringRedisTemplate redis;

	@AfterEach
	void removeTestData() {
		holdRepository.deleteAll(holdRepository.findByEventId(eventId));
		inventoryRepository.findByEventIdOrderByCategoryId(eventId).forEach(inventoryRepository::delete);
		saleStatusRepository.deleteById(eventId);
		redis.delete(redis.keys("*{" + eventId + "}*"));
	}

	@Test
	void eightBuyersRacingForTheLastTicketProduceExactlyOneHold() {
		seed(1);

		List<HttpResponse<String>> responses = raceForTickets(8, 1);

		assertThat(countWithStatus(responses, 201)).isEqualTo(1);
		assertThat(countWithStatus(responses, 409)).isEqualTo(7);
		responses.stream()
				.filter(response -> response.statusCode() == 409)
				.forEach(response -> assertThat(response.body()).contains("INSUFFICIENT_INVENTORY"));

		// no oversell: the counter stopped at zero rather than going negative
		assertThat(available()).isZero();
		assertThat(holdRepository.findByEventId(eventId)).hasSize(1);
	}

	@Test
	void buyersRacingForTheLastTwoTicketsCannotBothTakeThem() {
		seed(2);

		// six buyers each want both remaining tickets; only one pair exists
		List<HttpResponse<String>> responses = raceForTickets(6, 2);

		assertThat(countWithStatus(responses, 201)).isEqualTo(1);
		assertThat(countWithStatus(responses, 409)).isEqualTo(5);
		assertThat(available()).isZero();
		assertThat(holdRepository.findByEventId(eventId)).hasSize(1);
		assertThat(holdRepository.findByEventId(eventId).getFirst().getQuantity()).isEqualTo(2);
	}

	@Test
	void everyTicketIsHandedOutExactlyOnceWhenBuyersOutnumberTickets() {
		seed(5);

		List<HttpResponse<String>> responses = raceForTickets(12, 1);

		assertThat(countWithStatus(responses, 201)).isEqualTo(5);
		assertThat(countWithStatus(responses, 409)).isEqualTo(7);
		assertThat(available()).isZero();
		assertThat(holdRepository.findByEventId(eventId)).hasSize(5);
	}

	@Test
	void oneBuyerInSeveralTabsCannotGetPastTheFiveTicketLimit() {
		seed(20);
		String userSub = UUID.randomUUID().toString();

		// six tabs at once, two tickets each, with plenty of stock: only the limit can stop them
		List<HttpResponse<String>> responses = race(6, 2, () -> userSub,
				sub -> TestAdmissionTokens.forNewBuyer(eventId, sub));

		// two holds make four tickets; a third would make six
		assertThat(countWithStatus(responses, 201)).isEqualTo(2);
		assertThat(countWithStatus(responses, 409)).isEqualTo(4);
		responses.stream()
				.filter(response -> response.statusCode() == 409)
				.forEach(response -> assertThat(response.body()).contains("PURCHASE_LIMIT_EXCEEDED"));
		assertThat(available()).isEqualTo(16);
	}

	/** Fires {@code buyers} simultaneous hold requests, each for {@code quantity} tickets. */
	private List<HttpResponse<String>> raceForTickets(int buyers, int quantity) {
		// each buyer is a different person with their own admission, and arrives with the
		// Cognito id API Gateway would have put on the request
		return race(buyers, quantity, () -> UUID.randomUUID().toString(),
				userSub -> TestAdmissionTokens.forNewBuyer(eventId, userSub));
	}

	private List<HttpResponse<String>> race(int attemptCount, int quantity, Supplier<String> callerSub,
			Function<String, String> tokenFor) {
		CyclicBarrier startTogether = new CyclicBarrier(attemptCount);
		List<Callable<HttpResponse<String>>> attempts = new ArrayList<>();
		for (int i = 0; i < attemptCount; i++) {
			attempts.add(() -> {
				String userSub = callerSub.get();
				String token = tokenFor.apply(userSub);
				HttpRequest request = HttpRequest.newBuilder(URI.create(baseUrl() + "/api/v1/reservations"))
						.header("Content-Type", "application/json")
						.header(GatewayHeaders.USER_SUB, userSub)
						.POST(HttpRequest.BodyPublishers.ofString("""
								{ "event_id": "%s", "category_id": "%s", "quantity": %d, "admission_token": "%s" }
								""".formatted(eventId, categoryId, quantity, token)))
						.build();
				startTogether.await();
				return HTTP.send(request, HttpResponse.BodyHandlers.ofString());
			});
		}

		try (ExecutorService pool = Executors.newFixedThreadPool(attemptCount)) {
			List<HttpResponse<String>> responses = new ArrayList<>();
			for (Future<HttpResponse<String>> attempt : pool.invokeAll(attempts)) {
				responses.add(attempt.get());
			}
			return responses;
		}
		catch (Exception ex) {
			throw new IllegalStateException("The race could not be run", ex);
		}
	}

	private void seed(int total) {
		HttpRequest request = jsonPost("/internal/v1/events/" + eventId + "/inventory", """
				{ "categories": [ { "category_id": "%s", "total_quantity": %d, "unit_amount": 28800 } ] }
				""".formatted(categoryId, total));
		try {
			assertThat(HTTP.send(request, HttpResponse.BodyHandlers.ofString()).statusCode()).isEqualTo(200);
		}
		catch (Exception ex) {
			throw new IllegalStateException("Could not seed inventory", ex);
		}
	}

	private String baseUrl() {
		return "http://localhost:" + this.port;
	}

	private HttpRequest jsonPost(String path, String body) {
		return HttpRequest.newBuilder(URI.create(baseUrl() + path))
				.header("Content-Type", "application/json")
				.POST(HttpRequest.BodyPublishers.ofString(body))
				.build();
	}

	private static long countWithStatus(List<HttpResponse<String>> responses, int status) {
		return responses.stream().filter(response -> response.statusCode() == status).count();
	}

	private int available() {
		return Integer.parseInt(redis.opsForValue().get(RedisKeys.available(eventId, categoryId)));
	}
}
