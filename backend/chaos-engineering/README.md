# chaos-engineering

Chaos experiments for the Ticketto platform: inject a failure with AWS FIS, drive buyer traffic
through the API Gateway (`driver/`), and check that the system degrades and recovers the way the spec
promises (`checks/`). This is one of the team's self-directed research items, separate from the
Playwright end-to-end tests (which check the happy path and live in the frontend repo).

## Status (2026-11-06)

All eight experiments have run under a ticket-release rush (2,500 buyers for 1,250 tickets in the
first minute). Nothing was oversold in any run.

| # | Fault | Result |
|---|---|---|
| C1 | Stop the RabbitMQ broker for 3 min | Held: admissions and tickets wait, then catch up |
| C2 | Reboot reservation Redis | Held: fast 503s, 37 s stall |
| C3 | Stop every reservation task | Held for stock; a failed order's hold blocks its ticket for 11 min |
| C4 | Stop queue-admitter | Held, no visible effect |
| C5 | Stop checkout's worker during a cancellation | Held: every paid order refunded exactly once |
| C6 | Stop ticket-service's worker | Held: tickets delayed (p95 38 s) |
| C7 | Stop one of two reservation tasks | Held, nothing found |
| C8 | Stop every checkout API task | **Failed:** 178 buyers charged with no ticket until Stripe retried the webhook; fixed in checkout-service MR !13 |

Also found: the shared database's ~60-connection limit (raised to 150), and checkout saturating at a
release while its health check kills busy tasks. Details, numbers and fixes: `RESULTS.md`.

## Layout

| Path | What |
|---|---|
| `PLAN.md` | What we test and why (spec claims), production as tested, guardrails |
| `RESULTS.md` | Every run day: hypothesis, fault, result, invariants, finding, fix, rerun |
| `results/2026-11-05/` | The evidence behind the 2026-11-05 numbers: per-run summaries, checker output, FIS state, Stripe refunds, the chart |
| `experiments/spike.md` | Runbook: setup, the run order, what to check, how to restore |
| `experiments/run-one.sh` | One run: buyers, FIS at a set offset, then checker and RDS load |
| `experiments/run-c5.sh` | C5: sell out, stop the worker, cancel, check refunds |
| `experiments/check-only.sh` | The checker and RDS load for a run that already ended |
| `experiments/timeline.py` | Confirmations per 10 s and the longest gap, per run |
| `experiments/chart.py` | The tickets-confirmed-over-time figure (needs matplotlib) |
| `terraform/` | FIS role, templates C1-C8, abort alarm, the `chaos-engineering` CloudWatch dashboard, the driver's security group |
| `driver/` | Buyer traffic through the API Gateway (`python -m driver --help`; settings in `driver/env.example`) |
| `checks/` | Invariant checker (`python -m checker --event-id <id>`); `make-env.sh` builds its `.env`, `tunnels.sh` opens the SSM tunnels to the VPC-only RDS and Redis |

## Running an experiment

Follow `experiments/spike.md`. In short:

```sh
cd backend/chaos-engineering
set -a; . driver/.env; set +a                       # or export the values in driver/env.example
bash checks/make-env.sh                             # once: checker connection strings
driver/.venv/bin/python -m driver event --run n-c4 --capacity 1250
bash experiments/run-one.sh n-c4 EXT3BGzj5TjXxCnC 10   # FIS 10 s after the first buyer
python3 experiments/timeline.py n-control n-c4
```

Template IDs: `cd terraform && tofu output experiment_templates`. Watch the `chaos-engineering`
dashboard during a run. Before a session, switch the notification worker's email to the log backend,
and restore it afterwards (runbook, step 2).

Setup once: `tofu init -backend-config=backend.hcl && tofu apply` in `terraform/`; a venv in each of `driver/`
and `checks/` (`python3 -m venv .venv && .venv/bin/pip install -e . pytest`); test users with `python -m driver users --count 2500`.
`python -m driver cleanup` deletes the test Cognito accounts.
