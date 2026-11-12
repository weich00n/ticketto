package com.ticketto.contracts;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;

/** The package's one ObjectMapper, strict about trailing content like Python's json.loads. */
final class Json {
    static final ObjectMapper MAPPER =
            new ObjectMapper().enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);

    private Json() {}

    static JsonNode read(InputStream in) {
        try (in) {
            return MAPPER.readTree(in);
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    /** The body as a JSON object, or null if it is not one. */
    static ObjectNode objectOrNull(byte[] body) {
        try {
            JsonNode node = MAPPER.readTree(body);
            return node instanceof ObjectNode object ? object : null;
        } catch (IOException e) {
            return null;
        }
    }

    static byte[] bytes(JsonNode node) {
        try {
            return MAPPER.writeValueAsBytes(node);
        } catch (JsonProcessingException e) {
            throw new IllegalStateException(e);
        }
    }
}
