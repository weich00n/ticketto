package com.ticketing.reservation_service.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.Optional;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.transaction.support.TransactionCallback;
import org.springframework.transaction.support.TransactionTemplate;

import com.ticketing.reservation_service.config.ReservationProperties;
import com.ticketing.reservation_service.config.ReservationProperties.TokenAlgorithm;
import com.ticketing.reservation_service.dto.ConfirmRequest;
import com.ticketing.reservation_service.dto.CreateHoldRequest;
import com.ticketing.reservation_service.dto.HoldView;
import com.ticketing.reservation_service.exception.ApiException;
import com.ticketing.reservation_service.exception.ErrorCode;
import com.ticketing.reservation_service.model.CategoryInventory;
import com.ticketing.reservation_service.model.CategoryInventoryId;
import com.ticketing.reservation_service.model.EventSaleStatus;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.HoldStatus;
import com.ticketing.reservation_service.model.SaleStatus;
import com.ticketing.reservation_service.repository.CategoryInventoryRepository;
import com.ticketing.reservation_service.repository.EventSaleStatusRepository;
import com.ticketing.reservation_service.repository.HoldRedisRepository;
import com.ticketing.reservation_service.repository.HoldRedisRepository.HoldOutcome;
import com.ticketing.reservation_service.repository.HoldRedisRepository.Status;
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.service.AdmissionTokenService.AdmissionClaims;

/**
 * The decisions creating and confirming a hold make, without Redis or MySQL: what is refused
 * before any ticket moves, that every failure after Redis has allocated hands the tickets back,
 * and that a confirmation racing an event's cancellation still leaves the refund recorded.
 *
 * <p>The integration tests prove the same flows end to end; these pin the order of the steps,
 * which is what keeps a refused request from costing inventory.
 */
@ExtendWith(MockitoExtension.class)
class ReservationServiceTest {

	private static final String EVENT = "event-1";

	private static final String CATEGORY = "category-1";

	private static final String TOKEN = "header.claims.signature";

	private static final String USER_SUB = "cognito-sub";

	private static final Instant NOW = Instant.parse("2026-11-20T12:00:00Z");

	private final AdmissionClaims claims = new AdmissionClaims("admission-1", USER_SUB, EVENT,
			NOW.plus(Duration.ofMinutes(5)), NOW.plus(Duration.ofMinutes(11)));

	@Mock
	private HoldRepository holdRepository;

	@Mock
	private CategoryInventoryRepository inventoryRepository;

	@Mock
	private EventSaleStatusRepository saleStatusRepository;

	@Mock
	private HoldRedisRepository holdRedis;

	@Mock
	private AdmissionTokenService admissionTokenService;

	@Mock
	private IdempotencyService idempotencyService;

	@Mock
	private InventoryRebuildService rebuildService;

	@Mock
	private TransactionTemplate transactions;

	@Mock
	private OutboxWriter outbox;

	private ReservationService service;

	@BeforeEach
	void createService() {
		ReservationProperties properties = new ReservationProperties(6, 5, 5, 10, TokenAlgorithm.HS256, "unused",
				null);
		service = new ReservationService(holdRepository, inventoryRepository, saleStatusRepository, holdRedis,
				admissionTokenService, idempotencyService, rebuildService, properties,
				Clock.fixed(NOW, ZoneOffset.UTC), transactions, outbox);
	}

	@Test
	void aRefusedTokenNeverReachesRedisOrTheDatabase() {
		when(admissionTokenService.verify(TOKEN, EVENT, USER_SUB))
				.thenThrow(new ApiException(ErrorCode.INVALID_ADMISSION));

		assertRefused(ErrorCode.INVALID_ADMISSION);
		verifyNoInteractions(holdRedis, holdRepository, inventoryRepository);
	}

	@Test
	void aUsedAdmissionIsRefusedBeforeAnyTicketMoves() {
		admitted();
		when(holdRepository.findByAdmissionId("admission-1")).thenReturn(Optional.of(new Hold()));

		assertRefused(ErrorCode.ADMISSION_ALREADY_HAS_HOLD);
		verifyNoInteractions(holdRedis);
	}

	@Test
	void aCancelledEventIsRefusedFromTheDatabaseWithoutAskingRedis() {
		admitted();
		when(saleStatusRepository.findById(EVENT)).thenReturn(Optional.of(sale(SaleStatus.CANCELLED)));

		assertRefused(ErrorCode.EVENT_CANCELLED);
		verifyNoInteractions(holdRedis);
	}

	@Test
	void anUnknownCategoryIsRefusedWithoutAskingRedis() {
		admitted();
		onSale();
		when(inventoryRepository.findById(new CategoryInventoryId(EVENT, CATEGORY))).thenReturn(Optional.empty());

		assertRefused(ErrorCode.CATEGORY_NOT_FOUND);
		verifyNoInteractions(holdRedis);
	}

	@Test
	void aSuccessfulHoldSnapshotsThePriceAndSetsBothDeadlines() {
		readyToHold();
		when(holdRedis.takeHold(any(), anyInt(), any())).thenReturn(new HoldOutcome(Status.OK, null));

		HoldView view = service.createHold(request(2), USER_SUB, null).hold();

		ArgumentCaptor<Hold> stored = ArgumentCaptor.forClass(Hold.class);
		verify(holdRepository).saveAndFlush(stored.capture());
		Hold hold = stored.getValue();
		assertThat(hold.getUserId()).isEqualTo(USER_SUB);
		assertThat(hold.getUserSub()).isEqualTo(USER_SUB);
		assertThat(hold.getAdmissionId()).isEqualTo("admission-1");
		assertThat(hold.getStatus()).isEqualTo(HoldStatus.ACTIVE);
		assertThat(hold.getUnitAmount()).isEqualTo(28800);
		assertThat(hold.getTotalAmount()).isEqualTo(57600);
		// the buyer sees five minutes; the Redis key lives one minute longer as the buffer
		assertThat(hold.getPaymentExpiresAt()).isEqualTo(NOW.plus(Duration.ofMinutes(5)));
		assertThat(hold.getHoldExpiresAt()).isEqualTo(NOW.plus(Duration.ofMinutes(6)));
		assertThat(view.holdId()).isEqualTo(hold.getHoldId());
		assertThat(view.serverTime()).isEqualTo(NOW);
		// the category still has tickets, so nothing is owed to anyone
		verifyNoInteractions(outbox);
	}

	@Test
	void takingTheLastTicketIsNotASellOutBecauseTheHoldMayStillExpire() {
		readyToHold();
		when(holdRedis.takeHold(any(), anyInt(), any())).thenReturn(new HoldOutcome(Status.OK, 0));

		service.createHold(request(2), USER_SUB, null);

		verify(outbox, never()).eventSoldOut(any());
	}

	@Test
	void notEnoughTicketsSaysHowManyAreLeftAndStoresNothing() {
		readyToHold();
		when(holdRedis.takeHold(any(), anyInt(), any()))
				.thenReturn(new HoldOutcome(Status.INSUFFICIENT_INVENTORY, 3));

		assertThatThrownBy(() -> service.createHold(request(5), USER_SUB, null))
				.isInstanceOfSatisfying(ApiException.class, ex -> {
					assertThat(ex.getCode()).isEqualTo(ErrorCode.INSUFFICIENT_INVENTORY);
					assertThat(ex.getMessage()).contains("3 left");
				});
		verify(holdRepository, never()).saveAndFlush(any());
	}

	@Test
	void theLimitSaysHowManyMoreTheBuyerMayTake() {
		readyToHold();
		when(holdRedis.takeHold(any(), anyInt(), any()))
				.thenReturn(new HoldOutcome(Status.PURCHASE_LIMIT_EXCEEDED, 1));

		assertThatThrownBy(() -> service.createHold(request(2), USER_SUB, null))
				.isInstanceOfSatisfying(ApiException.class, ex -> {
					assertThat(ex.getCode()).isEqualTo(ErrorCode.PURCHASE_LIMIT_EXCEEDED);
					assertThat(ex.getMessage()).contains("1 more");
				});
	}

	@Test
	void lostCountersAreRebuiltAndTheHoldIsTriedOnceMore() {
		readyToHold();
		when(holdRedis.takeHold(any(), anyInt(), any()))
				.thenReturn(new HoldOutcome(Status.NOT_INITIALISED, null), new HoldOutcome(Status.OK, null));
		when(rebuildService.rebuild(EVENT)).thenReturn(true);

		service.createHold(request(1), USER_SUB, null);

		verify(holdRedis, times(2)).takeHold(any(), anyInt(), any());
		verify(holdRepository).saveAndFlush(any());
	}

	@Test
	void countersThatCannotBeRebuiltAskTheCallerToRetry() {
		readyToHold();
		when(holdRedis.takeHold(any(), anyInt(), any())).thenReturn(new HoldOutcome(Status.NOT_INITIALISED, null));
		when(rebuildService.rebuild(EVENT)).thenReturn(false);

		assertRefused(ErrorCode.DEPENDENCY_UNAVAILABLE);
		verify(holdRepository, never()).saveAndFlush(any());
	}

	@Test
	void anAdmissionThatWinsTheRaceToTheRowGivesTheLosersTicketsBack() {
		// two attempts passed the early check together; the unique constraint picks one
		readyToHold();
		when(holdRedis.takeHold(any(), anyInt(), any())).thenReturn(new HoldOutcome(Status.OK, null));
		when(holdRepository.saveAndFlush(any())).thenThrow(new DataIntegrityViolationException("uk_hold_admission_id"));

		assertRefused(ErrorCode.ADMISSION_ALREADY_HAS_HOLD);
		verify(holdRedis).reclaim(any(Hold.class));
	}

	@Test
	void anyOtherFailureToStoreTheHoldAlsoGivesTheTicketsBack() {
		readyToHold();
		when(holdRedis.takeHold(any(), anyInt(), any())).thenReturn(new HoldOutcome(Status.OK, null));
		IllegalStateException databaseDown = new IllegalStateException("connection refused");
		when(holdRepository.saveAndFlush(any())).thenThrow(databaseDown);

		assertThatThrownBy(() -> service.createHold(request(1), USER_SUB, null)).isSameAs(databaseDown);
		verify(holdRedis).reclaim(any(Hold.class));
		// a hold that was never stored cannot have sold the event out
		verifyNoInteractions(outbox);
	}

	@Test
	void aConfirmationThatLandsAsTheEventIsCancelledStillStandsAndOwesARefund() {
		// the sale check passes, the event is cancelled before the confirmation commits, and the
		// cancellation had already passed over this hold while it still read ACTIVE
		Hold active = hold(HoldStatus.ACTIVE);
		Hold confirmed = hold(HoldStatus.CONFIRMED);
		when(holdRepository.findById("hold-1")).thenReturn(Optional.of(active), Optional.of(confirmed));
		when(saleStatusRepository.findById(EVENT))
				.thenReturn(Optional.of(sale(SaleStatus.ON_SALE)), Optional.of(sale(SaleStatus.CANCELLED)));
		when(holdRedis.confirm(active, "order-1")).thenReturn(Status.OK);
		runTransactionsInline();
		when(holdRepository.confirmIfActive(any(), any(), any(), any(), any(), any())).thenReturn(1);
		when(outbox.refundRequired(confirmed)).thenReturn(true);

		var view = service.confirmHold("hold-1", new ConfirmRequest("order-1", "pi_1"));

		// the payment happened, so the confirmation stands and Checkout refunds it
		assertThat(view.status()).isEqualTo(HoldStatus.CONFIRMED);
		verify(outbox).refundRequired(confirmed);
	}

	@Test
	void anOrdinaryConfirmationOwesNoRefund() {
		Hold active = hold(HoldStatus.ACTIVE);
		when(holdRepository.findById("hold-1")).thenReturn(Optional.of(active), Optional.of(hold(HoldStatus.CONFIRMED)));
		onSale();
		when(holdRedis.confirm(active, "order-1")).thenReturn(Status.OK);
		runTransactionsInline();
		when(holdRepository.confirmIfActive(any(), any(), any(), any(), any(), any())).thenReturn(1);

		service.confirmHold("hold-1", new ConfirmRequest("order-1", "pi_1"));

		verifyNoInteractions(outbox);
	}

	@Test
	void aConfirmationAfterCancellationIsReportedAsTheCancellation() {
		// the cancellation already ended this hold; EVENT_CANCELLED says why better than an expiry
		when(holdRepository.findById("hold-1")).thenReturn(Optional.of(hold(HoldStatus.CANCELLED)));
		when(saleStatusRepository.findById(EVENT)).thenReturn(Optional.of(sale(SaleStatus.CANCELLED)));

		assertThatThrownBy(() -> service.confirmHold("hold-1", new ConfirmRequest("order-1", "pi_1")))
				.isInstanceOfSatisfying(ApiException.class,
						ex -> assertThat(ex.getCode()).isEqualTo(ErrorCode.EVENT_CANCELLED));
		verifyNoInteractions(holdRedis);
	}

	@SuppressWarnings("unchecked")
	private void runTransactionsInline() {
		lenient().when(transactions.execute(any())).thenAnswer(
				invocation -> ((TransactionCallback<Object>) invocation.getArgument(0)).doInTransaction(null));
	}

	private static Hold hold(HoldStatus status) {
		Hold hold = new Hold();
		hold.setHoldId("hold-1");
		hold.setEventId(EVENT);
		hold.setCategoryId(CATEGORY);
		hold.setQuantity(2);
		hold.setStatus(status);
		if (status == HoldStatus.CONFIRMED) {
			hold.setOrderId("order-1");
		}
		return hold;
	}

	private void readyToHold() {
		admitted();
		onSale();
		CategoryInventory inventory = new CategoryInventory();
		inventory.setEventId(EVENT);
		inventory.setCategoryId(CATEGORY);
		inventory.setTotalQuantity(10);
		inventory.setUnitAmount(28800);
		inventory.setCurrency("SGD");
		when(inventoryRepository.findById(new CategoryInventoryId(EVENT, CATEGORY))).thenReturn(Optional.of(inventory));
		runTransactionsInline();
	}

	private void admitted() {
		when(admissionTokenService.verify(TOKEN, EVENT, USER_SUB)).thenReturn(claims);
	}

	private void onSale() {
		when(saleStatusRepository.findById(EVENT)).thenReturn(Optional.of(sale(SaleStatus.ON_SALE)));
	}

	private static EventSaleStatus sale(SaleStatus status) {
		EventSaleStatus sale = new EventSaleStatus();
		sale.setEventId(EVENT);
		sale.setSaleStatus(status);
		return sale;
	}

	private void assertRefused(ErrorCode expected) {
		assertThatThrownBy(() -> service.createHold(request(1), USER_SUB, null))
				.isInstanceOfSatisfying(ApiException.class, ex -> assertThat(ex.getCode()).isEqualTo(expected));
	}

	private static CreateHoldRequest request(int quantity) {
		return new CreateHoldRequest(EVENT, CATEGORY, quantity, TOKEN);
	}
}
