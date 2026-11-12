package com.ticketing.reservation_service.config;

import org.springframework.boot.data.redis.autoconfigure.LettuceClientOptionsBuilderCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import io.lettuce.core.ClientOptions.DisconnectedBehavior;

/**
 * While Redis is unreachable, refuse commands at once instead of queueing them until it comes
 * back. Queued commands held every request open for the whole outage; refused ones become 503
 * DEPENDENCY_UNAVAILABLE straight away, and the caller retries. The client still reconnects on
 * its own, and a command already sent is never cut short by this.
 */
@Configuration
public class RedisClientConfig {

	@Bean
	public LettuceClientOptionsBuilderCustomizer rejectCommandsWhileDisconnected() {
		return options -> options.disconnectedBehavior(DisconnectedBehavior.REJECT_COMMANDS);
	}
}
