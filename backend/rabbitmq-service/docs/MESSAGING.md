# Publishing and consuming Ticketto messages

This is for anyone writing a service that sends or receives messages through the Ticketto broker.
Protocol: **AMQP 0-9-1**. Use `pika` in Python or `spring-boot-starter-amqp` in Java.

The broker owns the topology. Every exchange, queue, binding and retry rule is defined in this
repo (`topology/messages.yaml`) and created when the broker starts. Services connect, publish and
consume. They never create queues.

## The topology in one picture

```
event-service       ──► event.events        ┐
checkout-service    ──► checkout.events     │  topic exchanges, one per publisher
reservation-service ──► reservation.events  │
ticket-service      ──► ticket.events       ┘
                              │
          routing key (e.g. event.published) matches a binding?
                 │ yes                                   │ no
                 ▼                                       ▼
   <consumer>.<Message>  (quorum, one per consumer)   ticketto.unrouted (kept, not lost)
                 │ rejected / poison
                 ▼
   ticketto.dlx ──► <consumer>.<Message>.dlq
```

Each publisher has its own exchange, named after the service without `-service`. A message is
published to its publisher's exchange with its routing key, and reaches one queue per consumer.

| Message | Exchange, routing key | Publisher | Your queue, if you consume it |
|---|---|---|---|
| `EventPublished` | `event.events`, `event.published` | event-service | `queue-service.EventPublished`, `reservation-service.EventPublished`, `ticket-service.EventPublished`, `notification-service.EventPublished` |
| `EventCancelled` | `event.events`, `event.cancelled` | event-service | `queue-service.EventCancelled`, `reservation-service.EventCancelled`, `checkout-service.EventCancelled`, `ticket-service.EventCancelled`, `notification-service.EventCancelled`, `user-service.EventCancelled` |
| `OrderConfirmed` | `checkout.events`, `order.confirmed` | checkout-service | `ticket-service.OrderConfirmed`, `queue-service.OrderConfirmed`, `user-service.OrderConfirmed` |
| `OrderRefunded` | `checkout.events`, `order.refunded` | checkout-service | `notification-service.OrderRefunded` |
| `RefundFailed` | `checkout.events`, `refund.failed` | checkout-service | `notification-service.RefundFailed` |
| `RefundCompleted` | `checkout.events`, `refund.completed` | checkout-service | `ticket-service.RefundCompleted` |
| `ReservationExpired` | `reservation.events`, `reservation.expired` | reservation-service | `checkout-service.ReservationExpired` |
| `RefundRequired` | `reservation.events`, `refund.required` | reservation-service | `checkout-service.RefundRequired` |
| `EventSoldOut` | `reservation.events`, `event.sold_out` | reservation-service | `queue-service.EventSoldOut` |

The full list, including messages with no consumer yet, is in
[`topology/messages.yaml`](../topology/messages.yaml). To consume a message or add a new one, open a
merge request on this repo that edits that file (see the README).

## Connecting

Every service connects as **its own user**, named after the service (`reservation-service`, ...).

| Where | `RABBITMQ_URL` (for reservation-service) |
|---|---|
| Local (your compose stack joined to the `ticketto` network) | `amqp://reservation-service:ticketto-local@rabbitmq:5672/%2F` <!-- local-only password; gitleaks:allow --> |
| Local (your code on the host) | `amqp://reservation-service:ticketto-local@localhost:5672/%2F` <!-- local-only password; gitleaks:allow --> |
| AWS | your own secret, `rabibi/service/<your-service>`: `"valueFrom": "<its ARN>:url::"` (ask the broker owner for the ARN) |

There is **no `guest` user**, so `guest:guest` URLs fail with `ACCESS_REFUSED`. The admin user
(`ticketto`) is for people, not services.

### What your user may do

Your user's permissions are generated from `topology/messages.yaml`, so they always match what
the catalogue says your service publishes and consumes:

| You may | Only on |
|---|---|
| consume and ack (`basic_consume`, `basic_get`, passive `queue_declare`) | your own queues, `<your-service>.<Message>`, but **not** their `.dlq` |
| publish | your own exchange (`<name>.events`), and only with the routing keys of your own messages |
| declare your exchange (as below) | your own exchange |

Anything else closes your channel with **`403 ACCESS_REFUSED`**. That includes:
- declaring or deleting a queue
- reading another service's queue or a DLQ
- publishing to another service's exchange, or with a routing key that isn't yours

Service users can't log in to the management UI. To publish a new message or consume one, add it
to `messages.yaml` (see the README); your permissions follow on the next deploy.

## Publishing

Publish to **your service's exchange** (`event.events` for event-service, `checkout.events` for
checkout-service, ...) with the message's **`routing_key` from `messages.yaml`**, exactly as written:
dotted lower case (`order.confirmed`, not `OrderConfirmed`). The PascalCase name goes in the AMQP
`type` property and the envelope's `message_type`, never in the routing key.

Use a transactional outbox: write the message in the same database transaction as the state change,
and have a relay publish it. Every publish must:

1. be **persistent**: `delivery_mode=2`
2. use **publisher confirms** (`channel.confirm_delivery()`), with the outbox row marked sent **only
   after the broker confirms**. If the publish raises, leave the row and retry it later.
3. set `mandatory=True`
4. set the AMQP properties:
   - `message_id`, the envelope's `message_id`
   - `type`, the message name (`OrderConfirmed`)
   - `correlation_id`, from the `X-Correlation-Id` of the request that caused the message
   - `content_type="application/json"`
5. have a body in the envelope below, with a payload that matches your message's schema in
   [`contracts/`](../contracts/). The publisher owns that schema: change it with a merge request
   here. Consumers list the payload fields they read in `contracts/requirements/<consumer>.yaml`,
   and CI fails if your schema stops requiring any of them.

```json
{"message_id": "<uuid>", "message_type": "OrderConfirmed", "schema_version": 1,
 "occurred_at": "2026-10-18T10:00:00Z", "producer": "checkout-service", "payload": {}}
```

This is how event-service does it (`app/outbox.py`):

```python
channel.exchange_declare(exchange="event.events", exchange_type="topic", durable=True)
channel.confirm_delivery()
channel.basic_publish(
    exchange="event.events",
    routing_key=row.routing_key,  # "event.published"
    body=json.dumps(row.payload),  # the envelope
    properties=pika.BasicProperties(
        content_type="application/json",
        delivery_mode=pika.DeliveryMode.Persistent,
        message_id=row.message_id,
        type=row.message_type,  # "EventPublished"
        correlation_id=row.correlation_id,
    ),
    mandatory=True,
)  # returns only once the broker has stored it; raises otherwise
row.delivered_at = now()
```

### Declaring your exchange is allowed, but only exactly like this

`exchange_declare("<name>.events", exchange_type="topic", durable=True)`, with no arguments.

Declaring doesn't create a second exchange. An exchange declare means "make sure this exists": if
an exchange with that name already exists with the same settings, the call does nothing; if it is
missing, it is created. The broker creates every `*.events` exchange at boot, so a publisher's
declare is a no-op. It's allowed because event-service and queue-service already do it, and it
does no harm.

A declare with **different** settings (another type, `durable=False`, any arguments) closes your
channel with `406 PRECONDITION_FAILED`. The alternate exchange that keeps unroutable messages is set
by a policy, not an argument, so the plain declare above still matches.

## Consuming

### Consume your queue by name; don't declare it

A `queue_declare` call creates the queue if it's missing. If it exists, RabbitMQ checks that every
argument matches. Ours are quorum queues with dead-letter arguments, so a plain
`queue_declare("x", durable=True)` doesn't match and gets `406 PRECONDITION_FAILED`.

- **Python:** call `basic_consume` on the queue name. At most, call
  `queue_declare(name, passive=True)` at startup. A passive declare only checks that the queue
  exists (404 if not), and never creates or compares anything.
- **Spring:** use `@RabbitListener(queues = "reservation-service.EventPublished")`. Don't define
  `Queue`, `Exchange` or `Binding` beans. Set
  `spring.rabbitmq.listener.simple.missing-queues-fatal=true` so startup fails loudly if the queue
  is missing.

### Acknowledge manually, after your work is committed

**Never auto-ack.** With auto-ack the broker deletes the message as soon as it sends it, so a crash
mid-way loses it. The broker's `ticketto-check` lists any auto-ack consumer, and the deploy job
fails if there is one.

| Outcome | Python (pika) | What happens |
|---|---|---|
| Done (DB/Redis change committed) | `ch.basic_ack(tag)` | Removed from the queue. |
| Failed, might work later (DB down, timeout) | `ch.basic_reject(tag, requeue=True)` | Redelivered. After **5** redeliveries it is moved to `<queue>.dlq`. |
| Can never work (invalid payload, unknown event) | `ch.basic_reject(tag, requeue=False)` | Straight to `<queue>.dlq`. |
| Your process crashes before acking | — | Redelivered. This counts towards the 5. |

**Use `basic_reject`, not `basic_nack`, to retry.** On RabbitMQ 4.3, `basic_nack(requeue=True)` does
not count towards the redelivery limit, so a message that always fails would loop forever.
`tests/integration/test_dead_lettering.py` checks this, so an upgrade that changes it will be noticed.

```python
channel.basic_qos(prefetch_count=10)


def on_message(ch, method, properties, body):
    try:
        handle(json.loads(body))  # idempotent: skips message_ids it has already seen
    except PermanentError:
        ch.basic_reject(method.delivery_tag, requeue=False)
    except Exception:
        ch.basic_reject(method.delivery_tag, requeue=True)
    else:
        ch.basic_ack(method.delivery_tag)


channel.basic_consume("checkout-service.EventCancelled", on_message, auto_ack=False)
```

For Spring, use acknowledge mode `AUTO` (the container acks once your listener returns; it is
**not** AMQP auto-ack) or `MANUAL`. Never use `NONE`. For retries, use in-process retries, then
reject without requeue, so the broker's DLQ catches what still fails:

```properties
spring.rabbitmq.listener.simple.acknowledge-mode=auto
spring.rabbitmq.listener.simple.prefetch=10
spring.rabbitmq.listener.simple.default-requeue-rejected=false
spring.rabbitmq.listener.simple.retry.enabled=true
spring.rabbitmq.listener.simple.retry.max-attempts=3
spring.rabbitmq.listener.simple.retry.initial-interval=1s
```

**Why Spring retries 3 times when the broker allows 5.** They are two different counters:

- `retry.max-attempts=3` is Spring calling your listener up to 3 times **inside one delivery**,
  without giving the message back to the broker. After the third failure, Spring rejects it
  without requeueing, so it goes straight to `<queue>.dlq`.
- The broker's limit of 5 counts **redeliveries**: messages given back with
  `basic_reject(requeue=True)`, or held by a consumer that crashed.

Spring gives failed messages back with `basic.nack`, and on RabbitMQ 4.3 a nack with requeue does
not count towards the limit of 5 (see above), so a message that always fails would loop forever.
That's why Spring must not requeue (`default-requeue-rejected=false`) and retries in-process
instead. In a Spring service the broker's 5 still protects you against crashes: a message that
crashes the consumer every time is dead-lettered after 5 redeliveries.

### Expect duplicates and disorder

- **Duplicates.** Delivery is *at least once*: after a crash or a broker restart you can receive a
  message again. Record each `message_id` you've processed, in the same transaction as your
  change, and skip repeats.
- **Order.** Don't rely on the order messages arrive in. For example, `EventCancelled` can arrive
  before `EventPublished`. A cancellation always wins: never let an older `EventPublished` reopen a
  cancelled event. Event messages carry `event_version`: ignore a message whose version is not
  newer than the one you already have.
- **Slow consumers.** A consumer holding an unacknowledged message for more than **30 minutes** has
  its channel closed, and the message is redelivered.

#### Why not one queue for `EventPublished` and `EventCancelled`, to keep them in order?

A single queue would only keep them in order while nothing goes wrong. RabbitMQ delivers a queue's
messages in the order they arrived only when they come from one publisher channel, go to a single
consumer, and are never redelivered. Each of these breaks that:

- **Retries.** If `EventPublished` is rejected for a retry while the consumer moves on, the
  `EventCancelled` behind it is processed first.
- **Prefetch and more than one consumer.** With `prefetch_count=10`, or two instances of your
  service, several messages are handled at once and can finish in any order.
- **More than one publisher.** Two outbox relays, or a retry after a failed publish, can send
  messages out of their original order.

So consumers have to handle disorder anyway, and `event_version` does that whatever the queue
layout. Separate queues also give each message type its own backlog and DLQ, so one message type
that keeps failing doesn't hold up the others.

## Changing a contract

A contract is a message's catalogue entry (`topology/messages.yaml`), its payload schema
(`contracts/<Message>.payload.schema.json`, owned by the publisher) and the fields each consumer
reads (`contracts/requirements/<consumer>.yaml`, owned by that consumer). Every message, field and
reader is on [AMQP_Message_Contracts.md](AMQP_Message_Contracts.md).

1. Open a merge request on this repo that edits those files, then run
   `python -m tools.generate_definitions` and commit what it regenerates.
2. Bump `contracts/VERSION` and add a `contracts/CHANGELOG.md` entry naming the messages and
   services affected:

   | Change | Bump |
   |---|---|
   | descriptions, examples | patch |
   | new message, consumer or optional field; a consumer reading fewer fields | minor |
   | anything that can break a publisher or consumer (a removed or renamed field someone reads, a new required field, a type change, a routing key change, a removed consumer) | major |

   `python -m tools.check_contract_compat --base-ref origin/main` lists every change, its level
   and the services it affects, and tells you the version you need.
3. The merge request's pipeline then checks it:
   - `contracts-compat`: the bump is big enough
   - `contracts-python`: the package builds and passes its tests
   - `contracts-downstream`: the contract tests of every affected service run against this
     change (a pre-release, `X.Y.Z.devN`), on that service's current code. The failing job in
     that service's project is linked.
     - **patch or minor: if one fails, this merge request fails.** These versions promise not to
       break anyone, so the change is breaking after all: make it compatible, or make it major.
     - **major: a failure is a warning.** The merge request can merge, and the
       `contract-tests major change` job lists the services that must update before they move
       to the new major. Nothing breaks meanwhile: each service stays on its pinned version.
4. After the merge, the `main` pipeline publishes `ticketto-contracts X.Y.Z`. Each service bumps
   its pinned version when it's ready. If the change adds queues, bindings or permissions,
   someone presses **deploy-broker**.

**Breaking changes, without breaking anyone:** don't remove or rename a field in one step.
1. Add the new field (required, so consumers can count on it); keep sending the old one. (minor)
2. Each consumer switches to the new field and updates its requirements file.
3. Remove the old field. (major) The static tests refuse this while any consumer still lists it.

Or add a new message name (`OrderConfirmedV2`) with its own routing key and queues, run both side
by side, and remove the old one once nobody consumes it. Never bump the envelope's
`schema_version`: consumers reject versions they don't know.

**Contract tests in your service:** see
[packages/python/README.md](../packages/python/README.md). Once your pipeline has a
`contract-tests` job, add your project to `scripts/ci/contract-test-projects.yaml` so contract
merge requests check your service too.

## Testing your service against the real broker

```sh
# in rabbitmq-service
docker compose up -d --build
```

Then in your own `docker-compose.yml`, drop your RabbitMQ container and join the shared network:

```yaml
services:
  your-service:
    environment:
      RABBITMQ_URL: amqp://your-service:ticketto-local@rabbitmq:5672/%2F # local-only password; gitleaks:allow
    networks: [default, ticketto]

networks:
  ticketto:
    external: true
```

Use your service's name as the user, so your permissions match AWS and a missing one shows up
locally first. Open http://localhost:15672 (admin user `ticketto`, password `ticketto-local`)
to see your queue, its messages and its DLQ. `docker compose exec rabbitmq ticketto-check` tells you whether your
consumer is using auto-ack.
