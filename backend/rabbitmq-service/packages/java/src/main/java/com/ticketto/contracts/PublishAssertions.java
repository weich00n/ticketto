package com.ticketto.contracts;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SpecVersion;
import com.networknt.schema.ValidationMessage;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Collectors;

/**
 * Check one publish against the contracts. The problem codes and rules are shared with the
 * Python package ({@code ticketto_contracts.validate}) and pinned by
 * packages/conformance/cases.json in rabbitmq-service: change both together.
 */
public final class PublishAssertions {
    private static final JsonSchemaFactory SCHEMAS =
            JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012);
    // The schemas' $id is relative (e.g. "envelope.schema.json"), which networknt only accepts
    // against an absolute base. Nothing is fetched from it: every $ref is local (#/$defs/...).
    private static final SchemaLocation BASE = SchemaLocation.of("https://contracts.ticketto.invalid/schemas/");
    private static final Map<String, JsonSchema> COMPILED = new ConcurrentHashMap<>();

    private PublishAssertions() {}

    /** Every way {@code published} breaks the contracts when sent by {@code service}. */
    public static List<Problem> checkPublish(String service, PublishedMessage published) {
        List<Problem> problems = new ArrayList<>(propertyProblems(published));
        ObjectNode body = Json.objectOrNull(published.body());
        if (body == null) {
            problems.add(new Problem("body_json", "the body is not a JSON object"));
        }
        Object name = published.properties().get("type");
        if (isBlank(name) && body != null && body.has("message_type")) {
            JsonNode type = body.get("message_type");
            name = type.isTextual() ? type.asText() : type;
        }
        Optional<Message> entry =
                name instanceof String text ? Catalogue.message(text) : Optional.empty();
        if (entry.isEmpty()) {
            problems.add(new Problem("unknown_message", repr(name) + " is not a message in the catalogue"));
        } else {
            problems.addAll(routingProblems(service, published, entry.get()));
        }
        if (body != null) {
            problems.addAll(bodyProblems(service, published, body, entry.map(Message::name).orElse("")));
        }
        return problems;
    }

    /** Throw a {@link ContractError} listing every problem with {@code published}. */
    public static void assertValidPublish(String service, PublishedMessage published) {
        List<Problem> problems = checkPublish(service, published);
        if (!problems.isEmpty()) {
            Object type = published.properties().get("type");
            String name = isBlank(type) ? "a message" : type.toString();
            throw new ContractError(service + " publishing " + name, problems);
        }
    }

    static String schemaErrors(JsonNode schema, JsonNode value) {
        JsonSchema compiled = COMPILED.computeIfAbsent(schema.toString(), key -> SCHEMAS.getSchema(BASE, schema));
        return compiled.validate(value).stream()
                .sorted(Comparator.comparing((ValidationMessage m) -> m.getInstanceLocation().toString()))
                .map(m -> location(m) + ": " + m.getMessage())
                .collect(Collectors.joining("; "));
    }

    private static String location(ValidationMessage message) {
        String path = message.getInstanceLocation().toString();
        return path.equals("$") ? "(root)" : path;
    }

    private static List<Problem> propertyProblems(PublishedMessage published) {
        Map<String, Object> props = published.properties();
        List<Problem> problems = new ArrayList<>();
        if (!"application/json".equals(props.get("content_type"))) {
            problems.add(new Problem("content_type",
                    "content_type is " + repr(props.get("content_type")) + ", not 'application/json'"));
        }
        if (!isTwo(props.get("delivery_mode"))) {
            problems.add(new Problem("delivery_mode",
                    "delivery_mode is " + repr(props.get("delivery_mode")) + ", not 2 (persistent)"));
        }
        for (String name : List.of("message_id", "type", "correlation_id")) {
            if (isBlank(props.get(name))) {
                problems.add(new Problem(name, "the " + name + " property is missing"));
            }
        }
        if (!published.mandatory()) {
            problems.add(new Problem("mandatory", "published without mandatory=true"));
        }
        if (!published.confirmed()) {
            problems.add(new Problem("confirms", "publisher confirms were not on"));
        }
        return problems;
    }

    private static List<Problem> routingProblems(String service, PublishedMessage published, Message entry) {
        List<Problem> problems = new ArrayList<>();
        if (!service.equals(entry.publisher())) {
            problems.add(new Problem("not_publisher",
                    entry.name() + " is published by " + entry.publisher() + ", not " + service));
        }
        if (!entry.exchange().equals(published.exchange())) {
            problems.add(new Problem("exchange",
                    "published to " + repr(published.exchange()) + ", not " + repr(entry.exchange())));
        }
        if (!entry.routingKey().equals(published.routingKey())) {
            problems.add(new Problem("routing_key",
                    "routing key " + repr(published.routingKey()) + ", not " + repr(entry.routingKey())));
        }
        return problems;
    }

    private static List<Problem> bodyProblems(
            String service, PublishedMessage published, ObjectNode body, String name) {
        Map<String, Object> props = published.properties();
        List<Problem> problems = new ArrayList<>();
        String envelope = schemaErrors(Catalogue.envelopeSchema(), body);
        if (!envelope.isEmpty()) {
            problems.add(new Problem("envelope", envelope));
        }
        if (body.has("producer") && !textEquals(body.get("producer"), service)) {
            problems.add(new Problem("producer",
                    "envelope producer is " + body.get("producer") + ", not '" + service + "'"));
        }
        if (!isBlank(props.get("type")) && body.has("message_type")
                && !textEquals(body.get("message_type"), props.get("type"))) {
            problems.add(new Problem("message_type_mismatch",
                    "envelope message_type " + body.get("message_type")
                            + " differs from the type property " + repr(props.get("type"))));
        }
        if (!isBlank(props.get("message_id")) && body.has("message_id")
                && !textEquals(body.get("message_id"), props.get("message_id"))) {
            problems.add(new Problem("message_id_mismatch",
                    "envelope message_id " + body.get("message_id")
                            + " differs from the message_id property " + repr(props.get("message_id"))));
        }
        Optional<JsonNode> schema = Catalogue.payloadSchema(name);
        JsonNode payload = body.get("payload");
        if (schema.isPresent() && payload instanceof ObjectNode) {
            String errors = schemaErrors(schema.get(), payload);
            if (!errors.isEmpty()) {
                problems.add(new Problem("payload", errors));
            }
        }
        return problems;
    }

    private static boolean textEquals(JsonNode node, Object value) {
        return node.isTextual() && value instanceof String text && node.asText().equals(text);
    }

    private static boolean isTwo(Object value) {
        return (value instanceof Integer || value instanceof Long || value instanceof Short)
                && ((Number) value).longValue() == 2L;
    }

    /** Like Python's falsiness for a property: missing, null, empty or false. */
    static boolean isBlank(Object value) {
        return value == null
                || (value instanceof String s && s.isEmpty())
                || Objects.equals(value, Boolean.FALSE);
    }

    private static String repr(Object value) {
        return value instanceof String s ? "'" + s + "'" : String.valueOf(value);
    }
}
