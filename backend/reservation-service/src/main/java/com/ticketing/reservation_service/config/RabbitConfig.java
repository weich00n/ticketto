package com.ticketing.reservation_service.config;

import java.net.URI;
import java.net.URISyntaxException;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import org.springframework.amqp.AmqpRejectAndDontRequeueException;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.amqp.autoconfigure.RabbitConnectionDetails;
import org.springframework.boot.amqp.autoconfigure.RabbitListenerRetrySettingsCustomizer;
import org.springframework.boot.amqp.autoconfigure.RabbitProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.util.StringUtils;

import com.ticketing.reservation_service.service.OutboxWriter;

/**
 * Connects to the shared Ticketto broker from the one RABBITMQ_URL every service is given, for
 * example amqp://ticketto:secret@broker:5672/%2F.
 *
 * <p>Spring Boot can read a URL like this itself, but it does not decode it: the vhost %2F
 * would be taken literally instead of as "/", and a password with an escaped character would be
 * sent still escaped. So the URL is parsed here. A URL without credentials falls back to
 * spring.rabbitmq.username and password, which default to the local broker's login.
 *
 * <p>No queue, exchange or binding is declared anywhere in this service. rabbitmq-service owns
 * the topology, and a declaration that differs from it closes the channel.
 */
@Configuration
public class RabbitConfig {

	/**
	 * This service's own topic exchange. Each publisher has one, named after the service without
	 * -service, and rabbitmq-service creates it.
	 */
	public static final String EXCHANGE = "reservation.events";

	/** The envelope's producer: this service, as rabbitmq-service's messages.yaml names it. */
	public static final String PRODUCER = "reservation-service";

	/**
	 * Each message's routing key, from rabbitmq-service's messages.yaml. The PascalCase type goes
	 * in the envelope and the AMQP type property, never in the routing key.
	 */
	private static final Map<String, String> ROUTING_KEYS = Map.of(
			OutboxWriter.RESERVATION_EXPIRED, "reservation.expired",
			OutboxWriter.REFUND_REQUIRED, "refund.required",
			OutboxWriter.EVENT_SOLD_OUT, "event.sold_out");

	public static final String EVENT_PUBLISHED_QUEUE = "reservation-service.EventPublished";

	public static final String EVENT_CANCELLED_QUEUE = "reservation-service.EventCancelled";

	/** The routing key a message of this type is published with. */
	public static String routingKey(String eventType) {
		String key = ROUTING_KEYS.get(eventType);
		if (key == null) {
			throw new IllegalStateException("No routing key for message type " + eventType);
		}
		return key;
	}

	@Bean
	RabbitConnectionDetails rabbitConnectionDetails(@Value("${reservation.rabbitmq-url}") String url,
			RabbitProperties properties) {
		return parse(url, properties.getUsername(), properties.getPassword());
	}

	/**
	 * Retrying helps only a failure that might pass, such as MySQL being briefly down. A message
	 * the listener has already rejected as wrong goes straight to the dead-letter queue.
	 */
	@Bean
	RabbitListenerRetrySettingsCustomizer skipRetryForRejectedMessages() {
		return settings -> settings.setExceptionPredicate(RabbitConfig::isRetryable);
	}

	static boolean isRetryable(Throwable failure) {
		// The listener's exception arrives wrapped in the container's own.
		for (Throwable cause = failure; cause != null; cause = cause.getCause()) {
			if (cause instanceof AmqpRejectAndDontRequeueException) {
				return false;
			}
		}
		return true;
	}

	static RabbitConnectionDetails parse(String url, String defaultUsername, String defaultPassword) {
		URI uri;
		try {
			uri = new URI(url);
		}
		catch (URISyntaxException ex) {
			throw new IllegalStateException("RABBITMQ_URL is not a valid URL", ex);
		}
		if (!"amqp".equals(uri.getScheme()) || uri.getHost() == null) {
			// amqps would need TLS set up as well; connecting in plain text to a TLS port is worse
			// than refusing to start.
			throw new IllegalStateException("RABBITMQ_URL must look like amqp://user:password@host:5672/vhost");
		}

		String username = defaultUsername;
		String password = defaultPassword;
		String userInfo = uri.getRawUserInfo();
		if (StringUtils.hasText(userInfo)) {
			int colon = userInfo.indexOf(':');
			username = decode(colon < 0 ? userInfo : userInfo.substring(0, colon));
			password = colon < 0 ? null : decode(userInfo.substring(colon + 1));
		}

		// "/%2F" is the vhost "/"; no path at all also means the default vhost.
		String path = uri.getRawPath();
		String virtualHost = (path == null || path.length() <= 1) ? "/" : decode(path.substring(1));

		String host = uri.getHost();
		int port = (uri.getPort() == -1) ? 5672 : uri.getPort();
		String finalUsername = username;
		String finalPassword = password;
		return new RabbitConnectionDetails() {

			@Override
			public String getUsername() {
				return finalUsername;
			}

			@Override
			public String getPassword() {
				return finalPassword;
			}

			@Override
			public String getVirtualHost() {
				return virtualHost;
			}

			@Override
			public List<Address> getAddresses() {
				return List.of(new Address(host, port));
			}
		};
	}

	private static String decode(String value) {
		// URLDecoder would also turn '+' into a space, which is form encoding, not URL encoding.
		return URLDecoder.decode(value.replace("+", "%2B"), StandardCharsets.UTF_8);
	}
}
