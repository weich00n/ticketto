package com.ticketing.reservation_service.service;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyFactory;
import java.security.MessageDigest;
import java.security.PublicKey;
import java.security.Signature;
import java.security.spec.X509EncodedKeySpec;
import java.time.Clock;
import java.time.Instant;
import java.util.Base64;
import java.util.Map;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

import com.ticketing.reservation_service.config.ReservationProperties;
import com.ticketing.reservation_service.config.ReservationProperties.TokenAlgorithm;
import com.ticketing.reservation_service.exception.ApiException;
import com.ticketing.reservation_service.exception.ErrorCode;

import tools.jackson.databind.json.JsonMapper;

/**
 * Verifies the admission token Queue issued, then reads its claims.
 *
 * <p>The order matters: the signature is checked before any claim is believed. Two rules keep
 * that check honest. The algorithm comes from configuration, so a forged token cannot ask to be
 * verified a weaker way or not at all, and the signature comparison is constant-time, so it
 * leaks nothing about how close a guess was.
 *
 * <p>This is separate from the Cognito access token, which this service never verifies: API
 * Gateway does that and passes the buyer down as X-User-Sub.
 */
@Service
public class AdmissionTokenService {

	private static final Logger log = LoggerFactory.getLogger(AdmissionTokenService.class);

	private static final String HMAC_SHA256 = "HmacSHA256";

	private static final String SHA256_WITH_RSA = "SHA256withRSA";

	private final JsonMapper jsonMapper;

	private final Clock clock;

	private final TokenAlgorithm algorithm;

	private final byte[] secret;

	private final PublicKey publicKey;

	public AdmissionTokenService(JsonMapper jsonMapper, Clock clock, ReservationProperties properties) {
		this.jsonMapper = jsonMapper;
		this.clock = clock;
		this.algorithm = properties.admissionTokenAlgorithm();
		this.secret = readSecret(properties);
		this.publicKey = readPublicKey(properties);
	}

	/**
	 * Verifies the token and returns its claims, or refuses it.
	 *
	 * @param expectedEventId the event the request is for; a token for another event is refused
	 * @param authenticatedUserSub the buyer API Gateway authenticated, never a value from the body
	 */
	public AdmissionClaims verify(String token, String expectedEventId, String authenticatedUserSub) {
		if (!StringUtils.hasText(authenticatedUserSub)) {
			// Behind the gateway this cannot happen: its parameter mapping always sets the header.
			throw new ApiException(ErrorCode.UNAUTHENTICATED);
		}
		if (!StringUtils.hasText(token)) {
			throw new ApiException(ErrorCode.INVALID_ADMISSION, "An admission token is required.");
		}

		String[] parts = token.split("\\.");
		if (parts.length != 3) {
			throw new ApiException(ErrorCode.INVALID_ADMISSION, "Admission token is not a signed token.");
		}
		requireExpectedAlgorithm(parts[0]);
		requireValidSignature(parts[0] + "." + parts[1], parts[2]);

		AdmissionClaims claims = readClaims(parts[1]);
		requireMatchingBuyer(claims, authenticatedUserSub);
		requireMatchingEvent(claims, expectedEventId);
		requireWithinDeadlines(claims);
		return claims;
	}

	/** The token's own header may not choose the algorithm; it must match what we configured. */
	private void requireExpectedAlgorithm(String encodedHeader) {
		Object declared = decodeJson(encodedHeader).get("alg");
		if (!this.algorithm.name().equals(declared)) {
			log.warn("Refused an admission token declaring alg {} while {} is configured", declared, this.algorithm);
			throw new ApiException(ErrorCode.INVALID_ADMISSION, "Admission token is not signed as expected.");
		}
	}

	private void requireValidSignature(String signingInput, String encodedSignature) {
		byte[] signature;
		try {
			signature = Base64.getUrlDecoder().decode(encodedSignature);
		}
		catch (RuntimeException ex) {
			throw new ApiException(ErrorCode.INVALID_ADMISSION, "Admission token signature is unreadable.");
		}
		boolean valid = switch (this.algorithm) {
			case HS256 -> hmacMatches(signingInput, signature);
			case RS256 -> rsaVerifies(signingInput, signature);
		};
		if (!valid) {
			throw new ApiException(ErrorCode.INVALID_ADMISSION,
					"Admission token signature is invalid, so the token cannot be trusted.");
		}
	}

	private boolean hmacMatches(String signingInput, byte[] signature) {
		try {
			Mac mac = Mac.getInstance(HMAC_SHA256);
			mac.init(new SecretKeySpec(this.secret, HMAC_SHA256));
			byte[] expected = mac.doFinal(signingInput.getBytes(StandardCharsets.US_ASCII));
			// Constant-time: a byte-by-byte comparison would leak where a forgery first differs.
			return MessageDigest.isEqual(expected, signature);
		}
		catch (Exception ex) {
			throw new IllegalStateException("HMAC verification could not run", ex);
		}
	}

	private boolean rsaVerifies(String signingInput, byte[] signature) {
		try {
			Signature verifier = Signature.getInstance(SHA256_WITH_RSA);
			verifier.initVerify(this.publicKey);
			verifier.update(signingInput.getBytes(StandardCharsets.US_ASCII));
			return verifier.verify(signature);
		}
		catch (GeneralSecurityException | RuntimeException ex) {
			// A malformed signature is a refusal, not a server fault.
			return false;
		}
	}

	private AdmissionClaims readClaims(String encodedClaims) {
		Map<String, Object> claims = decodeJson(encodedClaims);
		return new AdmissionClaims(
				requiredClaim(claims, "admission_id"),
				requiredClaim(claims, "user_id"),
				requiredClaim(claims, "event_id"),
				requiredInstant(claims, "selection_expires_at"),
				requiredInstant(claims, "admission_expires_at"));
	}

	/**
	 * The token must belong to the buyer the gateway authenticated.
	 *
	 * <p>The team's user_id is the Cognito sub: Queue signs the admission with the X-User-Sub it
	 * was given, and the gateway hands us the same header. Anything else is a token issued to
	 * someone else - shared or stolen - and using it would also count the tickets against the
	 * wrong buyer's five-ticket allowance.
	 */
	private void requireMatchingBuyer(AdmissionClaims claims, String authenticatedUserSub) {
		if (!claims.userId().equals(authenticatedUserSub)) {
			log.warn("Admission {} was issued to another buyer than the authenticated caller", claims.admissionId());
			throw new ApiException(ErrorCode.INVALID_ADMISSION,
					"Admission token is invalid, or not for this buyer and event.");
		}
	}

	private void requireMatchingEvent(AdmissionClaims claims, String expectedEventId) {
		if (!claims.eventId().equals(expectedEventId)) {
			throw new ApiException(ErrorCode.INVALID_ADMISSION,
					"Admission token is invalid, or not for this buyer and event.");
		}
	}

	/**
	 * An eleven-minute token does not permit a hold after the five-minute selection deadline, so
	 * both deadlines are checked and the selection one is reported separately.
	 */
	private void requireWithinDeadlines(AdmissionClaims claims) {
		Instant now = this.clock.instant();
		if (now.isAfter(claims.admissionExpiresAt())) {
			throw new ApiException(ErrorCode.INVALID_ADMISSION, "This admission has expired.");
		}
		if (now.isAfter(claims.selectionExpiresAt())) {
			throw new ApiException(ErrorCode.SELECTION_DEADLINE_PASSED,
					"The time to choose tickets for this admission has passed.");
		}
	}

	private Map<String, Object> decodeJson(String encoded) {
		try {
			byte[] json = Base64.getUrlDecoder().decode(encoded);
			@SuppressWarnings("unchecked")
			Map<String, Object> decoded = jsonMapper.readValue(new String(json, StandardCharsets.UTF_8), Map.class);
			return decoded;
		}
		catch (RuntimeException ex) {
			throw new ApiException(ErrorCode.INVALID_ADMISSION, "Admission token could not be read.");
		}
	}

	private static String requiredClaim(Map<String, Object> claims, String name) {
		String value = optionalString(claims, name);
		if (value == null) {
			throw new ApiException(ErrorCode.INVALID_ADMISSION, "Admission token is missing " + name + ".");
		}
		return value;
	}

	private static String optionalString(Map<String, Object> claims, String name) {
		Object value = claims.get(name);
		return (value instanceof String text && StringUtils.hasText(text)) ? text : null;
	}

	private static Instant requiredInstant(Map<String, Object> claims, String name) {
		String value = requiredClaim(claims, name);
		try {
			return Instant.parse(value);
		}
		catch (RuntimeException ex) {
			throw new ApiException(ErrorCode.INVALID_ADMISSION, name + " is not a UTC ISO-8601 timestamp.");
		}
	}

	/**
	 * Refuses to start without the key material for the configured algorithm. A service that
	 * cannot verify admission tokens must not come up quietly accepting whatever it is sent.
	 */
	private static byte[] readSecret(ReservationProperties properties) {
		if (properties.admissionTokenAlgorithm() != TokenAlgorithm.HS256) {
			return null;
		}
		if (!StringUtils.hasText(properties.admissionTokenSecret())) {
			throw new IllegalStateException(
					"reservation.admission-token-algorithm is HS256, so ADMISSION_TOKEN_SECRET must be set. "
							+ "Without it admission tokens cannot be verified and no hold could be trusted.");
		}
		return properties.admissionTokenSecret().getBytes(StandardCharsets.UTF_8);
	}

	private static PublicKey readPublicKey(ReservationProperties properties) {
		if (properties.admissionTokenAlgorithm() != TokenAlgorithm.RS256) {
			return null;
		}
		if (!StringUtils.hasText(properties.admissionTokenPublicKey())) {
			throw new IllegalStateException(
					"reservation.admission-token-algorithm is RS256, so ADMISSION_TOKEN_PUBLIC_KEY must be set "
							+ "to Queue's public key.");
		}
		String pem = properties.admissionTokenPublicKey()
				.replace("-----BEGIN PUBLIC KEY-----", "")
				.replace("-----END PUBLIC KEY-----", "")
				.replaceAll("\\s", "");
		try {
			byte[] encoded = Base64.getDecoder().decode(pem);
			return KeyFactory.getInstance("RSA").generatePublic(new X509EncodedKeySpec(encoded));
		}
		catch (Exception ex) {
			throw new IllegalStateException("ADMISSION_TOKEN_PUBLIC_KEY is not a PEM-encoded RSA public key", ex);
		}
	}

	/** The identity and deadlines Queue put inside the token. userId is the buyer's Cognito sub. */
	public record AdmissionClaims(String admissionId, String userId, String eventId,
			Instant selectionExpiresAt, Instant admissionExpiresAt) {
	}
}
