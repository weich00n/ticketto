package com.ticketing.reservation_service.repository;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

import org.springframework.data.redis.core.Cursor;
import org.springframework.data.redis.core.ScanOptions;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.RedisScript;
import org.springframework.stereotype.Repository;

import com.ticketing.reservation_service.model.Hold;

/**
 * Runs the Lua scripts that take and return holds. All this class does is marshal arguments and
 * read the script's answer back; every decision lives in the script, because only there is the
 * check-and-change a single atomic step.
 */
@Repository
public class HoldRedisRepository {

	private final StringRedisTemplate redis;

	@SuppressWarnings("rawtypes")
	private final RedisScript<List> holdScript;

	@SuppressWarnings("rawtypes")
	private final RedisScript<List> reclaimScript;

	@SuppressWarnings("rawtypes")
	private final RedisScript<List> confirmScript;

	@SuppressWarnings("rawtypes")
	private final RedisScript<List> returnPendingScript;

	/**
	 * How long a hold's info outlives the hold itself: long enough for reconciliation, which runs
	 * every minute, to find any hold whose MySQL row was left unfinished.
	 */
	static final Duration HOLD_INFO_RETENTION = Duration.ofDays(1);

	@SuppressWarnings("rawtypes")
	public HoldRedisRepository(StringRedisTemplate redis, RedisScript<List> holdScript,
			RedisScript<List> reclaimScript, RedisScript<List> confirmScript,
			RedisScript<List> returnPendingScript) {
		this.redis = redis;
		this.holdScript = holdScript;
		this.reclaimScript = reclaimScript;
		this.confirmScript = confirmScript;
		this.returnPendingScript = returnPendingScript;
	}

	/**
	 * Checks the sale status, the five-ticket limit and availability, then takes the tickets and
	 * registers the hold, all in one atomic step.
	 */
	public HoldOutcome takeHold(Hold hold, int maxTicketsPerUser, Duration holdTtl) {
		List<String> keys = List.of(
				RedisKeys.saleStatus(hold.getEventId()),
				RedisKeys.available(hold.getEventId(), hold.getCategoryId()),
				RedisKeys.userQuantity(hold.getEventId(), hold.getUserId()),
				RedisKeys.hold(hold.getEventId(), hold.getHoldId()),
				RedisKeys.expiryIndex(hold.getEventId(), hold.getCategoryId()),
				RedisKeys.holdInfo(hold.getEventId(), hold.getHoldId()));

		List<String> args = new ArrayList<>(List.of(
				String.valueOf(hold.getQuantity()),
				String.valueOf(maxTicketsPerUser),
				String.valueOf(holdTtl.toSeconds()),
				String.valueOf(hold.getHoldExpiresAt().getEpochSecond()),
				hold.getHoldId(),
				String.valueOf(holdTtl.plus(HOLD_INFO_RETENTION).toSeconds())));
		// the hold hash: what release, expiry and confirmation need to act on a hold they only know by id
		holdFields(hold).forEach((field, value) -> {
			args.add(field);
			args.add(value);
		});

		return read(redis.execute(holdScript, keys, args.toArray()));
	}

	/**
	 * Claims a hold's ending and returns its tickets.
	 *
	 * <p>Answers false when someone else claimed it first, which is the caller's signal to do
	 * nothing at all: not return inventory, and not write a final status. See reclaim.lua.
	 */
	public boolean reclaim(Hold hold) {
		return reclaim(hold.getEventId(), hold.getCategoryId(), hold.getUserId(), hold.getHoldId(),
				hold.getQuantity());
	}

	/**
	 * The same claim for a hold with no MySQL row, which the expiry worker meets when Redis took
	 * a hold and the insert then failed.
	 */
	public boolean reclaim(String eventId, String categoryId, String userId, String holdId, int quantity) {
		List<String> keys = List.of(
				RedisKeys.expiryIndex(eventId, categoryId),
				RedisKeys.available(eventId, categoryId),
				RedisKeys.userQuantity(eventId, userId),
				RedisKeys.hold(eventId, holdId),
				RedisKeys.holdInfo(eventId, holdId));

		HoldOutcome outcome = read(redis.execute(reclaimScript, keys, holdId, String.valueOf(quantity)));
		return outcome.status() == Status.OK;
	}

	/**
	 * Claims a hold for a completed payment, leaving availability and the buyer's allowance
	 * alone because held tickets have become sold ones. A claim this order already won on an
	 * earlier attempt counts as won again, so a retry can finish what that attempt started.
	 *
	 * @return OK, NOT_ALIVE if the hold's key has already died, or ALREADY_RECLAIMED if another
	 * caller owns this hold's ending
	 */
	public Status confirm(Hold hold, String orderId) {
		List<String> keys = List.of(
				RedisKeys.expiryIndex(hold.getEventId(), hold.getCategoryId()),
				RedisKeys.hold(hold.getEventId(), hold.getHoldId()),
				RedisKeys.holdInfo(hold.getEventId(), hold.getHoldId()));

		return read(redis.execute(confirmScript, keys, hold.getHoldId(),
				String.valueOf(HOLD_INFO_RETENTION.toSeconds()), orderId)).status();
	}

	/**
	 * Writes a hold's info as still held. Used when the rebuild puts Redis state back for a hold
	 * MySQL says is ACTIVE.
	 */
	public void recordHeld(Hold hold, Duration remaining) {
		String key = RedisKeys.holdInfo(hold.getEventId(), hold.getHoldId());
		Map<String, String> info = holdFields(hold);
		info.put("state", HoldEnding.HELD.name());
		redis.opsForHash().putAll(key, info);
		Duration lifetime = remaining.isNegative() ? HOLD_INFO_RETENTION : remaining.plus(HOLD_INFO_RETENTION);
		redis.expire(key, lifetime);
	}

	/** How the hold ended as far as Redis knows, or null if its info is gone (Redis was lost). */
	public HoldEnding endingOf(String eventId, String holdId) {
		Object state = redis.opsForHash().get(RedisKeys.holdInfo(eventId, holdId), "state");
		return (state == null) ? null : HoldEnding.valueOf(String.valueOf(state));
	}

	/** The hold's info: quantity, user_id, category_id and state. Empty once it has expired. */
	public Map<Object, Object> readHoldInfo(String eventId, String holdId) {
		return redis.opsForHash().entries(RedisKeys.holdInfo(eventId, holdId));
	}

	/**
	 * Gives back the tickets of a hold a confirmation claimed but never recorded in MySQL, at
	 * most once. NOT_INITIALISED means the counter is missing and the rebuild must recount it.
	 */
	public Status returnSoldPending(Hold hold) {
		List<String> keys = List.of(
				RedisKeys.holdInfo(hold.getEventId(), hold.getHoldId()),
				RedisKeys.available(hold.getEventId(), hold.getCategoryId()),
				RedisKeys.userQuantity(hold.getEventId(), hold.getUserId()));

		return read(redis.execute(returnPendingScript, keys, String.valueOf(hold.getQuantity()))).status();
	}

	/** Whether the sweep will still find this hold, i.e. no one has claimed its ending yet. */
	public boolean isIndexedForExpiry(Hold hold) {
		return redis.opsForZSet().score(RedisKeys.expiryIndex(hold.getEventId(), hold.getCategoryId()),
				hold.getHoldId()) != null;
	}

	/** Drops a member from the expiry index without touching inventory. */
	public void forget(String eventId, String categoryId, String holdId) {
		redis.opsForZSet().remove(RedisKeys.expiryIndex(eventId, categoryId), holdId);
	}

	/** Hold ids whose expiry has come and gone, oldest first. */
	public Set<String> dueHoldIds(String expiryIndexKey, Instant now, int limit) {
		Set<String> due = redis.opsForZSet().rangeByScore(expiryIndexKey, Double.NEGATIVE_INFINITY,
				now.getEpochSecond(), 0, limit);
		return (due == null) ? Set.of() : due;
	}

	/** Every expiry index currently in Redis, found with SCAN so Redis is never blocked. */
	public Set<String> expiryIndexKeys() {
		try (Cursor<String> cursor = redis
				.scan(ScanOptions.scanOptions().match(RedisKeys.EXPIRY_INDEX_PATTERN).count(256).build())) {
			return cursor.stream().collect(Collectors.toCollection(LinkedHashSet::new));
		}
	}

	/** Sets a buyer's running total only if it is absent, used by the rebuild. */
	public boolean seedUserQuantityIfAbsent(String eventId, String userId, long quantity) {
		Boolean created = redis.opsForValue()
				.setIfAbsent(RedisKeys.userQuantity(eventId, userId), String.valueOf(quantity));
		return Boolean.TRUE.equals(created);
	}

	/**
	 * Puts a hold's key back with only the time it has left, so a rebuilt hold stays confirmable
	 * for exactly as long as it should have been and no longer.
	 */
	public void restoreHoldKey(Hold hold, Duration remaining) {
		String key = RedisKeys.hold(hold.getEventId(), hold.getHoldId());
		redis.opsForHash().putAll(key, holdFields(hold));
		redis.expire(key, remaining);
	}

	/** Puts a hold back in the expiry index, which is what lets the sweep find it. */
	public void indexForExpiry(Hold hold) {
		redis.opsForZSet().add(RedisKeys.expiryIndex(hold.getEventId(), hold.getCategoryId()), hold.getHoldId(),
				hold.getHoldExpiresAt().getEpochSecond());
	}

	/** One rebuild per event at a time, so a Redis loss does not stampede MySQL. */
	public boolean acquireRebuildLock(String eventId, Duration ttl) {
		return Boolean.TRUE.equals(redis.opsForValue().setIfAbsent(RedisKeys.rebuildLock(eventId), "1", ttl));
	}

	public void releaseRebuildLock(String eventId) {
		redis.delete(RedisKeys.rebuildLock(eventId));
	}

	/** True while the hold's key is alive, which is what makes a hold confirmable. */
	public boolean isAlive(String eventId, String holdId) {
		return Boolean.TRUE.equals(redis.hasKey(RedisKeys.hold(eventId, holdId)));
	}

	/** What the hold's own key and its info both carry, in the order the scripts receive it. */
	private static Map<String, String> holdFields(Hold hold) {
		Map<String, String> fields = new LinkedHashMap<>();
		fields.put("user_id", hold.getUserId());
		fields.put("event_id", hold.getEventId());
		fields.put("category_id", hold.getCategoryId());
		fields.put("admission_id", hold.getAdmissionId());
		fields.put("quantity", String.valueOf(hold.getQuantity()));
		return fields;
	}

	@SuppressWarnings("rawtypes")
	private static HoldOutcome read(List result) {
		if (result == null || result.isEmpty()) {
			throw new IllegalStateException("A Lua script returned nothing");
		}
		Status status = Status.valueOf(String.valueOf(result.get(0)));
		Integer detail = result.size() > 1 ? Integer.valueOf(String.valueOf(result.get(1))) : null;
		return new HoldOutcome(status, detail);
	}

	/**
	 * What the script answered. detail carries the number needed to explain a refusal: tickets
	 * still available, or how many more this buyer may take. On a successful hold it is the
	 * category's availability after it, so 0 means this hold took the last ticket.
	 */
	public record HoldOutcome(Status status, Integer detail) {
	}

	/** The state field of a hold's info, as the Lua scripts write it. */
	public enum HoldEnding {
		/** Still allocated to the hold. */
		HELD,
		/** Given back to availability by a release, an expiry or a cancellation. */
		RETURNED,
		/** Claimed by a confirmation: sold once MySQL records it, still subtracted until then. */
		SOLD_PENDING
	}

	public enum Status {
		OK,
		EVENT_CANCELLED,
		/** Counters are missing: a Redis problem, not a sell-out. */
		NOT_INITIALISED,
		INSUFFICIENT_INVENTORY,
		PURCHASE_LIMIT_EXCEEDED,
		ALREADY_RECLAIMED,
		/** The hold's own key has died, so the hold can no longer be confirmed. */
		NOT_ALIVE
	}
}
