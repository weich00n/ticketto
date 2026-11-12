package com.ticketto.contracts;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * Records what a publisher does with its AMQP channel, for the contract checks: every publish,
 * whether publisher confirms were on, and declare mistakes. It never talks to a broker. The
 * Java counterpart of the Python package's RecordingChannel.
 *
 * <p>Declares are checked as they happen: services must not create queues, and may declare only
 * their own exchange, exactly as rabbitmq-service's docs/MESSAGING.md shows. For Spring AMQP,
 * {@link com.ticketto.contracts.spring.SpringAmqpSupport#recordingConnectionFactory} feeds one
 * of these from a real RabbitTemplate.
 */
public final class PublishRecorder {
    private final String service;
    private final List<PublishedMessage> published = new ArrayList<>();
    private final List<Problem> violations = new ArrayList<>();
    private boolean confirming;

    /** @param service the publishing service; needed only to check exchange declares */
    public PublishRecorder(String service) {
        this.service = service;
    }

    public String service() {
        return service;
    }

    /** Publisher confirms are on from now on (pika's confirm_delivery, AMQP's confirm.select). */
    public void confirmDelivery() {
        confirming = true;
    }

    public boolean confirming() {
        return confirming;
    }

    /** Record one basic.publish, with confirms as currently set on this recorder. */
    public void publish(
            String exchange,
            String routingKey,
            byte[] body,
            Map<String, Object> properties,
            boolean mandatory) {
        published.add(new PublishedMessage(exchange, routingKey, body, properties, mandatory, confirming));
    }

    /** Check an exchange.declare. A passive declare is always fine. */
    public void declareExchange(
            String exchange,
            String type,
            boolean passive,
            boolean durable,
            boolean autoDelete,
            boolean internal,
            Map<String, Object> arguments) {
        if (passive) {
            return;
        }
        List<String> wrong = new ArrayList<>();
        Optional<String> own = service == null ? Optional.empty() : Catalogue.exchangeFor(service);
        if (own.isPresent() && !own.get().equals(exchange)) {
            wrong.add("not " + service + "'s exchange '" + own.get() + "'");
        }
        if (!"topic".equals(type)) {
            wrong.add("type '" + type + "', not 'topic'");
        }
        if (!durable || autoDelete || internal) {
            wrong.add("must be durable=true, autoDelete=false, internal=false");
        }
        if (arguments != null && !arguments.isEmpty()) {
            wrong.add("must have no arguments, got " + arguments);
        }
        if (!wrong.isEmpty()) {
            violations.add(new Problem("exchange_declare",
                    "exchangeDeclare('" + exchange + "'): " + String.join("; ", wrong)));
        }
    }

    /** Check a queue.declare: only a passive one is allowed, since the broker owns its queues. */
    public void declareQueue(String queue, boolean passive) {
        if (!passive) {
            violations.add(new Problem("queue_declare",
                    "queueDeclare('" + queue + "') without passive=true: the broker owns its queues"));
        }
    }

    public List<PublishedMessage> published() {
        return Collections.unmodifiableList(published);
    }

    public List<Problem> violations() {
        return Collections.unmodifiableList(violations);
    }
}
