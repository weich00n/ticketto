package com.ticketto.contracts;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.Test;

class UnitsTest {
    // ---------- catalogue ----------

    @Test
    void theBundleIsTheContractsNextToThisPackage() throws Exception {
        String version = Files.readString(Path.of("..", "..", "contracts", "VERSION")).strip();
        // In a merge request pipeline the bundle is the pre-release X.Y.Z.devN.
        assertTrue(Catalogue.version().equals(version) || Catalogue.version().startsWith(version + ".dev"),
                Catalogue.version());
    }

    @Test
    void servicesAndRoles() {
        assertTrue(Catalogue.services().contains("reservation-service"));
        assertTrue(Catalogue.publishedBy("checkout-service").contains("OrderConfirmed"));
        assertTrue(Catalogue.consumedBy("user-service").contains("OrderConfirmed"));
        assertEquals(Set.of("user-service.EventCancelled", "user-service.OrderConfirmed"),
                new HashSet<>(Catalogue.queuesFor("user-service")));
        assertEquals("checkout.events", Catalogue.exchangeFor("checkout-service").orElseThrow());
        assertTrue(Catalogue.exchangeFor("user-service").isEmpty());
        assertEquals(List.of("user_id", "event_id"), Catalogue.requirements("user-service").get("OrderConfirmed"));
    }

    @Test
    void anUnknownServiceNamesTheKnownOnes() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> Catalogue.publishedBy("billing-service"));
        assertTrue(e.getMessage().contains("checkout-service"), e.getMessage());
    }

    @Test
    void messagesComeWithTheirRouting() {
        Message message = Catalogue.message("EventSoldOut").orElseThrow();
        assertEquals("reservation-service", message.publisher());
        assertEquals("reservation.events", message.exchange());
        assertEquals("event.sold_out", message.routingKey());
        assertTrue(message.hasSchema());
        assertTrue(Catalogue.message("PaymentCompleted").isEmpty());
    }

    @Test
    void examplesAreCopies() {
        Catalogue.example("OrderConfirmed").put("message_type", "changed");
        assertEquals("OrderConfirmed", Catalogue.example("OrderConfirmed").get("message_type").asText());
    }

    // ---------- bodies ----------

    @Test
    void everyExampleIsAValidPublish() {
        for (String name : Catalogue.messages()) {
            Message message = Catalogue.message(name).orElseThrow();
            Bodies.Delivery delivery = Bodies.delivery(name, "example");
            PublishedMessage published = new PublishedMessage(message.exchange(), message.routingKey(),
                    delivery.body(), delivery.properties(), true, true);
            assertEquals(List.of(), PublishAssertions.checkPublish(message.publisher(), published), name);
        }
    }

    @Test
    void minimalKeepsOnlyRequiredFieldsAndStaysValid() {
        for (String name : Catalogue.messages()) {
            ObjectNode body = Bodies.body(name, "minimal");
            JsonNode schema = Catalogue.payloadSchema(name).orElseThrow();
            Set<String> required = new HashSet<>();
            schema.get("required").forEach(r -> required.add(r.asText()));
            body.get("payload").fieldNames().forEachRemaining(f -> assertTrue(required.contains(f), name + "." + f));
            assertEquals("", PublishAssertions.schemaErrors(schema, body.get("payload")), name);
        }
    }

    @Test
    void extraFieldsAddsAnUnknownFieldToEveryObject() {
        JsonNode payload = Bodies.body("EventPublished", "extra-fields").get("payload");
        assertTrue(payload.has(Bodies.UNKNOWN_FIELD));
        assertTrue(payload.get("ticket_categories").get(0).has(Bodies.UNKNOWN_FIELD));
    }

    @Test
    void invalidDropsTheFirstRequiredField() {
        JsonNode schema = Catalogue.payloadSchema("OrderConfirmed").orElseThrow();
        String first = schema.get("required").get(0).asText();
        assertFalse(Bodies.body("OrderConfirmed", "invalid").get("payload").has(first));
        assertFalse(PublishAssertions.schemaErrors(schema, Bodies.body("OrderConfirmed", "invalid").get("payload")).isEmpty());
    }

    @Test
    void anUnknownBodyCaseIsRefused() {
        assertThrows(IllegalArgumentException.class, () -> Bodies.body("OrderConfirmed", "huge"));
    }

    @Test
    void deliveriesCarryAWellBehavedPublishersProperties() {
        Bodies.Delivery delivery = Bodies.delivery("EventCancelled", "minimal");
        assertEquals("application/json", delivery.properties().get("content_type"));
        assertEquals(2, delivery.properties().get("delivery_mode"));
        assertEquals("EventCancelled", delivery.properties().get("type"));
    }

    // ---------- recorder ----------

    @Test
    void theRecorderRecordsConfirmsAsTheyWereAtPublishTime() {
        PublishRecorder recorder = new PublishRecorder("event-service");
        recorder.publish("event.events", "event.published", new byte[0], Map.of(), true);
        recorder.confirmDelivery();
        recorder.publish("event.events", "event.published", new byte[0], Map.of(), true);
        assertFalse(recorder.published().get(0).confirmed());
        assertTrue(recorder.published().get(1).confirmed());
    }

    @Test
    void declaringAQueueOrAnotherExchangeIsAViolation() {
        PublishRecorder recorder = new PublishRecorder("event-service");
        recorder.declareExchange("event.events", "topic", false, true, false, false, Map.of());
        recorder.declareQueue("event-service.mine", true);
        assertEquals(List.of(), recorder.violations());
        recorder.declareExchange("checkout.events", "topic", false, true, false, false, Map.of());
        recorder.declareExchange("event.events", "direct", false, false, false, false, Map.of());
        recorder.declareQueue("event-service.mine", false);
        assertEquals(List.of("exchange_declare", "exchange_declare", "queue_declare"),
                recorder.violations().stream().map(Problem::code).toList());
    }

    @Test
    void aContractErrorListsEveryProblem() {
        ContractError error = new ContractError("x", List.of(new Problem("a", "one"), new Problem("b", "two")));
        assertTrue(error.getMessage().contains("  - a: one\n  - b: two"), error.getMessage());
        assertEquals(2, error.problems().size());
    }
}
