package com.ticketing.reservation_service.service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.HexFormat;
import java.util.Optional;

import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

import com.ticketing.reservation_service.dto.CreateHoldRequest;
import com.ticketing.reservation_service.exception.ApiException;
import com.ticketing.reservation_service.exception.ErrorCode;
import com.ticketing.reservation_service.model.IdempotencyRecord;
import com.ticketing.reservation_service.repository.IdempotencyRepository;

/**
 * Makes a retried create return the original hold rather than a second one.
 *
 * <p>Claiming a key is an insert, so two retries arriving together cannot both win it, and only
 * the winner creates a hold. The loser reads the row: if the hold is there it is returned, and
 * if it is not there yet then a create is genuinely in flight - unless the claim is so old that
 * the request that made it must have died, in which case the retry takes it over.
 *
 * <p>The fingerprint deliberately covers the buyer and what they asked for, but not the
 * admission token itself. A token can be reissued between a request and its retry, and the
 * retry is still the same request; including the token would turn that into a false conflict.
 */
@Service
public class IdempotencyService {

	/** Long enough for a UUID or a Stripe-style key, short enough to be a sane primary key. */
	private static final int MAX_KEY_LENGTH = 128;

	/**
	 * A claim still without a hold after this long belongs to a request that died - its task
	 * stopped or was replaced mid-create - and would otherwise answer 503 to every retry for good.
	 * Well past the slowest real create, which every Redis and MySQL timeout bounds. Taking over a
	 * claim whose request is somehow still running cannot make a second hold: the admission's
	 * unique constraint refuses it.
	 */
	static final Duration ABANDONED_AFTER = Duration.ofSeconds(60);

	private final IdempotencyRepository repository;

	private final Clock clock;

	public IdempotencyService(IdempotencyRepository repository, Clock clock) {
		this.repository = repository;
		this.clock = clock;
	}

	/**
	 * Tries to claim a key for this request.
	 *
	 * @return empty when the key is ours and the caller should create the hold, or the id of the
	 * hold an earlier identical request already created
	 * @throws ApiException 409 if the key was used for a different request, or 503 if an
	 * identical request is still in flight
	 */
	public Optional<String> claim(String idempotencyKey, String userSub, CreateHoldRequest request) {
		requireUsableKey(idempotencyKey);
		String fingerprint = fingerprint(userSub, request);

		try {
			this.repository.insertClaim(idempotencyKey, fingerprint, Instant.now(this.clock));
			return Optional.empty();
		}
		catch (DataIntegrityViolationException ex) {
			// Someone already holds this key: the row says whether that was this same request.
			return resolveExistingClaim(idempotencyKey, fingerprint);
		}
	}

	/** Records which hold the key produced, so later retries can be answered from it. */
	public void recordHold(String idempotencyKey, String holdId) {
		this.repository.attachHold(idempotencyKey, holdId);
	}

	/** Releases a key whose hold was never created, so the buyer can genuinely try again. */
	public void releaseClaim(String idempotencyKey) {
		this.repository.deleteById(idempotencyKey);
	}

	/** The existing hold's id, or empty if the claim was abandoned and is now ours. */
	private Optional<String> resolveExistingClaim(String idempotencyKey, String fingerprint) {
		IdempotencyRecord existing = this.repository.findById(idempotencyKey)
				// Claimed and then released between our insert failing and this read.
				.orElseThrow(() -> new ApiException(ErrorCode.DEPENDENCY_UNAVAILABLE,
						"That request is still being processed. Please retry shortly."));
		if (!fingerprint.equals(existing.getRequestFingerprint())) {
			throw new ApiException(ErrorCode.IDEMPOTENCY_KEY_CONFLICT);
		}
		if (existing.getHoldId() == null) {
			Instant now = Instant.now(this.clock);
			if (existing.getCreatedAt().plus(ABANDONED_AFTER).isBefore(now)
					&& this.repository.takeOverClaim(idempotencyKey, existing.getCreatedAt(), now) == 1) {
				return Optional.empty();
			}
			// An identical request is mid-flight. Asking for a retry is honest; creating a second
			// hold would hand the same buyer two sets of tickets.
			throw new ApiException(ErrorCode.DEPENDENCY_UNAVAILABLE,
					"That request is still being processed. Please retry shortly.");
		}
		return Optional.of(existing.getHoldId());
	}

	private static void requireUsableKey(String idempotencyKey) {
		if (idempotencyKey.length() > MAX_KEY_LENGTH) {
			throw new ApiException(ErrorCode.INVALID_REQUEST,
					"Idempotency-Key must be at most " + MAX_KEY_LENGTH + " characters.");
		}
	}

	/** Who asked, and what for. Not the token, and not the correlation id. */
	private static String fingerprint(String userSub, CreateHoldRequest request) {
		String canonical = String.join("\n", userSub, request.eventId(), request.categoryId(),
				String.valueOf(request.quantity()));
		try {
			byte[] digest = MessageDigest.getInstance("SHA-256").digest(canonical.getBytes(StandardCharsets.UTF_8));
			return HexFormat.of().formatHex(digest);
		}
		catch (Exception ex) {
			throw new IllegalStateException("SHA-256 is unavailable", ex);
		}
	}

	/** True when the caller sent a key at all; without one, nothing here applies. */
	public static boolean isPresent(String idempotencyKey) {
		return StringUtils.hasText(idempotencyKey);
	}
}
