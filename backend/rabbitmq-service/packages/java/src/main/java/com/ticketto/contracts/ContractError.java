package com.ticketto.contracts;

import java.util.List;
import java.util.stream.Collectors;

/** A publish or a consumer breaks the contracts. Lists every problem, not just the first. */
public class ContractError extends AssertionError {
    private final transient List<Problem> problems;

    public ContractError(String subject, List<Problem> problems) {
        super("contracts " + Catalogue.version() + " broken by " + subject + ":\n"
                + problems.stream().map(p -> "  - " + p).collect(Collectors.joining("\n")));
        this.problems = List.copyOf(problems);
    }

    public List<Problem> problems() {
        return problems;
    }
}
