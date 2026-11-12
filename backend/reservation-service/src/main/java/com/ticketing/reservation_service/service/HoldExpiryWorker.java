package com.ticketing.reservation_service.service;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;
import java.util.function.Function;
import java.util.stream.Collectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.HoldStatus;
import com.ticketing.reservation_service.repository.AvailabilityRedisRepository;
import com.ticketing.reservation_service.repository.HoldRedisRepository;
import com.ticketing.reservation_service.repository.HoldRedisRepository.HoldEnding;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.repository.RedisKeys;

/**
 * Returns the tickets of holds nobody paid for.
 *
 * <p>A hold's Redis key dies on its own when its TTL runs out, which is what stops it being
 * confirmed. But a dead key returns no tickets: the expiry index is what remembers that someone
 * still owes this category an unpaid hold, and this sweep is what acts on it.
 *
 * <p>Every release goes through reclaim.lua's claim, so the sweep, a buyer's DELETE and a
 * confirmation can all arrive at the same hold in the same instant and only one of them will
 * change anything.
 */
@Component
public class HoldExpiryWorker {

	private static final Logger log = LoggerFactory.getLogger(HoldExpiryWorker.class);

	/** Enough to drain a busy event quickly, small enough never to stall Redis on one call. */
	private static final int BATCH_SIZE = 500;

	/**
	 * How far past its deadline a hold must be before reconciliation looks at it: comfortably more
	 * than one sweep, so it only ever sees holds the sweep can no longer reach.
	 */
	static final Duration RECONCILE_AFTER = Duration.ofMinutes(2);

	private final HoldRepository holdRepository;

	private final HoldRedisRepository holdRedis;

	private final Clock clock;

	private final TransactionTemplate transactions;

	private final OutboxWriter outbox;

	private final InventoryRebuildService rebuildService;

	private final AvailabilityRedisRepository availabilityRedis;

	public HoldExpiryWorker(HoldRepository holdRepository, HoldRedisRepository holdRedis, Clock clock,
			TransactionTemplate transactions, OutboxWriter outbox, InventoryRebuildService rebuildService,
			AvailabilityRedisRepository availabilityRedis) {
		this.holdRepository = holdRepository;
		this.holdRedis = holdRedis;
		this.clock = clock;
		this.transactions = transactions;
		this.outbox = outbox;
		this.rebuildService = rebuildService;
		this.availabilityRedis = availabilityRedis;
	}

	/**
	 * Runs on a timer in the deployed service; tests call it directly instead, which is why the
	 * sweep interval is set absurdly high under the test profile.
	 */
	@Scheduled(fixedDelayString = "${reservation.expiry-sweep-seconds}s",
			initialDelayString = "${reservation.expiry-sweep-seconds}s")
	public void sweep() {
		try {
			int reclaimed = sweepOnce();
			if (reclaimed > 0) {
				log.info("Expiry sweep returned {} hold(s) worth of tickets", reclaimed);
			}
		}
		catch (RuntimeException ex) {
			// Never let a bad sweep kill the schedule; the next one will try again.
			log.error("Expiry sweep failed", ex);
		}
	}

	/**
	 * Returns how many holds this sweep resolved.
	 *
	 * <p>A failure is contained to its own hold or index: one that fails every time would otherwise
	 * stop every sweep at the same spot, and nothing after it would ever expire.
	 */
	public int sweepOnce() {
		Instant now = this.clock.instant();
		int resolved = 0;
		for (String indexKey : this.holdRedis.expiryIndexKeys()) {
			try {
				resolved += sweepIndex(indexKey, now);
			}
			catch (RuntimeException ex) {
				log.error("Expiry sweep skipped index {}", indexKey, ex);
			}
		}
		return resolved;
	}

	private int sweepIndex(String indexKey, Instant now) {
		RedisKeys.ExpiryIndex index = RedisKeys.parseExpiryIndex(indexKey);
		Set<String> due = this.holdRedis.dueHoldIds(indexKey, now, BATCH_SIZE);
		// One query for the whole batch, since an expiry spike is exactly when sweeps must be quick.
		Map<String, Hold> stored = this.holdRepository.findAllById(due).stream()
				.collect(Collectors.toMap(Hold::getHoldId, Function.identity()));
		int resolved = 0;
		for (String holdId : due) {
			try {
				if (resolve(index, holdId, stored.get(holdId))) {
					resolved++;
				}
			}
			catch (RuntimeException ex) {
				log.error("Expiry sweep skipped hold {}", holdId, ex);
			}
		}
		return resolved;
	}

	/** {@code hold} is the hold's row, or null if it has none. */
	private boolean resolve(RedisKeys.ExpiryIndex index, String holdId, Hold hold) {
		if (hold == null) {
			return resolveOrphan(index, holdId);
		}
		if (hold.getStatus() != HoldStatus.ACTIVE) {
			// Already confirmed, released or expired. Whoever ended it owns its inventory, so
			// this sweep only clears the index entry.
			this.holdRedis.forget(index.eventId(), index.categoryId(), holdId);
			return false;
		}
		if (!this.holdRedis.reclaim(hold)) {
			// Someone claimed it in the same instant. Theirs to finish.
			return false;
		}
		if (!markExpired(hold)) {
			log.error("Hold {} was claimed for expiry but its row was no longer ACTIVE", holdId);
		}
		log.debug("Expired hold {}, returning {} ticket(s) to category {}", holdId, hold.getQuantity(),
				hold.getCategoryId());
		return true;
	}

	/** The status and its message commit together, so Checkout always hears of an expiry. */
	private boolean markExpired(Hold hold) {
		Boolean changed = this.transactions.execute(status -> {
			if (this.holdRepository.transitionStatus(hold.getHoldId(), HoldStatus.ACTIVE, HoldStatus.EXPIRED) == 0) {
				return false;
			}
			this.outbox.reservationExpired(hold);
			return true;
		});
		return Boolean.TRUE.equals(changed);
	}

	@Scheduled(fixedDelayString = "${reservation.reconcile-seconds}s",
			initialDelayString = "${reservation.reconcile-seconds}s")
	public void reconcile() {
		try {
			int resolved = reconcileOnce();
			if (resolved > 0) {
				log.warn("Reconciliation finished {} hold(s) the sweep could no longer reach", resolved);
			}
		}
		catch (RuntimeException ex) {
			log.error("Reconciliation failed", ex);
		}
	}

	/**
	 * The periodic scan of the hold records that section 4 asks for, catching holds the sweep can
	 * no longer see.
	 *
	 * <p>Every ending claims a hold in Redis before it writes MySQL. If that write never lands, the
	 * row stays ACTIVE while the hold has already left the expiry index, so the sweep will never
	 * look at it again. The hold's info says what the claim was:
	 * <ul>
	 * <li>RETURNED: a release, expiry or cancellation already gave the tickets back; only the row
	 * is left to finish</li>
	 * <li>SOLD_PENDING: a confirmation took it and never recorded the sale; the tickets are still
	 * subtracted and go back once the row is ended</li>
	 * <li>HELD: nobody ended it after all; indexing it again lets the sweep do so</li>
	 * <li>no info, and the category's counter is gone too: Redis itself was lost, so the event is
	 * rebuilt from MySQL, once however many of its holds are stuck</li>
	 * <li>no info but the counter is there: the hold predates hold info, or its info was evicted.
	 * How it ended is unknowable, so the row is finished and the counter left alone - at worst a
	 * few tickets stay unsold, where guessing could sell them twice</li>
	 * </ul>
	 * Rows are always ended in MySQL before any ticket goes back. The row update is what decides
	 * against a confirmation still committing: if that confirmation wins, the tickets are sold and
	 * must stay subtracted.
	 *
	 * <p>A finished hold is recorded as EXPIRED with a ReservationExpired, whatever ended it, which
	 * is what Checkout needs to stop the order being paid.
	 *
	 * @return how many holds this run finished
	 */
	public int reconcileOnce() {
		Instant cutoff = this.clock.instant().minus(RECONCILE_AFTER);
		Set<String> eventsToRebuild = new LinkedHashSet<>();
		int resolved = 0;
		for (Hold hold : this.holdRepository.findByStatusAndHoldExpiresAtBefore(HoldStatus.ACTIVE, cutoff)) {
			// Contained to its own hold, as in the sweep, so one bad hold cannot stall the rest.
			try {
				if (reconcile(hold, eventsToRebuild)) {
					resolved++;
				}
			}
			catch (RuntimeException ex) {
				log.error("Reconciliation skipped hold {}", hold.getHoldId(), ex);
			}
		}
		for (String eventId : eventsToRebuild) {
			try {
				if (!this.rebuildService.rebuild(eventId)) {
					log.warn("Event {} has holds to reconcile but could not be rebuilt now; the next run retries",
							eventId);
				}
			}
			catch (RuntimeException ex) {
				log.error("Rebuild of event {} failed; the next run retries", eventId, ex);
			}
		}
		return resolved;
	}

	/** Returns true if this hold was finished now; a rebuild it needs is added to the set. */
	private boolean reconcile(Hold hold, Set<String> eventsToRebuild) {
		if (this.holdRedis.isIndexedForExpiry(hold)) {
			// Still the sweep's to end.
			return false;
		}
		HoldEnding ending = this.holdRedis.endingOf(hold.getEventId(), hold.getHoldId());
		if (ending == null) {
			if (this.availabilityRedis.readAvailable(hold.getEventId(), hold.getCategoryId()) == null) {
				eventsToRebuild.add(hold.getEventId());
				return false;
			}
			if (markExpired(hold)) {
				log.warn("Finished hold {}, which had no hold info; its {} ticket(s) were not counted back",
						hold.getHoldId(), hold.getQuantity());
				return true;
			}
			return false;
		}
		switch (ending) {
			case HELD -> this.holdRedis.indexForExpiry(hold);
			case RETURNED -> {
				if (markExpired(hold)) {
					log.warn("Finished hold {}: its tickets were returned but its row was left ACTIVE",
							hold.getHoldId());
					return true;
				}
			}
			case SOLD_PENDING -> {
				if (markExpired(hold)) {
					returnSoldPending(hold, eventsToRebuild);
					return true;
				}
			}
		}
		return false;
	}

	/** The row is EXPIRED, so the claim that took these tickets will never become a sale. */
	private void returnSoldPending(Hold hold, Set<String> eventsToRebuild) {
		switch (this.holdRedis.returnSoldPending(hold)) {
			case OK -> log.warn("Returned {} ticket(s) of hold {}: a confirmation claimed it but never recorded "
					+ "the sale", hold.getQuantity(), hold.getHoldId());
			// The rebuild counts only ACTIVE rows, and this one no longer is.
			case NOT_INITIALISED -> eventsToRebuild.add(hold.getEventId());
			default -> log.error("Hold {} is EXPIRED but its {} ticket(s) could not be returned: its claim changed "
					+ "hands meanwhile", hold.getHoldId(), hold.getQuantity());
		}
	}

	/**
	 * A due hold with no MySQL row: Redis took the tickets and the insert then failed, and the
	 * undo could not run either. Nobody can ever confirm it, so its tickets must come back.
	 *
	 * <p>The quantity is read off the hold's info, which outlives the hold's own key by a day for
	 * exactly this: by the time the hold is due, its key has died with its TTL. If the info has
	 * gone too the quantity is unknowable here, so the index entry is cleared and the counter is
	 * left for InventoryRebuildService to rebuild from MySQL rather than guessed at.
	 */
	private boolean resolveOrphan(RedisKeys.ExpiryIndex index, String holdId) {
		Map<Object, Object> hash = this.holdRedis.readHoldInfo(index.eventId(), holdId);
		Object quantity = hash.get("quantity");
		Object userId = hash.get("user_id");
		if (quantity == null || userId == null) {
			log.warn("Hold {} is due with no MySQL row and no hold info, so its {} ticket(s) cannot be "
					+ "counted back here; the rebuild from MySQL will correct the counter", holdId,
					(quantity == null) ? "unknown number of" : quantity);
			this.holdRedis.forget(index.eventId(), index.categoryId(), holdId);
			return false;
		}
		boolean claimed = this.holdRedis.reclaim(index.eventId(), index.categoryId(), String.valueOf(userId), holdId,
				Integer.parseInt(String.valueOf(quantity)));
		if (claimed) {
			log.warn("Returned {} ticket(s) from orphaned hold {}: Redis held them but no row was ever stored",
					quantity, holdId);
		}
		return claimed;
	}
}
