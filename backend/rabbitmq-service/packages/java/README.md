# ticketto-contracts (Java)

`com.ticketto:ticketto-contracts`: Ticketto's AMQP message contracts, and the contract tests a
Java service runs against them. The Java counterpart of [the Python package](../python/README.md):
the same contracts, the same checks and the same problem codes, held together by the shared cases
in [`../conformance/cases.json`](../conformance/cases.json).

- **Versions:** `X.Y.Z` is contracts `X.Y.Z` (rabbitmq-service's `contracts/VERSION`). A contract
  merge request publishes the pre-release `X.Y.Z.devN`, the same version string as the Python
  package's, for the downstream checks.
- **Java 21, JUnit Jupiter (5 or 6), and Spring AMQP 4 (Spring Boot 4)** for the Spring helpers.
  JUnit and Spring AMQP are `provided`: your service brings its own, and a service without Spring
  doesn't get it.
- **Self-contained:** its JSON and schema libraries (Jackson 2, networknt `json-schema-validator`)
  are shaded into the jar under `com.ticketto.contracts.shaded`, so they can't clash with your
  own versions. Its only real dependency is `slf4j-api`. Where the API returns JSON
  (`Catalogue.example`, `Bodies.body`), the types are the shaded Jackson ones; prefer
  `Bodies.delivery`, which gives bytes.
- **Where:** rabbitmq-service's GitLab package registry (Maven).

## Add it to your service

```xml
<properties>
  <!-- the contracts version under test; rabbitmq-service's downstream check overrides it -->
  <contracts.version>2.0.0</contracts.version>
</properties>

<repositories>
  <repository>
    <id>ticketto-contracts</id>
    <url>https://gitlab.com/api/v4/projects/87750337/packages/maven</url>
  </repository>
</repositories>

<dependencies>
  <dependency>
    <groupId>com.ticketto</groupId>
    <artifactId>ticketto-contracts</artifactId>
    <version>${contracts.version}</version>
    <scope>test</scope>
  </dependency>
</dependencies>
```

If the registry is private, give Maven credentials for the `ticketto-contracts` server id in
`settings.xml`: in CI, a `Job-Token` header with `${env.CI_JOB_TOKEN}` (rabbitmq-service must allow
your project in its job token settings); on your machine, a `Private-Token` header with a personal
access token (`read_api`).

## Write the contract test

One test class, and an adapter to your real code for the roles the catalogue gives your service:

```java
class MessageContractTest extends ContractTests {
    @Override protected String service() { return "reservation-service"; }
    @Override protected ContractAdapter adapter() { return new ReservationContractAdapter(...); }
    @Override protected Set<String> bodies() { return Set.of("extra-fields", "invalid"); } // optional
}
```

```java
interface ContractAdapter {
    void publish(String message, PublishRecorder recorder);                // publishers
    Set<String> listenerQueues();                                          // consumers
    Outcome handle(String queue, byte[] body, Map<String, Object> props);  // consumers: ACK, REJECT or REQUEUE
}
```

The package generates the tests from the catalogue, so you write adapters, not tests:

| Test | Checks |
|---|---|
| `publishes` (one per message you publish) | everything your adapter's `publish` recorded: exchange, routing key, `mandatory`, publisher confirms, the AMQP properties (`delivery_mode=2`, `message_id`, `type`, `correlation_id`, `content_type`), the envelope, the payload against its schema, and that no queue was declared and no exchange but your own |
| `listensOnCataloguedQueues` | your listeners are exactly your catalogued queues |
| `accepts` (one per message you consume, per body) | your handler acks the `minimal` body (only the fields the publisher always sends) and the `extra-fields` one, and rejects the `invalid` one without requeueing |

A role the catalogue doesn't give your service is skipped. A role it does give, without its
adapter method, fails naming the method. `adapter()` is called once per test. A test can't be
marked as an expected failure: if your service doesn't match the contracts, it fails until it, or
the contract, is fixed.

`invalid` drops the publisher's first required payload field. Leave it out if your service doesn't
read that field: acking the body is then correct.

## With Spring AMQP

`com.ticketto.contracts.spring.SpringAmqpSupport` drives a Spring Boot service's real
`RabbitTemplate` and `@RabbitListener`s, without a broker:

```java
class ReservationContractAdapter implements ContractAdapter {
    private final ApplicationContext context;
    ReservationContractAdapter(ApplicationContext context) { this.context = context; }

    @Override
    public void publish(String message, PublishRecorder recorder) {
        RabbitTemplate template = context.getBean(RabbitTemplate.class);
        ConnectionFactory real = template.getConnectionFactory();
        template.setConnectionFactory(SpringAmqpSupport.recordingConnectionFactory(recorder, real));
        try {
            // Make your real outbox relay publish one `message`: write the row through the code
            // that writes real rows, then run the relay once.
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
}

@SpringBootTest(properties = "spring.rabbitmq.listener.simple.auto-startup=false")
class MessageContractTest extends ContractTests {
    @Autowired ApplicationContext context;

    @Override protected String service() { return "reservation-service"; }
    @Override protected ContractAdapter adapter() { return new ReservationContractAdapter(context); }
}
```

- **Publishing:** the recording factory's channels capture what your template really sends.
  Publisher confirms count as on if your real connection factory enables them
  (`spring.rabbitmq.publisher-confirm-type`), since the factory, not the template, turns them on.
  Each confirm is acked at once, as the broker would, so a relay that waits on its
  `CorrelationData` before marking an outbox row sent carries on as in production.
- **`mandatory` needs a `ReturnsCallback` or a `CorrelationData` with an id.** Spring's
  `RabbitTemplate` only sends `mandatory=true` with one of them (`template.setReturnsCallback(...)`
  with `spring.rabbitmq.publisher-returns=true`, or `send(..., new CorrelationData(id))`).
  Otherwise `setMandatory(true)` publishes non-mandatory, and the `publishes` tests fail with
  `mandatory`.
- **Consuming:** `handle` runs the listener container's real message listener for the queue (so
  your message converter and argument resolution run), and reports what the container would do:
  - **MANUAL acknowledgement:** whatever the listener did on the channel.
  - **AUTO:** ack if it returned. If it threw, reject if the exception is fatal to Spring's
    `ConditionalRejectingErrorHandler` (e.g. a conversion error, or
    `AmqpRejectAndDontRequeueException`), otherwise requeue.

  If you change the error handler or `default-requeue-rejected`, pass your
  `FatalExceptionStrategy` and setting to the other `handle` overload.
- **Topology:** the broker owns it. `SpringAmqpSupport.declarableProblems(context)` lists any
  `Queue`, `Exchange` or `Binding` beans; assert it's empty in a test of your own.

## In CI

```yaml
contract-tests:
  image: maven:3.9-eclipse-temurin-21
  stage: test
  script:
    # CONTRACTS_VERSION: set when rabbitmq-service triggers this pipeline for a contract merge
    # request (its pre-release); otherwise the pinned contracts.version is used.
    - mvn -B -s ci_settings.xml test -Dtest='MessageContractTest' ${CONTRACTS_VERSION:+-Dcontracts.version=$CONTRACTS_VERSION}
```

Plus, as in the Python services: a `workflow` rule that sets `CONTRACT_TESTS_ONLY` for
`$CI_PIPELINE_SOURCE == "pipeline"`, `when: never` for every other job in those pipelines, and the
GitLab settings in rabbitmq-service's `scripts/ci/contract-test-projects.yaml`. Then add your
project to that file.

## Use the pieces directly

```java
Catalogue.publishedBy("reservation-service");               // [ReservationExpired, RefundRequired, EventSoldOut]
PublishAssertions.assertValidPublish("reservation-service", published);  // ContractError listing every problem
Bodies.delivery("EventPublished", "minimal");               // body bytes and AMQP properties
Listeners.assertListenerQueues("reservation-service", queues);
```

## Building it (rabbitmq-service)

The contracts are copied in before building; `src/main/resources/ticketto-contracts/` is not
committed.

```sh
python -m tools.build_contracts_bundle --out packages/java/src/main/resources/ticketto-contracts
cd packages/java && mvn verify -Drevision=$(cat ../../contracts/VERSION)
```

In CI, `contracts-java` builds and tests it with the bundle `contracts-python` built. On a merge
request, `contracts-prerelease-java` publishes `X.Y.Z.devN`. On `main`, `publish-contracts-java`
publishes `X.Y.Z` if it isn't in the registry yet.
