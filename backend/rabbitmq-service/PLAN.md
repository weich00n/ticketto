# rabbitmq-service: plan and implementation log

The reference for what this repo was meant to do, what was built, and why. Tick a task in the same
commit that completes it, and add an entry to the [implementation log](#implementation-log) at the
end of each phase.

## Goal

One RabbitMQ broker, owned by this repo, that routes Ticketto's domain events to the right
services. The repo contains:
- a custom image with configuration and topology preloaded
- dead-lettering
- tests that prove every message reaches the right queues and is never silently lost
- a pipeline that deploys the broker to the existing EC2 broker host

## Decisions

| Decision | Why |
|---|---|
| Run on the **EC2 broker host**, not ECS | RabbitMQ already runs there with an EBS data volume. On Fargate it would need EFS, Cloud Map or an NLB, and an ECS service with no ALB. |
| **One topic exchange per publisher**, `<service>.events` (`event.events`, `checkout.events`, ...), with dotted routing keys (`event.published`) | What event-service and queue-service already build (changed from one `ticketto.events` exchange on 2026-10-19; see the log). Each team owns its exchange. |
| **One queue per consumer per message**: `<consumer>.<Message>` | Separate backlogs and DLQs; one service's poison message never blocks another. |
| **Every queue is a quorum queue**; the vhost defaults to quorum | Messages are written to disk even if a publisher forgets `delivery_mode=2`. |
| **Each queue has its own DLQ** via `ticketto.dlx`; `delivery-limit 5` | Poison messages are moved aside after 5 redeliveries rather than blocking the queue. |
| **An alternate exchange keeps unroutable messages** in `ticketto.unrouted` | Messages for consumers that don't exist yet are kept, not retried forever by `mandatory` outboxes. Set by policy on every `*.events` exchange, so the publishers' argument-less declares still match. |
| **Fixed structure in queue arguments; tunables in policies** | Arguments are frozen when a queue is created; policies can be edited live. |
| **Generate `definitions.json` from `messages.yaml`** | Each consumer needs 4 entries that must agree. The YAML is the readable catalogue, and the generator keeps the JSON consistent. |
| **Full catalogue now, but no notification queues** | Notification's consumer is undecided. Its messages are held in `ticketto.unrouted`. |
| **No delay queue for hold expiry** | Reservation expires holds with a Redis TTL plus `HoldExpiryWorker`. The broker only routes the resulting `ReservationExpired`. |
| **Checkout switches to `checkout.events`** (no bridge for `orders.topic`) | Checkout hasn't built consumers yet, so nothing depends on `orders.topic`. |
| **AMQP 0-9-1** | What `pika` and Spring AMQP speak. AMQP 1.0 is not used. |
| **Services never declare queues** | The broker owns the topology. A mismatching declare fails with 406. |

## Topology

Generated from [`topology/messages.yaml`](topology/messages.yaml).

**Exchanges:**
- `event.events`, `checkout.events`, `reservation.events`, `ticket.events` (topic, one per publisher)
- `ticketto.dlx` (direct)
- `ticketto.unrouted` (fanout; the alternate exchange)

**Queues: 25 in total, all quorum**
- 12 consumer queues, each with arguments `x-dead-letter-exchange=ticketto.dlx`,
  `x-dead-letter-routing-key=<own name>`, `x-dead-letter-strategy=at-least-once` and
  `x-overflow=reject-publish`
- 12 DLQs
- `ticketto.unrouted`

**Policies:**
- `events-ae`: alternate exchange `ticketto.unrouted`, on every `*.events` exchange
- `consumer-queues`: `delivery-limit` 5
- `holding-cap`: on DLQs and `ticketto.unrouted`, `max-length` 50 000, `overflow` reject-publish,
  `delivery-limit` −1

| Message | Exchange, routing key | Publisher (status at 2026-10-19) | Consumer queues |
|---|---|---|---|
| `EventPublished` | `event.events`, `event.published` | event-service ✅ | queue-service, reservation-service |
| `EventCancelled` | `event.events`, `event.cancelled` | event-service ✅ | queue-service, reservation-service, checkout-service, ticket-service |
| `OrderConfirmed` | `checkout.events`, `order.confirmed` | checkout-service ✅ (on `orders.topic` today) | ticket-service, queue-service |
| `ReservationExpired` | `reservation.events`, `reservation.expired` | reservation-service (planned) | checkout-service |
| `RefundRequired` | `reservation.events`, `refund.required` | reservation-service (planned) | checkout-service |
| `EventSoldOut` | `reservation.events`, `event.sold_out` | reservation-service (proposed by queue-service) | queue-service |

These have no consumer yet and go to `ticketto.unrouted`:
- `TicketIssued`
- `PaymentCompleted`, `OrderRefunded`, `RefundCompleted`, `RefundFailed`

## Phases

### Phase 1: catalogue and skeleton
- [x] This plan as `PLAN.md`, linked from the README
- [x] `topology/messages.yaml` from the routing table
- [x] `contracts/`: the §8 envelope plus event-service's two payloads, with examples
- [x] `pyproject.toml` and ruff/pylint config, mirroring event-service
- [x] `docs/MESSAGING.md`: publisher and consumer rules, why not to declare queues, ack and retry
  rules

### Phase 2: broker image and local broker
- [x] `tools/generate_definitions.py`, producing `definitions.json` and `queues.txt`
- [x] `Dockerfile`, `config/rabbitmq.conf`, `config/enabled_plugins`, `docker-entrypoint-ticketto.sh`
- [x] `scripts/ticketto-check.sh`, the in-image health, topology and auto-ack check
- [x] `docker-compose.yml`: shared `ticketto` network, health check, 60 s stop grace period
- [x] **Milestone:** event-service's real outbox delivers through this broker (see log, 2026-10-18)

### Phase 3: tests and CI
- [x] `tests/static`: 63 tests, no Docker
- [x] `tests/integration`: 55 tests against the real image (Testcontainers)
- [x] `.gitlab-ci.yml`: test (lint, static tests) → build (multi-arch) → integration-test
      → publish to ECR → manual deploy
- [ ] First green pipeline on GitLab (first run failed in `build-image`; fixed, see log)

### Phase 4: deploy to its own EC2 instance (`rabibi`)
- [x] `deploy/run-rabbitmq.sh`: pull, graceful swap, health check, rollback (rehearsed locally)
- [x] `scripts/ci/deploy-broker.sh`: SSM send-command plus post-deploy `ticketto-check`
- [x] `deploy/terraform/`: instance, data volume, security group, ECR, secret, OIDC deploy role
  (see log, 2026-10-20)
- [x] `deploy/README.md`: one-time setup, first release and the day-to-day runbook
- [ ] `tofu apply` and the GitLab variables, including `DEPLOY_ENABLED`
- [ ] First deploy to AWS

### Phase 5: hardening
- [x] Shovel plugins enabled; DLQ replay and unrouted redelivery runbook (`deploy/README.md`)
- [x] `rabbitmq_prometheus` enabled on port 15692 (private)
- [x] Persistence and redeploy behaviour tested, including a hard-kill experiment (see log)
- [x] Per-service users with least-privilege permissions, generated from `messages.yaml`, one
  `rabibi/service/<svc>` secret each (see log, 2026-10-22)
- [ ] Alerts when a DLQ or `ticketto.unrouted` is non-empty, or `ticketto-check` fails
- [ ] Chaos runbook: stop the broker, see the outboxes hold their messages, restart, see them drain

### Phase 6: changes the other services need (raise with each team)
- [ ] **checkout-service**
  - `EXCHANGE = "checkout.events"`, routing keys from `messages.yaml` (`order.confirmed`, ...)
  - confirms and `mandatory`, marking SENT only after the confirm
  - `message_id`, `type` and `correlation_id` properties, and the `message_type`/`producer`/`payload`
    envelope
  - stop publishing `RefundJob`
  - reuse one channel
  - `admission_id` in `OrderConfirmed`
  - AMQP consumers instead of the `/events/*` HTTP routes
  - `RefundFailed`
- [ ] **queue-service:** its exchanges, routing keys and envelope already match. It still declares
  its own queues (`queue-service.event-lifecycle`, `.sold-out`, `.order-confirmed`) and its own
  `queue-service.dlx` in `messaging/topology.py`. On this broker those would get a copy of every
  message *as well as* the broker's `queue-service.*` queues, which would then fill up unread.
  Consume the 4 broker queues by name and drop `messaging/topology.py`; local compose on this image
- [ ] **reservation-service** (its Phase 9)
  - `spring-boot-starter-amqp` with no Queue or Binding beans
  - ack mode `AUTO`/`MANUAL` with `default-requeue-rejected=false` and retries
  - consume `reservation-service.EventPublished` and `reservation-service.EventCancelled`
  - publish to `reservation.events`, with the `message_type`/`producer`/`payload` envelope
  - agree `EventSoldOut` with queue-service
- [ ] **event-service:** exchange, routing keys and envelope already match. Local compose and CI
  on this image with the `ticketto` user; task
  definitions use the secret's `url` key
- [ ] **Notification:** choose a consumer, add it to `messages.yaml`, then shovel the held messages
  out of `ticketto.unrouted`

## Implementation log

### 2026-10-18: phases 1–3 built, phase 4 repo side ready

**Built:** everything ticked above. `pytest` (63 static) and `pytest -m docker` (55) pass locally.
ruff, pylint (10/10) and shellcheck are clean.

**Verified end to end with the real event-service.** Its compose stack ran with its own RabbitMQ
disabled and pointed at this broker (a throwaway override; event-service's repo was not changed).
- Publishing an event put one `EventPublished` in each of `queue-service.EventPublished` and
  `reservation-service.EventPublished`.
- Cancelling it put one `EventCancelled` in each of the 4 `*.EventCancelled` queues.
- Both outbox rows were delivered on the first attempt.
- Both real message bodies validate against `contracts/`, and `correlation_id` is carried from the
  HTTP request to the AMQP property.

**Deviations from the plan, and findings:**

1. **RabbitMQ 4.3.6, not 4.1.x.** The EC2 host runs the floating `rabbitmq:4-management` tag, which
   resolves to 4.3.6. RabbitMQ can't be downgraded onto newer data, so the image pins 4.3.6.
   - A simulated cutover from stock 4.3.6 data worked with no manual steps: the user was kept and
     its password rotated, the vhost was switched to quorum by default, and all 23 queues were
     created.
2. **Users go in a second definitions file.** The broker imports the directory
   `/etc/rabbitmq/definitions/`: `10-topology.json` is baked in, and `20-users.json` is written by
   the entrypoint at every start. That is simpler than editing JSON in the shell.
   - `definitions.skip_if_unchanged` is not used, so a rotated password applies on the next
     restart (tested).
3. **The password hash is computed with `openssl`** (salted SHA-256, RabbitMQ's algorithm).
   `rabbitmqctl hash_password` hangs when given the password on stdin, and passing it as an argument
   would expose it in the process list.
4. **The entrypoint creates the Erlang cookie.** Found while testing: a root CLI call, such as a
   health check, made during boot creates the cookie as root, and the server then can't start.
5. **`ticketto-check` lives in the image (bash) instead of `tools/check_consumers.py`.** It runs
   wherever the container runs: locally, in tests and over SSM, with no Python needed on the host.
   - It has a `--skip-consumers` mode for the deploy's rollback decision, because a service's
     auto-ack bug isn't a reason to roll the broker back.
   - Found while testing: `rabbitmqctl list_consumers` prints columns in its own order, so the
     script finds the ack flag by its value.
6. **`holding-cap` also sets `delivery-limit: -1`.** Quorum queues in 4.x drop a message after 20
   failed deliveries by default, and DLQs have no DLX of their own to catch it.
7. **On 4.3, `basic.nack(requeue=True)` does not count towards `delivery-limit`;
   `basic.reject(requeue=True)` and consumer crashes do.** On 4.1 all three counted. MESSAGING.md
   therefore says to retry with `basic_reject`. A test pins this, so an upgrade that changes it will
   be noticed.
8. **Quorum queues overshoot `max-length` by one before refusing.** With a cap of 1, two messages are
   accepted. Nothing is ever dropped; the overflow test asserts this.
9. **A hard kill (SIGKILL) within seconds of a queue being created can lose that queue and its
   messages.** On 4.3.6, `ticketto.unrouted`, the last queue created at boot, lost its messages in
   4 of 10 runs. After a graceful stop (0/10) or a SIGKILL 30 s after boot (0/8), nothing was lost.
   Responses:
   - `run-rabbitmq.sh` always stops gracefully.
   - The container runs with `--stop-timeout 60`, and compose has `stop_grace_period: 60s`.
   - The redeploy test stops gracefully, as the deploy does.
10. **The local password is `ticketto-local`.** The entrypoint requires 8+ characters.
11. **The deploy state file is on the data volume** (`/data/rabbitmq-service.tag`), so a replaced
    instance boots the image that was last deployed.
12. **Contracts are split:** the envelope schema plus a `<Type>.data.schema.json` per message.
    Validating `data` separately avoids `$ref` resolution.
13. **Checkout's current publisher loses messages on this broker.** It declares its own
    `orders.topic` and publishes without `mandatory` or confirms. Nothing is bound there, so the
    broker drops them silently. A compatibility test demonstrates this; Phase 6 has the fix.

**Left open:**
- The first green GitLab pipeline.
- The infra changes.
- The first AWS deploy.
- Phase 5 items needing infra (per-service users, alerts).
- Phase 6 change requests to the other teams.

### 2026-10-18: first pipeline run

1. **`build-image` failed: "could not create a builder instance with TLS data loaded from
   environment".** With dind over TLS (`DOCKER_TLS_CERTDIR: /certs`), `docker buildx create`
   can't build a builder straight from `DOCKER_HOST` and the cert variables. The job now runs
   `docker context create ci` first and creates the builder on that context.
2. **Stages renamed: `lint` → `test`, `test` → `integration-test`.** The old order "lint → build
   → test" made it look as if every test ran after the build. Only the integration tests do,
   because they test the built image itself: the image is built once, and the image that passed
   `integration-test` is the one that ships. SAST and secret detection now run in their
   templates' default stage (`test`), so their stage overrides are gone.
3. **The Docker suite is now the integration suite**, matching event-service's `tests/integration`.
   - `tests/docker` → `tests/integration`
   - pytest marker `docker` → `integration`, so it runs with `pytest -m integration`
   - CI job `docker-tests` → `integration-tests`
   - The folder is no longer a package called `docker` that shadows the Docker SDK by name.
4. **Secret detection flagged the local password, `ticketto-local`, in 4 places.** It matched the
   `user:password@` part of the local AMQP URLs. These are false positives: the password only
   exists on a developer's local broker, and the AWS broker always gets its password from
   Secrets Manager. Each URL line now carries a `gitleaks:allow` comment, so the scanner skips it.
   Compose now publishes 5672 and 15672 on `127.0.0.1` only. Before, Docker listened on every
   interface, so anyone on the same network could log in with the published password. Checked:
   localhost connects, and the machine's Wi-Fi address is refused.

### 2026-10-19: per-publisher exchanges and the services' envelope

1. **One exchange per publisher instead of `ticketto.events`.** event-service publishes to
   `event.events` with keys like `event.published`, and queue-service binds to `event.events`,
   `reservation.events` and `checkout.events`. Rather than change two working services, the
   broker now matches them.
   - Each message in `messages.yaml` has a `routing_key` (dotted lower case, unique across the
     catalogue). Its exchange is the publisher's name without `-service`, plus `.events`.
   - Every publisher gets an exchange, even with no consumers yet, so its publishes are accepted.
   - `events-ae` now applies to every `*.events` exchange. `ticketto.dlx` and `ticketto.unrouted`
     are unchanged, and so are the queue names.
2. **Contracts use the `message_type` / `producer` / `payload` envelope**, which event-service
   publishes and queue-service parses. No service used the old `event_type` / `correlation_id` /
   `data` one: reservation-service writes no envelope yet, and checkout publishes a bare payload.
   The correlation id travels only as the AMQP `correlation_id` property. Payload schemas are now
   `<Message>.payload.schema.json` and match what event-service sends (`event_version`,
   `release_at`, `total_capacity`, `price_amount`). Both examples parse with queue-service's
   `parse_message`.
3. **Shovelling held messages now takes one shovel per exchange.** Messages in `ticketto.unrouted`
   came from different exchanges, and a shovel publishes to one. Messages that don't match the
   chosen exchange's bindings go back to `ticketto.unrouted` and are picked up by the next shovel
   (`deploy/README.md`).

### 2026-10-20: the broker gets its own EC2 instance

`infra/` has never been applied, so the shared broker host the old deploy plan relied on doesn't
exist. The broker now has its own small stack in `deploy/terraform/`, which deploys only
RabbitMQ.

1. **Placement.** One `t4g.small` named `rabibi` in the default VPC (`us-east-1a`), running only
   the `rabbitmq` container. Data is on a separate encrypted 10 GiB gp3 volume with
   `prevent_destroy`.
2. **Access.** No SSH and no key pair; admin access is SSM only. The instance has a public IP for
   outbound traffic only, because a default VPC has no NAT: 5 VPC endpoints would cost about
   US$36/month against US$3.65 for the IP. The security group opens 5672 only to the listed
   security groups or CIDRs (none at first), and never opens 15672.
3. **Continuous deployment.** Every merge publishes the tested image to ECR, and `deploy-broker`
   releases it straight away. It was a manual click at first, because a restart disconnects every
   service for about 15 s; it was made automatic once every service reconnected on its own. Both
   jobs are gated on `DEPLOY_ENABLED == "true"`, as in event-service, so `main` keeps passing
   until AWS is set up.
4. **The instance ignores `user_data` changes.** `run-rabbitmq.sh` is embedded in the user data,
   so with `user_data_replace_on_change` any edit to it, even a comment, would have replaced the
   broker on the next apply. Instead, `deploy-broker` installs the commit's `run-rabbitmq.sh` on
   every deploy, and replacing the instance (new boot script or AMI) is an explicit
   `tofu apply -replace=aws_instance.broker`.
5. **The boot logic is a plain script** (`deploy/boot-rabbitmq.sh`), so shellcheck checks it.
   `user-data.sh.tftpl` only installs it with its settings. Rendered user data is 8.4 KB, against
   EC2's 16 KB limit.
6. **The secret is `rabibi/rabbitmq`, not `broker/rabbitmq`,** so it can't clash with `infra/` if
   that is applied later. It has a ready-made `url` key for services.
7. **Account checks before writing any code** (read-only, and EC2 `--dry-run`): every permission
   the stack needs is allowed for `user/sophia`; the default VPC exists; `t4g.small` is offered in
   `us-east-1a`; no GitLab OIDC provider and no name clashes. A stopped `t3.micro` called
   `messaging-server` exists, with SSH and 15672 open to `0.0.0.0/0`; it isn't part of this stack.
8. **New CI job `tofu-validate`:** `tofu fmt -check` plus `tofu validate` with no backend and no
   AWS access, in `ghcr.io/opentofu/opentofu:1.10`. Run locally in that image: passes.

### 2026-10-22: one least-privilege user per service

Every service used to connect as the admin user, so any of them could read every queue, publish
any message, or delete the topology. Each service now has its own user.

1. **Permissions are generated from `messages.yaml`** into `topology/service-users.tsv`, which
   is committed and checked by CI like the other outputs. For each service:

   | Permission | Value |
   |---|---|
   | configure | its own exchange only, so its idempotent `exchange_declare` still works |
   | write | its own exchange only |
   | read | its own consumer queues, `^<svc>\.[A-Za-z]+$`. That excludes `.dlq`, so replays stay an admin job. |
   | topic write | only its own messages' routing keys, so it can't publish e.g. a fake `order.confirmed` even on its own exchange |

   Services that only consume or only publish get `^$`, which matches nothing, for the other side.
   No tags: no UI or HTTP API.
2. **Passwords come from the environment, as with the admin user.** The entrypoint reads
   `RABBITMQ_PASS_<SERVICE>`, or `RABBITMQ_SERVICE_PASS_DEFAULT` (local compose only). A
   service with neither is skipped with a warning, and `ticketto-check` warns about it. All
   these variables are unset before the server starts.
3. **AWS:** OpenTofu creates `rabibi/service/<svc>` for every service in `messages.yaml`, each
   with a `url` key. `run-rabbitmq.sh` finds them by name prefix (`secretsmanager:ListSecrets`
   on the instance role, which shows names, not values), and passes them to the container by
   variable name only. Rehearsed locally with a stub `aws` command and a local registry: users
   were created only for the services that had secrets, and the broker came up healthy.
4. **Verified against the real broker** (`tests/integration/test_service_users.py`):
   - a service consumes, acks and passive-declares its own queue
   - it gets `403` for another service's queue, its own DLQ, declaring a queue, another
     exchange, or another service's routing key
   - a consume-only service can't publish
   - service users get `401` from the HTTP API
5. **Rollout:** `tofu apply` first (new secrets, instance role), then **deploy-broker**. Services
   switch `RABBITMQ_URL` to their own secret one at a time; the admin URL keeps working, so
   nothing breaks in between.
6. **Static tests check each regex against the real names:** each service's read regex matches
   exactly its consumer queues and none of the DLQs, and so on for exchanges and routing keys.
   A badly escaped `.` would have matched more names than intended.

### 2026-11-03: deploys are manual again; contract jobs only run on contract changes

1. **`deploy-broker` is a manual button on the `main` pipeline.** It had been automatic since
   2026-10-20, so every merge restarted the broker and disconnected every service for about 15 s,
   even merges that only touched docs or contract schemas. `publish-to-ecr` still runs on every
   merge, so the button always has a tested image to release. The job has `allow_failure: true`,
   so a pipeline nobody deploys stays green instead of "blocked".
2. **Contract jobs run only when `contracts/` or `topology/messages.yaml` change.** The paths are
   defined once (`.contracts-changed` in `.gitlab-ci.yml`). `contracts-compat` uses them now, and
   the package jobs of [docs/CONTRACT-TESTING-PLAN.md](docs/CONTRACT-TESTING-PLAN.md) will extend
   the same template.
