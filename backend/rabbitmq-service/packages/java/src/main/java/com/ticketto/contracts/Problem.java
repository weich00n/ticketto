package com.ticketto.contracts;

/**
 * One way a publish or a consumer breaks the contracts. The codes are shared with the Python
 * package and pinned by packages/conformance/cases.json in rabbitmq-service.
 */
public record Problem(String code, String detail) {
    @Override
    public String toString() {
        return code + ": " + detail;
    }
}
