package com.ticketto.contracts;

import java.util.Map;

/**
 * What a publisher handed to its AMQP client for one message.
 *
 * @param properties the AMQP properties, keyed as in the contracts: {@code content_type},
 *     {@code delivery_mode} (an integer), {@code message_id}, {@code type} and
 *     {@code correlation_id}
 * @param confirmed publisher confirms were on for the channel
 */
public record PublishedMessage(
        String exchange,
        String routingKey,
        byte[] body,
        Map<String, Object> properties,
        boolean mandatory,
        boolean confirmed) {
    public PublishedMessage {
        properties = properties == null ? Map.of() : Map.copyOf(properties);
        body = body == null ? new byte[0] : body.clone();
    }

    @Override
    public byte[] body() {
        return body.clone();
    }
}
