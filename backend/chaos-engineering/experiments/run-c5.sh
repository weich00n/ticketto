#!/bin/bash
# C5: sell out a small event, stop checkout-service-worker, cancel the event while it is down,
# then check every paid order got exactly one refund once the worker is back.
#
#   experiments/run-c5.sh <run> <fis template id> [buyers]
set -euo pipefail
RUN=$1 TEMPLATE=$2 BUYERS=${3:-200}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/driver/state/runs/$RUN"
export AWS_REGION=${AWS_REGION:-us-east-1} PYTHONUNBUFFERED=1
cd "$ROOT/driver"

.venv/bin/python -m driver buy --run "$RUN" --rate "$BUYERS" --duration 1 > "$DIR/buy.log" 2>&1
echo "$RUN: sale ended at $(date -u +%H:%M:%S)"
sed -n '/^[0-9]* buyers$/,/^success/p' "$DIR/buy.log"

EXP=$(aws fis start-experiment --experiment-template-id "$TEMPLATE" --tags run="$RUN" --query experiment.id --output text)
echo "$EXP" > "$DIR/fis-experiment.txt"
echo "$RUN: FIS $EXP started at $(date -u +%H:%M:%S)"
# Wait until the worker task is actually stopping, so the cancel lands while it is down.
until [ "$(aws fis get-experiment --id "$EXP" --query experiment.state.status --output text)" != "initiating" ] \
  && [ "$(aws fis get-experiment --id "$EXP" --query 'experiment.actions."stop-tasks".state.status' --output text)" != "pending" ]; do
  sleep 2
done
.venv/bin/python -m driver cancel --run "$RUN" | tee "$DIR/cancel.txt"

until [[ "$(aws fis get-experiment --id "$EXP" --query experiment.state.status --output text)" =~ ^(completed|stopped|failed)$ ]]; do sleep 5; done
aws fis get-experiment --id "$EXP" --query 'experiment.[state.status,state.reason,startTime,endTime]' --output text | tee "$DIR/fis-state.txt"
aws ecs wait services-stable --cluster TicketPlatform --services checkout-service-worker
echo "$RUN: worker back at $(date -u +%H:%M:%S)"

# Refunds run every 30 s in the worker; give it a few rounds, then check.
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 30
  if .venv/bin/python -m driver refunds --run "$RUN" > "$DIR/refunds.txt" 2>&1; then break; fi
done
echo "$RUN: refunds checked at $(date -u +%H:%M:%S) (attempt $i)"
cat "$DIR/refunds.txt"
