package com.ticketing.reservation_service;

import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Base64;
import java.util.UUID;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Builds admission tokens for tests, signed the way Queue signs them.
 *
 * <p>The secret must match the one in src/test/resources/application-test.properties, or every
 * token here would be refused.
 */
final class TestAdmissionTokens {

	static final String SECRET = "test-only-admission-secret-not-used-anywhere-real";

	private static final Base64.Encoder ENCODER = Base64.getUrlEncoder().withoutPadding();

	private TestAdmissionTokens() {
	}

	/** A fresh buyer, a fresh admission, inside the selection window. */
	static String forNewBuyer(String eventId, String userSub) {
		return builder(eventId, userSub).sign();
	}

	static Builder builder(String eventId, String userSub) {
		return new Builder(eventId, userSub);
	}

	/** Leaves the claims untouched but breaks the signature, as tampering would. */
	static String tamperWith(String token) {
		String[] parts = token.split("\\.");
		char[] signature = parts[2].toCharArray();
		signature[0] = (signature[0] == 'A') ? 'B' : 'A';
		return parts[0] + "." + parts[1] + "." + new String(signature);
	}

	/** Mutable so each test can bend exactly the one claim it is about. */
	static final class Builder {

		private final String eventId;

		/** The buyer, as Queue writes it: user_id is the Cognito sub the gateway sends. */
		private String userId;

		private String admissionId = UUID.randomUUID().toString();

		private Instant selectionExpiresAt;

		private Instant admissionExpiresAt;

		private String algorithm = "HS256";

		private Builder(String eventId, String userSub) {
			Instant admittedAt = Instant.now().truncatedTo(ChronoUnit.SECONDS);
			this.eventId = eventId;
			this.userId = userSub;
			this.selectionExpiresAt = admittedAt.plus(Duration.ofMinutes(5));
			this.admissionExpiresAt = admittedAt.plus(Duration.ofMinutes(11));
		}

		Builder userId(String userId) {
			this.userId = userId;
			return this;
		}

		Builder admissionId(String admissionId) {
			this.admissionId = admissionId;
			return this;
		}

		Builder selectionExpiresAt(Instant selectionExpiresAt) {
			this.selectionExpiresAt = selectionExpiresAt;
			return this;
		}

		Builder admissionExpiresAt(Instant admissionExpiresAt) {
			this.admissionExpiresAt = admissionExpiresAt;
			return this;
		}

		Builder algorithm(String algorithm) {
			this.algorithm = algorithm;
			return this;
		}

		String sign() {
			String header = encode("{\"alg\":\"" + this.algorithm + "\",\"typ\":\"JWT\"}");
			String claims = encode(claimsJson());
			return header + "." + claims + "." + hmac(header + "." + claims);
		}

		/** Signs with the wrong secret, which is what an outsider minting a token would manage. */
		String signWith(String otherSecret) {
			String header = encode("{\"alg\":\"" + this.algorithm + "\",\"typ\":\"JWT\"}");
			String claims = encode(claimsJson());
			return header + "." + claims + "." + hmac(header + "." + claims, otherSecret);
		}

		/** No signature at all, the oldest JWT trick. */
		String unsigned() {
			String header = encode("{\"alg\":\"none\",\"typ\":\"JWT\"}");
			return header + "." + encode(claimsJson()) + ".";
		}

		private String claimsJson() {
			StringBuilder json = new StringBuilder("{");
			json.append("\"admission_id\":\"").append(this.admissionId).append("\",");
			json.append("\"user_id\":\"").append(this.userId).append("\",");
			json.append("\"event_id\":\"").append(this.eventId).append("\",");
			json.append("\"selection_expires_at\":\"").append(this.selectionExpiresAt).append("\",");
			json.append("\"admission_expires_at\":\"").append(this.admissionExpiresAt).append("\"}");
			return json.toString();
		}
	}

	private static String encode(String json) {
		return ENCODER.encodeToString(json.getBytes(StandardCharsets.UTF_8));
	}

	private static String hmac(String signingInput) {
		return hmac(signingInput, SECRET);
	}

	private static String hmac(String signingInput, String secret) {
		try {
			Mac mac = Mac.getInstance("HmacSHA256");
			mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
			return ENCODER.encodeToString(mac.doFinal(signingInput.getBytes(StandardCharsets.US_ASCII)));
		}
		catch (Exception ex) {
			throw new IllegalStateException("Could not sign a test token", ex);
		}
	}
}
