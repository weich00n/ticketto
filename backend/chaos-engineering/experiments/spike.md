# Runbook: experiments under a ticket-release rush

Every experiment runs with the traffic a popular release gets: 2,500 buyers arriving in the first
minute for 1,250 tickets, so the event sells out. A no-fault control run comes first, so each fault
run is compared with the same load without the fault. This is how C1-C8 ran on 2026-11-05 (results
in `RESULTS.md`, last section); the 2026-11-01 spike runs used the same load with an earlier version
of these steps.

## Before you start

1. **Tell the team.** Every service shares `db-ticketing` (db.t3.micro), and C1 stops the broker for
   everyone. Agree a window with no deploys, not within 24 h of the demo.
2. **Email off.** `notification-service-worker` (ECS) sends every email, through SES, which is in its
   sandbox (200 a day, verified addresses only). Test buyers are `@example.com`, so every email would
   fail, retry and trip the abort alarm. Register a copy of its live task definition with
   `EMAIL_BACKEND=log`, deploy it, and note both revisions in `RESULTS.md` (2026-11-05: `:6` -> `:7`).
   Your IAM user needs `iam:PassRole` on `notification-service-worker-task` and
   `notification-service-execution` for this.
3. **Environment**, in every terminal (values also in `driver/env.example`):

   ```sh
   cd backend/chaos-engineering
   export AWS_REGION=us-east-1 GATEWAY_URL=https://x0vxfeenrl.execute-api.us-east-1.amazonaws.com \
     COGNITO_USER_POOL_ID=us-east-1_2g8EYCEa1 COGNITO_CLIENT_ID=2jr04pk573rr5pe95jm0chv47o
   ```

4. **Checker connections:** `bash checks/make-env.sh` once (it reads Secrets Manager and writes
   `checks/.env`). The runners open the SSM tunnels themselves right before each checker run
   (`checks/tunnels.sh`): RDS and both Redis instances are VPC-only, SSM closes a tunnel after 20
   idle minutes, and C2's reboot drops them anyway.
5. **Buyers:** `driver/state/users.json` needs 2,500 unused buyers per event
   (`python -m driver users --count <n> --workers 10` adds more).
6. **Steady state:** every DLQ empty, the abort alarm OK, every service at its desired count, and
   `db-ticketing` well below `max_connections` (150 since 2026-11-05):

   ```sh
   aws cloudwatch describe-alarms --alarm-names chaos-engineering-abort --query 'MetricAlarms[].StateValue' --output text
   aws ssm send-command --instance-ids i-099e873a0ccf998db --document-name AWS-RunShellScript \
     --parameters 'commands=["docker exec rabbitmq rabbitmqctl list_queues -q name messages consumers"]'
   ```

   Keep the `chaos-engineering` CloudWatch dashboard open during the runs.

## Events

One fresh event per run, so sales from one run never mix with another's. Create them all at once:
their 16-minute waits overlap, and later runs start as soon as they're called, because their release
has passed. C5 uses a small event.

```sh
for r in n-control n-c4 n-c7 n-c8 n-c6 n-c2 n-c3 n-c1; do driver/.venv/bin/python -m driver event --run $r --capacity 1250; done
driver/.venv/bin/python -m driver event --run n-c5 --capacity 200
```

## Runs (one at a time)

```sh
bash experiments/run-one.sh <run> [<template> <seconds after the first buyer>]
bash experiments/run-c5.sh n-c5 EXT3xFdEqEBpFeG 200
python3 experiments/timeline.py n-control <run>      # confirmations per 10 s, longest gap
```

`run-one.sh` starts 2,500 buyers (signed in first, about 30 s, not measured), starts FIS the given
number of seconds after the first buyer, waits for the sale and the experiment to end, then runs the
checker and records RDS CPU and connections, all into `driver/state/runs/<run>/`. If only the checker
failed, `experiments/check-only.sh <run>` repeats that half.

| Run | Template | FIS start | Why |
|---|---|---|---|
| `n-control` | none | - | Baseline: sell-out time and when confirmations peak (`T_peak`, about 30 s) |
| `n-c4` | `EXT3BGzj5TjXxCnC` stop queue-admitter | +10 s | While the waiting room is admitting |
| `n-c7` | `EXTDfp9DSEtPBbpeL` stop 1 of 2 reservation tasks | +0 s | The ALB takes about 40 s to drain it, so the loss lands near `T_peak` |
| `n-c8` | `EXTD2uNmAYsKBpZKv` stop every checkout API task | +0 s | Same; orders and webhooks hit the outage |
| `n-c6` | `EXT2FLJ5RNJQijBW` stop ticket-service-worker | +20 s | Just before the `OrderConfirmed` peak |
| `n-c5` | `EXT3xFdEqEBpFeG` stop checkout-service-worker | after the sale | `run-c5.sh` cancels the event while the worker is down, then checks refunds |
| `n-c2` | `EXT3M2X5BcggJ1pc` reboot reservation Redis | +30 s (`T_peak`) | The ~30 s outage lands while holds are busiest |
| `n-c3` | `EXT3gdgbLyoGpYcic` stop every reservation task | +0 s | Confirmations hit the outage at about `T_peak` |
| `n-c1` | `EXT2ujd4mwwqHn1Lk` stop the broker for 3 min | +30 s | The outage covers the rest of the sale |

Between runs, wait for recovery: every service back at its desired count with healthy targets, the
broker's queues empty with their consumers, no stray `session-manager-plugin` processes.

## What to check per run

- **Stock** (`checker.txt`): sold = confirmed, never above capacity; no stale holds; slots freed.
- **Buyers** (`summary.txt`, `timeline.py`): who ended where, step p50/p95, retried errors, the longest
  gap in confirmations, and the sell-out time against the control.
- **Money**, for any run with `order_expired` or `settle_failed` buyers who reached the pay step:
  look the PaymentIntent up in Stripe by `metadata['order_id']`. Charged with no ticket and no
  refund is a failure (C8, 2026-11-05). For C5, `python -m driver refunds --run n-c5` checks every
  paid order has exactly one succeeded refund.
- **Platform:** ECS service events (task replacements), the dashboard, and `rds.txt`, so a database
  or checkout bottleneck isn't mistaken for the fault.

## Afterwards

- Restore the notification worker's original revision and confirm `EMAIL_BACKEND=ses`.
- Recheck any charged-no-ticket buyers in Stripe until each is refunded.
- Before the demo: `python -m driver cleanup` deletes the test Cognito accounts (profiles and events
  stay; cancel events still on sale with `cancel --run`).

## Limits

- One laptop runs 2,500 threads; check its CPU stays well below 100% in the control run, or the
  driver becomes the bottleneck. Keep it awake (`caffeinate -ims`).
- Access tokens last 60 minutes from sign-in, so keep each run under an hour.
- Stripe test mode allows about 25 requests a second; `--pay-concurrency` (default 10) keeps the
  driver below that.
- The checker reads reservation and queue only: duplicate tickets and checkout's own state are
  checked through the API and Stripe, not the database.
