package com.ticketto.contracts.spring;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.platform.engine.discovery.DiscoverySelectors.selectClass;

import com.ticketto.contracts.Bodies;
import com.ticketto.contracts.ContractAdapter;
import com.ticketto.contracts.ContractTests;
import com.ticketto.contracts.Outcome;
import com.ticketto.contracts.PublishAssertions;
import com.ticketto.contracts.PublishRecorder;
import com.ticketto.contracts.PublishedMessage;
import java.util.Map;
import java.util.Set;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.platform.testkit.engine.EngineTestKit;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.amqp.rabbit.listener.RabbitListenerEndpointRegistry;
import org.springframework.amqp.rabbit.listener.SimpleMessageListenerContainer;
import org.springframework.amqp.support.converter.MessageConversionException;
import org.springframework.context.annotation.AnnotationConfigApplicationContext;

class SpringAmqpSupportTest {
    @AfterEach
    void reset() {
        SampleReservationApp.mandatory = true;
        SampleReservationApp.returnsCallback = true;
        SampleReservationApp.confirms = true;
    }

    /** The adapter a Spring service writes: its real RabbitTemplate and @RabbitListeners. */
    static class SampleAdapter implements ContractAdapter, AutoCloseable {
        final AnnotationConfigApplicationContext context =
                new AnnotationConfigApplicationContext(SampleReservationApp.class);

        @Override
        public void publish(String message, PublishRecorder recorder) {
            RabbitTemplate template = context.getBean(RabbitTemplate.class);
            ConnectionFactory real = template.getConnectionFactory();
            template.setConnectionFactory(SpringAmqpSupport.recordingConnectionFactory(recorder, real));
            try {
                context.getBean(SampleReservationApp.OutboxRelay.class).publish(message);
            } catch (java.io.IOException e) {
                throw new IllegalStateException(e);
            } finally {
                template.setConnectionFactory(real);
            }
        }

        @Override
        public Set<String> listenerQueues() {
            return SpringAmqpSupport.listenerQueues(context.getBean(RabbitListenerEndpointRegistry.class));
        }

        @Override
        public Outcome handle(String queue, byte[] body, Map<String, Object> properties) {
            return SpringAmqpSupport.handle(context.getBean(RabbitListenerEndpointRegistry.class), queue, body, properties);
        }

        @Override
        public void close() {
            context.close();
        }
    }

    static class SampleReservationContractTest extends ContractTests {
        @Override protected String service() { return "reservation-service"; }
        @Override protected ContractAdapter adapter() { return new SampleAdapter(); }
        @Override protected Set<String> bodies() { return Set.of("extra-fields", "invalid"); }
    }

    private static org.junit.platform.testkit.engine.Events runSample() {
        return EngineTestKit.engine("junit-jupiter").selectors(selectClass(SampleReservationContractTest.class))
                .execute().testEvents();
    }

    @Test
    void theSampleServicePassesItsGeneratedContractTests() {
        // 3 publishes + listensOn + 2 consumed messages x 3 bodies (one AUTO-ack, one MANUAL)
        runSample().assertStatistics(s -> s.succeeded(10).failed(0));
    }

    @Test
    void aTemplateWithoutMandatoryFailsEveryPublish() {
        SampleReservationApp.mandatory = false;
        runSample().assertStatistics(s -> s.succeeded(7).failed(3));
    }

    @Test
    void mandatoryWithoutAReturnsCallbackIsNotSentSoFailsEveryPublish() {
        // Spring's RabbitTemplate drops mandatory unless a ReturnsCallback is set: the recording
        // shows what the template really sends.
        SampleReservationApp.returnsCallback = false;
        runSample().assertStatistics(s -> s.succeeded(7).failed(3));
    }

    @Test
    void aFactoryWithoutPublisherConfirmsFailsEveryPublish() {
        SampleReservationApp.confirms = false;
        runSample().assertStatistics(s -> s.succeeded(7).failed(3));
    }

    @Test
    void theRealTemplatesPublishIsRecorded() {
        try (SampleAdapter adapter = new SampleAdapter()) {
            PublishRecorder recorder = new PublishRecorder("reservation-service");
            adapter.publish("EventSoldOut", recorder);
            PublishedMessage published = recorder.published().get(0);
            assertEquals("reservation.events", published.exchange());
            assertEquals("event.sold_out", published.routingKey());
            assertTrue(published.mandatory());
            assertTrue(published.confirmed());
            assertEquals(2, published.properties().get("delivery_mode"));
            assertEquals(java.util.List.of(), PublishAssertions.checkPublish("reservation-service", published));
        }
    }

    @Test
    void aRelayWaitingForItsConfirmGetsAnAck() throws Exception {
        // Like a relay that marks its outbox row sent only once the broker confirms it.
        try (SampleAdapter adapter = new SampleAdapter()) {
            RabbitTemplate template = adapter.context.getBean(RabbitTemplate.class);
            PublishRecorder recorder = new PublishRecorder("reservation-service");
            template.setConnectionFactory(SpringAmqpSupport.recordingConnectionFactory(recorder, template.getConnectionFactory()));
            org.springframework.amqp.rabbit.connection.CorrelationData confirmation =
                    new org.springframework.amqp.rabbit.connection.CorrelationData("m-1");
            Bodies.Delivery delivery = Bodies.delivery("EventSoldOut", "example");
            template.send("reservation.events", "event.sold_out",
                    SpringAmqpSupport.message("unused", delivery.body(), delivery.properties()), confirmation);
            assertTrue(confirmation.getFuture().get(1, java.util.concurrent.TimeUnit.SECONDS).ack());
            assertEquals(1, recorder.published().size());
        }
    }

    @Test
    void channelDeclaresAreChecked() throws Exception {
        PublishRecorder recorder = new PublishRecorder("reservation-service");
        com.rabbitmq.client.Channel channel = SpringAmqpSupport.recordingChannel(recorder);
        channel.exchangeDeclare("reservation.events", "topic", true);
        channel.queueDeclarePassive("reservation-service.EventPublished");
        assertEquals(java.util.List.of(), recorder.violations());
        channel.queueDeclare("reservation-service.mine", true, false, false, Map.of());
        channel.exchangeDeclare("reservation.events", "direct", true, false, Map.of());
        assertEquals(java.util.List.of("queue_declare", "exchange_declare"),
                recorder.violations().stream().map(com.ticketto.contracts.Problem::code).toList());
    }

    @Test
    void listenerExceptionsMapToWhatTheContainerWouldDo() {
        try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext(SampleReservationApp.class)) {
            RabbitListenerEndpointRegistry registry = context.getBean(RabbitListenerEndpointRegistry.class);
            SimpleMessageListenerContainer container = (SimpleMessageListenerContainer) registry.getListenerContainer("eventPublished");
            String queue = "reservation-service.EventPublished";
            Bodies.Delivery delivery = Bodies.delivery("EventPublished", "minimal");

            container.setMessageListener(m -> {
                throw new IllegalStateException("database down");
            });
            assertEquals(Outcome.REQUEUE, SpringAmqpSupport.handle(registry, queue, delivery.body(), delivery.properties()));

            container.setMessageListener(m -> {
                throw new MessageConversionException("bad JSON");
            });
            assertEquals(Outcome.REJECT, SpringAmqpSupport.handle(registry, queue, delivery.body(), delivery.properties()));

            container.setMessageListener(m -> {
                throw new IllegalStateException("database down");
            });
            assertEquals(Outcome.REJECT, SpringAmqpSupport.handle(registry, queue, delivery.body(), delivery.properties(),
                    new org.springframework.amqp.listener.ConditionalRejectingErrorHandler.DefaultExceptionStrategy(), false));
        }
    }

    @Test
    void declarableBeansAreReported() {
        try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext()) {
            context.register(SampleReservationApp.class);
            context.registerBean("myQueue", Queue.class, () -> new Queue("reservation-service.mine"));
            context.refresh();
            assertEquals(1, SpringAmqpSupport.declarableProblems(context).size());
        }
        try (AnnotationConfigApplicationContext context = new AnnotationConfigApplicationContext(SampleReservationApp.class)) {
            assertTrue(SpringAmqpSupport.declarableProblems(context).isEmpty());
        }
    }
}
