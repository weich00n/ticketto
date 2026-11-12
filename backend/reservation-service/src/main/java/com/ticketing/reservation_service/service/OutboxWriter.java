package com.ticketing.reservation_service.service;

import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.temporal.ChronoUnit;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Service;

import com.ticketing.reservation_service.config.CorrelationIdFilter;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.repository.OutboxRepository;

import tools.jackson.databind.json.JsonMapper;

/**
 * Records the messages this service owes other services, for {@code OutboxPublisher} to send.
 * Only the three another service consumes, one for each sad path: Checkout hears of an unpaid
 * hold expiring and of a paid hold whose event was cancelled, and Queue hears of a sell-out.
 *
 * <p>Only the message's payload is stored. The publisher wraps it in the broker's envelope when
 * it sends it, from the row's own id, type and time, and sends the correlation id beside it.
 *
 * <p>Each message id is derived from its type and hold, so recording the same fact twice - a
 * cancellation re-run, or a confirmation and a cancellation both noticing the same refund -
 * leaves exactly one message.
 */
@Service
public class OutboxWriter {

	public static final String RESERVATION_EXPIRED = "ReservationExpired";

	public static final String REFUND_REQUIRED = "RefundRequired";

	public static final String EVENT_SOLD_OUT = "EventSoldOut";

	/** Why a hold was refunded; the only reason there is. */
	public static final String EVENT_CANCELLED = "EVENT_CANCELLED";

	private static final String SCHEMA_VERSION = "1";

	private final OutboxRepository outboxRepository;

	private final JsonMapper jsonMapper;

	private final Clock clock;

	public OutboxWriter(OutboxRepository outboxRepository, JsonMapper jsonMapper, Clock clock) {
		this.outboxRepository = outboxRepository;
		this.jsonMapper = jsonMapper;
		this.clock = clock;
	}

	/** Nobody paid in time; the sweep has returned the tickets and the hold can never be confirmed. */
	public boolean reservationExpired(Hold hold) {
		Map<String, Object> data = new LinkedHashMap<>();
		data.put("hold_id", hold.getHoldId());
		data.put("admission_id", hold.getAdmissionId());
		data.put("user_id", hold.getUserId());
		data.put("event_id", hold.getEventId());
		data.put("category_id", hold.getCategoryId());
		data.put("quantity", hold.getQuantity());
		data.put("expired_at", now());
		return write(RESERVATION_EXPIRED, hold, data);
	}

	/**
	 * A paid hold whose event was cancelled. The tickets stay sold - the payment happened - and
	 * Checkout refunds the order.
	 *
	 * @return true if this call recorded the obligation, false if it was already recorded
	 */
	public boolean refundRequired(Hold hold) {
		Map<String, Object> data = new LinkedHashMap<>();
		data.put("hold_id", hold.getHoldId());
		data.put("order_id", hold.getOrderId());
		data.put("user_id", hold.getUserId());
		data.put("event_id", hold.getEventId());
		data.put("total_amount", hold.getTotalAmount());
		data.put("currency", hold.getCurrency());
		data.put("reason", EVENT_CANCELLED);
		data.put("required_at", now());
		return write(REFUND_REQUIRED, hold, data);
	}

	/**
	 * The confirmation that sold the event's last ticket, so Queue can close the event for good.
	 *
	 * <p>Final, because sold tickets only come back if the event is cancelled. Written inside the
	 * confirmation's transaction. The id comes from the confirmed hold, which is confirmed once,
	 * and the message is filed under the event rather than the hold.
	 */
	public boolean eventSoldOut(Hold lastHold) {
		Map<String, Object> data = new LinkedHashMap<>();
		data.put("event_id", lastHold.getEventId());
		data.put("sold_out_at", now());
		return write(EVENT_SOLD_OUT, lastHold.getEventId(), lastHold, data);
	}

	private boolean write(String eventType, Hold hold, Map<String, Object> data) {
		return write(eventType, hold.getHoldId(), hold, data);
	}

	private boolean write(String eventType, String aggregateId, Hold hold, Map<String, Object> data) {
		String messageId = UUID.nameUUIDFromBytes((eventType + ":" + hold.getHoldId())
				.getBytes(StandardCharsets.UTF_8)).toString();
		if (outboxRepository.existsById(messageId)) {
			return false;
		}
		try {
			outboxRepository.insert(messageId, eventType, SCHEMA_VERSION, aggregateId,
					jsonMapper.writeValueAsString(data), clock.instant().truncatedTo(ChronoUnit.SECONDS),
					correlationId(hold));
			return true;
		}
		catch (DataIntegrityViolationException ex) {
			// Written by the other side of the race between the check and the insert. Only a
			// refund can race like this - a confirmation and a cancellation both seeing it - and
			// neither calls this inside a wider transaction, so nothing else is rolled back. The
			// other messages are written once, by whoever won the hold's claim: EventSoldOut,
			// the one written inside a transaction, by the single confirmation of its hold.
			return false;
		}
	}

	/**
	 * The request that caused this message, or failing that the request that created the hold:
	 * the expiry sweep and the message consumers run outside any request, and a message must
	 * always carry a correlation id.
	 */
	private static String correlationId(Hold hold) {
		String current = CorrelationIdFilter.current();
		return (current != null) ? current : hold.getCorrelationId();
	}

	private String now() {
		return clock.instant().truncatedTo(ChronoUnit.SECONDS).toString();
	}
}
