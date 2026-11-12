package com.ticketto.contracts.spring;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.rabbitmq.client.Channel;
import com.ticketto.contracts.Catalogue;
import com.ticketto.contracts.Message;
import java.io.IOException;
import org.springframework.amqp.AmqpRejectAndDontRequeueException;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.rabbit.annotation.EnableRabbit;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.amqp.rabbit.config.SimpleRabbitListenerContainerFactory;
import org.springframework.amqp.rabbit.connection.CachingConnectionFactory;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * A small Spring AMQP service shaped like reservation-service, for testing SpringAmqpSupport: it
 * publishes through a RabbitTemplate and consumes with @RabbitListener. Nothing connects to a
 * broker: listener containers don't start, and the tests swap the template's connection factory.
 */
@Configuration
@EnableRabbit
class SampleReservationApp {
    static final ObjectMapper JSON = new ObjectMapper();

    /** Switches for the broken variants the tests check. */
    static boolean mandatory = true;
    static boolean returnsCallback = true;
    static boolean confirms = true;

    @Bean
    CachingConnectionFactory connectionFactory() {
        CachingConnectionFactory factory = new CachingConnectionFactory("localhost");
        if (confirms) {
            factory.setPublisherConfirmType(CachingConnectionFactory.ConfirmType.CORRELATED);
        }
        factory.setPublisherReturns(true); // unroutable mandatory messages come back to the callback
        return factory;
    }

    @Bean
    RabbitTemplate rabbitTemplate(ConnectionFactory connectionFactory) {
        RabbitTemplate template = new RabbitTemplate(connectionFactory);
        template.setMandatory(mandatory);
        // RabbitTemplate only sends mandatory=true when it has a ReturnsCallback.
        if (returnsCallback) {
            template.setReturnsCallback(returned -> { });
        }
        return template;
    }

    @Bean
    SimpleRabbitListenerContainerFactory rabbitListenerContainerFactory(ConnectionFactory connectionFactory) {
        SimpleRabbitListenerContainerFactory factory = new SimpleRabbitListenerContainerFactory();
        factory.setConnectionFactory(connectionFactory);
        factory.setAutoStartup(false);
        return factory;
    }

    @Bean
    OutboxRelay outboxRelay(RabbitTemplate template) {
        return new OutboxRelay(template);
    }

    @Bean
    Listeners listeners() {
        return new Listeners();
    }

    /** Publishes one outbox row: here, the message's example, as the real relay would. */
    static class OutboxRelay {
        private final RabbitTemplate template;

        OutboxRelay(RabbitTemplate template) {
            this.template = template;
        }

        void publish(String messageType) throws IOException {
            Message message = Catalogue.message(messageType).orElseThrow();
            JsonNode body = Catalogue.example(messageType);
            MessageProperties props = new MessageProperties();
            props.setContentType(MessageProperties.CONTENT_TYPE_JSON);
            props.setDeliveryMode(MessageDeliveryMode.PERSISTENT);
            props.setMessageId(body.get("message_id").asText());
            props.setType(messageType);
            props.setCorrelationId("corr-sample");
            template.send(message.exchange(), message.routingKey(),
                    new org.springframework.amqp.core.Message(JSON.writeValueAsBytes(body), props));
        }
    }

    static class Listeners {
        /** AUTO acknowledgement: a payload without event_id can never be used, so it's rejected. */
        @RabbitListener(id = "eventPublished", queues = "reservation-service.EventPublished")
        void eventPublished(org.springframework.amqp.core.Message message) throws IOException {
            JsonNode payload = JSON.readTree(message.getBody()).path("payload");
            if (!payload.hasNonNull("event_id")) {
                throw new AmqpRejectAndDontRequeueException("EventPublished without event_id");
            }
        }

        /** MANUAL acknowledgement: the listener settles the delivery itself. */
        @RabbitListener(id = "eventCancelled", queues = "reservation-service.EventCancelled", ackMode = "MANUAL")
        void eventCancelled(org.springframework.amqp.core.Message message, Channel channel) throws IOException {
            long tag = message.getMessageProperties().getDeliveryTag();
            JsonNode payload = JSON.readTree(message.getBody()).path("payload");
            if (payload.hasNonNull("event_id")) {
                channel.basicAck(tag, false);
            } else {
                channel.basicReject(tag, false);
            }
        }
    }
}
