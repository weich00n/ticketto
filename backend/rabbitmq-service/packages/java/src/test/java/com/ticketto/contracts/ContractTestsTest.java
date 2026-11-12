package com.ticketto.contracts;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.platform.engine.discovery.DiscoverySelectors.selectClass;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.Test;
import org.junit.platform.testkit.engine.EngineTestKit;
import org.junit.platform.testkit.engine.Events;

/** The generated tests, run on sample services through the JUnit Platform test kit. */
class ContractTestsTest {
    private static Events run(Class<?> testClass) {
        return EngineTestKit.engine("junit-jupiter").selectors(selectClass(testClass)).execute().testEvents();
    }

    /** Publishes each message exactly as its example, through the recorder. */
    static ContractAdapter examplePublisher(String routingKeyOverride, boolean confirms) {
        return new ContractAdapter() {
            @Override
            public void publish(String name, PublishRecorder recorder) {
                Message message = Catalogue.message(name).orElseThrow();
                Bodies.Delivery delivery = Bodies.delivery(name, "example");
                if (confirms) {
                    recorder.confirmDelivery();
                }
                recorder.declareExchange(message.exchange(), "topic", false, true, false, false, Map.of());
                recorder.publish(message.exchange(), routingKeyOverride != null ? routingKeyOverride : message.routingKey(),
                        delivery.body(), delivery.properties(), true);
            }
        };
    }

    /** Acks a body that has every field the service reads, rejects one that doesn't. */
    static ContractAdapter consumer(String service, boolean requeueBadBodies) {
        return new ContractAdapter() {
            @Override
            public Set<String> listenerQueues() {
                return Set.copyOf(Catalogue.queuesFor(service));
            }

            @Override
            public Outcome handle(String queue, byte[] body, Map<String, Object> properties) {
                String message = queue.substring(service.length() + 1);
                JsonNode payload = Json.objectOrNull(body).get("payload");
                for (String field : Catalogue.requirements(service).getOrDefault(message, List.of())) {
                    if (!field.contains("[]") && !payload.has(field.split("\\.")[0])) {
                        return requeueBadBodies ? Outcome.REQUEUE : Outcome.REJECT;
                    }
                }
                return Outcome.ACK;
            }
        };
    }

    static class GoodPublisher extends ContractTests {
        @Override protected String service() { return "event-service"; }
        @Override protected ContractAdapter adapter() { return examplePublisher(null, true); }
    }

    @Test
    void aMatchingPublisherPasses() {
        run(GoodPublisher.class).assertStatistics(s -> s.succeeded(2).failed(0));
        // listensOnCataloguedQueues (a test) and accepts (a test factory) are skipped: event-service
        // consumes nothing.
        Events all = EngineTestKit.engine("junit-jupiter").selectors(selectClass(GoodPublisher.class))
                .execute().allEvents();
        assertEquals(2, all.aborted().count());
    }

    static class WrongRoutingKey extends ContractTests {
        @Override protected String service() { return "reservation-service"; }
        @Override protected ContractAdapter adapter() {
            return new ContractAdapter() {
                final ContractAdapter publisher = examplePublisher("event.sold.out", true);
                final ContractAdapter consumer = consumer("reservation-service", false);
                @Override public void publish(String m, PublishRecorder r) { publisher.publish(m, r); }
                @Override public Set<String> listenerQueues() { return consumer.listenerQueues(); }
                @Override public Outcome handle(String q, byte[] b, Map<String, Object> p) { return consumer.handle(q, b, p); }
            };
        }
    }

    @Test
    void aWrongRoutingKeyFailsEveryPublishNamingTheProblem() {
        Events events = run(WrongRoutingKey.class);
        events.assertStatistics(s -> s.failed(3));
        String message = events.failed().stream().findFirst().orElseThrow()
                .getPayload(org.junit.platform.engine.TestExecutionResult.class).orElseThrow()
                .getThrowable().orElseThrow().getMessage();
        assertTrue(message.contains("routing_key: routing key 'event.sold.out'"), message);
    }

    static class NoConfirms extends ContractTests {
        @Override protected String service() { return "event-service"; }
        @Override protected ContractAdapter adapter() { return examplePublisher(null, false); }
    }

    @Test
    void publishingWithoutConfirmsFails() {
        run(NoConfirms.class).assertStatistics(s -> s.succeeded(0).failed(2));
    }

    static class GoodConsumer extends ContractTests {
        @Override protected String service() { return "ticket-service"; }
        @Override protected ContractAdapter adapter() {
            return new ContractAdapter() {
                final ContractAdapter consumer = consumer("ticket-service", false);
                final ContractAdapter publisher = examplePublisher(null, true);
                @Override public void publish(String m, PublishRecorder r) { publisher.publish(m, r); }
                @Override public Set<String> listenerQueues() { return consumer.listenerQueues(); }
                @Override public Outcome handle(String q, byte[] b, Map<String, Object> p) { return consumer.handle(q, b, p); }
            };
        }
        @Override protected Set<String> bodies() { return Set.of("extra-fields", "invalid"); }
    }

    @Test
    void aMatchingConsumerPassesEveryBody() {
        // 1 publish (TicketIssued) + listensOn + 4 messages x 3 bodies
        run(GoodConsumer.class).assertStatistics(s -> s.succeeded(14).failed(0));
    }

    static class RequeuesInvalid extends ContractTests {
        @Override protected String service() { return "user-service"; }
        @Override protected ContractAdapter adapter() { return consumer("user-service", true); }
        @Override protected Set<String> bodies() { return Set.of("invalid"); }
    }

    @Test
    void requeueingAnInvalidBodyFails() {
        // EventCancelled's invalid body drops event_id, which user-service reads; OrderConfirmed's
        // drops order_id, which it doesn't, so that one is acked: a failure too.
        Events events = run(RequeuesInvalid.class);
        events.assertStatistics(s -> s.failed(2));
    }

    static class MissingListenerMethods extends ContractTests {
        @Override protected String service() { return "user-service"; }
        @Override protected ContractAdapter adapter() { return new ContractAdapter() {}; }
    }

    @Test
    void aMissingAdapterMethodFailsNamingIt() {
        Events events = run(MissingListenerMethods.class);
        events.assertStatistics(s -> s.failed(3));
        String message = events.failed().stream().findFirst().orElseThrow()
                .getPayload(org.junit.platform.engine.TestExecutionResult.class).orElseThrow()
                .getThrowable().orElseThrow().getMessage();
        assertTrue(message.contains("implement ContractAdapter."), message);
    }

    static class UnknownService extends ContractTests {
        @Override protected String service() { return "billing-service"; }
        @Override protected ContractAdapter adapter() { return new ContractAdapter() {}; }
    }

    @Test
    void anUnknownServiceFailsNamingTheKnownOnes() {
        Events events = EngineTestKit.engine("junit-jupiter").selectors(selectClass(UnknownService.class))
                .execute().allEvents();
        assertTrue(events.failed().count() >= 1);
    }

    static class UnknownBody extends ContractTests {
        @Override protected String service() { return "user-service"; }
        @Override protected ContractAdapter adapter() { return consumer("user-service", false); }
        @Override protected Set<String> bodies() { return Set.of("huge"); }
    }

    @Test
    void anUnknownBodyCaseFails() {
        Events events = EngineTestKit.engine("junit-jupiter").selectors(selectClass(UnknownBody.class))
                .execute().allEvents();
        assertTrue(events.failed().count() >= 1);
    }
}
