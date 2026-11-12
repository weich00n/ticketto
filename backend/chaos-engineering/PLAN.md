# Chaos engineering plan

**Status (2026-11-06):** every planned experiment has run. C1-C3 ran on 2026-10-31 and under spike
load on 2026-11-01; all of C1-C8 ran under spike load on 2026-11-05. One failed (C8) and led to a fix
in checkout-service. Results: `RESULTS.md`. How to run them again: `experiments/spike.md`.

## Context

Chaos engineering is one of the team's three self-directed research items (with serverless CI/CD for the
notification service, and Playwright end-to-end tests owned by a teammate). It is graded on: (1) value
added, (2) how well that value is evidenced, (3) where it was not useful and when it would be.

End-to-end tests prove the three scenarios work on the happy path. Chaos engineering is separate: it
injects failures and checks the **failure guarantees the team's spec already promises**
(`reservation-service/docs/302 Specifications.pdf`, sections 4-7, 10, 11 and "Important Controls" in each
scenario). Each experiment's hypothesis is a quoted spec claim, so a failed hypothesis is a real finding
with a fix, and a held one is evidence the design works. The team has no separate fallback document;
the spec is it.

Everything lives in `backend/chaos-engineering`. Teammates' services are targeted by name; this repo
doesn't change their code (the C8 fix went through checkout-service's own MR).

## What production looks like (affects blast radius)

As of 2026-11-05:

| Component | Where | Instances |
|---|---|---|
| reservation-service, checkout api, queue-api | ECS `TicketPlatform` | **2-4** tasks each (CPU autoscaling) |
| checkout worker, event api / worker, ticket api / worker, user api / worker, notification api / worker, frontend | ECS `TicketPlatform` | **1** each |
| queue-admitter, queue-consumer | ECS `TicketPlatform` (moved from `queue-service-staging`) | **1** each |
| RabbitMQ `rabibi` | EC2 t4g.small | 1 |
| reservation Redis, queue Redis | EC2 t2.nano, VPC-only | 1 each |
| MySQL `db-ticketing` | RDS db.t3.micro, single-AZ, shared, VPC-only; `max_connections` 150 (parameter group `ticketto-mysql84`) | 1 (not a target: too shared) |

Every API sits behind the shared internal ALB `ticketto-internal-api` (one target group each), which
API Gateway reaches through one VPC link. Most services run one task, so "stop a task" on them is a full
outage of that service: that is the point of several experiments below (does the rest of the flow wait
and recover, or lose work?).

## Experiments (hypothesis = spec claim)

| # | Fault (FIS action) | Spec claim tested | Pass if | Result (2026-11-05) |
|---|---|---|---|---|
| C1 | Stop RabbitMQ `rabibi` for 3 min (`aws:ec2:stop-instances`, auto restart) | "Critical changes use a transactional outbox. Consumers safely handle duplicate and delayed messages." | HTTP paths keep working; the backlog drains; every message is delivered once in effect; queue slots freed | Held: 199 s of no admissions, tickets late, no errors |
| C2 | Reboot reservation Redis | "Redis failure does not erase authoritative hold records. All Redis state is rebuildable." + 503 = "temporary dependency failure" | Holds get fast 503s, not hangs; afterwards Redis = MySQL (no oversell) | Held: 37 s stall, fast 503s, no oversell |
| C3 | Stop **all** reservation tasks while payments are confirming | "A timeout during confirmation means the outcome is unknown. Checkout retries or checks; it does not immediately refund." | Paid orders end CONFIRMED after reservation returns; none refunded wrongly, none stuck | Held for stock; failed holds block tickets for 11 min; 1 buyer charged with no ticket |
| C4 | Stop `queue-admitter` | "Queue capacity recovers after completion or expiry." Admission is atomic | Admission pauses then resumes in order; nobody admitted twice | Held, no visible effect |
| C5 | Stop `checkout-service-worker` during a cancellation | "Checkout retains refund obligations... Duplicate cancellation events do not create duplicate refunds." | Every paid order gets exactly one refund after the worker returns | Held: 200/200, one refund each |
| C6 | Stop `ticket-service-worker` during an `OrderConfirmed` burst | "Database uniqueness prevents duplicate issuance." Messages wait in the queue | Every order gets its ticket after restart; nothing in a DLQ | Held: tickets p95 38 s |
| C7 | Stop one of the 2 reservation tasks | Redundancy + the ALB | Few failed holds; replacement in ~1 min. Expected "nothing found" (criterion 3) | Held, nothing found |
| C8 | Stop every checkout API task | Reliability: "operations can be retried safely" | No buyer charged without a ticket or a refund | **Failed:** 178 charged with no ticket until Stripe retried the webhook (30-60 min). Fixed in checkout-service MR !13 |

**Task counts were not changed up front.** Each experiment ran on the counts the owners chose.
Workers and the admitter stay at 1 on purpose: C4-C6 test that queued work waits and resumes, and the
admitter is single by design (two would admit twice).

Not done: RDS faults (shared single-AZ micro, too much blast radius), network latency injection
(Fargate FIS network actions need the SSM agent sidecar; out of time budget), and a C8 rerun after the
fix. They go in the report's "when it would be useful" section.

## Repo layout

```
README.md            what this is, how to run it, current status
PLAN.md              this plan
RESULTS.md           one section per run day, one subsection per experiment
terraform/           FIS role, templates C1-C8, abort alarm, /fis/chaos-engineering log group,
                     the chaos-engineering dashboard, the driver's security group
driver/              buyer traffic through the API Gateway (Python): users, event, buy, cancel,
                     refunds, report, cleanup
checks/              invariant checker (Python), runs on a laptop through SSM tunnels
experiments/         spike.md (runbook), run-one.sh, run-c5.sh, check-only.sh, timeline.py
```

### Driver (`driver/`, runs on a laptop)

- Goes through the public API Gateway with Cognito test users (USER_PASSWORD_AUTH, as the frontend
  does), so every request takes the production path: JWT, gateway, ALB, service. An earlier plan to
  bypass the gateway from inside the VPC (k6 as an ECS task) was dropped: the gateway route is what
  buyers use.
- S1: create and publish a test event as the organiser. S2: join the queue, poll until admitted,
  hold, create the order (Idempotency-Key), confirm the PaymentIntent with Stripe test mode
  (`pm_card_visa`) so Stripe sends the real webhook, poll until settled, wait for the ticket.
  S3: cancel the event; `refunds` checks every paid order got exactly one refund.
- Signs every buyer in before the window, caps Stripe calls (`--pay-concurrency`, default 10), and
  records each buyer's outcome and per-step timings in `outcomes.jsonl`.

### Checker (`checks/`)

Reads reservation's and queue's MySQL schemas and both Redis instances after each run:

1. No oversell: Redis available + active holds + sold = total per category, and sold <= total.
2. No stale holds: no active hold past its expiry (with a grace period).
3. Slots freed: no active admission whose order is confirmed, and queue capacity recovered.

It doesn't read checkout or ticket-service. Money is checked through the API (`driver refunds`) and
Stripe (PaymentIntents by `metadata['order_id']`); duplicate tickets were not checked.

### Evidence per experiment (`RESULTS.md`)

Hypothesis (spec quote) / fault and FIS experiment ID / load and event / buyer outcomes, step p95,
retried errors / confirmations per 10 s and the longest gap / checker output / RDS load / held? /
finding and fix / rerun.

## Guardrails

Ask before every FIS start, `tofu apply` and push, unless the user approved a batch. One experiment at
a time, 1-5 min of fault. Every template stops on the abort alarm. Email goes to the log backend for a
session and is restored after it. If an invariant fails, stop and report; don't rerun straight away.
Don't change another team member's service; propose the fix to its owner.

## Verification

- `tofu plan` in `terraform/` shows only chaos resources.
- A no-fault control run at the same load before the fault runs: every ticket sold, checker PASS.
- Checker unit tests (`checks/tests`), driver unit tests (`driver/tests`).
- Each experiment: FIS experiment reaches `completed`, abort alarm stays OK, `RESULTS.md` section filled.
