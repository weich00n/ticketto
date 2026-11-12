package com.ticketing.reservation_service.service;

import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Optional;
import java.util.UUID;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.util.StringUtils;

import com.ticketing.reservation_service.config.CorrelationIdFilter;
import com.ticketing.reservation_service.config.ReservationProperties;
import com.ticketing.reservation_service.dto.AdmissionReservationView;
import com.ticketing.reservation_service.dto.ConfirmRequest;
import com.ticketing.reservation_service.dto.ConfirmView;
import com.ticketing.reservation_service.dto.CreateHoldRequest;
import com.ticketing.reservation_service.dto.HoldCreation;
import com.ticketing.reservation_service.dto.HoldView;
import com.ticketing.reservation_service.dto.InternalHoldView;
import com.ticketing.reservation_service.dto.ReleaseView;
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
import com.ticketing.reservation_service.repository.HoldRepository;
import com.ticketing.reservation_service.service.AdmissionTokenService.AdmissionClaims;

/** Creating a hold: the one flow the whole design is built around. */
@Service
public class ReservationService {

	private static final Logger log = LoggerFactory.getLogger(ReservationService.class);

	private final HoldRepository holdRepository;

	private final CategoryInventoryRepository inventoryRepository;

	private final EventSaleStatusRepository saleStatusRepository;

	private final HoldRedisRepository holdRedis;

	private final AdmissionTokenService admissionTokenService;

	private final IdempotencyService idempotencyService;

	private final InventoryRebuildService rebuildService;

	private final ReservationProperties properties;

	private final Clock clock;

	private final TransactionTemplate transactions;

	private final OutboxWriter outbox;

	public ReservationService(HoldRepository holdRepository, CategoryInventoryRepository inventoryRepository,
			EventSaleStatusRepository saleStatusRepository, HoldRedisRepository holdRedis,
			AdmissionTokenService admissionTokenService, IdempotencyService idempotencyService,
			InventoryRebuildService rebuildService, ReservationProperties properties, Clock clock,
			TransactionTemplate transactions, OutboxWriter outbox) {
		this.holdRepository = holdRepository;
		this.inventoryRepository = inventoryRepository;
		this.saleStatusRepository = saleStatusRepository;
		this.holdRedis = holdRedis;
		this.admissionTokenService = admissionTokenService;
		this.idempotencyService = idempotencyService;
		this.rebuildService = rebuildService;
		this.properties = properties;
		this.clock = clock;
		this.transactions = transactions;
		this.outbox = outbox;
	}

	/**
	 * Creates a hold, or returns the one an identical earlier request already created.
	 *
	 * <p>Without an Idempotency-Key this is simply a create. With one, the key is claimed before
	 * any work happens, so two retries landing together cannot both produce a hold, and a replay
	 * is answered from the stored hold with its <em>original</em> deadlines: retrying must never
	 * buy the buyer more time.
	 */
	public HoldCreation createHold(CreateHoldRequest request, String authenticatedUserSub, String idempotencyKey) {
		if (!IdempotencyService.isPresent(idempotencyKey)) {
			return new HoldCreation(create(request, authenticatedUserSub), false);
		}
		if (!StringUtils.hasText(authenticatedUserSub)) {
			throw new ApiException(ErrorCode.UNAUTHENTICATED);
		}
		// Claiming the key before doing any work is what stops two retries both creating a hold.
		Optional<String> alreadyCreated = idempotencyService.claim(idempotencyKey, authenticatedUserSub, request);
		if (alreadyCreated.isPresent()) {
			// Read back rather than rebuilt, so the original deadlines are returned unchanged: a
			// retry must never buy the buyer more time.
			return new HoldCreation(getHold(alreadyCreated.get(), authenticatedUserSub), true);
		}
		try {
			HoldView hold = create(request, authenticatedUserSub);
			idempotencyService.recordHold(idempotencyKey, hold.holdId());
			return new HoldCreation(hold, false);
		}
		catch (RuntimeException ex) {
			// No hold came of it, so the key must not stay claimed or the buyer could never retry.
			idempotencyService.releaseClaim(idempotencyKey);
			throw ex;
		}
	}

	/**
	 * Takes the tickets in Redis first, then writes the durable row.
	 *
	 * <p>That order is deliberate. Redis is the only place where checking and allocating can be
	 * one atomic step, so it decides. MySQL then records what was decided; if that insert fails
	 * the Redis hold is undone, so tickets are never left allocated to a hold nobody can find.
	 *
	 * <p>Not transactional on purpose: the row must be committed and visible by the time this
	 * returns, because Checkout may call back about it within seconds.
	 */
	private HoldView create(CreateHoldRequest request, String authenticatedUserSub) {
		// Verified first: nothing about the request is believed until the token proves the buyer
		// was admitted to this event and is still inside the selection window.
		AdmissionClaims claims = admissionTokenService.verify(request.admissionToken(), request.eventId(),
				authenticatedUserSub);
		// Refused before Redis, so a replayed token never takes tickets even for a moment. The
		// unique constraint in persist() still decides two attempts that pass here together.
		if (holdRepository.findByAdmissionId(claims.admissionId()).isPresent()) {
			throw new ApiException(ErrorCode.ADMISSION_ALREADY_HAS_HOLD);
		}

		EventSaleStatus sale = saleStatusRepository.findById(request.eventId())
				.orElseThrow(() -> new ApiException(ErrorCode.EVENT_NOT_FOUND));
		if (sale.getSaleStatus() == SaleStatus.CANCELLED) {
			throw new ApiException(ErrorCode.EVENT_CANCELLED);
		}
		CategoryInventory inventory = inventoryRepository
				.findById(new CategoryInventoryId(request.eventId(), request.categoryId()))
				.orElseThrow(() -> new ApiException(ErrorCode.CATEGORY_NOT_FOUND));

		Hold hold = buildHold(request, claims, inventory, authenticatedUserSub);
		allocateInRedis(hold);
		persist(hold);

		log.info("Created hold {} for {} ticket(s) in category {}", hold.getHoldId(), hold.getQuantity(),
				hold.getCategoryId());
		return toView(hold, now());
	}

	private Hold buildHold(CreateHoldRequest request, AdmissionClaims claims, CategoryInventory inventory,
			String authenticatedUserSub) {
		Instant now = now();
		Hold hold = new Hold();
		hold.setHoldId(UUID.randomUUID().toString());
		hold.setUserId(claims.userId());
		// The same buyer as user_id - the admission token was checked against it - kept as the
		// column ownership checks read.
		hold.setUserSub(authenticatedUserSub);
		hold.setEventId(request.eventId());
		hold.setCategoryId(request.categoryId());
		hold.setAdmissionId(claims.admissionId());
		hold.setQuantity(request.quantity());
		// The price is snapshotted here, so what Checkout bills cannot drift if the category
		// price changes while the buyer is paying. It arrives with EventPublished, never per hold.
		hold.setUnitAmount(inventory.getUnitAmount());
		hold.setTotalAmount(inventory.getUnitAmount() * request.quantity());
		hold.setCurrency(inventory.getCurrency());
		hold.setStatus(HoldStatus.ACTIVE);
		hold.setCorrelationId(CorrelationIdFilter.current());
		hold.setCreatedAt(now);
		hold.setPaymentExpiresAt(now.plus(properties.paymentDeadline()));
		hold.setHoldExpiresAt(now.plus(properties.holdTtl()));
		return hold;
	}

	/** Turns the script's refusal into the contract's status and wording. */
	private void allocateInRedis(Hold hold) {
		HoldRedisRepository.HoldOutcome outcome = holdRedis.takeHold(hold, properties.maxTicketsPerUser(),
				properties.holdTtl());
		if (outcome.status() == HoldRedisRepository.Status.NOT_INITIALISED
				&& rebuildService.rebuild(hold.getEventId())) {
			// The live state was lost and has just been put back from MySQL; this buyer should not
			// have to see a failure for that.
			outcome = holdRedis.takeHold(hold, properties.maxTicketsPerUser(), properties.holdTtl());
		}
		switch (outcome.status()) {
			case OK -> {
				return;
			}
			case EVENT_CANCELLED -> throw new ApiException(ErrorCode.EVENT_CANCELLED);
			case INSUFFICIENT_INVENTORY -> throw new ApiException(ErrorCode.INSUFFICIENT_INVENTORY,
					"Not enough tickets available in this category. " + outcome.detail() + " left.");
			case PURCHASE_LIMIT_EXCEEDED -> throw new ApiException(ErrorCode.PURCHASE_LIMIT_EXCEEDED,
					"A buyer may hold or buy at most " + properties.maxTicketsPerUser()
							+ " tickets per event. You may take " + outcome.detail() + " more.");
			// The counters are gone, which is a Redis problem rather than a sell-out.
			// InventoryRebuildService rebuilds them from MySQL instead of failing here.
			case NOT_INITIALISED -> throw new ApiException(ErrorCode.DEPENDENCY_UNAVAILABLE,
					"Live availability is unavailable for this event. Please retry shortly.");
			default -> throw new IllegalStateException("Unexpected hold outcome " + outcome.status());
		}
	}

	/** Redis already allocated the tickets, so any failure here must hand them back. */
	private void persist(Hold hold) {
		try {
			holdRepository.saveAndFlush(hold);
		}
		catch (DataIntegrityViolationException ex) {
			// The unique constraint on admission_id: this admission already holds tickets.
			holdRedis.reclaim(hold);
			throw new ApiException(ErrorCode.ADMISSION_ALREADY_HAS_HOLD);
		}
		catch (RuntimeException ex) {
			holdRedis.reclaim(hold);
			log.error("Could not store hold {}; its tickets were returned", hold.getHoldId(), ex);
			throw ex;
		}
	}

	/**
	 * A buyer reading their own hold.
	 *
	 * <p>Redis liveness decides whether an ACTIVE row is really still active: the sweep may not
	 * have run yet, and a buyer must never be shown a countdown for a hold that can no longer be
	 * confirmed. The row is left alone here - reporting is not the same as resolving, and only a
	 * caller that wins the claim may write a final status.
	 */
	public HoldView getHold(String holdId, String authenticatedUserSub) {
		Hold hold = findOwnHold(holdId, authenticatedUserSub);
		HoldStatus status = observedStatus(hold);
		return toView(hold, status, now());
	}

	/**
	 * A buyer releasing their own hold early, so the tickets go back immediately rather than
	 * sitting idle until the hold expires.
	 */
	public ReleaseView releaseHold(String holdId, String authenticatedUserSub) {
		Hold hold = findOwnHold(holdId, authenticatedUserSub);
		if (observedStatus(hold) != HoldStatus.ACTIVE) {
			throw new ApiException(ErrorCode.HOLD_NOT_ACTIVE);
		}
		// The claim comes first and decides everything. Losing it means the sweep or a
		// confirmation got there in the same instant, and this caller must change nothing.
		if (!holdRedis.reclaim(hold)) {
			throw new ApiException(ErrorCode.HOLD_NOT_ACTIVE);
		}
		int changed = holdRepository.transitionStatus(holdId, HoldStatus.ACTIVE, HoldStatus.CANCELLED);
		if (changed == 0) {
			// Impossible unless something ended this hold without winning the claim.
			log.error("Hold {} was claimed for release but its row was no longer ACTIVE", holdId);
		}
		log.info("Released hold {}, returning {} ticket(s) to category {}", holdId, hold.getQuantity(),
				hold.getCategoryId());
		return new ReleaseView(holdId, HoldStatus.CANCELLED, now());
	}

	/**
	 * The hold as Checkout needs it: the ids it builds an order from and the price snapshot it
	 * bills, since the frontend never decides the payable amount.
	 *
	 * <p>No ownership check, because an internal caller is not a buyer. These routes have no
	 * API Gateway route at all and must stay unreachable from a browser.
	 */
	public InternalHoldView getHoldForService(String holdId) {
		Hold hold = holdRepository.findById(holdId)
				.orElseThrow(() -> new ApiException(ErrorCode.HOLD_NOT_FOUND));
		return new InternalHoldView(hold.getHoldId(), hold.getUserId(), hold.getEventId(), hold.getCategoryId(),
				hold.getAdmissionId(), observedStatus(hold), hold.getQuantity(), hold.getUnitAmount(),
				hold.getTotalAmount(), hold.getCurrency(), hold.getOrderId(), hold.getPaymentExpiresAt(),
				hold.getHoldExpiresAt(), now());
	}

	/**
	 * Whether an admission ever produced a hold, which Queue asks before releasing a
	 * selection-timeout slot. Answers either way rather than 404, so Queue can tell an absent
	 * hold from an absent service.
	 */
	public AdmissionReservationView getAdmissionReservation(String admissionId) {
		return holdRepository.findByAdmissionId(admissionId)
				.map(hold -> AdmissionReservationView.of(admissionId, hold.getHoldId(), observedStatus(hold)))
				.orElseGet(() -> AdmissionReservationView.none(admissionId));
	}

	/**
	 * Checkout confirming a hold after a verified payment. Safe to call more than once: section 7
	 * requires Checkout to retry a timed-out confirmation rather than assume it failed, so the
	 * same call can legitimately arrive twice.
	 *
	 * <p>What the response means to Checkout matters more than usual here. 200 is success,
	 * duplicate or not. 409 HOLD_EXPIRED or EVENT_CANCELLED means record a refund obligation,
	 * so neither may be returned while a confirmation might still succeed. 503 means retry, and
	 * is the honest answer when another confirmation for this hold is in flight.
	 */
	public ConfirmView confirmHold(String holdId, ConfirmRequest request) {
		Hold hold = holdRepository.findById(holdId)
				.orElseThrow(() -> new ApiException(ErrorCode.HOLD_NOT_FOUND));

		// A retry of a confirmation that already landed is a success, so this is checked before
		// anything about liveness: by now the hold's Redis key is deliberately gone.
		ConfirmView alreadyDone = asSettledConfirmation(hold, request.orderId());
		if (alreadyDone != null) {
			return alreadyDone;
		}
		// Before the status check, so a hold the cancellation already ended is reported as the
		// cancellation it was rather than as an expiry. Both mean Checkout refunds.
		requireEventStillSelling(hold.getEventId());
		if (hold.getStatus() != HoldStatus.ACTIVE) {
			// Released or swept before the payment arrived: Checkout must refund.
			throw new ApiException(ErrorCode.HOLD_EXPIRED,
					"The hold expired or was released before payment arrived.");
		}

		// The claim decides, and it checks liveness in the same atomic step, so the sweep cannot
		// expire this hold between asking whether it is alive and acting on it.
		HoldRedisRepository.Status claim = holdRedis.confirm(hold, request.orderId());
		if (claim == HoldRedisRepository.Status.NOT_ALIVE) {
			throw new ApiException(ErrorCode.HOLD_EXPIRED,
					"The hold expired or was released before payment arrived.");
		}
		if (claim == HoldRedisRepository.Status.ALREADY_RECLAIMED) {
			return resolveLostClaim(holdId, request.orderId());
		}

		Instant confirmedAt = now();
		int changed;
		try {
			changed = writeConfirmation(hold, request, confirmedAt);
		}
		catch (RuntimeException ex) {
			// The claim stays. A failed write may still have committed - a timeout says nothing
			// either way - and putting the hold back would let the sweep return tickets that were
			// sold after all. Checkout's retry carries on from the claim; if none comes,
			// reconciliation ends the row and only then returns the tickets.
			log.error("Could not record the confirmation of hold {}; Checkout's retry will finish it", holdId, ex);
			throw ex;
		}
		if (changed == 0) {
			// Nothing should be able to transition this hold without the claim we just won.
			log.error("Hold {} was claimed for confirmation but its row was no longer ACTIVE", holdId);
			return resolveLostClaim(holdId, request.orderId());
		}
		log.info("Confirmed hold {} for order {}: {} ticket(s) in category {} are now sold", holdId,
				request.orderId(), hold.getQuantity(), hold.getCategoryId());
		recordRefundIfCancelledMeanwhile(holdRepository.findById(holdId).orElseThrow());
		return new ConfirmView(holdId, request.orderId(), HoldStatus.CONFIRMED, hold.getQuantity(), false,
				confirmedAt);
	}

	/**
	 * The status change and the sold count commit together. Were they separate, a failure between
	 * them would leave a sold hold uncounted, and the rebuild from MySQL would then hand the same
	 * tickets out again.
	 *
	 * <p>Availability is deliberately not touched: those tickets left the counter when the hold
	 * was taken and have merely turned from held into sold.
	 *
	 * <p>The sell-out is decided here too, because only sold tickets never come back: a hold that
	 * empties the counter may still expire, and Queue closes an event for good on EventSoldOut.
	 * Only a confirmation that fills its own category looks at the others, so the ordinary ones
	 * lock nothing beyond their own row. That one locks the event's rows (read as last committed)
	 * after its own update. Two confirmations filling the last two categories at once then
	 * deadlock; MySQL rolls one back, Checkout retries it, and the retry sees both full and
	 * announces it - so it is announced exactly once.
	 */
	private int writeConfirmation(Hold hold, ConfirmRequest request, Instant confirmedAt) {
		return transactions.execute(status -> {
			int changed = holdRepository.confirmIfActive(hold.getHoldId(), HoldStatus.ACTIVE,
					HoldStatus.CONFIRMED, request.orderId(), request.paymentReference(), confirmedAt);
			if (changed > 0) {
				inventoryRepository.addSold(hold.getEventId(), hold.getCategoryId(), hold.getQuantity());
				if (soldOut(hold.getEventId(), hold.getCategoryId())) {
					outbox.eventSoldOut(hold);
				}
			}
			return changed;
		});
	}

	/** True when every category is now sold, checked only once this hold's own category is. */
	private boolean soldOut(String eventId, String categoryId) {
		boolean ownFull = inventoryRepository.findById(new CategoryInventoryId(eventId, categoryId))
				.map(own -> own.getSoldQuantity() >= own.getTotalQuantity())
				.orElse(false);
		if (!ownFull) {
			return false;
		}
		return inventoryRepository.lockByEventId(eventId).stream()
				.allMatch(category -> category.getSoldQuantity() >= category.getTotalQuantity());
	}

	/**
	 * A confirmation that has already happened: the same order succeeds again, another clashes.
	 * The cancellation check is repeated, because the attempt that confirmed may have failed
	 * before it could make it, and this retry is then the only one left to.
	 */
	private ConfirmView asSettledConfirmation(Hold hold, String orderId) {
		if (hold.getStatus() != HoldStatus.CONFIRMED) {
			return null;
		}
		if (!orderId.equals(hold.getOrderId())) {
			// Section 7: reject and flag for reconciliation rather than quietly accepting.
			log.error("Hold {} is already confirmed for order {} but order {} tried to confirm it too",
					hold.getHoldId(), hold.getOrderId(), orderId);
			throw new ApiException(ErrorCode.ORDER_MISMATCH);
		}
		recordRefundIfCancelledMeanwhile(hold);
		return new ConfirmView(hold.getHoldId(), hold.getOrderId(), HoldStatus.CONFIRMED, hold.getQuantity(),
				true, hold.getConfirmedAt());
	}

	/**
	 * Someone else held the claim. Re-reading the row says who: a retry of this same order, a
	 * different order, or the sweep having expired the hold first.
	 */
	private ConfirmView resolveLostClaim(String holdId, String orderId) {
		Hold current = holdRepository.findById(holdId)
				.orElseThrow(() -> new ApiException(ErrorCode.HOLD_NOT_FOUND));
		ConfirmView settled = asSettledConfirmation(current, orderId);
		if (settled != null) {
			return settled;
		}
		if (current.getStatus() == HoldStatus.ACTIVE) {
			// A confirmation is mid-flight. Saying HOLD_EXPIRED here would make Checkout refund a
			// payment that is about to be accepted, so ask it to retry instead.
			throw new ApiException(ErrorCode.DEPENDENCY_UNAVAILABLE,
					"A confirmation for this hold is already in progress. Please retry shortly.");
		}
		throw new ApiException(ErrorCode.HOLD_EXPIRED, "The hold expired or was released before payment arrived.");
	}

	/**
	 * The event may have been cancelled between the sale check above and this commit, after the
	 * cancellation had already passed over this hold while it still read ACTIVE. Read after our
	 * own write, this is the half of the handshake EventCancellationService relies on: one side
	 * or the other always sees the paid hold and owes the refund. The payment did happen, so the
	 * confirmation still stands and Checkout is told 200.
	 */
	private void recordRefundIfCancelledMeanwhile(Hold confirmed) {
		boolean cancelled = saleStatusRepository.findById(confirmed.getEventId())
				.map(sale -> sale.getSaleStatus() == SaleStatus.CANCELLED)
				.orElse(false);
		if (cancelled && outbox.refundRequired(confirmed)) {
			log.warn("Hold {} was confirmed as event {} was being cancelled; a refund is owed for order {}",
					confirmed.getHoldId(), confirmed.getEventId(), confirmed.getOrderId());
		}
	}

	private void requireEventStillSelling(String eventId) {
		EventSaleStatus sale = saleStatusRepository.findById(eventId)
				.orElseThrow(() -> new ApiException(ErrorCode.EVENT_NOT_FOUND));
		if (sale.getSaleStatus() == SaleStatus.CANCELLED) {
			// Checkout records a refund obligation; EventCancellationService resolves the hold.
			throw new ApiException(ErrorCode.EVENT_CANCELLED);
		}
	}

	/**
	 * Another buyer's hold is reported as 404 rather than 403, because a 403 would confirm that
	 * the hold exists.
	 */
	private Hold findOwnHold(String holdId, String authenticatedUserSub) {
		if (!StringUtils.hasText(authenticatedUserSub)) {
			throw new ApiException(ErrorCode.UNAUTHENTICATED);
		}
		Hold hold = holdRepository.findById(holdId).orElseThrow(() -> new ApiException(ErrorCode.HOLD_NOT_FOUND));
		if (!authenticatedUserSub.equals(hold.getUserSub())) {
			throw new ApiException(ErrorCode.HOLD_NOT_FOUND);
		}
		return hold;
	}

	/** What the hold really is now, rather than what its row was last told. */
	private HoldStatus observedStatus(Hold hold) {
		if (hold.getStatus() != HoldStatus.ACTIVE) {
			return hold.getStatus();
		}
		return holdRedis.isAlive(hold.getEventId(), hold.getHoldId()) ? HoldStatus.ACTIVE : HoldStatus.EXPIRED;
	}

	private static HoldView toView(Hold hold, Instant serverTime) {
		return toView(hold, hold.getStatus(), serverTime);
	}

	private static HoldView toView(Hold hold, HoldStatus status, Instant serverTime) {
		return new HoldView(hold.getHoldId(), hold.getAdmissionId(), status, hold.getQuantity(),
				hold.getUnitAmount(), hold.getTotalAmount(), hold.getCurrency(), hold.getPaymentExpiresAt(),
				hold.getHoldExpiresAt(), serverTime);
	}

	private Instant now() {
		return clock.instant().truncatedTo(ChronoUnit.SECONDS);
	}
}
