package com.ticketto.contracts;

import java.util.List;

/** A catalogued message, as catalogue.json in the bundle describes it. */
public record Message(
        String name,
        String publisher,
        String exchange,
        String routingKey,
        String status,
        List<String> consumers,
        List<String> queues,
        boolean hasSchema,
        boolean hasExample) {}
