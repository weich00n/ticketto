# Contract testing

How Ticketto checks that every service publishes and consumes AMQP messages the way the catalogue
says, without a broker and without the other services running. This repo owns the contracts and
ships them as a package in Python and Java. Each service runs that package's generated tests in its
own CI, and a contract change here runs the affected services' tests before it can merge.

This page is the overview. The details are in:

| Document | For |
|---|---|
| [docs/MESSAGING.md](docs/MESSAGING.md#changing-a-contract) | changing a contract: what to edit, how to bump the version, what CI checks |
| [docs/AMQP_Message_Contracts.md](docs/AMQP_Message_Contracts.md) | every message's routing, payload and consumers (generated, don't edit) |
| [packages/python/README.md](packages/python/README.md) | adding the contract tests to a Python service |
| [packages/java/README.md](packages/java/README.md) | adding them to a Java / Spring AMQP service |
| [docs/CONTRACT-TESTING-PLAN.md](docs/CONTRACT-TESTING-PLAN.md) | the design, and the build and rollout record |

## What is checked, and where

| Layer | Where it runs | Checks |
|---|---|---|
| **The contract** | this repo, every pipeline (`static-tests`) | every consumed message has a payload schema and a valid example; every field a consumer reads is required in the publisher's schema; the generated docs and topology are up to date |
| **Contract changes** | this repo, merge requests (`contracts-compat`) | the change is classified as patch, minor or major, and `contracts/VERSION` is bumped at least that much |
| **Publishers** | each service's CI (`contract-tests`) | every message the service publishes: exchange, routing key, `mandatory`, publisher confirms, AMQP properties (`delivery_mode=2`, `message_id`, `type`, `correlation_id`, `content_type`), the envelope, the payload against its schema, and no queue or foreign exchange declared |
| **Consumers** | each service's CI (`contract-tests`) | the service listens on exactly its catalogued queues; its real handler acks a body with only the publisher's required fields; optionally, it ignores unknown fields and dead-letters a body missing a required field |
| **Across repos** | this repo, merge requests (`contracts-downstream`) | the contract tests of every affected service, run against this merge request's pre-release |

Not covered, by design: connecting to the broker, broker permissions at runtime, and business logic
after a message is handled. The broker's own routing is tested separately by `tests/integration`.

## Ownership

| Part of a contract | Owned by | File |
|---|---|---|
| Exchange, routing key, publisher, consumers | this repo | `topology/messages.yaml` |
| Envelope | this repo, agreed with every team | `contracts/envelope.schema.json` |
| Payload: every field, its type, whether it's always sent | **the publisher** | `contracts/<Message>.payload.schema.json`, `contracts/examples/<Message>.json` |
| The payload fields a consumer reads | **that consumer** | `contracts/requirements/<service>.yaml` |
| The contract version | this repo | `contracts/VERSION`, `contracts/CHANGELOG.md` |

A consumer's requirements are a subset of the publisher's schema, never a second schema. If a
consumer lists a field, the publisher must always send it: a change that removes it, renames it or
makes it optional fails `static-tests` here, whoever makes the change.

## The packages

Both are built from the same bundle (catalogue, schemas, examples, requirements) and give the same
verdicts: `packages/conformance/cases.json` holds the shared cases, and both test suites run all of
them.

| | Python | Java |
|---|---|---|
| Package | `ticketto-contracts` (PyPI) | `com.ticketto:ticketto-contracts` (Maven) |
| Source | `packages/python/` | `packages/java/` |
| Targets | Python 3.12+, pika | Java 21, JUnit 5/6, Spring AMQP 4 (Spring Boot 4) |
| A service writes | `tests/contract/test_contracts.py` plus fixtures (`contract_publish`, `contract_queues`, `contract_handle`) | a `ContractTests` subclass plus a `ContractAdapter` (`spring.SpringAmqpSupport` drives a real `RabbitTemplate` and `@RabbitListener`s) |
| Registry | this project's GitLab package registry (project 87750337) | the same |

**Versions:** the package version is the contract version: `ticketto-contracts 2.0.0` is contracts
2.0.0 in either language. A merge request that changes the contracts publishes the pre-release
`X.Y.Z.devN` (N is the pipeline id), with the same version string in both languages.

**No expected failures.** A test can't be marked as a known gap: a service that doesn't match the
contracts fails until it, or the contract, is fixed. (`CONTRACT_KNOWN_GAPS` existed in 1.0.0; from
2.0.0 the package refuses it.)

## What happens when a contract changes

```
merge request changing contracts/, topology/messages.yaml or packages/
  │
  ├─ static-tests ............ the contract is complete and consistent
  ├─ contracts-compat ........ patch / minor / major, and VERSION bumped enough
  ├─ contracts-python ........ builds the bundle and the wheel, lint, tests (incl. conformance)
  ├─ contracts-java .......... builds the jar from the same bundle, tests (incl. conformance)
  ├─ contracts-prerelease(-java)  uploads X.Y.Z.devN in both languages
  ├─ contracts-downstream-plan    finds the affected services (publisher and consumers of every
  │                               changed message) listed in scripts/ci/contract-test-projects.yaml
  └─ contracts-downstream .... triggers each one's pipeline on its main branch, with
                               CONTRACTS_VERSION=X.Y.Z.devN: only its contract-tests job runs
          │
          ├─ patch or minor: a failing service FAILS this merge request
          └─ major:          a failing service is a WARNING; the report job names the services
                             that must update before they move to the new major

merged to main
  ├─ publish-contracts-python / -java  upload X.Y.Z (Java skips a version already published)
  └─ cleanup-contract-prereleases ..... deletes pre-releases older than 30 days
                                        (needs PACKAGE_CLEANUP_TOKEN)
```

**Why majors only warn:** services pin an exact package version, so publishing a new major can't
break them; it only tells each affected service what it has to fix when it upgrades. A patch or
minor change promises not to break anyone, so a failure there means the change is breaking after
all: make it compatible (expand and contract), or make it a major. See
[docs/MESSAGING.md](docs/MESSAGING.md#changing-a-contract).

**Services don't upgrade automatically.** Each one moves to a new version by changing its pin. The
downstream check tests every affected service against the new version before it merges, whatever
version the service pins.

## The services

All seven are wired into `scripts/ci/contract-test-projects.yaml`.

| Service | Publishes | Consumes | Tests | Bodies | Package pinned in |
|---|---|---|---|---|---|
| event-service | `EventPublished`, `EventCancelled` | | `tests/contract/` | | `requirements-contract.txt` |
| queue-service | | `EventPublished`, `EventCancelled`, `OrderConfirmed`, `EventSoldOut` | `tests/contract/` | extra-fields, invalid | `pyproject.toml` (uv group `contract`) |
| checkout-service | `OrderConfirmed`, `OrderRefunded`, `RefundCompleted`, `RefundFailed` | `EventCancelled`, `ReservationExpired`, `RefundRequired` | `tests/contract/` | extra-fields, invalid | `requirements-contract.txt` |
| ticket-service | `TicketIssued` | `EventPublished`, `EventCancelled`, `OrderConfirmed`, `RefundCompleted` | `tests/contract/` | extra-fields, invalid | `requirements-contract.txt` |
| user-service | | `OrderConfirmed`, `EventCancelled` | `tests/contract/` | extra-fields | `requirements-contract.txt` |
| notification-service | | `EventPublished`, `EventCancelled`, `OrderRefunded`, `RefundFailed`, `TicketIssued` | `tests/contract/` | extra-fields, invalid | `requirements-contract.txt` |
| reservation-service (Java) | `ReservationExpired`, `RefundRequired`, `EventSoldOut` | `EventPublished`, `EventCancelled` | `src/contract-test/java/.../TickettoContractTest` (Maven profile `contract-tests`) | extra-fields, invalid | `pom.xml` (`contracts.version`) |

- **Every service** has a `contract-tests` CI job that runs on every pipeline, and a `workflow`
  rule so a pipeline triggered from here runs only that job (nothing is built or deployed).
- **user-service doesn't use `invalid`:** that body drops the publisher's first required field,
  which for `OrderConfirmed` is `order_id`. user-service doesn't read it, so acking it is right.
- **reservation-service keeps the package in a Maven profile,** so its image build and other jobs
  never need this registry.

## Adding a service

1. Write the tests: [Python](packages/python/README.md) or [Java](packages/java/README.md). Drive
   the service's real outbox relay and real message handlers; mock only what sits behind them.
2. Add a `contract-tests` CI job, plus the `workflow` rule for `$CI_PIPELINE_SOURCE == "pipeline"`
   that sets `CONTRACT_TESTS_ONLY` (and `when: never` for every other job in that case). Copy it
   from any wired service.
3. GitLab settings, once:
   - **in the service:** Settings → CI/CD → Variables → "Minimum role to use pipeline variables"
     → **Developer**. The trigger passes `CONTRACTS_VERSION` as a pipeline variable.
   - **in the service:** contract authors need at least the **Developer** role.
   - **here:** Settings → CI/CD → Job token permissions → add the service's project, if the package
     registry is private.
4. Once its `contract-tests` passes on its `main`, add it to
   `scripts/ci/contract-test-projects.yaml`. Before that, contract merge requests report it as
   "affected but not wired yet" instead of failing.

## Running things locally

```sh
# this repo: the contract and the tooling
python -m pytest tests/static
python -m tools.check_contract_compat --base-ref origin/main      # level, version needed, affected services
python -m tools.generate_definitions                               # after editing messages.yaml or contracts/

# the Python package
python -m pytest packages/python/tests

# the Java package (copy the bundle in first; it isn't committed)
python -m tools.build_contracts_bundle --out packages/java/src/main/resources/ticketto-contracts
(cd packages/java && mvn verify -Drevision=$(cat ../../contracts/VERSION))
```

Each service's README has a "Contract tests" section with its own command.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `contracts-downstream`: "insufficient permissions to set pipeline variables" | The service's "Minimum role to use pipeline variables" isn't Developer (step 3 above). |
| A service's `contract-tests`: 401/403 downloading the package, or a `read_jobs` permission error | This project's job token permissions don't list that service (step 3 above). |
| A service's `contract-tests` passes with every test **skipped** | The package isn't installed. The CI jobs print `ticketto-contracts <version>` first and fail if it's missing. |
| `CONTRACT_KNOWN_GAPS is no longer supported` | The service's test module still sets it. Remove it: from 2.0.0 a mismatch must fail. |
| A Spring service fails `publishes` with `mandatory` although it sets `mandatory=true` | Spring's `RabbitTemplate` only sends `mandatory` with a `ReturnsCallback` or a `CorrelationData` with an id. See [packages/java/README.md](packages/java/README.md#with-spring-amqp). |
| A major change fails a service downstream | Expected: it's a warning, and the merge request can merge. The service fixes it when it moves to the new major. |
| Retrying a failed job doesn't pick up a CI fix you pushed | Retry reruns the job with its original commit's CI config. Run the pipeline for the new commit instead. |
| `RPC failed; HTTP 500` while fetching | A transient gitlab.com error. Retry the job. |
