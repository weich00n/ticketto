package com.ticketing.reservation_service.config;

import java.time.Clock;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * The service reads the time from this bean rather than Instant.now(), so tests can hold the
 * clock still. Always UTC, because every timestamp we publish is UTC.
 */
@Configuration
public class ClockConfig {

	@Bean
	public Clock clock() {
		return Clock.systemUTC();
	}
}
