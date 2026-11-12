package com.ticketto.contracts.spring;

import com.rabbitmq.client.AMQP;
import com.rabbitmq.client.Channel;
import com.ticketto.contracts.Outcome;
import com.ticketto.contracts.Problem;
import com.ticketto.contracts.PublishRecorder;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicLong;
import org.apache.commons.logging.Log;
import org.apache.commons.logging.LogFactory;
import org.springframework.amqp.core.AcknowledgeMode;
import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.Exchange;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.core.MessageDeliveryMode;
import org.springframework.amqp.core.MessageListener;
import org.springframework.amqp.core.MessageListenerContainer;
import org.springframework.amqp.core.MessageProperties;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.listener.ConditionalRejectingErrorHandler;
import org.springframework.amqp.listener.ContainerUtils;
import org.springframework.amqp.listener.FatalExceptionStrategy;
import org.springframework.amqp.listener.ListenerExecutionFailedException;
import org.springframework.amqp.rabbit.connection.ChannelProxy;
import org.springframework.amqp.rabbit.connection.Connection;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.amqp.rabbit.connection.PendingConfirm;
import org.springframework.amqp.rabbit.connection.PublisherCallbackChannel;
import org.springframework.amqp.rabbit.listener.AbstractMessageListenerContainer;
import org.springframework.amqp.rabbit.listener.RabbitListenerEndpointRegistry;
import org.springframework.amqp.rabbit.listener.api.ChannelAwareMessageListener;
import org.springframework.context.ApplicationContext;

/**
 * Spring AMQP helpers for a {@link com.ticketto.contracts.ContractAdapter}.
 *
 * <p>Publishing: {@link #recordingConnectionFactory} stands in for the service's real
 * connection factory, so its real RabbitTemplate publishes onto a {@link PublishRecorder}
 * instead of a broker. The template's own settings (mandatory, message properties, converter)
 * are what gets recorded. Publisher confirms come from the real factory's settings, because it is
 * the factory, not the template, that turns them on.
 *
 * <p>Consuming: {@link #listenerQueues} reads the queues of every {@code @RabbitListener}
 * container, and {@link #handle} runs the real listener for a queue on one delivery and reports
 * what the container would do with it.
 *
 * <p>Topology: {@link #declarableProblems} finds Queue, Exchange and Binding beans. The broker
 * owns the topology, so a service must not define them.
 */
public final class SpringAmqpSupport {
    private static final Log LOG = LogFactory.getLog(SpringAmqpSupport.class);

    private SpringAmqpSupport() {}

    // ---------- publishing ----------

    /**
     * A connection factory whose channels record onto {@code recorder}. Swap it in for the
     * duration of a publish:
     *
     * <pre>{@code
     * ConnectionFactory real = rabbitTemplate.getConnectionFactory();
     * rabbitTemplate.setConnectionFactory(SpringAmqpSupport.recordingConnectionFactory(recorder, real));
     * try { relay.publishPending(); } finally { rabbitTemplate.setConnectionFactory(real); }
     * }</pre>
     *
     * @param configured the service's real factory: publisher confirms are on if it enables them
     *     (correlated or simple). It is never connected.
     */
    public static ConnectionFactory recordingConnectionFactory(
            PublishRecorder recorder, ConnectionFactory configured) {
        boolean confirms = configured != null
                && (configured.isPublisherConfirms() || configured.isSimplePublisherConfirms());
        Connection connection = proxy(Connection.class, (self, method, args) -> switch (method.getName()) {
            case "createChannel" -> {
                if (confirms) {
                    recorder.confirmDelivery();
                }
                yield recordingChannel(recorder, confirms);
            }
            case "isOpen" -> true;
            default -> defaultValue(method);
        });
        return proxy(ConnectionFactory.class, (self, method, args) -> switch (method.getName()) {
            case "createConnection" -> connection;
            case "isPublisherConfirms" -> configured != null && configured.isPublisherConfirms();
            case "isSimplePublisherConfirms" -> configured != null && configured.isSimplePublisherConfirms();
            case "isPublisherReturns" -> configured != null && configured.isPublisherReturns();
            case "getHost" -> "recording";
            case "getVirtualHost" -> "/";
            case "getUsername" -> "recording";
            default -> defaultValue(method);
        });
    }

    /**
     * A RabbitMQ channel that records publishes and declares onto {@code recorder}. It is a
     * Spring {@link PublisherCallbackChannel}, which RabbitTemplate requires when the factory has
     * publisher confirms or returns on. Every publisher confirm is acked at once, as a broker that
     * stored the message would, so a relay that waits for its CorrelationData's confirm goes on as
     * in production instead of timing out.
     */
    public static Channel recordingChannel(PublishRecorder recorder) {
        return recordingChannel(recorder, false);
    }

    /**
     * As {@link #recordingChannel(PublishRecorder)}, for a factory with publisher confirms on:
     * the channel reports them on, as Spring's own channel proxy does, so RabbitTemplate tracks a
     * confirm for each publish.
     */
    static Channel recordingChannel(PublishRecorder recorder, boolean publisherConfirms) {
        AtomicLong nextSeqNo = new AtomicLong(1); // AMQP numbers confirms from 1
        return proxy(PublisherCallbackChannel.class, List.of(ChannelProxy.class), (self, method, args) -> {
            switch (method.getName()) {
                case "basicPublish" -> {
                    nextSeqNo.incrementAndGet();
                    recordPublish(recorder, args);
                }
                case "getNextPublishSeqNo" -> {
                    return nextSeqNo.get();
                }
                case "isPublisherConfirms", "isConfirmSelected" -> {
                    return publisherConfirms;
                }
                case "getTargetChannel" -> {
                    return self;
                }
                case "confirmSelect" -> recorder.confirmDelivery();
                case "addPendingConfirm" -> {
                    // What Spring's own channel does when the broker acks: complete the
                    // CorrelationData's future, then tell the template (its ConfirmCallback).
                    PendingConfirm pending = (PendingConfirm) args[2];
                    if (pending.getCorrelationData() != null) {
                        pending.getCorrelationData().getFuture().complete(new CorrelationData.Confirm(true, null));
                    }
                    ((PublisherCallbackChannel.Listener) args[0]).handleConfirm(pending, true);
                }
                case "exchangeDeclare" -> recordExchangeDeclare(recorder, args, false);
                case "exchangeDeclarePassive" -> recordExchangeDeclare(recorder, args, true);
                case "queueDeclare" -> recorder.declareQueue(args == null ? "(server-named)" : (String) args[0], false);
                case "queueDeclarePassive" -> recorder.declareQueue((String) args[0], true);
                case "isOpen" -> {
                    return true;
                }
                case "getChannelNumber" -> {
                    return 1;
                }
                case "getDelegate" -> {
                    return self;
                }
                default -> {
                    // everything else is a no-op
                }
            }
            return defaultValue(method);
        });
    }

    private static void recordPublish(PublishRecorder recorder, Object[] args) {
        // basicPublish(exchange, routingKey, [mandatory, [immediate,]] props, body)
        String exchange = (String) args[0];
        String routingKey = (String) args[1];
        boolean mandatory = args.length >= 5 && args[2] instanceof Boolean m && m;
        AMQP.BasicProperties props = (AMQP.BasicProperties) args[args.length - 2];
        byte[] body = (byte[]) args[args.length - 1];
        recorder.publish(exchange, routingKey, body, properties(props), mandatory);
    }

    private static void recordExchangeDeclare(PublishRecorder recorder, Object[] args, boolean passive) {
        String exchange = (String) args[0];
        if (passive) {
            recorder.declareExchange(exchange, "topic", true, true, false, false, Map.of());
            return;
        }
        Object rawType = args.length > 1 ? args[1] : null;
        String type = rawType instanceof Enum<?> e ? e.name().toLowerCase() : String.valueOf(rawType);
        boolean durable = args.length > 2 && args[2] instanceof Boolean d && d;
        boolean autoDelete = args.length > 3 && args[3] instanceof Boolean a && a;
        boolean internal = args.length > 5 && args[4] instanceof Boolean i && i;
        @SuppressWarnings("unchecked")
        Map<String, Object> arguments = (Map<String, Object>) Arrays.stream(args)
                .filter(Map.class::isInstance).findFirst().orElse(Map.of());
        recorder.declareExchange(exchange, type, false, durable, autoDelete, internal, arguments);
    }

    /** The checked AMQP properties, keyed as in the contracts. */
    static Map<String, Object> properties(AMQP.BasicProperties props) {
        Map<String, Object> result = new LinkedHashMap<>();
        if (props == null) {
            return result;
        }
        putIfPresent(result, "content_type", props.getContentType());
        putIfPresent(result, "delivery_mode", props.getDeliveryMode());
        putIfPresent(result, "message_id", props.getMessageId());
        putIfPresent(result, "type", props.getType());
        putIfPresent(result, "correlation_id", props.getCorrelationId());
        return result;
    }

    private static void putIfPresent(Map<String, Object> map, String key, Object value) {
        if (value != null) {
            map.put(key, value);
        }
    }

    // ---------- consuming ----------

    /** The queues every listener container in {@code registry} consumes. */
    public static Set<String> listenerQueues(RabbitListenerEndpointRegistry registry) {
        Set<String> queues = new LinkedHashSet<>();
        for (MessageListenerContainer container : registry.getListenerContainers()) {
            if (container instanceof AbstractMessageListenerContainer abstractContainer) {
                queues.addAll(Arrays.asList(abstractContainer.getQueueNames()));
            }
        }
        return queues;
    }

    /** A Spring AMQP message for a delivery, with the contracts' property keys mapped. */
    public static Message message(String queue, byte[] body, Map<String, Object> properties) {
        MessageProperties props = new MessageProperties();
        props.setContentType((String) properties.get("content_type"));
        if (properties.get("delivery_mode") instanceof Number mode) {
            props.setDeliveryMode(mode.intValue() == 2 ? MessageDeliveryMode.PERSISTENT : MessageDeliveryMode.NON_PERSISTENT);
        }
        props.setMessageId((String) properties.get("message_id"));
        props.setType((String) properties.get("type"));
        props.setCorrelationId((String) properties.get("correlation_id"));
        props.setConsumerQueue(queue);
        props.setReceivedRoutingKey(queue);
        props.setDeliveryTag(1L);
        props.setRedelivered(false);
        return new Message(body.clone(), props);
    }

    /**
     * Run the real listener for {@code queue} on one delivery and report what its container
     * would do, assuming Spring's defaults: a {@code ConditionalRejectingErrorHandler} and
     * requeueing rejected messages. If the service changes either, use the other overload.
     */
    public static Outcome handle(
            RabbitListenerEndpointRegistry registry, String queue, byte[] body, Map<String, Object> properties) {
        return handle(registry, queue, body, properties,
                new ConditionalRejectingErrorHandler.DefaultExceptionStrategy(), true);
    }

    /**
     * Run the real listener for {@code queue} on one delivery and report what its container would
     * do with it.
     *
     * <ul>
     *   <li>MANUAL acknowledgement: whatever the listener did on the channel (basicAck,
     *       basicReject or basicNack). If it threw instead, the rules below.
     *   <li>AUTO: ACK if the listener returned. If it threw: REJECT if {@code fatal} says so (the
     *       error handler turns it into AmqpRejectAndDontRequeueException), otherwise Spring's
     *       requeue rule (ContainerUtils.shouldRequeue with {@code defaultRequeueRejected}).
     * </ul>
     */
    public static Outcome handle(
            RabbitListenerEndpointRegistry registry,
            String queue,
            byte[] body,
            Map<String, Object> properties,
            FatalExceptionStrategy fatal,
            boolean defaultRequeueRejected) {
        AbstractMessageListenerContainer container = containerFor(registry, queue);
        Message message = message(queue, body, properties);
        List<Outcome> settled = new ArrayList<>();
        Channel channel = settlingChannel(settled);
        Object listener = container.getMessageListener();
        Throwable failure = null;
        try {
            if (listener instanceof ChannelAwareMessageListener aware) {
                aware.onMessage(message, channel);
            } else if (listener instanceof MessageListener plain) {
                plain.onMessage(message);
            } else {
                throw new IllegalStateException("the container for " + queue + " has no MessageListener");
            }
        } catch (Exception e) {
            failure = e instanceof ListenerExecutionFailedException ? e
                    : new ListenerExecutionFailedException("listener threw", e, message);
        }
        if (container.getAcknowledgeMode() == AcknowledgeMode.MANUAL && !settled.isEmpty()) {
            return settled.get(0);
        }
        if (failure == null) {
            if (container.getAcknowledgeMode() == AcknowledgeMode.MANUAL) {
                throw new AssertionError("the MANUAL-ack listener for " + queue
                        + " returned without acknowledging or rejecting the message");
            }
            return Outcome.ACK;
        }
        if (fatal.isFatal(failure)) {
            return Outcome.REJECT;
        }
        return ContainerUtils.shouldRequeue(defaultRequeueRejected, failure, LOG) ? Outcome.REQUEUE : Outcome.REJECT;
    }

    private static AbstractMessageListenerContainer containerFor(RabbitListenerEndpointRegistry registry, String queue) {
        for (MessageListenerContainer container : registry.getListenerContainers()) {
            if (container instanceof AbstractMessageListenerContainer abstractContainer
                    && Arrays.asList(abstractContainer.getQueueNames()).contains(queue)) {
                return abstractContainer;
            }
        }
        throw new AssertionError("no @RabbitListener consumes " + queue + "; listeners consume "
                + listenerQueues(registry));
    }

    /** A channel that records how the listener settled the delivery (MANUAL acknowledgement). */
    private static Channel settlingChannel(List<Outcome> settled) {
        return proxy(Channel.class, (self, method, args) -> {
            switch (method.getName()) {
                case "basicAck" -> settled.add(Outcome.ACK);
                case "basicReject" -> settled.add((Boolean) args[1] ? Outcome.REQUEUE : Outcome.REJECT);
                case "basicNack" -> settled.add((Boolean) args[2] ? Outcome.REQUEUE : Outcome.REJECT);
                case "isOpen" -> {
                    return true;
                }
                default -> {
                    // no-op
                }
            }
            return defaultValue(method);
        });
    }

    // ---------- topology ----------

    /** A problem for each Queue, Exchange or Binding bean: the broker owns the topology. */
    public static List<Problem> declarableProblems(ApplicationContext context) {
        List<Problem> problems = new ArrayList<>();
        for (Class<?> type : List.of(Queue.class, Exchange.class, Binding.class)) {
            for (String name : context.getBeanNamesForType(type)) {
                problems.add(new Problem("declarable",
                        "bean '" + name + "' is a " + type.getSimpleName()
                                + ": the broker owns the topology (rabbitmq-service topology/messages.yaml)"));
            }
        }
        return problems;
    }

    // ---------- proxies ----------

    @FunctionalInterface
    private interface Handler {
        Object invoke(Object self, Method method, Object[] args) throws Throwable;
    }

    private static <T> T proxy(Class<T> type, Handler handler) {
        return proxy(type, List.of(), handler);
    }

    private static <T> T proxy(Class<T> type, List<Class<?>> extra, Handler handler) {
        InvocationHandler invocation = (self, method, args) -> {
            if (method.getDeclaringClass() == Object.class) {
                return switch (method.getName()) {
                    case "equals" -> self == args[0];
                    case "hashCode" -> System.identityHashCode(self);
                    case "toString" -> "recording " + type.getSimpleName();
                    default -> null;
                };
            }
            try {
                return handler.invoke(self, method, args);
            } catch (InvocationTargetException e) {
                throw e.getCause();
            }
        };
        List<Class<?>> interfaces = new ArrayList<>(List.of(type));
        interfaces.addAll(extra);
        return type.cast(Proxy.newProxyInstance(type.getClassLoader(), interfaces.toArray(Class<?>[]::new), invocation));
    }

    private static Object defaultValue(Method method) {
        Class<?> type = method.getReturnType();
        if (type == boolean.class) {
            return false;
        }
        if (type == int.class) {
            return 0;
        }
        if (type == long.class) {
            return 0L;
        }
        if (type == short.class) {
            return (short) 0;
        }
        if (type == byte.class) {
            return (byte) 0;
        }
        if (type == double.class) {
            return 0.0;
        }
        if (type == float.class) {
            return 0.0f;
        }
        if (type == Collection.class || type == List.class) {
            return List.of();
        }
        return null;
    }
}
