# Evidence: spike runs of 2026-11-05

The files behind the numbers in `RESULTS.md` ("All experiments under spike load (2026-11-05)").
One folder per run; times are UTC. The raw per-buyer outcomes (`outcomes.jsonl`, with each test
buyer's email) stay out of the repo, in `driver/state/runs/` on the machine that ran them.

| File | What |
|---|---|
| `event.json` | The run's test event (id, category, release time, capacity) |
| `windows.json` | When the buyers arrived |
| `summary.txt` | The driver's summary: where each buyer ended, step p50/p95, retried errors |
| `timeline.txt` | Confirmations per 10 s after the first buyer, and the longest gap (`experiments/timeline.py`) |
| `checker.txt` | The invariant checker on the final state, after recovery |
| `fis-experiment.txt`, `fis-state.txt` | The FIS experiment id, its end state and its start and end time |
| `rds.txt` | `db-ticketing` peak CPU and connections from the first buyer to the check |
| `cancel.txt`, `refunds.txt` | C5 only: the cancellation, and the check that every paid order got exactly one refund |
| `stripe-charged-no-ticket.csv` | Every buyer in C8 and C3 who reached the pay step but didn't end confirmed, looked up in Stripe on 2026-11-05 at 17:44: charge, refund count, refund time |
| `chaos-timeline.png` | Tickets confirmed over time for the control, C1, C3 and C8 (`experiments/chart.py`) |

Notes for reading them:

- **C8 shows 1,248 confirmed in `summary.txt` and `timeline.txt`.** The other 2 were confirmed after
  the driver stopped waiting (`settle_failed`), so 1,250 were sold. Those 2 are the
  `settle_failed` rows with 0 refunds in the CSV; correctly, they were not refunded.
- **The other 179 CSV rows** (178 from C8, 1 from C3) are buyers charged without a ticket. Each has
  exactly one `succeeded` refund, issued when Stripe retried the webhook: 14:47-14:49 and
  15:17-15:19 for C8, 15:37 for C3.
- **C8's `checker.txt`** is from a second attempt at 14:43. The first, straight after the run,
  couldn't connect: the database was at its connection limit (see `RESULTS.md`, C8).
- **The RabbitMQ and DLQ checks** are in `RESULTS.md`, not here: they were read from the broker by
  hand between runs.
