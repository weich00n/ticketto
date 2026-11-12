package com.ticketing.reservation_service.repository;

/**
 * The one place Redis key names are built.
 *
 * <p>Every key carries the event id inside braces as a hash tag, so all of one event's keys
 * land in the same slot and a single Lua script may touch them together.
 */
public final class RedisKeys {

	private RedisKeys() {
	}

	/** avail:{eventId}:categoryId - integer, tickets free right now. */
	public static String available(String eventId, String categoryId) {
		return "avail:{" + eventId + "}:" + categoryId;
	}

	/** hold:{eventId}:holdId - hash with a TTL; its existence means the hold is alive. */
	public static String hold(String eventId, String holdId) {
		return "hold:{" + eventId + "}:" + holdId;
	}

	/**
	 * holdinfo:{eventId}:holdId - hash: the hold's quantity, buyer and category, and how it ended
	 * (HELD, RETURNED, SOLD_PENDING). Outlives the hold's own key by a day, so a hold whose MySQL
	 * row was never finished can still be resolved correctly after its key has died.
	 */
	public static String holdInfo(String eventId, String holdId) {
		return "holdinfo:{" + eventId + "}:" + holdId;
	}

	/** Matches every expiry index, for the sweep that looks for due holds. */
	public static final String EXPIRY_INDEX_PATTERN = "zexp:{*}:*";

	/** zexp:{eventId}:categoryId - sorted set, score = expiry epoch, member = holdId. */
	public static String expiryIndex(String eventId, String categoryId) {
		return "zexp:{" + eventId + "}:" + categoryId;
	}

	/** userqty:{eventId}:userId - held plus bought, for the five-ticket limit. */
	public static String userQuantity(String eventId, String userId) {
		return "userqty:{" + eventId + "}:" + userId;
	}

	/** sale:{eventId} - ON_SALE or CANCELLED. */
	public static String saleStatus(String eventId) {
		return "sale:{" + eventId + "}";
	}

	/** rebuild:{eventId} - held briefly so only one rebuild per event runs at a time. */
	public static String rebuildLock(String eventId) {
		return "rebuild:{" + eventId + "}";
	}

	/**
	 * Reads back the event and category out of an expiry index key, which is how the sweep knows
	 * which counters a due hold belongs to.
	 */
	public static ExpiryIndex parseExpiryIndex(String key) {
		int openBrace = key.indexOf('{');
		int closeBrace = key.indexOf('}', openBrace);
		if (!key.startsWith("zexp:") || openBrace < 0 || closeBrace < 0 || closeBrace + 2 > key.length()) {
			throw new IllegalArgumentException("Not an expiry index key: " + key);
		}
		return new ExpiryIndex(key.substring(openBrace + 1, closeBrace), key.substring(closeBrace + 2));
	}

	public record ExpiryIndex(String eventId, String categoryId) {
	}
}
