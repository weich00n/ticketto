package com.ticketing.reservation_service.service;

import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import com.ticketing.reservation_service.dto.AvailabilityView;
import com.ticketing.reservation_service.dto.CapacityView;
import com.ticketing.reservation_service.dto.CategoryAvailabilityView;
import com.ticketing.reservation_service.dto.CategoryCapacityView;
import com.ticketing.reservation_service.dto.EventPublishedData;
import com.ticketing.reservation_service.dto.SeedCategoryRequest;
import com.ticketing.reservation_service.dto.SeedInventoryRequest;
import com.ticketing.reservation_service.dto.SeedInventoryView;
import com.ticketing.reservation_service.exception.ApiException;
import com.ticketing.reservation_service.exception.ErrorCode;
import com.ticketing.reservation_service.model.CategoryInventory;
import com.ticketing.reservation_service.model.EventSaleStatus;
import com.ticketing.reservation_service.model.SaleStatus;
import com.ticketing.reservation_service.repository.AvailabilityRedisRepository;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;

/** Reads live availability, and seeds an event's inventory. */
@Service
public class InventoryService {

	private static final String DEFAULT_CURRENCY = "SGD";

	private final CategoryInventoryRepository inventoryRepository;

	private final EventSaleStatusRepository saleStatusRepository;

	private final AvailabilityRedisRepository availabilityRedis;

	private final InventoryRebuildService rebuildService;

	private final Clock clock;

	public InventoryService(CategoryInventoryRepository inventoryRepository,
			EventSaleStatusRepository saleStatusRepository, AvailabilityRedisRepository availabilityRedis,
			InventoryRebuildService rebuildService, Clock clock) {
		this.inventoryRepository = inventoryRepository;
		this.saleStatusRepository = saleStatusRepository;
		this.availabilityRedis = availabilityRedis;
		this.rebuildService = rebuildService;
		this.clock = clock;
	}

	/**
	 * total and sold come from MySQL; free is read straight off the Redis counter. Nothing is
	 * subtracted here: the counter already has held and sold tickets taken out of it, and it is
	 * the answer.
	 */
	@Transactional(readOnly = true)
	public AvailabilityView getAvailability(String eventId) {
		EventSaleStatus sale = saleStatusRepository.findById(eventId)
				.orElseThrow(() -> new ApiException(ErrorCode.EVENT_NOT_FOUND));
		List<CategoryAvailabilityView> categories = new ArrayList<>();
		for (CategoryInventory row : inventoryRepository.findByEventIdOrderByCategoryId(eventId)) {
			categories.add(new CategoryAvailabilityView(row.getCategoryId(), row.getTotalQuantity(),
					requireCounter(eventId, row.getCategoryId()), row.getSoldQuantity()));
		}
		return new AvailabilityView(eventId, sale.getSaleStatus(), now(), categories);
	}

	/**
	 * What Queue reads to size an admission batch.
	 *
	 * <p>ready is false until inventory has been initialised, which lets Queue tell an event that
	 * cannot sell yet from one that has sold out.
	 *
	 * <p>This is the only place held is produced, and it is derived rather than stored:
	 * total − sold − available, clamped at zero. Clamping matters because the three numbers are
	 * read at slightly different moments while holds are being taken, so the arithmetic can go
	 * briefly negative; a negative held would be nonsense to its reader. It is advisory, and the
	 * payload says so.
	 */
	@Transactional(readOnly = true)
	public CapacityView getCapacity(String eventId) {
		EventSaleStatus sale = saleStatusRepository.findById(eventId)
				.orElseThrow(() -> new ApiException(ErrorCode.EVENT_NOT_FOUND));
		List<CategoryInventory> rows = inventoryRepository.findByEventIdOrderByCategoryId(eventId);

		List<CategoryCapacityView> categories = new ArrayList<>();
		for (CategoryInventory row : rows) {
			int available = requireCounter(eventId, row.getCategoryId());
			int held = Math.max(0, row.getTotalQuantity() - row.getSoldQuantity() - available);
			categories.add(new CategoryCapacityView(row.getCategoryId(), row.getTotalQuantity(), available, held,
					row.getSoldQuantity()));
		}
		return new CapacityView(eventId, !rows.isEmpty(), sale.getSaleStatus(), now(), categories);
	}

	/**
	 * What an EventPublished message does: puts the event on sale with its totals and prices.
	 *
	 * <p>A cancellation always wins. Messages of different types arrive on different queues, so
	 * EventCancelled can come first, and an EventPublished arriving after it - late, or delivered
	 * again - is ignored rather than refused, because refusing would only send it round again.
	 *
	 * @return false if the event was already cancelled and nothing was changed
	 */
	@Transactional
	public boolean initialiseFromEventPublished(EventPublishedData data) {
		boolean cancelled = saleStatusRepository.findById(data.eventId())
				.map(sale -> sale.getSaleStatus() == SaleStatus.CANCELLED)
				.orElse(false);
		if (cancelled) {
			return false;
		}
		seedInventory(data.eventId(), new SeedInventoryRequest(SaleStatus.ON_SALE, data.seedCategories()));
		return true;
	}

	/**
	 * Writes the durable totals and prices, then the Redis keys that availability and the hold
	 * script read. The EventPublished consumer comes through here; so does the internal seeding
	 * route, which tests and local runs use instead of a broker.
	 *
	 * <p>Replaying the same payload changes nothing and reports created = false. Seeding the same
	 * event with different totals, prices or sale status is refused rather than applied, because
	 * moving a live counter safely is not something the MVP needs.
	 *
	 * <p>MySQL is written first and Redis last, so a failed commit leaves counters for an event
	 * that reports EVENT_NOT_FOUND rather than inventory with no counters.
	 */
	@Transactional
	public SeedInventoryView seedInventory(String eventId, SeedInventoryRequest request) {
		SaleStatus requestedStatus = request.saleStatus() == null ? SaleStatus.ON_SALE : request.saleStatus();
		Map<String, SeedCategoryRequest> requested = indexByCategory(request.categories());

		List<CategoryInventory> existing = inventoryRepository.findByEventIdOrderByCategoryId(eventId);
		boolean created;
		List<CategoryInventory> rows;
		if (existing.isEmpty()) {
			rows = insertInventory(eventId, requested.values());
			created = true;
		}
		else {
			requireSamePayload(existing, requested);
			rows = existing;
			created = false;
		}
		writeSaleStatus(eventId, requestedStatus, created);

		List<CategoryAvailabilityView> categories = new ArrayList<>();
		for (CategoryInventory row : rows) {
			// Seeding a missing counter is the one place availability is derived from MySQL, and
			// SET NX means a replay can never reset a counter that live holds have moved.
			availabilityRedis.seedAvailableIfAbsent(eventId, row.getCategoryId(),
					row.getTotalQuantity() - row.getSoldQuantity());
			categories.add(new CategoryAvailabilityView(row.getCategoryId(), row.getTotalQuantity(),
					requireCounter(eventId, row.getCategoryId()), row.getSoldQuantity()));
		}
		// The hold script reads this key, so availability has nothing to check without it.
		availabilityRedis.writeSaleStatus(eventId, requestedStatus);

		return new SeedInventoryView(eventId, requestedStatus, created, categories, now());
	}

	private List<CategoryInventory> insertInventory(String eventId, Iterable<SeedCategoryRequest> requested) {
		List<CategoryInventory> rows = new ArrayList<>();
		for (SeedCategoryRequest category : requested) {
			CategoryInventory row = new CategoryInventory();
			row.setEventId(eventId);
			row.setCategoryId(category.categoryId());
			row.setTotalQuantity(category.totalQuantity());
			row.setSoldQuantity(0);
			row.setUnitAmount(category.unitAmount());
			row.setCurrency(currencyOf(category));
			rows.add(row);
		}
		return inventoryRepository.saveAll(rows);
	}

	private void writeSaleStatus(String eventId, SaleStatus requestedStatus, boolean created) {
		Instant now = now();
		EventSaleStatus sale = saleStatusRepository.findById(eventId).orElse(null);
		if (sale == null) {
			sale = new EventSaleStatus();
			sale.setEventId(eventId);
			sale.setSaleStatus(requestedStatus);
			sale.setCreatedAt(now);
			sale.setUpdatedAt(now);
			saleStatusRepository.save(sale);
			return;
		}
		if (sale.getSaleStatus() != requestedStatus) {
			throw new ApiException(ErrorCode.INVENTORY_CONFLICT, "This event is already " + sale.getSaleStatus()
					+ "; seeding cannot change it to " + requestedStatus + ".");
		}
	}

	/** A replay must carry exactly the inventory that is already stored. */
	private void requireSamePayload(List<CategoryInventory> existing, Map<String, SeedCategoryRequest> requested) {
		if (existing.size() != requested.size()) {
			throw new ApiException(ErrorCode.INVENTORY_CONFLICT, "This event already has " + existing.size()
					+ " categories; the request carries " + requested.size() + ".");
		}
		for (CategoryInventory row : existing) {
			SeedCategoryRequest category = requested.get(row.getCategoryId());
			if (category == null) {
				throw new ApiException(ErrorCode.INVENTORY_CONFLICT,
						"Category " + row.getCategoryId() + " is already initialised but is missing from the request.");
			}
			boolean same = row.getTotalQuantity() == category.totalQuantity()
					&& row.getUnitAmount() == category.unitAmount()
					&& Objects.equals(row.getCurrency(), currencyOf(category));
			if (!same) {
				throw new ApiException(ErrorCode.INVENTORY_CONFLICT,
						"Category " + row.getCategoryId() + " is already initialised with different values.");
			}
		}
	}

	private Map<String, SeedCategoryRequest> indexByCategory(List<SeedCategoryRequest> categories) {
		Map<String, SeedCategoryRequest> byCategory = new LinkedHashMap<>();
		for (SeedCategoryRequest category : categories) {
			if (byCategory.put(category.categoryId(), category) != null) {
				throw new ApiException(ErrorCode.INVALID_REQUEST,
						"Category " + category.categoryId() + " appears twice in the request.");
			}
		}
		return byCategory;
	}

	/**
	 * Availability is only ever read, never derived. A missing counter means the live state was
	 * lost, so it is rebuilt from MySQL and read again. Only if it is still missing - because
	 * another rebuild holds the lock, say - is 503 returned, asking the caller to retry.
	 */
	private int requireCounter(String eventId, String categoryId) {
		Integer free = availabilityRedis.readAvailable(eventId, categoryId);
		if (free == null && rebuildService.rebuild(eventId)) {
			// The live state was lost and has just been put back from MySQL. A reader should not
			// see a failure for that.
			free = availabilityRedis.readAvailable(eventId, categoryId);
		}
		if (free == null) {
			throw new ApiException(ErrorCode.DEPENDENCY_UNAVAILABLE,
					"Live availability is unavailable for this event. Please retry shortly.");
		}
		return free;
	}

	private static String currencyOf(SeedCategoryRequest category) {
		return StringUtils.hasText(category.currency()) ? category.currency() : DEFAULT_CURRENCY;
	}

	/** Whole seconds, so timestamps read as 2026-11-20T12:00:00Z rather than carrying nanos. */
	private Instant now() {
		return clock.instant().truncatedTo(ChronoUnit.SECONDS);
	}
}
