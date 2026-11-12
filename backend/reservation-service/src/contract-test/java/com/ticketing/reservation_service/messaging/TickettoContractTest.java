package com.ticketing.reservation_service.messaging;

import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.clearInvocations;
import static org.mockito.Mockito.reset;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

import org.mockito.ArgumentCaptor;
import org.springframework.amqp.listener.ConditionalRejectingErrorHandler;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.rabbit.listener.RabbitListenerEndpointRegistry;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.amqp.autoconfigure.RabbitAutoConfiguration;
import org.springframework.boot.autoconfigure.ImportAutoConfiguration;
import org.springframework.boot.jackson.autoconfigure.JacksonAutoConfiguration;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.validation.autoconfigure.ValidationAutoConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionCallback;
import org.springframework.transaction.support.TransactionTemplate;

import com.ticketing.reservation_service.config.ClockConfig;
import com.ticketing.reservation_service.config.RabbitConfig;
import com.ticketing.reservation_service.dto.CancellationView;
import com.ticketing.reservation_service.model.Hold;
import com.ticketing.reservation_service.model.HoldStatus;
import com.ticketing.reservation_service.model.OutboxMessage;
import com.ticketing.reservation_service.model.SaleStatus;
import com.ticketing.reservation_service.repository.OutboxRepository;
import com.ticketing.reservation_service.service.EventCancellationService;
import com.ticketing.reservation_service.service.InventoryService;
import com.ticketing.reservation_service.service.OutboxWriter;
import com.ticketto.contracts.ContractAdapter;
import com.ticketto.contracts.ContractTests;
import com.ticketto.contracts.Outcome;
import com.ticketto.contracts.PublishRecorder;
import com.ticketto.contracts.spring.SpringAmqpSupport;

/**
 * Our side of the AMQP message contracts, checked with rabbitmq-service's ticketto-contracts
 * package (the contract-tests Maven profile; see README.md, "Contract tests"). The tests are
 * generated from its catalogue:
 * <ul>
 * <li>publishes: ReservationExpired, RefundRequired and EventSoldOut, each written by the real
 * OutboxWriter and sent by the real OutboxPublisher through the RabbitTemplate Spring Boot builds
 * from application.properties, onto a recording connection instead of a broker: exchange, routing
 * key, mandatory, publisher confirms, the AMQP properties, the envelope and the payload against its
 * schema</li>
 * <li>listensOnCataloguedQueues: the @RabbitListener queues are exactly ours</li>
 * <li>accepts: EventMessageListener acks EventPublished and EventCancelled with only the fields
 * their publisher always sends, ignores unknown fields, and dead-letters a body missing a required
 * field</li>
 * </ul>
 *
 * <p>A slice of the application: the messaging beans and RabbitMQ's, Jackson's and validation's
 * auto-configuration, under the test profile (listeners never start). What sits behind the
 * messaging (MySQL, Redis) is mocked, so no service has to be running.
 */
@SpringBootTest(classes = TickettoContractTest.Messaging.class)
@ImportAutoConfiguration({ RabbitAutoConfiguration.class, JacksonAutoConfiguration.class,
		ValidationAutoConfiguration.class })
@ActiveProfiles("test")
class TickettoContractTest extends ContractTests {

	@Configuration
	@Import({ RabbitConfig.class, ClockConfig.class, OutboxWriter.class, OutboxPublisher.class,
			EventMessageListener.class })
	static class Messaging {

		/** Runs the publisher's batch as it is: there is no database transaction to open. */
		@Bean
		TransactionTemplate transactionTemplate() {
			return new TransactionTemplate() {
				@Override
				public void afterPropertiesSet() {
					// no transaction manager: there is no database here
				}

				@Override
				public <T> T execute(TransactionCallback<T> action) {
					return action.doInTransaction(new SimpleTransactionStatus());
				}
			};
		}
	}

	@MockitoBean
	private OutboxRepository outboxRepository;

	@MockitoBean
	private InventoryService inventoryService;

	@MockitoBean
	private EventCancellationService cancellationService;

	@Autowired
	private OutboxWriter outboxWriter;

	@Autowired
	private OutboxPublisher outboxPublisher;

	@Autowired
	private RabbitTemplate rabbitTemplate;

	@Autowired
	private RabbitListenerEndpointRegistry listeners;

	@Autowired
	private Clock clock;

	/** The listener container setting from application.properties. */
	@Value("${spring.rabbitmq.listener.simple.default-requeue-rejected:true}")
	private boolean defaultRequeueRejected;

	@Override
	protected String service() {
		return RabbitConfig.PRODUCER;
	}

	@Override
	protected Set<String> bodies() {
		return Set.of("extra-fields", "invalid");
	}

	@Override
	protected ContractAdapter adapter() {
		return new ContractAdapter() {
			@Override
			public void publish(String message, PublishRecorder recorder) {
				publishThroughTheOutbox(message, recorder);
			}

			@Override
			public Set<String> listenerQueues() {
				return SpringAmqpSupport.listenerQueues(listeners);
			}

			@Override
			public Outcome handle(String queue, byte[] body, Map<String, Object> properties) {
				when(cancellationService.cancelFromEventCancelled(anyString())).thenAnswer(call -> new CancellationView(
						call.getArgument(0), SaleStatus.CANCELLED, 0, 0, Instant.now(clock)));
				return SpringAmqpSupport.handle(listeners, queue, body, properties,
						new ConditionalRejectingErrorHandler.DefaultExceptionStrategy(), defaultRequeueRejected);
			}
		};
	}

	/**
	 * The real OutboxWriter records the message (its row captured from the mocked repository),
	 * then the real OutboxPublisher sends that row through the real RabbitTemplate.
	 */
	private void publishThroughTheOutbox(String message, PublishRecorder recorder) {
		reset(outboxRepository);
		Hold hold = confirmedHold();
		switch (message) {
			case OutboxWriter.RESERVATION_EXPIRED -> outboxWriter.reservationExpired(hold);
			case OutboxWriter.REFUND_REQUIRED -> outboxWriter.refundRequired(hold);
			case OutboxWriter.EVENT_SOLD_OUT -> outboxWriter.eventSoldOut(hold);
			default -> throw new IllegalArgumentException("OutboxWriter writes no " + message);
		}
		OutboxMessage row = recordedRow();
		clearInvocations(outboxRepository);
		when(outboxRepository.lockUnpublished(anyInt(), anyInt())).thenReturn(List.of(row));

		ConnectionFactory real = rabbitTemplate.getConnectionFactory();
		rabbitTemplate.setConnectionFactory(SpringAmqpSupport.recordingConnectionFactory(recorder, real));
		try {
			outboxPublisher.publishBatch();
		}
		finally {
			rabbitTemplate.setConnectionFactory(real);
		}
	}

	private OutboxMessage recordedRow() {
		ArgumentCaptor<String> messageId = ArgumentCaptor.forClass(String.class);
		ArgumentCaptor<String> eventType = ArgumentCaptor.forClass(String.class);
		ArgumentCaptor<String> schemaVersion = ArgumentCaptor.forClass(String.class);
		ArgumentCaptor<String> aggregateId = ArgumentCaptor.forClass(String.class);
		ArgumentCaptor<String> payload = ArgumentCaptor.forClass(String.class);
		ArgumentCaptor<Instant> occurredAt = ArgumentCaptor.forClass(Instant.class);
		ArgumentCaptor<String> correlationId = ArgumentCaptor.forClass(String.class);
		verify(outboxRepository).insert(messageId.capture(), eventType.capture(), schemaVersion.capture(),
				aggregateId.capture(), payload.capture(), occurredAt.capture(), correlationId.capture());
		OutboxMessage row = new OutboxMessage();
		row.setMessageId(messageId.getValue());
		row.setEventType(eventType.getValue());
		row.setSchemaVersion(schemaVersion.getValue());
		row.setAggregateId(aggregateId.getValue());
		row.setPayload(payload.getValue());
		row.setOccurredAt(occurredAt.getValue());
		row.setCorrelationId(correlationId.getValue());
		return row;
	}

	private static Hold confirmedHold() {
		Hold hold = new Hold();
		hold.setHoldId(UUID.randomUUID().toString());
		hold.setUserId(UUID.randomUUID().toString());
		hold.setEventId(UUID.randomUUID().toString());
		hold.setCategoryId(UUID.randomUUID().toString());
		hold.setAdmissionId(UUID.randomUUID().toString());
		hold.setOrderId(UUID.randomUUID().toString());
		hold.setQuantity(2);
		hold.setUnitAmount(28800);
		hold.setTotalAmount(57600);
		hold.setCurrency("SGD");
		hold.setStatus(HoldStatus.CONFIRMED);
		hold.setCorrelationId(UUID.randomUUID().toString());
		return hold;
	}
}
