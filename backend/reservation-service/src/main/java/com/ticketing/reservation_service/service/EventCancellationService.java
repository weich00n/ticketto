package com.ticketing.reservation_service.service;

import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import com.ticketing.reservation_service.dto.CancellationView;
import com.ticketing.reservation_service.exception.ApiException;
import com.ticketing.reservation_service.exception.ErrorCode;
import com.ticketing.reservation_service.model.EventSaleStatus;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.HoldStatus;
import com.ticketing.reservation_service.model.SaleStatus;
import com.ticketing.reservation_service.repository.AvailabilityRedisRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRedisRepository;
import com.ticketing.reservation_service.repository.HoldRepository;

/**
 * Cancelling an event mid-sale. Every hold must end either released or sold with a refund
 * owed, and never both.
 *
 * <p>The event is closed first, in MySQL and then in Redis, so no new hold or confirmation can
 * start. Each hold is then resolved with the same claim the sweep, a release and a confirmation
 * use, so whichever caller wins a hold decides its ending:
 * <ul>
 * <li>cancellation wins an ACTIVE hold: its tickets go back and it becomes CANCELLED</li>
 * <li>a confirmation won it first: it stays sold and a refund is owed</li>
 * </ul>
 *
 * <p>One race needs both sides to cooperate. A confirmation can win the claim just before this
 * reaches the hold yet not have committed CONFIRMED when the row is read, so the row still says
 * ACTIVE here. That confirmation re-checks the sale status after it commits and records the
 * refund itself. Because each side writes before it reads the other's write, at least one of
 * them sees it; the refund is recorded idempotently, so both seeing it still leaves one.
 *
 * <p>A hold being created at the instant of cancellation may land after the holds were read.
 * It can never be sold - confirmation refuses a cancelled event - and the sweep returns it.
 */
@Service
public class EventCancellationService {

	private static final Logger log = LoggerFactory.getLogger(EventCancellationService.class);

	private final EventSaleStatusRepository saleStatusRepository;

	private final HoldRepository holdRepository;

	private final HoldRedisRepository holdRedis;

	private final AvailabilityRedisRepository availabilityRedis;

	private final OutboxWriter outbox;

	private final TransactionTemplate transactions;

	private final Clock clock;

	public EventCancellationService(EventSaleStatusRepository saleStatusRepository, HoldRepository holdRepository,
			HoldRedisRepository holdRedis, AvailabilityRedisRepository availabilityRedis, OutboxWriter outbox,
			TransactionTemplate transactions, Clock clock) {
		this.saleStatusRepository = saleStatusRepository;
		this.holdRepository = holdRepository;
		this.holdRedis = holdRedis;
		this.availabilityRedis = availabilityRedis;
		this.outbox = outbox;
		this.transactions = transactions;
		this.clock = clock;
	}

	/**
	 * Cancels the event and resolves every hold on it. Safe to run again: a re-run finds nothing
	 * left to change, which is also how a cancellation interrupted halfway is finished.
	 */
	public CancellationView cancel(String eventId) {
		return cancel(eventId, false);
	}

	/**
	 * What an EventCancelled message does. Unlike the internal route it accepts an event this
	 * service has never heard of: EventCancelled can overtake EventPublished, and the
	 * cancellation is recorded so the EventPublished that follows cannot put it on sale.
	 */
	public CancellationView cancelFromEventCancelled(String eventId) {
		return cancel(eventId, true);
	}

	private CancellationView cancel(String eventId, boolean recordUnknownEvent) {
		closeSale(eventId, recordUnknownEvent);

		int cancelled = 0;
		int refunds = 0;
		for (Hold hold : holdRepository.findByEventId(eventId)) {
			switch (resolve(hold)) {
				case CANCELLED -> cancelled++;
				case REFUND_RECORDED -> refunds++;
				case UNCHANGED -> {
				}
			}
		}
		log.info("Cancelled event {}: {} hold(s) cancelled, {} refund(s) owed", eventId, cancelled, refunds);
		return new CancellationView(eventId, SaleStatus.CANCELLED, cancelled, refunds, now());
	}

	/**
	 * MySQL first, because hold creation and confirmation both check it and it survives a Redis
	 * loss; then Redis, so hold.lua refuses at once rather than when the counters are next rebuilt.
	 */
	private void closeSale(String eventId, boolean recordUnknownEvent) {
		transactions.executeWithoutResult(status -> {
			EventSaleStatus sale = saleStatusRepository.findById(eventId).orElse(null);
			if (sale == null) {
				if (!recordUnknownEvent) {
					throw new ApiException(ErrorCode.EVENT_NOT_FOUND);
				}
				// A concurrent EventPublished inserting the same row makes this insert fail; the
				// message is retried and then finds the row there.
				sale = new EventSaleStatus();
				sale.setEventId(eventId);
				sale.setSaleStatus(SaleStatus.CANCELLED);
				sale.setCreatedAt(now());
				sale.setUpdatedAt(now());
				saleStatusRepository.save(sale);
			}
			else if (sale.getSaleStatus() != SaleStatus.CANCELLED) {
				sale.setSaleStatus(SaleStatus.CANCELLED);
				sale.setUpdatedAt(now());
				saleStatusRepository.save(sale);
			}
		});
		availabilityRedis.writeSaleStatus(eventId, SaleStatus.CANCELLED);
	}

	private Outcome resolve(Hold hold) {
		if (hold.getStatus() == HoldStatus.ACTIVE) {
			if (holdRedis.reclaim(hold)) {
				return releaseClaimed(hold);
			}
			// Lost the claim: a confirmation, a release or the sweep owns this hold's ending. Only
			// a confirmation leaves anything for us to do, so read what it became.
			hold = holdRepository.findById(hold.getHoldId()).orElse(hold);
		}
		if (hold.getStatus() == HoldStatus.CONFIRMED) {
			return outbox.refundRequired(hold) ? Outcome.REFUND_RECORDED : Outcome.UNCHANGED;
		}
		// EXPIRED or CANCELLED already, or a confirmation still committing, which records its own
		// refund once it sees the cancellation.
		return Outcome.UNCHANGED;
	}

	/**
	 * No message: everyone who cares about an unpaid hold on a cancelled event already hears of
	 * it from Event Service's EventCancelled.
	 */
	private Outcome releaseClaimed(Hold hold) {
		if (holdRepository.transitionStatus(hold.getHoldId(), HoldStatus.ACTIVE, HoldStatus.CANCELLED) == 0) {
			// Impossible unless something ended this hold without winning the claim.
			log.error("Hold {} was claimed for cancellation but its row was no longer ACTIVE", hold.getHoldId());
		}
		// The tickets went back with the claim either way.
		return Outcome.CANCELLED;
	}

	private Instant now() {
		return clock.instant().truncatedTo(ChronoUnit.SECONDS);
	}

	private enum Outcome {
		CANCELLED, REFUND_RECORDED, UNCHANGED
	}
}
