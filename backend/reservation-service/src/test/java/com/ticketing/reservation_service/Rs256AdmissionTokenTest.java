package com.ticketing.reservation_service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.nio.charset.StandardCharsets;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.Signature;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.UUID;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import com.ticketing.reservation_service.exception.ApiException;
import com.ticketing.reservation_service.service.AdmissionTokenService;
import com.ticketing.reservation_service.service.AdmissionTokenService.AdmissionClaims;

/**
 * Verifying RS256 admission tokens, which is how Queue signs them.
 *
 * <p>Queue's real public key does not exist yet, so this test generates a keypair, hands the
 * service the public half and signs with the private half - exactly the shape of the real
 * arrangement, where this service holds nothing that could mint a token.
 */
@SpringBootTest
@ActiveProfiles("test")
class Rs256AdmissionTokenTest {

	private static final KeyPair QUEUE_KEYS = generateKeyPair();

	private static final KeyPair SOMEONE_ELSES_KEYS = generateKeyPair();

	private static final Base64.Encoder URL = Base64.getUrlEncoder().withoutPadding();

	private final String eventId = UUID.randomUUID().toString();

	private final String userSub = UUID.randomUUID().toString();

	@Autowired
	private AdmissionTokenService admissionTokenService;

	@DynamicPropertySource
	static void queuePublicKey(DynamicPropertyRegistry registry) {
		registry.add("reservation.admission-token-algorithm", () -> "RS256");
		registry.add("reservation.admission-token-public-key",
				() -> Base64.getEncoder().encodeToString(QUEUE_KEYS.getPublic().getEncoded()));
	}

	@Test
	void aTokenSignedByQueueIsAccepted() {
		String token = sign(claims(), QUEUE_KEYS);

		AdmissionClaims claims = admissionTokenService.verify(token, eventId, userSub);

		assertThat(claims.userId()).isEqualTo(userSub);
		assertThat(claims.eventId()).isEqualTo(eventId);
		assertThat(claims.admissionId()).isNotBlank();
	}

	@Test
	void aTokenSignedByAnotherKeyIsRefused() {
		String forged = sign(claims(), SOMEONE_ELSES_KEYS);

		assertThatThrownBy(() -> admissionTokenService.verify(forged, eventId, userSub))
				.isInstanceOf(ApiException.class)
				.hasMessageContaining("signature is invalid");
	}

	@Test
	void editedClaimsAreRefused() {
		String token = sign(claims(), QUEUE_KEYS);
		String[] parts = token.split("\\.");
		String rewritten = URL.encodeToString(claims(UUID.randomUUID().toString())
				.getBytes(StandardCharsets.UTF_8));

		assertThatThrownBy(
				() -> admissionTokenService.verify(parts[0] + "." + rewritten + "." + parts[2], eventId, userSub))
				.isInstanceOf(ApiException.class);
	}

	@Test
	void anHmacTokenIsRefusedWhileRsaIsConfigured() {
		// Even signed correctly for HS256: the algorithm is ours to choose, not the token's.
		String hmacToken = TestAdmissionTokens.forNewBuyer(eventId, userSub);

		assertThatThrownBy(() -> admissionTokenService.verify(hmacToken, eventId, userSub))
				.isInstanceOf(ApiException.class)
				.hasMessageContaining("not signed as expected");
	}

	@Test
	void theSelectionDeadlineIsStillEnforcedUnderRsa() {
		Instant past = Instant.now().minus(Duration.ofMinutes(1));
		String token = sign("""
				{"admission_id":"%s","user_id":"%s","event_id":"%s",
				 "selection_expires_at":"%s","admission_expires_at":"%s"}
				""".formatted(UUID.randomUUID(), userSub, eventId, past,
				Instant.now().plus(Duration.ofMinutes(5))), QUEUE_KEYS);

		assertThatThrownBy(() -> admissionTokenService.verify(token, eventId, userSub))
				.isInstanceOf(ApiException.class)
				.hasMessageContaining("time to choose tickets");
	}

	private String claims() {
		return claims(userSub);
	}

	private String claims(String sub) {
		Instant now = Instant.now();
		return """
				{"admission_id":"%s","user_id":"%s","event_id":"%s",
				 "selection_expires_at":"%s","admission_expires_at":"%s"}
				""".formatted(UUID.randomUUID(), sub, eventId,
				now.plus(Duration.ofMinutes(5)), now.plus(Duration.ofMinutes(11)));
	}

	private static String sign(String claimsJson, KeyPair keys) {
		String header = URL.encodeToString("{\"alg\":\"RS256\",\"typ\":\"JWT\"}".getBytes(StandardCharsets.UTF_8));
		String payload = URL.encodeToString(claimsJson.getBytes(StandardCharsets.UTF_8));
		try {
			Signature signer = Signature.getInstance("SHA256withRSA");
			signer.initSign(keys.getPrivate());
			signer.update((header + "." + payload).getBytes(StandardCharsets.US_ASCII));
			return header + "." + payload + "." + URL.encodeToString(signer.sign());
		}
		catch (Exception ex) {
			throw new IllegalStateException("Could not sign a test token", ex);
		}
	}

	private static KeyPair generateKeyPair() {
		try {
			KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
			generator.initialize(2048);
			return generator.generateKeyPair();
		}
		catch (Exception ex) {
			throw new IllegalStateException("Could not generate a test keypair", ex);
		}
	}
}
