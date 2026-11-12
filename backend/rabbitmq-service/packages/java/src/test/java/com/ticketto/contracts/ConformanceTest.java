package com.ticketto.contracts;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.DynamicTest.dynamicTest;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import java.util.stream.StreamSupport;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;

/** packages/conformance/cases.json: the verdicts this package must share with the Python one. */
class ConformanceTest {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final JsonNode SPEC = read(Path.of("..", "conformance", "cases.json"));

    private static JsonNode read(Path path) {
        try {
            return MAPPER.readTree(Files.readString(path));
        } catch (IOException e) {
            throw new IllegalStateException("can't read " + path.toAbsolutePath(), e);
        }
    }

    private record Draft(String service, PublishedMessage published) {}

    /** The baseline publish for the case's message, with the case's changes applied. */
    private static Draft published(JsonNode testCase) {
        Message message = Catalogue.message(testCase.get("message").asText()).orElseThrow();
        ObjectNode example = Catalogue.example(message.name());
        ObjectNode draft = MAPPER.createObjectNode();
        draft.put("service", message.publisher());
        draft.put("exchange", message.exchange());
        draft.put("routing_key", message.routingKey());
        draft.put("mandatory", true);
        draft.put("confirmed", true);
        ObjectNode properties = draft.putObject("properties");
        properties.put("content_type", "application/json");
        properties.put("delivery_mode", 2);
        properties.put("message_id", example.get("message_id").asText());
        properties.put("type", message.name());
        properties.put("correlation_id", "corr-conformance-1");
        draft.set("body", example);
        if (testCase.has("service")) {
            draft.put("service", testCase.get("service").asText());
        }
        testCase.path("set").fields().forEachRemaining(e -> set(draft, e.getKey(), e.getValue()));
        testCase.path("remove").forEach(path -> remove(draft, path.asText()));
        byte[] body = testCase.has("raw_body")
                ? testCase.get("raw_body").asText().getBytes(StandardCharsets.UTF_8)
                : Json.bytes(draft.get("body"));
        Map<String, Object> props = new LinkedHashMap<>();
        draft.get("properties").fields().forEachRemaining(e -> props.put(e.getKey(), value(e.getValue())));
        return new Draft(draft.get("service").asText(), new PublishedMessage(
                draft.get("exchange").asText(),
                draft.get("routing_key").asText(),
                body,
                props,
                draft.get("mandatory").asBoolean(),
                draft.get("confirmed").asBoolean()));
    }

    private static Object value(JsonNode node) {
        if (node.isInt() || node.isLong()) {
            return node.asLong() == (int) node.asLong() ? (Object) node.asInt() : (Object) node.asLong();
        }
        if (node.isBoolean()) {
            return node.asBoolean();
        }
        return node.isNull() ? null : node.asText();
    }

    private static JsonNode parent(ObjectNode root, String[] parts) {
        JsonNode node = root;
        for (int i = 0; i < parts.length - 1; i++) {
            node = node.isArray() ? node.get(Integer.parseInt(parts[i])) : node.get(parts[i]);
        }
        return node;
    }

    private static void set(ObjectNode root, String path, JsonNode value) {
        String[] parts = path.split("\\.");
        JsonNode parent = parent(root, parts);
        String last = parts[parts.length - 1];
        if (parent instanceof ArrayNode array) {
            array.set(Integer.parseInt(last), value);
        } else {
            ((ObjectNode) parent).set(last, value);
        }
    }

    private static void remove(ObjectNode root, String path) {
        String[] parts = path.split("\\.");
        JsonNode parent = parent(root, parts);
        String last = parts[parts.length - 1];
        if (parent instanceof ArrayNode array) {
            array.remove(Integer.parseInt(last));
        } else {
            ((ObjectNode) parent).remove(last);
        }
    }

    private static Stream<JsonNode> cases(String section) {
        return StreamSupport.stream(SPEC.get(section).get("cases").spliterator(), false);
    }

    private static Set<String> strings(JsonNode array) {
        Set<String> values = new HashSet<>();
        array.forEach(v -> values.add(v.asText()));
        return values;
    }

    @TestFactory
    Stream<DynamicTest> publishCases() {
        return cases("publish").map(c -> dynamicTest(c.get("id").asText(), () -> {
            Draft draft = published(c);
            List<Problem> problems = PublishAssertions.checkPublish(draft.service(), draft.published());
            Set<String> codes = problems.stream().map(Problem::code).collect(Collectors.toSet());
            assertEquals(strings(c.get("expect")), codes, problems.toString());
        }));
    }

    @TestFactory
    Stream<DynamicTest> listenerCases() {
        return cases("listeners").map(c -> dynamicTest(c.get("id").asText(), () -> {
            List<String> queues = new ArrayList<>(strings(c.get("queues")));
            assertEquals(strings(c.get("expect")),
                    new HashSet<>(Listeners.checkListenerQueues(c.get("service").asText(), queues)));
        }));
    }

    @Test
    void everyProblemCodeIsKnown() {
        Set<String> known = new HashSet<>();
        SPEC.get("publish").get("codes").fieldNames().forEachRemaining(known::add);
        cases("publish").forEach(c -> {
            Draft draft = published(c);
            for (Problem p : PublishAssertions.checkPublish("checkout-service", draft.published())) {
                assertTrue(known.contains(p.code()), p.toString());
            }
        });
    }
}
