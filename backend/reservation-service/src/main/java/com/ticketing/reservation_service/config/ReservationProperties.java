package com.ticketing.reservation_service.config;

import java.time.Duration;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * The reservation.* settings from application.properties, bound once so no class has to read
 * raw strings.
 *
 * <p>paymentDeadlineMinutes is what the buyer sees counting down; holdTtlMinutes is one minute
 * longer, and that extra minute is the contract's processing buffer: a payment landing right on
 * the deadline can still be confirmed.
 */
@ConfigurationProperties("reservation")
public record ReservationProperties(int holdTtlMinutes, int paymentDeadlineMinutes, int maxTicketsPerUser,
		int expirySweepSeconds, TokenAlgorithm admissionTokenAlgorithm, String admissionTokenSecret,
		String admissionTokenPublicKey) {

	/**
	 * How Queue signs the admission token. Configuration decides this, never the token's own
	 * header, which is what stops a forged token asking to be checked a weaker way.
	 */
	public enum TokenAlgorithm {

		/** HMAC-SHA256 with a secret shared with Queue. */
		HS256,

		/** RSA-SHA256 against Queue's public key, so we hold nothing that could mint a token. */
		RS256
	}

	public Duration holdTtl() {
		return Duration.ofMinutes(holdTtlMinutes);
	}

	public Duration paymentDeadline() {
		return Duration.ofMinutes(paymentDeadlineMinutes);
	}
}
