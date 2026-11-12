package com.ticketto.contracts;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * Message bodies for consumer tests, generated from each message's example. The same cases as
 * the Python package's {@code ticketto_contracts.bodies}:
 *
 * <ul>
 *   <li>{@code minimal}: only the fields the publisher's schema requires, which is what a
 *       consumer can count on
 *   <li>{@code example}: the committed example
 *   <li>{@code extra-fields}: the example plus an unknown field in every payload object;
 *       consumers must ignore fields they don't know
 *   <li>{@code invalid}: the example without one required payload field; consumers must reject
 *       it without requeueing
 * </ul>
 */
public final class Bodies {
    public static final List<String> CASES = List.of("minimal", "example", "extra-fields", "invalid");
    public static final String UNKNOWN_FIELD = "x_contract_test_unknown_field";

    /** The body bytes and AMQP properties a well-behaved publisher would deliver. */
    public record Delivery(byte[] body, Map<String, Object> properties) {
        public Delivery {
            body = body.clone();
            properties = Map.copyOf(properties);
        }

        @Override
        public byte[] body() {
            return body.clone();
        }
    }

    private Bodies() {}

    public static ObjectNode body(String name, String bodyCase) {
        return switch (bodyCase) {
            case "minimal" -> minimal(name);
            case "example" -> Catalogue.example(name);
            case "extra-fields" -> extraFields(name);
            case "invalid" -> invalid(name);
            default -> throw new IllegalArgumentException(
                    "unknown body case '" + bodyCase + "'; one of " + String.join(", ", CASES));
        };
    }

    public static Delivery delivery(String name, String bodyCase) {
        ObjectNode built = body(name, bodyCase);
        Map<String, Object> properties = new LinkedHashMap<>();
        properties.put("content_type", "application/json");
        properties.put("delivery_mode", 2);
        properties.put("message_id", built.get("message_id").asText());
        properties.put("type", name);
        properties.put("correlation_id", "corr-contract-test");
        return new Delivery(Json.bytes(built), properties);
    }

    static ObjectNode minimal(String name) {
        ObjectNode body = Catalogue.example(name);
        Optional<JsonNode> schema = Catalogue.payloadSchema(name);
        schema.ifPresent(s -> body.set("payload", stripOptional(body.get("payload"), s, s)));
        return body;
    }

    static ObjectNode extraFields(String name) {
        ObjectNode body = Catalogue.example(name);
        addUnknownFields(body.get("payload"));
        return body;
    }

    static ObjectNode invalid(String name) {
        ObjectNode body = Catalogue.example(name);
        JsonNode schema = Catalogue.payloadSchema(name).orElse(null);
        if (schema == null || !schema.has("required") || schema.get("required").isEmpty()) {
            throw new IllegalArgumentException(name + " has no required payload field to remove");
        }
        ((ObjectNode) body.get("payload")).remove(schema.get("required").get(0).asText());
        return body;
    }

    private static JsonNode resolve(JsonNode node, JsonNode root) {
        while (node.has("$ref")) {
            String ref = node.get("$ref").asText().replaceFirst("^#/\\$defs/", "");
            node = root.get("$defs").get(ref);
        }
        return node;
    }

    /** {@code value} with every property its schema doesn't require removed, recursively. */
    static JsonNode stripOptional(JsonNode value, JsonNode schemaNode, JsonNode root) {
        JsonNode node = resolve(schemaNode, root);
        if (value instanceof ObjectNode object && node.has("properties")) {
            Set<String> required = new HashSet<>();
            node.path("required").forEach(r -> required.add(r.asText()));
            ObjectNode result = Json.MAPPER.createObjectNode();
            List<String> names = new ArrayList<>();
            object.fieldNames().forEachRemaining(names::add);
            for (String key : names) {
                if (required.contains(key)) {
                    JsonNode child = node.get("properties").path(key);
                    result.set(key, stripOptional(object.get(key), child.isMissingNode()
                            ? Json.MAPPER.createObjectNode() : child, root));
                }
            }
            return result;
        }
        if (value instanceof ArrayNode array && node.has("items")) {
            ArrayNode result = Json.MAPPER.createArrayNode();
            array.forEach(item -> result.add(stripOptional(item, node.get("items"), root)));
            return result;
        }
        return value;
    }

    private static void addUnknownFields(JsonNode value) {
        if (value instanceof ObjectNode object) {
            object.elements().forEachRemaining(Bodies::addUnknownFields);
            object.put(UNKNOWN_FIELD, "ignore me");
        } else if (value instanceof ArrayNode array) {
            array.forEach(Bodies::addUnknownFields);
        }
    }
}
