package com.ticketing.reservation_service;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.hamcrest.Matchers;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.context.ActiveProfiles;

import com.ticketing.reservation_service.config.CorrelationIdFilter;
import com.ticketing.reservation_service.controller.HealthController;
import com.ticketing.reservation_service.controller.InternalEventController;
import com.ticketing.reservation_service.service.EventCancellationService;
import com.ticketing.reservation_service.service.InventoryService;

/**
 * The shape of what we return, checked without touching MySQL or Redis: the section 10 error
 * body, the correlation id, and snake_case JSON with no data envelope.
 */
@WebMvcTest(controllers = { InternalEventController.class, HealthController.class })
@ActiveProfiles("test")
class ErrorContractTest {

	private static final String EVENT_ID = "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6a7b";

	private static final String UUID_PATTERN = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";

	@Autowired
	private MockMvc mockMvc;

	@MockitoBean
	private InventoryService inventoryService;

	@MockitoBean
	private EventCancellationService cancellationService;

	@Test
	void healthReportsUp() throws Exception {
		mockMvc.perform(get("/health"))
				.andExpect(status().isOk())
				.andExpect(jsonPath("$.status").value("UP"));
	}

	@Test
	void anEmptyCategoryListIsRejectedWithTheContractErrorBody() throws Exception {
		mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", EVENT_ID)
				.contentType(MediaType.APPLICATION_JSON)
				.content("{\"categories\": []}"))
				.andExpect(status().isBadRequest())
				.andExpect(jsonPath("$.error.code").value("INVALID_REQUEST"))
				.andExpect(jsonPath("$.error.message").value(Matchers.containsString("categories")))
				// correlation_id proves snake_case naming, and sits beside error, not inside a wrapper
				.andExpect(jsonPath("$.correlation_id").isNotEmpty())
				.andExpect(jsonPath("$.data").doesNotExist())
				.andExpect(jsonPath("$.correlationId").doesNotExist());
	}

	@Test
	void aMissingQuantityIsRejected() throws Exception {
		mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", EVENT_ID)
				.contentType(MediaType.APPLICATION_JSON)
				.content("""
						{ "categories": [ { "category_id": "8c7d6e5f-4a3b-42c1-9d8e-7f6a5b4c3d2e",
						                    "unit_amount": 28800 } ] }
						"""))
				.andExpect(status().isBadRequest())
				.andExpect(jsonPath("$.error.code").value("INVALID_REQUEST"));
	}

	@Test
	void unparseableJsonIsRejected() throws Exception {
		mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", EVENT_ID)
				.contentType(MediaType.APPLICATION_JSON)
				.content("{ not json"))
				.andExpect(status().isBadRequest())
				.andExpect(jsonPath("$.error.code").value("INVALID_REQUEST"))
				.andExpect(jsonPath("$.correlation_id").isNotEmpty());
	}

	@Test
	void aSuppliedCorrelationIdIsEchoedBackAndUsedInTheErrorBody() throws Exception {
		String correlationId = "9c8b7a65-4321-4fed-9876-0a1b2c3d4e5f";

		mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", EVENT_ID)
				.header(CorrelationIdFilter.HEADER, correlationId)
				.contentType(MediaType.APPLICATION_JSON)
				.content("{\"categories\": []}"))
				.andExpect(status().isBadRequest())
				.andExpect(header().string(CorrelationIdFilter.HEADER, correlationId))
				.andExpect(jsonPath("$.correlation_id").value(correlationId));
	}

	@Test
	void aCorrelationIdIsGeneratedWhenTheCallerSendsNone() throws Exception {
		mockMvc.perform(get("/health"))
				.andExpect(status().isOk())
				.andExpect(header().string(CorrelationIdFilter.HEADER, Matchers.not(Matchers.emptyOrNullString())));
	}

	/** Longer than the varchar(64) it is stored in with every hold, which would fail the insert. */
	@Test
	void aCorrelationIdTooLongToStoreIsReplaced() throws Exception {
		String tooLong = "x".repeat(65);

		mockMvc.perform(get("/health").header(CorrelationIdFilter.HEADER, tooLong))
				.andExpect(status().isOk())
				.andExpect(header().string(CorrelationIdFilter.HEADER, Matchers.matchesPattern(UUID_PATTERN)));
	}

	/** It is written into log lines, so nothing beyond plain id characters is let through. */
	@Test
	void aCorrelationIdWithUnsafeCharactersIsReplaced() throws Exception {
		mockMvc.perform(get("/health").header(CorrelationIdFilter.HEADER, "abc def<script>"))
				.andExpect(status().isOk())
				.andExpect(header().string(CorrelationIdFilter.HEADER, Matchers.matchesPattern(UUID_PATTERN)));
	}

	@Test
	void anUnknownPathIsA404InTheContractBodyNotA500() throws Exception {
		mockMvc.perform(get("/api/v1/no-such-endpoint"))
				.andExpect(status().isNotFound())
				.andExpect(jsonPath("$.error.code").value("ROUTE_NOT_FOUND"))
				.andExpect(jsonPath("$.correlation_id").isNotEmpty());
	}

	@Test
	void aWrongMethodIsA405ThatSaysWhichMethodsAreAllowed() throws Exception {
		mockMvc.perform(post("/health"))
				.andExpect(status().isMethodNotAllowed())
				.andExpect(header().string("Allow", Matchers.containsString("GET")))
				.andExpect(jsonPath("$.error.code").value("METHOD_NOT_ALLOWED"));
	}

	@Test
	void aBodyThatIsNotJsonIsAMalformedRequest() throws Exception {
		mockMvc.perform(post("/internal/v1/events/{eventId}/inventory", EVENT_ID)
				.contentType(MediaType.TEXT_PLAIN)
				.content("categories"))
				.andExpect(status().isBadRequest())
				.andExpect(jsonPath("$.error.code").value("INVALID_REQUEST"));
	}
}
