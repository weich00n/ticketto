package com.ticketto.contracts;

import static org.junit.jupiter.api.Assertions.fail;
import static org.junit.jupiter.api.DynamicTest.dynamicTest;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.function.Supplier;
import java.util.stream.Stream;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;

/**
 * Contract tests for one service, generated from the bundled catalogue: the JUnit 5
 * counterpart of the Python package's {@code ticketto_contracts.suite}.
 *
 * <pre>{@code
 * class MessageContractTest extends ContractTests {
 *     protected String service() { return "reservation-service"; }
 *     protected ContractAdapter adapter() { return new ReservationContractAdapter(...); }
 *     protected Set<String> bodies() { return Set.of("extra-fields", "invalid"); }  // optional
 * }
 * }</pre>
 *
 * <p>Generated tests:
 *
 * <ul>
 *   <li>{@code publishes}: one per message the service publishes. Everything the adapter's
 *       publish recorded is checked: exchange, routing key, mandatory, publisher confirms, the
 *       AMQP properties, the envelope and the payload against its schema, and declares.
 *   <li>{@code listensOnCataloguedQueues}: its listeners are exactly its catalogued queues.
 *   <li>{@code accepts}: one per message it consumes and body case. The handler acks the
 *       {@code minimal} body (only the publisher's required fields) and the {@code extra-fields}
 *       one, and rejects the {@code invalid} one without requeueing.
 * </ul>
 *
 * <p>A role the catalogue doesn't give the service is skipped. {@link #adapter()} is called
 * once per test. There is no way to mark a test as an expected failure: a service that doesn't
 * match the contracts fails until it, or the contract, is fixed.
 */
public abstract class ContractTests {
    /** The service's name in the catalogue, e.g. {@code "reservation-service"}. */
    protected abstract String service();

    /** A fresh adapter to the service's real code. */
    protected abstract ContractAdapter adapter();

    /** Body cases to run besides {@code minimal}: any of "extra-fields", "example", "invalid". */
    protected Set<String> bodies() {
        return Set.of();
    }

    private String checkedService() {
        String service = service();
        if (!Catalogue.services().contains(service)) {
            throw new IllegalArgumentException("service() must be one of "
                    + String.join(", ", Catalogue.services()) + " (got '" + service + "')");
        }
        return service;
    }

    private List<String> bodyCases() {
        List<String> cases = new ArrayList<>(List.of("minimal"));
        for (String bodyCase : Bodies.CASES) {
            if (!"minimal".equals(bodyCase) && bodies().contains(bodyCase)) {
                cases.add(bodyCase);
            }
        }
        Set<String> unknown = new java.util.TreeSet<>(bodies());
        unknown.removeAll(Bodies.CASES);
        if (!unknown.isEmpty()) {
            throw new IllegalArgumentException("bodies(): unknown cases " + unknown + "; use " + Bodies.CASES);
        }
        return cases;
    }

    private static <T> T call(Supplier<T> adapterCall) {
        try {
            return adapterCall.get();
        } catch (ContractAdapter.MissingAdapterMethod missing) {
            return fail(missing.getMessage());
        }
    }

    @TestFactory
    Stream<DynamicTest> publishes() {
        String service = checkedService();
        List<String> published = Catalogue.publishedBy(service);
        Assumptions.assumeFalse(published.isEmpty(), service + " publishes nothing");
        return published.stream().map(message -> dynamicTest(message, () -> checkPublish(service, message)));
    }

    private void checkPublish(String service, String message) {
        PublishRecorder recorder = new PublishRecorder(service);
        ContractAdapter adapter = adapter();
        call(() -> {
            adapter.publish(message, recorder);
            return null;
        });
        List<Problem> problems = new ArrayList<>(recorder.violations());
        List<PublishedMessage> all = recorder.published();
        boolean sent = all.stream().anyMatch(p -> message.equals(p.properties().get("type")));
        if (all.isEmpty()) {
            problems.add(new Problem("nothing_published", "publish(\"" + message + "\") published nothing"));
        } else if (!sent) {
            Set<String> types = new java.util.TreeSet<>();
            all.forEach(p -> types.add(String.valueOf(p.properties().get("type"))));
            problems.add(new Problem("wrong_message",
                    "asked for " + message + ", published type " + String.join(", ", types)));
        }
        for (PublishedMessage publishedMessage : all) {
            problems.addAll(PublishAssertions.checkPublish(service, publishedMessage));
        }
        if (!problems.isEmpty()) {
            throw new ContractError(service + " publishing " + message, problems);
        }
    }

    @Test
    void listensOnCataloguedQueues() {
        String service = checkedService();
        Assumptions.assumeFalse(Catalogue.consumedBy(service).isEmpty(), service + " consumes nothing");
        ContractAdapter adapter = adapter();
        Listeners.assertListenerQueues(service, call(adapter::listenerQueues));
    }

    @TestFactory
    Stream<DynamicTest> accepts() {
        String service = checkedService();
        List<String> consumed = Catalogue.consumedBy(service);
        Assumptions.assumeFalse(consumed.isEmpty(), service + " consumes nothing");
        List<String> cases = bodyCases();
        return consumed.stream().flatMap(message -> cases.stream().map(bodyCase -> dynamicTest(
                message + " (" + bodyCase + ")", () -> checkAccepts(service, message, bodyCase))));
    }

    private void checkAccepts(String service, String message, String bodyCase) {
        String queue = service + "." + message;
        Outcome expected = "invalid".equals(bodyCase) ? Outcome.REJECT : Outcome.ACK;
        Bodies.Delivery delivery = Bodies.delivery(message, bodyCase);
        ContractAdapter adapter = adapter();
        Outcome outcome = call(() -> adapter.handle(queue, delivery.body(), delivery.properties()));
        if (outcome == null) {
            throw new ContractError(service + " handling " + message,
                    List.of(new Problem("outcome", "handle() returned null, not ACK, REJECT or REQUEUE")));
        }
        if (outcome != expected) {
            throw new ContractError(service + " handling " + message + " (" + bodyCase + " body)",
                    List.of(new Problem("outcome", "the handler returned " + outcome + "; expected " + expected)));
        }
    }
}
