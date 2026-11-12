# Contract testing: build and rollout plan

How this repo will ship a contract-testing package, in Python and Java, and how each service will
use it to prove that what it publishes and consumes matches `topology/messages.yaml` and
`contracts/`. Tick tasks as they are done, the same way as in [PLAN.md](../PLAN.md).

## Goal

Every service checks its side of every message contract in its own CI, in seconds, with no broker
and no other services running:

- **Publishers** prove that every message they send has the right exchange, routing key, AMQP
  properties, envelope and payload.
- **Consumers** prove that they listen on exactly the queues the catalogue gives them, and that
  their handlers accept every body the schema allows.
- **This repo** proves that the contract itself is complete and that changes to it are compatible,
  and publishes it as a versioned package.

Together with this repo's existing routing tests (`tests/integration/test_routing.py`), that is a
full chain: publisher → contract ← broker → contract ← consumer.

## Scope

**In scope**

| Where | Checks |
|---|---|
| This repo | Every message with consumers has a payload schema and a full example. Examples validate. Every field a consumer says it reads is required in the publisher's schema. Schema changes are classified as compatible or breaking, and the version bump matches. |
| Publishers | Every message the catalogue gives the service is published, and nothing else. Each publish: exchange, routing key, `delivery_mode=2`, `message_id`, `type`, `correlation_id`, `content_type`, `mandatory`, publisher confirms. The envelope validates, agrees with the properties, and `producer` is the service. The payload validates. |
| Consumers | **The consumer's contract is its requirement list** (`contracts/requirements/<consumer>.yaml`): the payload fields it reads. This repo guarantees that list stays required in the publisher's schema, so a publisher change that would break a consumer fails here. In the consumer's own repo: listener queues equal `queues_for(service)`, with none missing and none extra, and the handler acks the `minimal` body (publisher-required fields only), which proves the requirement list covers everything the code reads. No non-passive `queue_declare`, and no Spring `Queue`/`Binding`/`Exchange` beans. |

**Out of scope** (accepted gaps): connecting and credentials, broker permissions at runtime,
client-library behaviour beyond what a recording fake sees, whether the relay and listeners start
in a deployed service, and business logic after a message is handled. The broker's own behaviour is
already covered by `tests/integration`. Deploy-time checks (`ticketto-check`, the Phase 5 alerts on
DLQs, `ticketto.unrouted` and queues with no consumers) cover what is left.

## Design

### One source, one bundle, two thin packages

```
contracts/                        source of truth (edited by hand)
  envelope.schema.json
  <Message>.payload.schema.json
  examples/<Message>.json
  VERSION                         contract version, e.g. 1.0.0
  CHANGELOG.md
topology/messages.yaml            source of truth (edited by hand)
        │
        ▼  tools/build_contracts_bundle.py
build/contracts-bundle/           generated, not committed
  catalogue.json                  normalised catalogue: per message its publisher, exchange,
                                  routing key, consumers and queue names
  schemas/…  examples/…  VERSION
        │
        ├──► packages/python/     ticketto-contracts (PyPI, GitLab package registry)
        └──► packages/java/       com.ticketto:ticketto-contracts (Maven, GitLab package registry)
```

- **`catalogue.json` is the only catalogue the packages read.** It is produced by the existing
  `tools/generate_definitions.py` code (`load_catalogue`, `publisher_exchange`), so neither
  package parses YAML or reimplements naming rules such as "`checkout-service` publishes to
  `checkout.events`".
- **Both packages bundle the same files** as package data or jar resources. Their versions are
  always `contracts/VERSION`, so `ticketto-contracts==1.3.0` in Python and `1.3.0` in Maven
  contain identical contracts.
- **Cross-language parity is tested.** `packages/conformance/cases.json` lists recorded publishes
  and consumer outcomes with the verdict expected for each, e.g. "wrong routing key → fail
  `routing_key`". Both packages' own test suites run every case, so the two implementations can't
  drift apart.

### Who owns which part of the contract

| Part | Authority | Where |
|---|---|---|
| Exchange, routing key, publisher, consumers | the catalogue (this repo) | `topology/messages.yaml` |
| Envelope | this repo, agreed with every team | `contracts/envelope.schema.json` |
| Payload: every field, its type, and whether it's always sent | **the publisher** | `contracts/<Message>.payload.schema.json` |
| Which payload fields a consumer reads | **that consumer** | `contracts/requirements/<consumer>.yaml` |

- **A consumer's requirements are a subset of the publisher's schema, never a second schema.**
  Each consumer lists only the fields its code reads, as paths:

  ```yaml
  # contracts/requirements/ticket-service.yaml
  EventPublished:
    - event_id
    - title
    - venue
    - starts_at
    - ticket_categories[].category_id
    - ticket_categories[].name
  OrderConfirmed: [order_id, hold_id, admission_id, user_id, event_id, category_id, quantity,
                   delivery_email, confirmed_at, total_amount, currency]
  ```

- **A static test in this repo checks every requirement against the publisher's schema.** Each
  path must exist in the schema and be `required` at every level. A consumer that needs a field
  the publisher doesn't send, or sends only sometimes, fails here, before any consumer code runs.
- **The requirements make the compatibility check precise.** A schema change is reported with the
  consumers whose requirements it touches. Removing an optional field that no consumer reads is a
  minor change rather than a major one.
- **The requirement list is all a consumer has to keep stable.** Adding to it, or removing from
  it, is the consumer's own merge request. Anything else that would change what it gets (a field
  in its list removed, renamed or made optional) fails the static test, whoever makes the change.
- **They also back the consumer tests.** The `minimal` body is the publisher's required fields
  only, so a consumer passes it exactly when its real needs are covered by what is always sent.
- **`AMQP_Message_Contracts.md` is generated** from the catalogue, schemas, examples and
  requirement files, so it can't drift from them. It was the hand-written input for Phase A.

### Versioning

`contracts/VERSION` is semver for the contract, separate from the broker image:

| Change | Bump |
|---|---|
| Wording, descriptions, example values | patch |
| New message, new consumer, new optional payload field, a consumer reading fewer fields, removing an optional field no consumer reads | minor |
| Removed or renamed field that a consumer reads, a field becoming required, a type or enum narrowed, a changed routing key or exchange, a removed consumer or message, any envelope change | major |
| A consumer reading a new field | none by itself, but the field must already be required in the publisher's schema, or the static test fails |

- **Breaking payload changes never bump the envelope's `schema_version`.** It stays at 1, because
  consumers reject versions they don't know, so a bump would send every message to every DLQ.
  Breaking changes use expand-and-contract (add the new field, consumers switch, remove the old
  one), or a new message name with its own routing key and queues so both can run side by side.
- `tools/check_contract_compat.py` compares the merge request's bundle with its target branch's.
  It works out the minimum bump and fails if `VERSION` wasn't bumped at least that much. When it
  can't classify a schema change, it treats it as major. It also lists the services each change
  affects (`--affected`), for the downstream checks.
- Every bump adds a `CHANGELOG.md` entry naming the messages and services affected.

### Bodies used for consumer tests

Generated from the committed example, so no fuzzing library is needed. Only `minimal` is
required; the rest are opt-in for teams that want more:

| Case | How | Required |
|---|---|---|
| `minimal` | the example with every property not in the publisher's `required` removed, recursively | yes |
| `example` | `contracts/examples/<Message>.json` as committed | opt-in |
| `extra-fields` | the example plus an unknown field in every payload object (not the envelope, whose schema forbids unknown fields) | opt-in |
| `invalid` | the example with one required payload field removed; the handler must reject it without requeue | opt-in |

Today every field in every schema is required, so `minimal` and `example` are the same body.

### Python package: `ticketto-contracts`

Built in Phase C: [packages/python/README.md](../packages/python/README.md) is the guide for
service teams. Python 3.12+. Depends on `jsonschema`, with no dependency on `pika`.

```
ticketto_contracts/
  catalogue.py      published_by, consumed_by, queues_for, exchange_for, message, requirements,
                    payload_schema, example
  validate.py       check_publish / assert_valid_publish(svc, PublishedMessage): every problem,
                    with the conformance codes; ContractError lists them all
  listeners.py      check_listener_queues / assert_listener_queues
  bodies.py         minimal / example / extra-fields / invalid bodies, and delivery() for both
                    the body bytes and the AMQP properties
  recording.py      RecordingChannel: a pika BlockingChannel stand-in that records basic_publish
                    and confirm_delivery, and records a non-passive queue_declare or an
                    exchange_declare that differs from the documented one as a violation
  suite.py          the generated tests (below)
  data/             the bundle (package data, copied in at build time, not committed)
```

**How a service uses it:** one explicit test module, plus adapters in a conftest. Explicit means
the tests are visible in the service's tree, and the package never injects tests into a suite
that didn't ask for them. The service name goes in the test module, because the tests are
generated from it before any fixture exists.

```python
# tests/contract/test_contracts.py
from ticketto_contracts.suite import *  # noqa: F403  (generated publisher + consumer tests)

CONTRACT_SERVICE = "checkout-service"
CONTRACT_BODIES = []  # optional: ["extra-fields", "invalid"]
```

```python
# tests/contract/conftest.py
@pytest.fixture
def contract_publish():  # publishers: drive the real outbox relay onto the RecordingChannel
    def publish(message_name, channel):
        relay_once(OutboxRow.for_test(message_name), channel)

    return publish


@pytest.fixture
def contract_queues():  # consumers: the queue names the service listens on
    return set(LISTENERS)


@pytest.fixture
def contract_handle():  # consumers: run the real handler; return "ack" | "reject" | "requeue"
    ...
```

- `suite.py` uses `pytest_generate_tests` to parametrise from `published_by`/`consumed_by`.
- A role the catalogue doesn't give the service is skipped with the reason. A role it does give
  but whose fixture is missing fails, naming the fixture to add.
- Every publish the relay makes is checked, so it also fails if the relay sends a message the
  catalogue doesn't give the service (`not_publisher`, `unknown_message`).

**No known gaps:** a test can't be marked as an expected failure. If a service doesn't match the
contracts, its tests fail until the service or the contract is fixed. 1.0.0 had
`CONTRACT_KNOWN_GAPS` for this; 2.0.0 removed it, and the package refuses a test module that
still sets it.

### Java package: `com.ticketto:ticketto-contracts`

Java 21 (reservation-service's version), Spring AMQP 4. Its Jackson 2 and networknt
`json-schema-validator` (draft 2020-12) are shaded into the jar. JUnit and Spring AMQP are
`provided`, so non-Spring services can use it without pulling Spring in.

```
com.ticketto.contracts
  Catalogue                 publishedBy / consumedBy / queuesFor / message
  PublishedMessage          exchange, routingKey, body, properties, mandatory, confirmed
  PublishAssertions         assertValidPublish(service, PublishedMessage)
  Bodies                    the same five cases as Python
  ContractAdapter           interface the service implements:
                              void publish(String message, PublishRecorder recorder)
                              Set<String> listenerQueues()
                              Outcome handle(String queue, byte[] body, Map<String, Object> props)
                            (a plain map, keyed like the Python package, so the core needs no Spring)
  ContractTests             abstract base class with @TestFactory methods that generate
                            DynamicTests from the catalogue, like Python's suite.py
  spring.SpringAmqpSupport  helpers: record what RabbitTemplate sends (exchange, key, Message,
                            mandatory, confirm type), read listener queues from
                            RabbitListenerEndpointRegistry, and fail if the context defines
                            Queue, Binding or Exchange beans
```

How a Spring service uses it:

```java
@SpringBootTest
class MessageContractTest extends ContractTests {
    @Autowired ApplicationContext context;

    @Override protected String service() { return "reservation-service"; }
    @Override protected ContractAdapter adapter() { return new ReservationContractAdapter(context); }
}
```

As in Python, there are no known gaps: a dynamic test that doesn't match the contracts fails.

### CI in this repo

New jobs alongside the existing pipeline. None of them needs the broker image.

| Stage | Job | When | Does |
|---|---|---|---|
| test | `contracts-static` | always (part of `static-tests`) | every message with consumers has a schema and an example, and every consumer requirement is required by the publisher |
| test | `contracts-compat` | MR, if `contracts/` or `messages.yaml` changed | `check_contract_compat.py` against the MR's target branch (added in Phase B) |
| test | `contracts-python` | if `contracts/` or `messages.yaml` changed | build the bundle and wheel, ruff, pylint, pytest including the conformance cases |
| test | `contracts-java` | same | `mvn -B verify` in `maven:3.9-eclipse-temurin-21` |
| publish | `contracts-prerelease`, `contracts-prerelease-java` | MR, same changes | upload the wheel `contracts-python` built and tested, as `X.Y.Z.dev<pipeline id>`, and report it as `PRERELEASE_VERSION` (dotenv); upload the jar with the same version to the Maven registry, so `CONTRACTS_VERSION` means the same contracts in either language |
| publish | `contracts-downstream-plan` | MR, same changes | `tools/plan_contract_downstream.py`: the affected services (`check_contract_compat`) that are listed in `scripts/ci/contract-test-projects.yaml` → a child pipeline with one trigger job each |
| publish | `contracts-downstream` | MR, same changes | run that child pipeline: each service's `contract-tests` on its default branch with `CONTRACTS_VERSION`, `strategy: depend`. A failing service fails a patch or minor change, and is a warning (`allow_failure`) for a major one. Affected services not listed yet are reported, not failed |
| publish | `cleanup-contract-prereleases` | `main`, contract changes, once `PACKAGE_CLEANUP_TOKEN` is set | delete pre-releases older than 30 days |
| publish | `publish-contracts-python`, `publish-contracts-java` | `main`, if `contracts/VERSION` changed (Java: or `packages/java/` changed) | publish the tested `X.Y.Z` package; skip if already published. No git tag: CI's job token can't push tags, and the registry already lists every version |

- **Every contract job extends `.contracts-changed`** in `.gitlab-ci.yml`: it runs only when
  `contracts/` or `topology/messages.yaml` change (`contracts-compat`, merge requests only, uses
  the same paths). A merge that touches neither builds no package.
- The publish jobs go to the **GitLab** package registry, so they are not gated on
  `DEPLOY_ENABLED`, which only guards the AWS jobs. Publishing a package never deploys the
  broker: `deploy-broker` is a manual button.
- **Affected services:** the publisher and consumers of every message whose schema or catalogue
  entry changed, or every service if the envelope changed.
  `python -m tools.check_contract_compat --affected` prints this list.
- **Cleanup:** a registry cleanup policy deletes pre-releases after 30 days.

### CI in each service

```yaml
contract-tests:
  stage: test
  rules:
    - if: $CI_PIPELINE_SOURCE == "pipeline"      # triggered from rabbitmq-service
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  script:
    - pip install -e '.[dev]'
    - if [ -n "$CONTRACTS_VERSION" ]; then pip install "ticketto-contracts==$CONTRACTS_VERSION"; fi
    - pytest tests/contract
```

- **Registry access:** the index URL is the rabbitmq-service project's PyPI or Maven endpoint,
  authenticated with `CI_JOB_TOKEN`. rabbitmq-service must allow each service's project under
  **Settings → CI/CD → Job token permissions**.
- **Triggered pipelines:** a pipeline triggered from rabbitmq-service runs only `contract-tests`,
  so other jobs need `when: never` for `$CI_PIPELINE_SOURCE == "pipeline"`.
- **Who may trigger:** a triggered pipeline runs as the person whose contract merge request
  triggered it. They need at least the Developer role in the service's project (and permission
  to run pipelines on its default branch if it is protected).
- **Pipeline variables:** the trigger passes `CONTRACTS_VERSION`, a pipeline variable. Newer
  GitLab projects allow no one to set those by default, and the trigger then fails with
  "insufficient permissions to set pipeline variables". In the service's project: **Settings →
  CI/CD → Variables → Minimum role to use pipeline variables → Developer** (or the contract
  authors' role).
- **Wiring it up:** add the service to `scripts/ci/contract-test-projects.yaml` (its project
  path, and its branch if not `main`). Until then, contract merge requests report it as affected
  but not checked.
- **Java:** the same, with `mvn -B test -Dtest='*ContractTest' -Dticketto.contracts.version=$CONTRACTS_VERSION`.

## Build plan (this repo)

### Phase A: the contract is complete
- [x] Add a static test: every message with consumers has `<Message>.payload.schema.json` and an
  example. (No allowlist needed: every consumed message has one.)
- [x] Resolve the [discrepancies](#discrepancies-found-in-amqp_message_contractsmd) below.
- [x] Draft payload schemas from the "Publishes" sections of the hand-written
  `AMQP_Message_Contracts.md` (since replaced by the generated page). Each field in the publisher's example
  is required unless the publishing team says it is sometimes left out. All IDs are lower-case
  UUIDs, `reason` fields are free strings.
- [x] Each publishing team signs off its schemas.

  | Message | Publisher | Schema |
  |---|---|---|
  | `EventPublished` | event-service | exists; `venue` added as required |
  | `EventCancelled` | event-service | exists; matches |
  | `OrderConfirmed` | checkout-service | added |
  | `OrderRefunded` | checkout-service | added |
  | `RefundFailed` | checkout-service | added |
  | `RefundCompleted` | checkout-service | added |
  | `ReservationExpired` | reservation-service | added |
  | `RefundRequired` | reservation-service | added |
  | `EventSoldOut` | reservation-service | added |

  `TicketIssued` was exempt until notification-service started consuming it, in contracts 1.1.0.
  `PaymentCompleted` was removed from the catalogue in 2.0.0.
- [x] Write `contracts/requirements/<consumer>.yaml` from the "Consumes" sections.
- [x] Each consuming team signs off its own file. Waived 2026-11-03: the files come from each
  team's own "Consumes" section, and a wrong one fails that consumer's minimal-body test at
  adoption. notification-service confirmed its file.
- [x] notification-service lists its `RefundFailed` fields.
- [x] Add the static test that every requirement path exists and is required in the publisher's
  schema, and that every (consumer, message) pair in the catalogue has a requirements entry.
- [x] Replace `AMQP_Message_Contracts.md` with a page generated from the schemas and requirement
  files (`tools/contracts.py`, written by `python -m tools.generate_definitions`), checked for
  staleness in CI like the other generated files.
- [x] Add CODEOWNERS: each payload schema is owned by its publishing team, and consumers are
  required approvers. Waived 2026-11-03: review routing only (enforced approval needs GitLab
  Premium), and the requirement test already blocks changes that break a consumer. Add it if
  contract reviews start being missed.
- [x] Add `contracts/VERSION` (`1.0.0`) and `CHANGELOG.md`, with a static test that the newest
  changelog entry matches `VERSION`.

### Phase B: bundle and compatibility check
- [x] `tools/build_contracts_bundle.py`: writes `build/contracts-bundle/` (catalogue, schemas,
  examples, VERSION), from the working tree or any git ref (`--ref`)
- [x] `tools/check_contract_compat.py` with tests, one per rule in the versioning table, with
  unknown changes treated as major. It also lists the affected services for a diff (`--affected`).
- [x] `packages/conformance/cases.json`: the shared verdict cases, for publishes and listener
  queues. `tests/static/test_conformance_cases.py` rechecks their envelope, payload and listener
  verdicts against the real contracts, so they can't go stale.
- [x] CI: `contracts-compat` on merge requests that change the contracts

### Phase C: Python package
- [x] `packages/python/` with its own `pyproject.toml`, building the bundle into package data.
  The wheel is `ticketto_contracts-<contract version>`, its version read from the bundle.
- [x] `catalogue`, `validate`, `listeners`, `bodies`, `recording` and `suite`, plus known-gaps
  support
- [x] Tests (`packages/python/tests`): every conformance case, unit tests, and a sample
  reservation-service run through the generated suite with pytester, passing and in 12 broken
  variants that fail with the expected messages. They also pass against the installed wheel.
- [x] CI: `contracts-python` (build, install, lint and test the wheel) and
  `publish-contracts-python` (upload that wheel from `main` when `contracts/VERSION` changes;
  `scripts/ci/publish_contracts_python.py`, tested against a fake registry)
- [ ] Publish `1.0.0`: happens on the first `main` pipeline after this is merged

### Phase D: Java package
Deferred on 2026-11-03 until the Python rollout was done; built 2026-11-03.
- [x] `packages/java/` (`pom.xml`, Java 21), copying the bundle into
  `src/main/resources/ticketto-contracts/` (not committed). The version is `-Drevision`, and the
  published POM is flattened to it.
- [x] `Catalogue`, `PublishAssertions`, `Listeners`, `Bodies`, `PublishRecorder`,
  `ContractAdapter`, `ContractTests` and `spring.SpringAmqpSupport`
- [x] Tests: every conformance case (the same verdicts as Python), the generated tests run on
  sample services (JUnit Platform test kit), and a sample Spring AMQP app as a fixture (a plain
  Spring context: Spring Boot isn't needed to exercise Spring AMQP). The 40 generated bodies were
  checked identical to Python's.
- [x] CI: `contracts-java`, `contracts-prerelease-java`, `publish-contracts-java`; the
  pre-release cleanup also deletes Maven `X.Y.Z.devN`. `packages/**` now counts as a contract
  change, so a change to either package's code runs its tests.
- [x] Publish `2.0.0` (`publish-contracts-java` runs on `packages/java/` changes, and skips
  versions already published)
- Found while building it: Spring's `RabbitTemplate` sends `mandatory=true` only when it has a
  `ReturnsCallback`; `setMandatory(true)` alone publishes non-mandatory. The `publishes` tests
  catch it, and packages/java/README.md says how to set it up.

### Phase E: cross-repo checks
- [x] `contracts-prerelease`, `contracts-downstream-plan` (`tools/plan_contract_downstream.py`)
  and `contracts-downstream`: a child pipeline with one `strategy: depend` trigger per affected
  service listed in `scripts/ci/contract-test-projects.yaml`. The list is empty until Wave 1; until
  then affected services are reported, not checked.
- [x] Pre-release cleanup: GitLab's package cleanup policy can't delete versions by age, so
  `scripts/ci/cleanup_contract_prereleases.py` deletes `X.Y.Z.devN` older than 30 days through the
  packages API, on `main` merges that change the contracts (not a schedule, which would run every
  other `main` job too).
- [ ] One-time GitLab setup: `PACKAGE_CLEANUP_TOKEN` (project access token, `api` scope,
  Maintainer role, masked) on rabbitmq-service
- [x] Document the contract change process in `docs/MESSAGING.md` ("Changing a contract"): edit,
  bump, which downstream checks run, and the expand-and-contract steps for breaking changes

## Rollout plan (other services)

| Service | Language | Publishes | Consumes | Wave |
|---|---|---|---|---|
| event-service | Python (pika) | `EventPublished`, `EventCancelled` | none | 1 (pilot publisher) |
| queue-service | Python (pika) | none | `EventPublished`, `EventCancelled`, `OrderConfirmed`, `EventSoldOut` | 1 (pilot consumer) |
| reservation-service | Java (Spring AMQP) | `ReservationExpired`, `RefundRequired`, `EventSoldOut` | `EventPublished`, `EventCancelled` | 2 (pilot Java) |
| checkout-service | Python (pika) | `OrderConfirmed`, `OrderRefunded`, `RefundCompleted`, `RefundFailed` | `EventCancelled`, `ReservationExpired`, `RefundRequired` | 3 |
| ticket-service | to confirm | `TicketIssued` (planned) | `EventPublished`, `EventCancelled`, `OrderConfirmed`, `RefundCompleted` | 3 |
| user-service | to confirm | none | `EventCancelled`, `OrderConfirmed` | 3 |
| notification-service | to confirm | none | `EventPublished`, `EventCancelled`, `OrderRefunded`, `RefundFailed`, `TicketIssued` | 3 |

### Wave 1: Python pilot (event-service, queue-service)
Needs Phases A (at least the existing two schemas) and C.
- **event-service:** `contract_publish` drives `app/outbox.py`'s relay. This should pass on day
  one, since it already matches the contract (verified 2026-10-18).
- **queue-service: adopted 2026-11-03.** `tests/contract/` in queue-service: `contract_queues`
  reads its `CONSUMER_QUEUES`, and `contract_handle` runs its real `dispose()` (parsing and the
  ack / requeue / dead-letter decision; the MySQL and Redis side effects are left to its
  integration tests). 14 tests pass, with the `extra-fields` and `invalid` bodies on: it already
  consumes the broker's queues by name. The package is in its own uv
  dependency group, `contract`, so only `contract-tests` and `make test-contract` need registry
  credentials. Wired into `scripts/ci/contract-test-projects.yaml`.
  - [x] `contract-tests` passes in its CI, merged to its `main`
  - [x] wired into `contracts-downstream`
  - [x] a deliberately breaking contract merge request here is caught by its tests: making
    `OrderConfirmed.admission_id` a free string (2026-11-03, not merged) passed every check in
    this repo but failed queue-service's `test_accepts[OrderConfirmed-minimal]` and
    `[OrderConfirmed-extra-fields]` through `contracts-downstream`, which failed the merge
    request. First run needed queue-service's pipeline-variables setting (above).
- **Exit criteria:** both services run `contract-tests` in CI. One deliberately breaking contract
  merge request here is caught by `contracts-downstream` before merge. Fold the feedback on the
  helper API into the package before wave 2.

### Wave 2: Java pilot (reservation-service)
- **Adopted 2026-11-03.** `TickettoContractTest` (`src/contract-test/java` in reservation-service) runs the real `OutboxWriter` and `OutboxPublisher` through the
  `RabbitTemplate` Spring Boot builds from its properties, and its real `EventMessageListener`,
  in a slice of the application with MySQL and Redis mocked: 10 tests pass on 2.0.0. The package
  is in a Maven profile (`contract-tests`, with `src/contract-test/java`), so the image build and
  the other jobs never need this registry. Deliberate breaks were caught: publisher confirms off,
  `mandatory` off, routing key `event.sold.out`, and a listener rejecting valid bodies.
- To make it work, the package now targets Spring AMQP 4 (reservation-service is on Spring Boot
  4.1), shades its Jackson 2 and networknt `json-schema-validator` (reservation-service uses
  networknt 3, a different API under the same coordinates), and acks publisher confirms (its relay
  waits for them).
- [x] `contract-tests` passes in its CI, merged to its `main`
- [x] wired into `contracts-downstream`
- Its `EventSoldOut`, `ReservationExpired` and `RefundRequired` schemas from Phase A got their
  first real publisher check here, and pass.
- **Exit criteria:** green in CI. Met.

### Wave 3: everyone else
- **checkout-service first.** It has the most messages. Its tests pass on 2.0.0 with nothing
  excluded, once `PaymentCompleted` (no longer published) left the catalogue.
- **Then ticket-service, user-service and notification-service**, each consumer-only or nearly
  so, in whatever order their teams have capacity.

### Wave 4: enforcement
- [ ] `contract-tests` is a required job on every service's default branch.
- [x] Gate by level (2026-11-03): a failing service blocks a patch or minor change, and is a
  warning for a major one, with a report job naming the services that must update. Blocking a
  major protects nothing, since services pin exact versions: checkout-service's `main` failing on
  2.0.0 (it still set `CONTRACT_KNOWN_GAPS`) would otherwise have needed it to pin a pre-release
  before the contract could merge.
- [ ] "Pipelines must succeed" is on here, so a patch or minor contract merge request can't merge
  while a downstream service fails. A major one merges, and the affected services update when
  they move to it.
- [ ] Renovate (or a scheduled job) opens version-bump merge requests in each service when a new
  contract version is published.

### Per-service adoption checklist
1. Allow the service's project in rabbitmq-service's job token permissions.
2. Add the registry index and `ticketto-contracts` as a test dependency.
3. Add `tests/contract/` (Python) or `MessageContractTest` (Java) with adapters.
4. Run it, and fix the service (or the contract) until it passes.
5. Add the `contract-tests` CI job, including the `pipeline` trigger rule.
6. Add the service's project path to `contracts-downstream`'s service map here.

## Discrepancies found in AMQP_Message_Contracts.md

Compared with `messages.yaml` and the existing schemas on 2026-11-03, and all resolved the same
day. Each is the kind of break the contract tests are meant to catch.

| # | Message | Finding | Effect if shipped | Resolution |
|---|---|---|---|---|
| 1 | `EventPublished` | queue-service reads `sale_ends_at`; event-service doesn't send it. | Every `EventPublished` fails in queue-service and goes to its DLQ, or the field is silently missing. | The doc was out of date: queue-service reads `starts_at` (into its `sale_ends_at` field). Its requirement lists `starts_at`. |
| 2 | `EventSoldOut` | reservation-service lists routing key `event.sold.out`; the catalogue has `event.sold_out`. | Publishing with reservation-service's own user gets `403` (its topic permission is `event.sold_out`); with the admin user it lands in `ticketto.unrouted`. queue-service never sees it. | The catalogue's `event.sold_out` is the latest agreed key and stays. reservation-service must publish with `event.sold_out`. Its contract test catches this (Wave 2). |
| 3 | `RefundFailed` | The catalogue has notification-service consuming it (commit 9ef9ef7); its "Consumes" section doesn't list it. | Either notification-service doesn't consume it and `notification-service.RefundFailed` fills up unread, or its requirements are undocumented. | The commit history wins: notification-service consumes it. Its fields are listed in `contracts/requirements/notification-service.yaml`. |
| 4 | `EventPublished` | event-service now sends `venue`; the existing schema doesn't define it. ticket-service reads it. | ticket-service's requirement fails the static test until `venue` is added as required. | `venue` added to the schema as required. |
| 5 | reservation-service and checkout-service messages | Example IDs are placeholders (`"order-456"`, `"hold-123"`, `"evt-1"`, `"..."`); ticket-service's examples of the same fields are UUIDs. | Can't decide whether ID fields get the UUID pattern. | All IDs are UUIDs; every ID field uses the UUID pattern. |
| 6 | `RefundRequired`, `OrderRefunded`, `RefundFailed` | `reason` is `EVENT_CANCELLED` in one and `event_cancelled` in the others, with no list of values. | Typed as a free string until the publishers list the values. | Kept as free strings. |

## Open questions
- Which languages are ticket-service, user-service and notification-service? (The table above
  assumes nothing.)
- The GitLab group path and rabbitmq-service's project ID, for the registry URLs.
- ~~The Java version of reservation-service~~: Java 21 (answered 2026-11-03).
- Is Renovate available on the GitLab instance, or is a scheduled bump job needed?
- Who signs off a schema when the consuming team doesn't respond: the publishing team, or the
  broker owner?
