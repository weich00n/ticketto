package com.ticketto.contracts;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;

/**
 * The message catalogue and contracts bundled with this package, in
 * {@code ticketto-contracts/} on the classpath: a copy of the bundle built by
 * rabbitmq-service's tools/build_contracts_bundle.py, so this class knows no naming rules of
 * its own. Mirrors the Python package's {@code ticketto_contracts.catalogue}.
 */
public final class Catalogue {
    static final String ROOT = "ticketto-contracts/";
    private static final Map<String, JsonNode> FILES = new ConcurrentHashMap<>();

    private Catalogue() {}

    private static JsonNode file(String relative) {
        return FILES.computeIfAbsent(relative, Catalogue::load);
    }

    private static JsonNode load(String relative) {
        InputStream in = Catalogue.class.getClassLoader().getResourceAsStream(ROOT + relative);
        if (in == null) {
            throw new IllegalStateException(
                    ROOT + relative + " is not on the classpath: build the bundle into "
                            + "src/main/resources/ticketto-contracts first (python -m "
                            + "tools.build_contracts_bundle --out "
                            + "packages/java/src/main/resources/ticketto-contracts)");
        }
        return Json.read(in);
    }

    private static JsonNode catalogue() {
        return file("catalogue.json");
    }

    public static String version() {
        return catalogue().get("version").asText();
    }

    public static List<String> services() {
        List<String> names = new ArrayList<>();
        catalogue().get("services").fieldNames().forEachRemaining(names::add);
        names.sort(null);
        return names;
    }

    public static List<String> messages() {
        List<String> names = new ArrayList<>();
        catalogue().get("messages").fieldNames().forEachRemaining(names::add);
        return names;
    }

    /** The catalogued message called {@code name}, if there is one. */
    public static Optional<Message> message(String name) {
        JsonNode entry = name == null ? null : catalogue().get("messages").get(name);
        if (entry == null) {
            return Optional.empty();
        }
        return Optional.of(new Message(
                name,
                entry.get("publisher").asText(),
                entry.get("exchange").asText(),
                entry.get("routing_key").asText(),
                entry.get("status").asText(),
                strings(entry.get("consumers")),
                strings(entry.get("queues")),
                !entry.get("schema").isNull(),
                !entry.get("example").isNull()));
    }

    private static JsonNode service(String service) {
        JsonNode entry = catalogue().get("services").get(service);
        if (entry == null) {
            throw new IllegalArgumentException(
                    "'" + service + "' is not in the catalogue; known: " + String.join(", ", services()));
        }
        return entry;
    }

    public static List<String> publishedBy(String service) {
        return strings(service(service).get("publishes"));
    }

    public static List<String> consumedBy(String service) {
        return strings(service(service).get("consumes"));
    }

    /** The queues {@code service} consumes from: {@code <service>.<Message>} for each. */
    public static List<String> queuesFor(String service) {
        return strings(service(service).get("queues"));
    }

    /** The exchange {@code service} publishes to, if it publishes anything. */
    public static Optional<String> exchangeFor(String service) {
        JsonNode exchange = service(service).get("exchange");
        return exchange == null || exchange.isNull() ? Optional.empty() : Optional.of(exchange.asText());
    }

    /** The payload fields {@code service} reads, per message it consumes. */
    public static Map<String, List<String>> requirements(String service) {
        Map<String, List<String>> result = new LinkedHashMap<>();
        JsonNode entry = catalogue().get("requirements").get(service);
        if (entry != null) {
            entry.fields().forEachRemaining(e -> result.put(e.getKey(), strings(e.getValue())));
        }
        return result;
    }

    public static JsonNode envelopeSchema() {
        return file(catalogue().get("envelope_schema").asText()).deepCopy();
    }

    public static Optional<JsonNode> payloadSchema(String name) {
        JsonNode entry = catalogue().get("messages").get(name);
        if (entry == null || entry.get("schema").isNull()) {
            return Optional.empty();
        }
        return Optional.of(file(entry.get("schema").asText()).deepCopy());
    }

    /** A copy of the message's example body. */
    public static ObjectNode example(String name) {
        JsonNode entry = catalogue().get("messages").get(name);
        if (entry == null || entry.get("example").isNull()) {
            throw new IllegalArgumentException(
                    "'" + name + "' has no example in contracts " + version());
        }
        return (ObjectNode) file(entry.get("example").asText()).deepCopy();
    }

    private static List<String> strings(JsonNode array) {
        List<String> values = new ArrayList<>();
        if (array != null) {
            array.forEach(v -> values.add(v.asText()));
        }
        return List.copyOf(values);
    }
}
