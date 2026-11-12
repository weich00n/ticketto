package com.ticketing.reservation_service.config;

import java.util.List;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.io.ClassPathResource;
import org.springframework.data.redis.core.script.RedisScript;

/**
 * Loads the Lua scripts once at startup. Redis caches them by SHA, so each call ships only the
 * digest rather than the script body.
 *
 * <p>Each script returns a Lua table of strings, which arrives in Java as a List.
 */
@Configuration
public class RedisScriptConfig {

	@SuppressWarnings("rawtypes")
	@Bean
	public RedisScript<List> holdScript() {
		return RedisScript.of(new ClassPathResource("scripts/hold.lua"), List.class);
	}

	@SuppressWarnings("rawtypes")
	@Bean
	public RedisScript<List> reclaimScript() {
		return RedisScript.of(new ClassPathResource("scripts/reclaim.lua"), List.class);
	}

	@SuppressWarnings("rawtypes")
	@Bean
	public RedisScript<List> confirmScript() {
		return RedisScript.of(new ClassPathResource("scripts/confirm.lua"), List.class);
	}

	@SuppressWarnings("rawtypes")
	@Bean
	public RedisScript<List> returnPendingScript() {
		return RedisScript.of(new ClassPathResource("scripts/return-pending.lua"), List.class);
	}
}
