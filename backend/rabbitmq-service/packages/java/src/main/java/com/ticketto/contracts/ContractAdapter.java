package com.ticketto.contracts;

import java.util.Map;
import java.util.Set;

/**
 * What a service implements so {@link ContractTests} can drive its real code. Implement the
 * methods for the roles the catalogue gives the service: a publisher implements
 * {@link #publish}, a consumer {@link #listenerQueues} and {@link #handle}. The others keep
 * their default, which fails the test that needs it, naming the method.
 */
public interface ContractAdapter {
    /**
     * Make the service's real outbox relay (or whatever publishes) publish one
     * {@code message} onto {@code recorder}.
     */
    default void publish(String message, PublishRecorder recorder) {
        throw new MissingAdapterMethod("publish(String, PublishRecorder)", "publishes messages");
    }

    /** The queue names the service listens on. */
    default Set<String> listenerQueues() {
        throw new MissingAdapterMethod("listenerQueues()", "consumes messages");
    }

    /**
     * Run the service's real handler for {@code queue} on this delivery and report what it did.
     *
     * @param properties the AMQP properties, keyed as in {@link PublishedMessage#properties()}
     */
    default Outcome handle(String queue, byte[] body, Map<String, Object> properties) {
        throw new MissingAdapterMethod("handle(String, byte[], Map)", "consumes messages");
    }

    /** A role the catalogue gives the service, without its adapter method. */
    final class MissingAdapterMethod extends UnsupportedOperationException {
        MissingAdapterMethod(String method, String role) {
            super("the service " + role + " in the catalogue: implement ContractAdapter." + method);
        }
    }
}
