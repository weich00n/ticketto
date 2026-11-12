package com.ticketing.reservation_service.repository;

import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Repository;

import com.ticketing.reservation_service.model.SaleStatus;

/**
 * Reads the live availability counters and writes them only when seeding.
 *
 * <p>Availability is never computed and never read-then-written from Java. Reading it is a
 * plain GET; changing it happens inside a Lua script. The one exception is
 * seeding a counter that does not exist yet, which is why
 * {@link #seedAvailableIfAbsent(String, String, int)} uses SET NX: a replayed seed can then
 * never reset a counter that live holds have already moved.
 */
@Repository
public class AvailabilityRedisRepository {

	private final StringRedisTemplate redis;

	public AvailabilityRedisRepository(StringRedisTemplate redis) {
		this.redis = redis;
	}

	/** Tickets free right now, or null when the counter is missing. */
	public Integer readAvailable(String eventId, String categoryId) {
		String value = redis.opsForValue().get(RedisKeys.available(eventId, categoryId));
		return value == null ? null : Integer.valueOf(value);
	}

	/** Sets the counter only if it is absent. Returns true when this call created it. */
	public boolean seedAvailableIfAbsent(String eventId, String categoryId, int available) {
		Boolean created = redis.opsForValue()
				.setIfAbsent(RedisKeys.available(eventId, categoryId), String.valueOf(available));
		return Boolean.TRUE.equals(created);
	}

	public void writeSaleStatus(String eventId, SaleStatus status) {
		redis.opsForValue().set(RedisKeys.saleStatus(eventId), status.name());
	}

	/** The sale status the Lua scripts see, or null when it has never been written. */
	public String readSaleStatus(String eventId) {
		return redis.opsForValue().get(RedisKeys.saleStatus(eventId));
	}
}
