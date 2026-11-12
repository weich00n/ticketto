package com.ticketing.reservation_service.config;

import java.io.IOException;
import java.util.UUID;
import java.util.regex.Pattern;

import org.slf4j.MDC;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

/**
 * Gives every request a correlation id: the caller's X-Correlation-Id if it sent a usable one, a
 * fresh UUID otherwise. The id goes into the logging context, onto the response header, and into
 * every error body, so one buyer's journey can be followed across services.
 *
 * <p>A caller's id is kept only if it is short and plain. It is stored with each hold and each
 * outbox message in a varchar(64), so a longer one would make every hold insert fail; and it is
 * written into logs and onward messages, so it must not carry anything that could forge a log line.
 */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class CorrelationIdFilter extends OncePerRequestFilter {

	public static final String HEADER = "X-Correlation-Id";

	public static final String MDC_KEY = "correlation_id";

	/** A UUID fits, as does any other id made of letters, digits and simple separators. */
	private static final Pattern USABLE = Pattern.compile("[A-Za-z0-9._:-]{1,64}");

	@Override
	protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
			throws ServletException, IOException {
		String correlationId = request.getHeader(HEADER);
		if (!isUsable(correlationId)) {
			correlationId = UUID.randomUUID().toString();
		}
		MDC.put(MDC_KEY, correlationId);
		response.setHeader(HEADER, correlationId);
		try {
			chain.doFilter(request, response);
		}
		finally {
			MDC.remove(MDC_KEY);
		}
	}

	/** Whether a caller's id may be kept as it is. */
	public static boolean isUsable(String correlationId) {
		return correlationId != null && USABLE.matcher(correlationId).matches();
	}

	/** The current request's correlation id, or null outside a request. */
	public static String current() {
		return MDC.get(MDC_KEY);
	}
}
