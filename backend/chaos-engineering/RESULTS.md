# Results

One section per experiment, in the format from `PLAN.md`.

Run on 2026-10-31 (UTC; 2026-11-01 SGT). Driver: buyers through the API Gateway with Cognito test
users, each through queue -> hold -> order -> Stripe test payment -> confirmation -> ticket. Checker:
`python -m checker --event-id <id>` (reservation MySQL + Redis, queue MySQL + Redis). Rate is 2
buyers/min unless noted, because SES is in its sandbox (200 emails a day) and every ticket sends one.

Steady state before the runs: every DLQ of a service under test empty. Already non-empty and left
alone: `notification-service.EventPublished.dlq` 2, `notification-service.OrderRefunded.dlq` 4,
`ticketto.unrouted` 6. The notification queues show 0 consumers because notification-service is a
Lambda that EventBridge runs every minute; it drains its queues per run rather than holding a
consumer, and its main queues stayed at 0 throughout. Its two DLQs did not change during the runs.
`ticketto.unrouted` grew by exactly one message per paid order (26 after C2, 145 after C3, 157 during
C1): checkout's `PaymentCompleted` (`payment.completed`), which `messages.yaml` gives no consumer yet,
so the `events-ae` policy parks it in the alternate exchange's queue by design. Not caused by the
faults. The queue refuses (never drops) past 50 000 messages, so it grows without bound until
something consumes `PaymentCompleted`.

## C2: reboot reservation Redis

- **Hypothesis (spec):** "Redis failure does not erase authoritative hold records. All Redis state is
  rebuildable." A 503 means "temporary dependency failure".
- **Fault:** FIS `EXT3g36kg6U2dwqz` (reboot `reservation-Redis-v1.1`), experiment `EXPnxzo4fJMvHi66fU`,
  18:05:36-18:05:49; Redis unreachable for about 30 s after that.
- **Load:** event `f0705db5-7b1d-4d4b-a83a-afc1b3e1a078`, 20 buyers 18:03:26-18:13:07, image `a6b72ae`
  (includes the fail-fast fix `70eb786`).
- **Result:** 20/20 confirmed with a ticket. The one buyer who reached the hold step during the
  outage got eight fast `503 DEPENDENCY_UNAVAILABLE` responses over 31 s, retried, and was confirmed.
  No request hung. Hold p50 425 ms, p95 746 ms.
- **Checker:** PASS. Sold 20, active holds 0, Redis available 1980 of 2000.
- **Held?** Yes. **Before:** the 2026-10-25 E4 run on the image before `70eb786` (k6 probe, not this
  driver): about 28 s outage, 6.6% of requests failed, hanging to k6's 5 s timeout.
- **Limit:** only 1 buyer hit the 30 s window at 2/min. A burst rerun (20/min around the reboot) would
  give a rate; skipped today for the email budget.

## C3: stop every reservation task while payments confirm

- **Hypothesis (spec):** "A timeout during confirmation means the outcome is unknown. Checkout retries
  or checks; it does not immediately refund."
- **Fault:** FIS `EXT3gdgbLyoGpYcic` (stop every reservation-service task), three runs on event
  `a76a14e5-2bb5-4695-ae97-3bb1628b957d`.
- **Note on timing:** ECS takes about 40 s after FIS stops a task before it deregisters it from the
  load balancer (18:28:00 -> 18:28:44 in run 2), so a fault timed a few seconds before payment misses.

| Run | Buyers | FIS | What the outage hit | Result |
|---|---|---|---|---|
| 1 | 20 at 2/min, 18:13:59-18:23:41 | `EXPMCzqrkPoynmnKW2` 18:16:02 | Holds only: 503 then gateway 502 until about 18:18:25 (ECS reported 2 running at 18:17:46) | 19 confirmed; 1 `hold_failed` after retries, before paying (no money taken) |
| 2 | 50 at 20/min, all pay at 18:28:03 | `EXPrX6abPK993Je5Fp` 18:28:00 | Nothing: all confirmed by 18:28:12, tasks deregistered 18:28:44 | 50 confirmed; not a test of the claim |
| 3 | 50 at 20/min, all pay at 18:34:01 | `EXPniF9wDGRhnS2KjY` 18:33:03 | **Confirmation:** deregistered 18:33:41, new targets 18:34:10 | **50/50 CONFIRMED with a ticket**; settle 67-78 s (normally about 3.6 s) |

- **Driver change for runs 2-3:** `buy --pay-at <minutes>`: every buyer holds and orders, then all
  pay at that time, so FIS can be timed so that confirmations land in the outage.
- **Checker:** PASS after each run. After run 3: sold 119 = 19 + 50 + 50 confirmed buyers (no double
  confirmation), active holds 0, Redis available 1881.
- **Held?** Yes. Paid orders waited for reservation and confirmed; none refunded, none stuck.
- **Other:** Stripe test mode returned `429 rate_limit` to about half the payments when 50 paid at
  once (retried, all succeeded). Run 1's 502s lasting past ECS's "running" suggest the gateway's
  route to reservation lags task replacement.
- **Not checked:** the checker does not read checkout's database or Stripe, so "none refunded" rests
  on every order's final status being CONFIRMED.

## C1: stop the RabbitMQ broker for 3 minutes

- **Hypothesis (spec):** "Critical changes use a transactional outbox. Consumers safely handle
  duplicate and delayed messages."
- **Fault:** FIS `EXT2ujd4mwwqHn1Lk` (stop `rabibi`, FIS starts it again), experiment
  `EXPYtQL8yh2Meqa4pi`, 18:38:16-18:41:39. RabbitMQ healthy and consumers reconnected by 18:42:15.
- **Load:** event `2338e363-a67b-47ed-9244-96f24e7a8e0e`, 20 buyers 18:36:14-18:45:55.
- **Result:** 20/20 confirmed with a ticket. The HTTP path (queue, hold, order, payment, confirmation)
  was unaffected while the broker was down. Ticket issuance, which follows `OrderConfirmed` on the
  broker, waited: the 8 buyers who confirmed during the outage got their tickets 11-192 s later, all
  between 18:42:03 and 18:42:05, once the broker returned.
- **Checker:** PASS. Sold 20, active holds 0, Redis available 1980. Every work queue empty with its
  consumers after recovery; no new DLQ messages.
- **notification-service:** its runs at 18:38, 18:39, 18:40 and 18:41 logged `could not drain the
  queues` (connection refused), still sent due emails and finished; nothing it consumes was published
  during the outage, so nothing waited for it. Its log group is not watched by the abort alarm.
- **Held?** Yes. Messages waited in the outbox and were delivered once; one ticket per buyer.

Abort alarms (`chaos-engineering-abort`, `reservation-service-chaos-abort`) stayed OK throughout, and
every FIS experiment reached `completed`. SES: 160 of 200 sent in the 24 h window after these runs.

C4-C8 were not run that day; they ran on 2026-11-05 under spike load (last section).

## Spike runs: C2, C3, C1 under a ticket-release rush (2026-11-01)

Runbook: `experiments/spike.md`. Every run: a fresh event with 1,250 tickets, 2,500 buyers arriving in
the first minute (all signed in beforehand), Stripe payments capped at 10 at a time. Times are UTC.

**Session changes, all restored at 06:51:**

- Email set to `EMAIL_BACKEND=log` at 04:45, so the runs neither hit the SES sandbox limits nor
  trip the abort alarm (delivery was shown in the 2026-10-31 runs): `notification-service` Lambda
  `ses` -> `log`; `ticket-service-worker` task def `:4` (`smtp`) -> `:5` (`log`).
- `checkout-service` scaled from 1 to 3 tasks at 06:04:51, after the first control run showed it was
  the bottleneck (below).

The driver laptop stayed above 82% idle, and `db-ticketing` peaked at 24% CPU with about 50
connections and full CPU credits, so neither the driver nor the database limited any run.

| Run | FIS start (after first buyer) | Confirmed | Checker | Confirmations stalled | Sold out after |
|---|---|---|---|---|---|
| `spike-control` (checkout ×1) | none | 1,122 + 128 late | PASS | n/a: checkout saturated | ~11 min |
| `spike-control-3x` | none | 1,250 | PASS | none | 121 s |
| `spike-c2` Redis reboot | 32 s | 1,250 | PASS | ~30 s (50-80 s) | 148 s |
| `spike-c3` stop reservation | 3 s | 1,250 | PASS | ~90 s (30-120 s) | 694 s |
| `spike-c1` stop broker 3 min | 32 s | 1,250 | PASS | ~170 s (70-240 s) | 308 s |

Nothing was oversold in any run: sold was exactly 1,250, with no stale holds and every queue slot
freed. Buyers who missed out ended `queue_closed` or `INSUFFICIENT_INVENTORY`, not stuck.

### Control with checkout at 1 task (`spike-control`, event `f5776776`)

At 1 task × 0.25 vCPU, checkout's CPU went to 100% within a minute. 371 buyers had `ReadTimeout` on
order (243 gave up) or settle (128 gave up: they had paid and were confirmed after the driver gave
up). Confirmations held at about 1.4 a second, the waiting room admitted buyers that slowly (queue
p50 10.6 min), and confirmations jumped from about 14 to 115 per 10 s once checkout reached 3 tasks.
**Finding:** at a popular release, checkout at 1 task is the bottleneck. It needs more tasks or
autoscaling, which it doesn't have today.

### Control (`spike-control-3x`, event `a4857ff8`)

Checkout at 3 tasks. Every one of 1,250 confirmed in 121 s, with no errors at any step:
confirmations held at about 110 per 10 s from 10 s to 120 s. `T_peak` = 30 s.
Order p95 2.8 s, pay p95 18.8 s (the Stripe cap), settle p95 5.1 s.

### C2: reboot reservation Redis (`spike-c2`, experiment `EXPRXbASf3WHLJ5Pg6`)

FIS 06:26:31-06:27:07. **Held.** All 1,250 confirmed. Holds and orders got fast
`503 DEPENDENCY_UNAVAILABLE` (140) and `503 RESERVATION_UNAVAILABLE` (190): 14 buyers hit them at hold and 19 at order, and every one got through on retry.
Confirmations stopped for about 30 s, then caught up at up to 158 per 10 s; it sold out 27 s
later than the control. `reservation-service-chaos-abort` went to ALARM at 06:28:44 (441 and 263 error
log lines a minute, against a threshold of 20) and back to OK at 06:29:44. That was the outage being logged, and the
log metric lagged, so the alarm went off after FIS had already completed. At this load a
30 s Redis outage trips that alarm, which it didn't at 2 buyers/min.

### C3: stop every reservation task (`spike-c3`, experiment `EXPYCNY2Vzg4twWwLN`)

FIS 06:31:10-06:31:46, 3 s after the first buyer. The runbook's start of "45 s before `T_peak`" would
have been before the first buyer, because the control peaked at 30 s. **Held for correctness:**
sold exactly 1,250, every one confirmed, checker PASS, abort alarms OK. Confirmations stopped for
about 90 s until ECS replaced both tasks. 59 buyers gave up on the order step after 826
`503 RESERVATION_UNAVAILABLE`s. Their holds kept those 59 tickets until the holds expired (about
11 min), then they sold to buyers still waiting (680-694 s). **Finding:** that's why the sale took
694 s instead of about 220 s. A hold whose order failed blocks its ticket for the full hold time.

### C1: stop the RabbitMQ broker for 3 minutes (`spike-c1`, experiment `EXP217RAEXQZ5Z2Adir`)

FIS 06:45:28-06:48:55. **Held.** All 1,250 confirmed with a ticket, and no buyer saw an error at any
step. Two things slowed down: tickets (p95 221 s, against 5.5 s in the control) waited for
`OrderConfirmed`, and the waiting room admitted no one for about 170 s, because it frees slots on
broker messages. Both caught up once the broker was back. Afterwards every queue had 0 messages and its
consumers back. **Finding:** the broker outage turns into time spent in the waiting room, not errors.

**Not changed by the runs:** `ticketto.unrouted` grew by one per paid order (165 -> 6,415 across the
five runs), as expected; the notification DLQs stayed at 2 and 4.

## All experiments under spike load (2026-11-05)

Evidence for every number below: `results/2026-11-05/` (per-run summaries, checker output, FIS
state, the Stripe refund check, and the figure). Same load as the 2026-11-01 spike runs: a fresh event per run with 1,250 tickets, 2,500 buyers
arriving in the first minute (signed in beforehand), Stripe payments capped at 10 at a time. C5 used
200 tickets and 200 buyers. Times are UTC. Runner: `experiments/run-one.sh <run> <template> <seconds
after the first buyer>` (`run-c5.sh` for C5), which starts the buyers, starts FIS on time, then runs
the checker and records RDS load. `experiments/timeline.py` gives confirmations per 10 s.

**What changed since 2026-11-01:** checkout runs 2-4 tasks with CPU autoscaling (was 1, scaled to 3
by hand for the spike runs); the queue services moved into `TicketPlatform`; the outbox skips a
message after 10 failed attempts (checkout, reservation, event); queue retries back off; reservation's
idempotency claims expire; RDS and both Redis instances are VPC-only, so the checker connects through
SSM tunnels (`checks/tunnels.sh`).

**Session changes:** `notification-service-worker` (which now sends every email; ticket-service no
longer does) `:6` (`ses`) -> `:7` (`log`) at 13:44, restored to `:6` at 15:27. `db-ticketing`
moved to parameter group `ticketto-mysql84` (`max_connections` 150) at 14:40, kept (see C8).
SES sent 18 emails in the 24 h window. Teammates agreed not to deploy during the session.

| Run | Fault (FIS start after first buyer) | Confirmed | Checker | Confirmations stalled | Sold out after | Held? |
|---|---|---|---|---|---|---|
| `n-control` | none | 1,250 | PASS | none | 125 s | baseline |
| `n-c4` | stop queue-admitter (+10 s) | 1,250 | PASS | none | 122 s | yes |
| `n-c7` | stop 1 of 2 reservation tasks (+0 s) | 1,250 | PASS | none (max gap 3 s) | 128 s | yes, nothing found |
| `n-c8` | stop every checkout API task (+0 s) | 1,250 | PASS | ~50 s, then 476 s | 704 s | **no: 178 charged, no ticket** |
| `n-c6` | stop ticket-service-worker (+20 s) | 1,250 | PASS | none | 124 s | yes |
| `n-c5` | stop checkout-service-worker, cancel the event | 200, then all cancelled | refunds PASS | n/a | n/a | yes |
| `n-c2` (rerun) | reboot reservation Redis (+30 s) | 1,250 | PASS | 37 s (48-85 s) | 156 s | yes |
| `n-c3` (rerun) | stop every reservation task (+0 s) | 1,250 | PASS | ~90 s, then 464 s | 695 s | yes for stock, 1 charged with no ticket |
| `n-c1` (rerun) | stop the broker for 3 min (+30 s) | 1,250 | PASS | 199 s (63-262 s) | 322 s | yes |

Nothing was oversold in any run: sold was exactly 1,250 (200 for C5), no stale holds, every queue
slot freed, every work queue and DLQ empty afterwards. Buyers who missed out ended `queue_closed` or
`INSUFFICIENT_INVENTORY`, not stuck. The abort alarm stayed OK all session, and every FIS experiment
reached `completed`. RDS peaked at 19-28% CPU in every run.

### Control (`n-control`, event `3cac9bc5`)

1,250 confirmed in 125 s, about 110 per 10 s from 20 s on. Order p95 7.3 s, pay p95 13.7 s, 2 retried
`ReadTimeout`s. **Finding:** both checkout API tasks (0.25 vCPU each) reach 100% CPU within a minute.
A busy task fails the ALB health check (`/health`, 5 s timeout, 3 failures), so ECS replaced one task
in the middle of this run, and of C4 and C7 too. Autoscaling never acted: its target-tracking alarm
needs 3 minutes, and the rush is over in 2. More tasks to start with, or a longer health-check
timeout, would fix it; autoscaling alone can't react to a two-minute release.

### C4: stop queue-admitter (`n-c4`, experiment `EXPevXczYiX2bAA9VL`)

FIS 14:07:45; ECS started a replacement at 14:08:42, so admission ran without an admitter for about
57 s. **Held, with no visible effect:** confirmations never paused (last at 122 s against 125 s), and
nobody lost their place or was admitted twice (checker: slots freed). 60 retried `ReadTimeout`s and 3
`502`s at order and settle came from the checkout task replacement above, not the admitter.

### C7: stop one of the two reservation tasks (`n-c7`, experiment `EXPFCwmevSNbXajbNS`)

The ALB took the task out at 14:12:42, and its replacement was in by 14:13:11. **Held, nothing
found:** the other task carried the load (hold p95 350 ms, same as the control) and confirmations
never stopped for more than 3 s. This is the expected result for a service that runs two tasks, and
it is the evidence for "where chaos testing did not find anything".

### C8: stop every checkout API task (`n-c8`, experiment `EXPPEGBhf6E4AhmTK8`)

FIS 14:17:00; two new tasks were registered by 14:18:02. **Failed.** Stock stayed correct (1,250
sold, checker PASS), but money did not:

- **178 buyers were charged with no ticket.** They had their order and paid Stripe directly while
  checkout was down (the driver saw 503s on settle). Stripe's `payment_intent.succeeded` webhook got a 503, so
  checkout never learned of the payment. When the payment deadline passed, the expiry sweep marked
  the order `EXPIRED` and the payment `CANCELLED`, and tried to cancel the intent. Stripe refused
  (it had already succeeded), and checkout only logged that. Confirmed in Stripe: all 178 intents
  `succeeded`, SGD 10 each, no refund.
- **The money comes back only through Stripe's webhook retry.** When a retried webhook arrives,
  checkout's late-payment path refunds it (`payment succeeded after the booking ended; refunding`).
  All 178 were refunded this way, each exactly once (checked in Stripe at 16:07): 88 at 14:47-14:49
  and 90 at 15:17-15:19, the two Stripe retries about 30 and 60 minutes after the outage. So buyers
  waited 30-60 minutes for their money. It recovered tonight, but only because Stripe's retries
  got through: Stripe retries for a limited time (hours in test mode, up to 3 days live) and then
  gives up, and checkout had nothing else that would refund these buyers. The 2 buyers whose orders
  were confirmed late were not refunded, which is correct.
- **Fix (checkout-service MR !13, reviewed by the checkout owner, deployed 2026-11-05 ~15:50 as
  `d8381f1`):** when the sweep's cancel finds the intent already succeeded, record the payment and
  request the refund straight away. A new test reproduces C8
  (`test_success_whose_webhook_was_lost_is_refunded_by_the_sweep`): it fails before the fix
  (payment `CANCELLED`) and passes with it. C8 was not rerun after the deploy.
- **Selling slowed too.** Confirmations stopped for about 50 s, then 476 s more (197-673 s) while
  the holds of buyers whose orders failed (`order_failed`, `settle_failed`) blocked their tickets
  until they expired. Same mechanism as C3.
- **Second finding: the shared database ran out of connections.** `db-ticketing` (db.t3.micro) allowed
  about 60 connections (`{DBInstanceClassMemory/12582880}`), and the services' pools already held
  57-58 at rest. When ECS started checkout's replacement tasks, their new pools got `1040 Too many
  connections` (checkout logged it at 14:18 and 14:20, which is where the run's 128 `500
  INTERNAL_ERROR`s came from), and the checker couldn't connect afterwards. Fixed during the session:
  a parameter group with `max_connections` 150, applied with a reboot at 14:40 (database down
  for about 50 s; every service reconnected without errors). Whether the limit also slowed the
  earlier runs is not clear: C6 and C1 afterwards had 1-2 retried errors, but so did the control
  before it. `db-ticketing` is not managed by any OpenTofu stack, so this change is manual.

### C6: stop ticket-service-worker (`n-c6`, experiment `EXPjDZRmbg59WhynL2`)

FIS 14:45:31, 20 s after the first buyer; ECS started a replacement at 14:46:02. **Held.** All
1,250 confirmed and every one got its ticket. `OrderConfirmed` waited in the queue for the ~31 s
outage, so ticket p95 went from 5.5 s to 38 s; confirmations never paused. Nothing in any DLQ
afterwards. (Duplicate tickets were not checked: the checker does not read ticket-service's tables.)

### C5: stop checkout-service-worker during a cancellation (`n-c5`, experiment `EXP7k1D8VuHxSnptL2`)

200 buyers bought all 200 tickets (sale over at 14:52:33). FIS stopped the worker at about 14:52:47,
the event was cancelled at 14:52:54, and the replacement worker started at 14:53:14, so the
cancellation arrived while nothing could process it. **Held:** `EventCancelled` and the refunds
waited, and by 14:57:50 every one of the 200 paid orders was `CANCELLED` with exactly one
`SUCCEEDED` refund (`python -m driver refunds --run n-c5`). No duplicate refunds.

### C2 rerun: reboot reservation Redis (`n-c2`, experiment `EXPcN8KKqUucJHBASe`)

FIS 15:00:04, 30 s after the first buyer. **Held, as on 2026-11-01.** Confirmations stopped for 37
s (about 30 s then). Holds and orders got fast `503 DEPENDENCY_UNAVAILABLE` (290) and
`503 RESERVATION_UNAVAILABLE` (359); everyone got through on retry. Sold out at 156 s (148 s then).
The platform abort alarm stayed OK; the reservation-only alarm that tripped on 2026-11-01 no longer
exists (C2 and C7 moved into this stack).

### C3 rerun: stop every reservation task (`n-c3`, experiment `EXPvy71ESqLLYbfhMk`)

FIS 15:04:13, at the first buyer. **Same as 2026-11-01:** confirmations stopped for about 90 s
(30-120 s), 89 buyers gave up at the order step after 1,240 `503 RESERVATION_UNAVAILABLE`s, and their
holds blocked those tickets for the full hold time, so the sale ended at 695 s (694 s then). Nothing
was changed for this finding between the runs, so the same numbers are expected. One buyer was
charged with no ticket, and refunded at 15:37 by the same Stripe retry path as C8. Most likely its webhook was
handled while reservation was down, the confirm failed, the payment was not recorded, and the sweep
expired the order; this was not traced in the logs. The patch for C8 covers it either way, since
the sweep's cancel would find the intent already paid.

### C1 rerun: stop the broker for 3 minutes (`n-c1`, experiment `EXP21atcWBmgQGqe7Hc`)

FIS 15:19:05-15:22:19. **Held, as on 2026-11-01.** All 1,250 confirmed with a ticket, with 2 retried
errors in the whole run. The waiting room admitted nobody for 199 s (63-262 s; about 170 s then)
because it frees slots on broker messages, and tickets waited for `OrderConfirmed` (p95 213 s; 221 s
then). Sold out at 322 s (308 s then). Every queue was empty with its consumers back afterwards.

### Findings to fix, in order

1. **Charged with no ticket after a checkout outage** (C8, C3): refund in the expiry sweep when
   Stripe says the intent already succeeded. Fixed in checkout-service MR !13 (deployed); not rerun.
2. **Shared database connection limit** (C8): raised to 150 by hand; the pools should also be sized
   (SQLAlchemy defaults of 5 + 10 overflow per process, Hikari 10 per task), and `db-ticketing`
   brought into OpenTofu.
3. **A failed order's hold blocks its ticket for the hold time** (C3, C8; also 2026-11-01): release
   the hold when the order fails, or shorten the hold before payment starts.
4. **Checkout at 2 small tasks saturates at a release, and the health check kills busy tasks**
   (control): start with more tasks for a known release, or relax the health-check timeout.
