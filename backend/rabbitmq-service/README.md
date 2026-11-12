# rabbitmq-service

Ticketto's message broker: RabbitMQ 4.3 with the platform's whole messaging topology preloaded.
Each service publishes its domain events (`EventPublished`, `OrderConfirmed`, ...) to its own
exchange, and the broker delivers a copy to each consuming service's own queue. A message that keeps failing is
moved to a dead-letter queue instead of being lost or blocking the queue.

- **Using the broker from another service:** [docs/MESSAGING.md](docs/MESSAGING.md)
- **Every message, its payload fields and who reads them:**
  [docs/AMQP_Message_Contracts.md](docs/AMQP_Message_Contracts.md) (generated)
- **Contract testing** (the contract packages, the checks each service runs, and what a contract
  change triggers): [README_ContractTesting.md](README_ContractTesting.md)
- **Deploying it to AWS** (its own EC2 instance, `rabibi`): [deploy/README.md](deploy/README.md)
- **What was planned and built, and why:** [PLAN.md](PLAN.md)

## Run it locally

```sh
cp .env.example .env               # optional: change ports or the local password
docker compose up -d --build       # AMQP localhost:5672, UI http://localhost:15672
docker compose exec rabbitmq ticketto-check
```

Log in as `ticketto` / `ticketto-local`. There is no `guest` user.

Other services' local stacks join the `ticketto` network and use
`amqp://ticketto:ticketto-local@rabbitmq:5672/%2F` ([how](docs/MESSAGING.md#testing-your-service-against-the-real-broker)). <!-- local-only password; gitleaks:allow -->

## What the broker contains

All of it is generated from [`topology/messages.yaml`](topology/messages.yaml).

| | |
|---|---|
| **Exchanges** | `<name>.events` (topic), one per publisher: `event.events`, `checkout.events`, `reservation.events`, `ticket.events`. Routing key = the message's `routing_key` in `messages.yaml`, e.g. `event.published`<br>`ticketto.dlx` (direct): dead letters; routing key = the queue they failed in<br>`ticketto.unrouted` (fanout): the alternate exchange for every `*.events` exchange |
| **Queues** (all durable quorum queues) | `<consumer>.<Message>`: one per consumer per message (20)<br>`<consumer>.<Message>.dlq`: one per consumer queue (20)<br>`ticketto.unrouted`: messages nobody consumes yet, kept rather than lost |
| **Policies** | `events-ae`: unroutable messages on any `*.events` exchange go to `ticketto.unrouted`<br>`consumer-queues`: `delivery-limit 5`, so a message that keeps failing goes to its DLQ<br>`holding-cap`: DLQs and `ticketto.unrouted` hold at most 50 000 messages and refuse (never drop) more; they also never drop a message after repeated retries |
| **Users** | One admin user, from `RABBITMQ_ADMIN_USER` / `RABBITMQ_ADMIN_PASS`.<br>One least-privilege user per service, generated from `messages.yaml` (`topology/service-users.tsv`): it can consume only its own queues (not their DLQs), publish only to its own exchange with its own routing keys, and can't use the UI. Passwords come from `RABBITMQ_PASS_<SERVICE>` (or `RABBITMQ_SERVICE_PASS_DEFAULT` locally).<br>All are created at start-up; passwords are never in git or the image. |

### Why it's built this way

- **Persistence:** quorum queues write every message to disk, even ones a publisher forgot to mark
  persistent. Dead-lettering is at-least-once.
- **Settings that change vs. settings that don't:** structure that never changes for a queue lives
  in its arguments. Values we may want to tune (retry count, size caps) live in policies, which can
  be edited on a running broker without recreating queues.
- **Ownership:** the broker owns the topology, so services must not declare queues. A
  mismatching declare fails with `406`, which is deliberate.

## Change the topology

1. Edit `topology/messages.yaml`. For example, to make `user-service` consume `RefundCompleted`
   as well:
   ```yaml
   RefundCompleted:
     publisher: checkout-service
     routing_key: refund.completed
     status: implemented
     consumers: [ticket-service, user-service]
   ```
2. `python -m tools.generate_definitions`. This regenerates `topology/definitions.json`,
   `topology/queues.txt`, `topology/service-users.tsv` and `docs/AMQP_Message_Contracts.md`;
   commit all four. Run it after editing `contracts/` too. CI fails if any of them is stale. A new service in the catalogue also needs `tofu apply` in `deploy/terraform` (it
   creates that service's secret) before the next deploy.
3. Open a merge request. If the change adds a new service, run `tofu apply` in `deploy/terraform`
   first (step 2). After the merge, press **deploy-broker** on the `main` pipeline; the new queues appear when
   the broker restarts. Removed queues must be deleted by hand ([deploy/README.md](deploy/README.md#3-day-to-day)).

`status` (`implemented` / `planned` / `proposed`) only records how far the publisher has got. The
topology ignores it.

## Development

```sh
uv venv --python 3.12 && uv pip install -e '.[dev]'    # or: python -m venv .venv && pip install -e '.[dev]'
ruff check . && ruff format --check . && python -m pylint tools tests
pytest                    # static tests: generated files, topology rules, contracts (no Docker)
pytest -m integration     # starts the real image in Docker and tests the behaviour (about 1 min)
python -m tools.build_contracts_bundle                        # the contracts bundle, in build/
python -m tools.check_contract_compat --base-ref origin/main  # the VERSION bump a change needs
```

| Suite | Checks |
|---|---|
| `tests/static` | Generated files are up to date. Every queue is quorum with a DLQ. Bindings match the catalogue. Every queue and exchange gets the right policy. Each publisher's exchange matches how services declare it. Catalogue mistakes (bad or duplicate routing keys, unknown keys) are rejected. Contracts are valid and their examples validate. Every consumed message has a payload schema, and every field a consumer reads (`contracts/requirements/`) is required in the publisher's schema. The contracts bundle, the version-bump rules and the shared conformance cases. |
| `tests/integration` | Boot: the topology, policies, users and config are loaded, and `guest` can't log in.<br>Routing: every message reaches exactly its consumers, and only from its publisher's exchange.<br>Dead-lettering: rejected messages go to the DLQ, and poison messages after 5 redeliveries.<br>Persistence: messages survive a restart and a redeploy with a rotated password.<br>Acks: auto-ack consumers are detected, and unacked messages are redelivered.<br>Overflow: a full holding queue refuses rather than drops.<br>Compatibility: the publishers' exchange declares work, wrong declares fail. |

The image is `rabbitmq:4.3.6-management` plus our files. It has no `RUN` steps, so the arm64 image
for the EC2 host builds on amd64 CI without emulation.

| Path | What |
|---|---|
| `topology/messages.yaml` | the catalogue you edit |
| `topology/definitions.json`, `topology/queues.txt` | generated; imported by the broker at boot / read by `ticketto-check` |
| `tools/generate_definitions.py` | the generator |
| `tools/build_contracts_bundle.py`, `tools/check_contract_compat.py` | the contracts bundle the packages ship; the version bump a contract change needs |
| `packages/conformance/cases.json` | cases both contract packages (Python, Java) must agree on |
| `packages/python/` | `ticketto-contracts`, the Python contract package services test with ([its README](packages/python/README.md)) |
| `tools/plan_contract_downstream.py`, `scripts/ci/contract-test-projects.yaml` | which services' contract tests a contract merge request triggers |
| `tools/contracts.py` | loads the contracts, checks consumer requirements, renders `docs/AMQP_Message_Contracts.md` |
| `config/` | `rabbitmq.conf` (limits, timeouts, definitions import) and enabled plugins |
| `docker-entrypoint-ticketto.sh` | writes the admin user from the environment, then starts RabbitMQ |
| `scripts/ticketto-check.sh` | `ticketto-check` in the image: health, topology, no auto-ack consumers |
| `contracts/` | JSON Schemas for the message envelope (`message_type` / `producer` / `payload`) and payloads, with examples. The publisher owns each payload schema |
| `contracts/requirements/` | per consumer, the payload fields it reads; each must be required in the publisher's schema |
| `deploy/terraform/` | OpenTofu for the broker's EC2 instance, ECR repository, secret and CI deploy role |
| `deploy/`, `scripts/ci/` | the instance's boot and run scripts, and the CI deploy script ([deploy/README.md](deploy/README.md)) |

## CI/CD

| Stage | Jobs |
|---|---|
| test | ruff, pylint, shellcheck, static tests (and `generate_definitions --check`), `contracts-compat` (merge requests that change the contracts: is the `contracts/VERSION` bump big enough), `tofu-validate` (formatting and validity of `deploy/terraform`, no AWS access), SAST, secret detection. None of these need the image, so they run first. |
| build | multi-arch image (amd64 + arm64) → `$CI_REGISTRY_IMAGE:$CI_COMMIT_SHA`, built once |
| integration-test | `pytest -m integration` against that exact image (Docker-in-Docker) |
| publish (`main`) | the same image copied to ECR as `:<sha>` and `:latest`, on every merge |
| contracts (only when `contracts/`, `messages.yaml` or `packages/` change) | `contracts-python` and `contracts-java` build and test the contract packages (Python, and Java for reservation-service). On a merge request: `contracts-prerelease` and `contracts-prerelease-java` upload them as `X.Y.Z.devN`, and `contracts-downstream` runs the affected services' contract tests against it. A failure fails the merge request for a patch or minor change, and is only a warning for a major one. On `main`: `publish-contracts-python` and `publish-contracts-java` upload `X.Y.Z` when `contracts/VERSION` changes (Java also when `packages/java/` changes, skipping a version already published). See [docs/MESSAGING.md](docs/MESSAGING.md#changing-a-contract) |
| deploy (`main`, manual) | `deploy-broker`: a manual button on the `main` pipeline. SSM → `/opt/ticketto/run-rabbitmq.sh <sha>` on the `rabibi` instance; rolls back on failure. A deploy restarts the broker (services are disconnected for about 15 s), so someone chooses when |

The publish and deploy jobs run only once the `DEPLOY_ENABLED` CI variable is `true`
([deploy/README.md](deploy/README.md#1c-gitlab-variables)). Until then, `main` pipelines stop after the integration tests.
