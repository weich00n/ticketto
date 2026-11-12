package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.time.Duration;
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
import org.springframework.test.web.servlet.ResultActions;

import com.ticketing.reservation_service.config.GatewayHeaders;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.RedisKeys;

/**
 * Admission enforcement: who may take a hold, for which event, and for how long.
 *
 * <p>Every refusal here must also leave inventory untouched, so a rejected token can never cost
 * a ticket.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class AdmissionEnforcementTest {

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
	void aProperlySignedTokenIsAccepted() throws Exception {
		attempt(TestAdmissionTokens.forNewBuyer(eventId, userSub), userSub)
				.andExpect(status().isCreated());
	}

	@Test
	void aTamperedSignatureIsRefused() throws Exception {
		String token = TestAdmissionTokens.forNewBuyer(eventId, userSub);

		refused(TestAdmissionTokens.tamperWith(token), "INVALID_ADMISSION");
	}

	@Test
	void aTokenSignedWithTheWrongSecretIsRefused() throws Exception {
		// what an outsider minting their own admission would manage
		String forged = TestAdmissionTokens.builder(eventId, userSub).signWith("not-the-real-secret");

		refused(forged, "INVALID_ADMISSION");
	}

	@Test
	void anUnsignedTokenIsRefusedRatherThanTrusted() throws Exception {
		// alg: none, the oldest JWT trick. The algorithm comes from configuration, so the token
		// cannot talk its way out of being checked.
		refused(TestAdmissionTokens.builder(eventId, userSub).unsigned(), "INVALID_ADMISSION");
	}

	@Test
	void aTokenClaimingAnotherAlgorithmIsRefused() throws Exception {
		refused(TestAdmissionTokens.builder(eventId, userSub).algorithm("RS256").sign(), "INVALID_ADMISSION");
	}

	@Test
	void editedClaimsAreRefusedBecauseTheSignatureNoLongerMatches() throws Exception {
		// a buyer trying to extend their own deadline by rewriting the claims
		String token = TestAdmissionTokens.forNewBuyer(eventId, userSub);
		String[] parts = token.split("\\.");
		String rewritten = java.util.Base64.getUrlEncoder().withoutPadding().encodeToString("""
				{"admission_id":"%s","user_id":"%s","event_id":"%s",
				 "selection_expires_at":"%s","admission_expires_at":"%s"}
				""".formatted(UUID.randomUUID(), userSub, eventId,
				Instant.now().plus(Duration.ofDays(1)), Instant.now().plus(Duration.ofDays(1)))
				.getBytes(java.nio.charset.StandardCharsets.UTF_8));

		refused(parts[0] + "." + rewritten + "." + parts[2], "INVALID_ADMISSION");
	}

	@Test
	void aTokenForAnotherEventIsRefused() throws Exception {
		String otherEvent = UUID.randomUUID().toString();

		refused(TestAdmissionTokens.forNewBuyer(otherEvent, userSub), "INVALID_ADMISSION");
	}

	@Test
	void aTokenIssuedToAnotherBuyerIsRefused() throws Exception {
		String someoneElse = TestAdmissionTokens.builder(eventId, UUID.randomUUID().toString()).sign();

		// the caller API Gateway authenticated is not the buyer the token was issued to
		refused(someoneElse, "INVALID_ADMISSION");
	}

	@Test
	void aHoldAfterTheSelectionDeadlineIsRefusedEvenWhileTheTokenStillLives() throws Exception {
		// the contract's case: an eleven-minute token does not permit a hold at minute six
		String token = TestAdmissionTokens.builder(eventId, userSub)
				.selectionExpiresAt(Instant.now().minus(Duration.ofMinutes(1)))
				.admissionExpiresAt(Instant.now().plus(Duration.ofMinutes(5)))
				.sign();

		mockMvc.perform(request(token, userSub))
				.andExpect(status().isForbidden())
				.andExpect(jsonPath("$.error.code").value("SELECTION_DEADLINE_PASSED"))
				.andExpect(jsonPath("$.correlation_id").isNotEmpty());
		assertThat(available()).isEqualTo(10);
	}

	@Test
	void anExpiredAdmissionIsRefused() throws Exception {
		String token = TestAdmissionTokens.builder(eventId, userSub)
				.selectionExpiresAt(Instant.now().minus(Duration.ofMinutes(10)))
				.admissionExpiresAt(Instant.now().minus(Duration.ofMinutes(4)))
				.sign();

		refused(token, "INVALID_ADMISSION");
	}

	@Test
	void aRequestWithoutTheGatewayIdentityHeaderIsUnauthenticated() throws Exception {
		mockMvc.perform(post("/api/v1/reservations")
				.contentType(MediaType.APPLICATION_JSON)
				.content(body(TestAdmissionTokens.forNewBuyer(eventId, userSub))))
				.andExpect(status().isUnauthorized())
				.andExpect(jsonPath("$.error.code").value("UNAUTHENTICATED"));
		assertThat(available()).isEqualTo(10);
	}

	@Test
	void aSecondHoldOnTheSameAdmissionIsRefused() throws Exception {
		String admissionId = UUID.randomUUID().toString();
		String token = TestAdmissionTokens.builder(eventId, userSub).admissionId(admissionId).sign();

		attempt(token, userSub).andExpect(status().isCreated());

		// a fresh token for the same admission, as a second tab would produce
		String again = TestAdmissionTokens.builder(eventId, userSub).admissionId(admissionId).sign();
		mockMvc.perform(request(again, userSub))
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("ADMISSION_ALREADY_HAS_HOLD"));

		assertThat(holdRepository.findByEventId(eventId)).hasSize(1);
		// the refused attempt returned its ticket
		assertThat(available()).isEqualTo(9);
	}

	@Test
	void aUsedAdmissionIsRefusedForWhatItIsBeforeTheLimitIsConsidered() throws Exception {
		// the buyer has taken all five, so the limit would also refuse; the real reason is that
		// this admission is spent, and no ticket may move while finding that out
		String admissionId = UUID.randomUUID().toString();
		mockMvc.perform(request(TestAdmissionTokens.builder(eventId, userSub).admissionId(admissionId).sign(),
				userSub, 5)).andExpect(status().isCreated());

		String again = TestAdmissionTokens.builder(eventId, userSub).admissionId(admissionId).sign();
		mockMvc.perform(request(again, userSub, 1))
				.andExpect(status().isConflict())
				.andExpect(jsonPath("$.error.code").value("ADMISSION_ALREADY_HAS_HOLD"));

		assertThat(available()).isEqualTo(5);
	}

	/** Every refusal returns 403 INVALID_ADMISSION and costs no inventory. */
	private void refused(String token, String expectedCode) throws Exception {
		mockMvc.perform(request(token, userSub))
				.andExpect(status().isForbidden())
				.andExpect(jsonPath("$.error.code").value(expectedCode))
				.andExpect(jsonPath("$.correlation_id").isNotEmpty());

		assertThat(available()).isEqualTo(10);
		assertThat(holdRepository.findByEventId(eventId)).isEmpty();
	}

	private ResultActions attempt(String token, String callerSub) throws Exception {
		return mockMvc.perform(request(token, callerSub));
	}

	private org.springframework.test.web.servlet.RequestBuilder request(String token, String callerSub) {
		return request(token, callerSub, 1);
	}

	private org.springframework.test.web.servlet.RequestBuilder request(String token, String callerSub,
			int quantity) {
		return post("/api/v1/reservations")
				.header(GatewayHeaders.USER_SUB, callerSub)
				.contentType(MediaType.APPLICATION_JSON)
				.content(body(token, quantity));
	}

	private String body(String token) {
		return body(token, 1);
	}

	private String body(String token, int quantity) {
		return """
				{ "event_id": "%s", "category_id": "%s", "quantity": %d, "admission_token": "%s" }
				""".formatted(eventId, categoryId, quantity, token);
	}

	private int available() {
		return Integer.parseInt(redis.opsForValue().get(RedisKeys.available(eventId, categoryId)));
	}
}
