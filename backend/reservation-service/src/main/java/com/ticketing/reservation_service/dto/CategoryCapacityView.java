package com.ticketing.reservation_service.dto;

/**
 * One category as Queue sees it when sizing an admission batch.
 *
 * <p>total and sold are stored figures and available is read straight off the counter, but
 * <strong>held is derived and advisory</strong>: total − sold − available, clamped at zero. It
 * is not stored anywhere and may be momentarily approximate while holds are being taken, so it
 * must not be used for a correctness decision. This payload is the only place it is produced.
 */
public record CategoryCapacityView(String categoryId, int total, int available, int held, int sold) {
}
