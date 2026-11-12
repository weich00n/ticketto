package com.ticketing.reservation_service.service;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.ticketing.reservation_service.model.CategoryInventory;
import com.ticketing.reservation_service.model.EventSaleStatus;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.HoldStatus;
import com.ticketing.reservation_service.repository.AvailabilityRedisRepository;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRedisRepository;
import com.ticketing.reservation_service.repository.HoldRedisRepository.HoldEnding;
import com.ticketing.reservation_service.repository.HoldRepository;

/**
 * Puts Redis back together from MySQL after the live state is lost.
 *
 * <p>This is what makes losing Redis survivable rather than fatal, and it is the acceptance
 * check "Redis failure does not erase authoritative hold records". Every counter is derivable,
 * because MySQL kept one row per hold all along.
 *
 * <p><strong>The order of the writes is the guard.</strong> No hold can be created while the
 * availability counter is missing - hold.lua refuses outright - so the counter is written
 * <em>last</em>, after the buyer allowances and the expiry index have been restored. Until that
 * final write the event is closed to new holds, which is exactly the quiet moment a rebuild
 * needs. Writing the counter first would reopen the event and let a new hold interleave with
 * the restore, and the restore would then overwrite it.
 *
 * <p>Counters are written with SET NX, so a rebuild can never flatten a counter that live holds
 * have already moved, however many rebuilds race.
 */
@Service
public class InventoryRebuildService {

	private static final Logger log = LoggerFactory.getLogger(InventoryRebuildService.class);

	/** Long enough to finish one event, short enough that a crashed rebuild unblocks itself. */
	private static final Duration LOCK_TTL = Duration.ofSeconds(30);

	private final CategoryInventoryRepository inventoryRepository;

	private final EventSaleStatusRepository saleStatusRepository;

	private final HoldRepository holdRepository;

	private final HoldRedisRepository holdRedis;

	private final AvailabilityRedisRepository availabilityRedis;

	private final Clock clock;

	public InventoryRebuildService(CategoryInventoryRepository inventoryRepository,
			EventSaleStatusRepository saleStatusRepository, HoldRepository holdRepository,
			HoldRedisRepository holdRedis, AvailabilityRedisRepository availabilityRedis, Clock clock) {
		this.inventoryRepository = inventoryRepository;
		this.saleStatusRepository = saleStatusRepository;
		this.holdRepository = holdRepository;
		this.holdRedis = holdRedis;
		this.availabilityRedis = availabilityRedis;
		this.clock = clock;
	}

	/**
	 * Rebuilds one event's live state, if it needs it and nobody else is already doing it.
	 *
	 * @return true if a rebuild ran, false if another one holds the lock or the event is unknown
	 */
	@Transactional(readOnly = true)
	public boolean rebuild(String eventId) {
		EventSaleStatus sale = this.saleStatusRepository.findById(eventId).orElse(null);
		if (sale == null) {
			// Nothing to rebuild from; the caller will report the event as unknown.
			return false;
		}
		if (!this.holdRedis.acquireRebuildLock(eventId, LOCK_TTL)) {
			log.debug("A rebuild of event {} is already running", eventId);
			return false;
		}
		try {
			Instant now = this.clock.instant();
			List<CategoryInventory> categories = this.inventoryRepository.findByEventIdOrderByCategoryId(eventId);

			// Written before the counter, while the event is still closed to new holds.
			this.availabilityRedis.writeSaleStatus(eventId, sale.getSaleStatus());
			// Only the categories whose counter is gone are rebuilt. A surviving counter already
			// reflects its holds, so re-indexing them could return tickets that already went back.
			Set<String> lost = categories.stream()
					.map(CategoryInventory::getCategoryId)
					.filter(categoryId -> this.availabilityRedis.readAvailable(eventId, categoryId) == null)
					.collect(Collectors.toSet());
			List<Hold> active = this.holdRepository.findByEventIdAndStatus(eventId, HoldStatus.ACTIVE);
			Map<String, HoldEnding> endings = endingsOf(active);
			restoreBuyerAllowances(eventId, active, endings);
			restoreLiveHolds(active.stream().filter(hold -> lost.contains(hold.getCategoryId())).toList(), endings,
					now);

			int rebuilt = restoreCounters(categories, active, endings);
			log.warn("Rebuilt {} availability counter(s) for event {} from MySQL after a Redis loss", rebuilt,
					eventId);
			return true;
		}
		finally {
			this.holdRedis.releaseRebuildLock(eventId);
		}
	}

	/**
	 * How each ACTIVE hold ended as far as Redis still knows. Normally none of these survive a
	 * Redis loss, but a rebuild also runs when only some counters are missing, and then a hold
	 * whose MySQL write failed may already have been RETURNED or claimed (SOLD_PENDING). Those
	 * belong to reconciliation and must not be put back as held, or the sweep would return the
	 * same tickets a second time.
	 */
	private Map<String, HoldEnding> endingsOf(List<Hold> active) {
		Map<String, HoldEnding> endings = new HashMap<>();
		for (Hold hold : active) {
			HoldEnding ending = this.holdRedis.endingOf(hold.getEventId(), hold.getHoldId());
			endings.put(hold.getHoldId(), (ending == null) ? HoldEnding.HELD : ending);
		}
		return endings;
	}

	/**
	 * Every ACTIVE hold counts as held - overdue ones too, because only the sweep gives their
	 * tickets back, and crediting them here as well would hand them out twice - except one whose
	 * tickets were already RETURNED. A SOLD_PENDING hold is still subtracted until reconciliation
	 * returns it.
	 */
	private int restoreCounters(List<CategoryInventory> categories, List<Hold> active,
			Map<String, HoldEnding> endings) {
		Map<String, Integer> heldByCategory = new HashMap<>();
		for (Hold hold : active) {
			if (endings.get(hold.getHoldId()) != HoldEnding.RETURNED) {
				heldByCategory.merge(hold.getCategoryId(), hold.getQuantity(), Integer::sum);
			}
		}
		int rebuilt = 0;
		for (CategoryInventory category : categories) {
			int held = heldByCategory.getOrDefault(category.getCategoryId(), 0);
			int available = category.getTotalQuantity() - category.getSoldQuantity() - held;
			if (available < 0) {
				// Only reachable if sold and held together exceed the total, which would mean an
				// oversell had already happened. Clamp and say so loudly rather than write nonsense.
				log.error("Event {} category {} computes {} available from total {}, sold {}, held {}",
						category.getEventId(), category.getCategoryId(), available, category.getTotalQuantity(),
						category.getSoldQuantity(), held);
				available = 0;
			}
			if (this.availabilityRedis.seedAvailableIfAbsent(category.getEventId(), category.getCategoryId(),
					available)) {
				rebuilt++;
			}
		}
		return rebuilt;
	}

	/** Held plus bought per buyer, less any hold whose tickets - and allowance - already went back. */
	private void restoreBuyerAllowances(String eventId, List<Hold> active, Map<String, HoldEnding> endings) {
		Map<String, Long> returned = new HashMap<>();
		for (Hold hold : active) {
			if (endings.get(hold.getHoldId()) == HoldEnding.RETURNED) {
				returned.merge(hold.getUserId(), (long) hold.getQuantity(), Long::sum);
			}
		}
		for (HoldRepository.BuyerQuantity buyer : this.holdRepository.sumQuantityPerBuyer(eventId, HoldStatus.ACTIVE,
				HoldStatus.CONFIRMED)) {
			long quantity = buyer.getQuantity() - returned.getOrDefault(buyer.getUserId(), 0L);
			this.holdRedis.seedUserQuantityIfAbsent(eventId, buyer.getUserId(), quantity);
		}
	}

	/** Puts every still-held hold back so it can be confirmed and the sweep can end it. */
	private void restoreLiveHolds(List<Hold> active, Map<String, HoldEnding> endings, Instant now) {
		for (Hold hold : active) {
			if (endings.get(hold.getHoldId()) != HoldEnding.HELD) {
				continue;
			}
			Duration remaining = Duration.between(now, hold.getHoldExpiresAt());
			// Counted as held above, so its info must say so too, or reconciliation would read it
			// as lost Redis state and rebuild again.
			this.holdRedis.recordHeld(hold, remaining);
			if (!remaining.isNegative() && !remaining.isZero()) {
				this.holdRedis.restoreHoldKey(hold, remaining);
			}
			this.holdRedis.indexForExpiry(hold);
		}
	}
}
