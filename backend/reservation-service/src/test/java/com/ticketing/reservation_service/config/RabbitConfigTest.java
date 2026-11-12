package com.ticketing.reservation_service.config;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import org.junit.jupiter.api.Test;
import org.springframework.amqp.AmqpRejectAndDontRequeueException;
import org.springframework.amqp.rabbit.support.ListenerExecutionFailedException;
import org.springframework.boot.amqp.autoconfigure.RabbitConnectionDetails;

/** RABBITMQ_URL is read the way every other service reads it, escapes and all. */
class RabbitConfigTest {

	@Test
	void theBrokersOwnUrlFormIsDecoded() {
		RabbitConnectionDetails details = RabbitConfig.parse("amqp://ticketto:p%40ss+word@broker:5673/%2F", "x", "y");

		assertThat(details.getUsername()).isEqualTo("ticketto");
		assertThat(details.getPassword()).isEqualTo("p@ss+word");
		assertThat(details.getVirtualHost()).isEqualTo("/");
		assertThat(details.getFirstAddress().host()).isEqualTo("broker");
		assertThat(details.getFirstAddress().port()).isEqualTo(5673);
	}

	@Test
	void aUrlWithoutCredentialsUsesTheConfiguredOnes() {
		RabbitConnectionDetails details = RabbitConfig.parse("amqp://localhost", "ticketto", "local");

		assertThat(details.getUsername()).isEqualTo("ticketto");
		assertThat(details.getPassword()).isEqualTo("local");
		assertThat(details.getVirtualHost()).isEqualTo("/");
		assertThat(details.getFirstAddress().port()).isEqualTo(5672);
	}

	@Test
	void aNamedVirtualHostIsKept() {
		assertThat(RabbitConfig.parse("amqp://h:5672/ticketing", null, null).getVirtualHost()).isEqualTo("ticketing");
	}

	@Test
	void onlyPlainAmqpIsAccepted() {
		assertThatThrownBy(() -> RabbitConfig.parse("amqps://h:5671/%2F", null, null))
				.isInstanceOf(IllegalStateException.class);
		assertThatThrownBy(() -> RabbitConfig.parse("localhost:5672", null, null))
				.isInstanceOf(IllegalStateException.class);
	}

	@Test
	void aRejectedMessageIsNotRetriedButAnOutageIs() {
		Exception rejected = new ListenerExecutionFailedException("listener failed",
				new AmqpRejectAndDontRequeueException("bad payload"));
		Exception outage = new ListenerExecutionFailedException("listener failed",
				new IllegalStateException("MySQL is down"));

		assertThat(RabbitConfig.isRetryable(rejected)).isFalse();
		assertThat(RabbitConfig.isRetryable(outage)).isTrue();
	}
}
