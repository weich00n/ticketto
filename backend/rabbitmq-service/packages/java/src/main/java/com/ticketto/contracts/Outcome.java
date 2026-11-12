package com.ticketto.contracts;

/** What a consumer did with one delivery. */
public enum Outcome {
    /** basic.ack: handled. */
    ACK,
    /** basic.reject (or nack) without requeue: dead-lettered at once. */
    REJECT,
    /** basic.reject (or nack) with requeue: to be retried. */
    REQUEUE
}
