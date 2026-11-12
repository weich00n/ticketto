package com.ticketto.contracts;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/** Check the queues a consumer listens on against the catalogue. */
public final class Listeners {
    private Listeners() {}

    /**
     * {@code missing_queue:<q>} for each catalogued queue {@code service} doesn't listen on, and
     * {@code extra_queue:<q>} for each queue it listens on that the catalogue doesn't give it.
     * Sorted; empty if they match.
     */
    public static List<String> checkListenerQueues(String service, Collection<String> queues) {
        Set<String> expected = new HashSet<>(Catalogue.queuesFor(service));
        Set<String> listened = new HashSet<>(queues);
        List<String> problems = new ArrayList<>();
        expected.stream().filter(q -> !listened.contains(q)).forEach(q -> problems.add("missing_queue:" + q));
        listened.stream().filter(q -> !expected.contains(q)).forEach(q -> problems.add("extra_queue:" + q));
        problems.sort(null);
        return problems;
    }

    public static void assertListenerQueues(String service, Collection<String> queues) {
        List<Problem> problems = new ArrayList<>();
        for (String item : checkListenerQueues(service, queues)) {
            String[] parts = item.split(":", 2);
            problems.add(new Problem(parts[0], parts[1] + ("missing_queue".equals(parts[0])
                    ? " is not listened on" : " is not in the catalogue")));
        }
        if (!problems.isEmpty()) {
            throw new ContractError(service + "'s listeners", problems);
        }
    }
}
