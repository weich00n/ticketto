package com.ticketing.reservation_service.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Optional;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import com.ticketing.reservation_service.dto.CapacityView;
import com.ticketing.reservation_service.dto.CategoryCapacityView;
import com.ticketing.reservation_service.dto.SeedCategoryRequest;
import com.ticketing.reservation_service.dto.SeedInventoryRequest;
import com.ticketing.reservation_service.exception.ApiException;
import com.ticketing.reservation_service.exception.ErrorCode;
import com.ticketing.reservation_service.model.CategoryInventory;
import com.ticketing.reservation_service.model.EventSaleStatus;
import com.ticketing.reservation_service.model.SaleStatus;
import com.ticketing.reservation_service.repository.AvailabilityRedisRepository;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;

/**
 * Availability is read off the counter, never derived from MySQL, except where the counter is
 * gone; and seeding never moves a counter that already exists.
 */
@ExtendWith(MockitoExtension.class)
class InventoryServiceTest {

	private static final String EVENT = "event-1";

	private static final String CATEGORY = "category-1";

	private static final Instant NOW = Instant.parse("2026-11-20T12:00:00Z");

	@Mock
	private CategoryInventoryRepository inventoryRepository;

	@Mock
	private EventSaleStatusRepository saleStatusRepository;

	@Mock
	private AvailabilityRedisRepository availabilityRedis;

	@Mock
	private InventoryRebuildService rebuildService;

	private InventoryService service;

	@BeforeEach
	void createService() {
		service = new InventoryService(inventoryRepository, saleStatusRepository, availabilityRedis, rebuildService,
				Clock.fixed(NOW, ZoneOffset.UTC));
	}

	@Test
	void availabilityIsTheCounterNotTotalMinusSold() {
		onSale();
		stored(10, 2);
		// 2 sold and 3 held: only the counter knows about the held ones
		when(availabilityRedis.readAvailable(EVENT, CATEGORY)).thenReturn(5);

		var category = service.getAvailability(EVENT).categories().getFirst();

		assertThat(category.free()).isEqualTo(5);
		assertThat(category.total()).isEqualTo(10);
		assertThat(category.sold()).isEqualTo(2);
	}

	@Test
	void capacityDerivesHeldFromTheOtherThree() {
		onSale();
		stored(10, 2);
		when(availabilityRedis.readAvailable(EVENT, CATEGORY)).thenReturn(5);

		CategoryCapacityView category = service.getCapacity(EVENT).categories().getFirst();

		assertThat(category.held()).isEqualTo(3);
	}

	@Test
	void heldIsClampedAtZeroWhenTheReadsDisagree() {
		// the three numbers are read at slightly different moments while holds move
		onSale();
		stored(10, 6);
		when(availabilityRedis.readAvailable(EVENT, CATEGORY)).thenReturn(5);

		assertThat(service.getCapacity(EVENT).categories().getFirst().held()).isZero();
	}

	@Test
	void anEventWithNoInventoryIsNotReady() {
		onSale();
		when(inventoryRepository.findByEventIdOrderByCategoryId(EVENT)).thenReturn(List.of());

		CapacityView capacity = service.getCapacity(EVENT);

		assertThat(capacity.ready()).isFalse();
		assertThat(capacity.categories()).isEmpty();
	}

	@Test
	void anUnknownEventIsNotFound() {
		when(saleStatusRepository.findById(EVENT)).thenReturn(Optional.empty());

		assertThatThrownBy(() -> service.getAvailability(EVENT))
				.isInstanceOfSatisfying(ApiException.class,
						ex -> assertThat(ex.getCode()).isEqualTo(ErrorCode.EVENT_NOT_FOUND));
	}

	@Test
	void aLostCounterIsRebuiltAndReadAgain() {
		onSale();
		stored(10, 0);
		when(availabilityRedis.readAvailable(EVENT, CATEGORY)).thenReturn(null, 7);
		when(rebuildService.rebuild(EVENT)).thenReturn(true);

		assertThat(service.getAvailability(EVENT).categories().getFirst().free()).isEqualTo(7);
	}

	@Test
	void aCounterThatCannotBeRebuiltAsksTheCallerToRetry() {
		onSale();
		stored(10, 0);
		when(availabilityRedis.readAvailable(EVENT, CATEGORY)).thenReturn(null);
		when(rebuildService.rebuild(EVENT)).thenReturn(false);

		assertThatThrownBy(() -> service.getAvailability(EVENT))
				.isInstanceOfSatisfying(ApiException.class,
						ex -> assertThat(ex.getCode()).isEqualTo(ErrorCode.DEPENDENCY_UNAVAILABLE));
	}

	@Test
	void reseedingWithDifferentTotalsIsRefusedAndTouchesNoCounter() {
		stored(10, 0);

		assertThatThrownBy(() -> service.seedInventory(EVENT, seed(CATEGORY, 20)))
				.isInstanceOfSatisfying(ApiException.class,
						ex -> assertThat(ex.getCode()).isEqualTo(ErrorCode.INVENTORY_CONFLICT));
		verifyNoInteractions(availabilityRedis);
	}

	@Test
	void aCategoryListedTwiceIsRejectedBeforeAnythingIsWritten() {
		SeedInventoryRequest twice = new SeedInventoryRequest(null, List.of(
				new SeedCategoryRequest(CATEGORY, 10, 28800L, null),
				new SeedCategoryRequest(CATEGORY, 10, 28800L, null)));

		assertThatThrownBy(() -> service.seedInventory(EVENT, twice))
				.isInstanceOfSatisfying(ApiException.class,
						ex -> assertThat(ex.getCode()).isEqualTo(ErrorCode.INVALID_REQUEST));
		verify(inventoryRepository, never()).saveAll(any());
		verifyNoInteractions(availabilityRedis);
	}

	@Test
	void aReplayOnlySeedsACounterThatIsMissing() {
		// SET NX: a replay must never reset a counter that live holds have already moved
		stored(10, 0);
		onSale();
		when(availabilityRedis.readAvailable(EVENT, CATEGORY)).thenReturn(4);

		var view = service.seedInventory(EVENT, seed(CATEGORY, 10));

		assertThat(view.created()).isFalse();
		assertThat(view.categories().getFirst().free()).isEqualTo(4);
		verify(availabilityRedis).seedAvailableIfAbsent(anyString(), anyString(), anyInt());
	}

	private void onSale() {
		EventSaleStatus sale = new EventSaleStatus();
		sale.setEventId(EVENT);
		sale.setSaleStatus(SaleStatus.ON_SALE);
		when(saleStatusRepository.findById(EVENT)).thenReturn(Optional.of(sale));
	}

	private void stored(int total, int sold) {
		CategoryInventory row = new CategoryInventory();
		row.setEventId(EVENT);
		row.setCategoryId(CATEGORY);
		row.setTotalQuantity(total);
		row.setSoldQuantity(sold);
		row.setUnitAmount(28800);
		row.setCurrency("SGD");
		when(inventoryRepository.findByEventIdOrderByCategoryId(EVENT)).thenReturn(List.of(row));
	}

	private static SeedInventoryRequest seed(String categoryId, int total) {
		return new SeedInventoryRequest(null, List.of(new SeedCategoryRequest(categoryId, total, 28800L, null)));
	}
}
