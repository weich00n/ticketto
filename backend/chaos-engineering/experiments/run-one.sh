#!/bin/bash
# One spike run: buyers through the API Gateway, an optional FIS fault a set number of seconds after
# the first buyer, then the checker and the RDS load for the window. Output lands in the run folder.
#
#   experiments/run-one.sh <run> [<fis template id> <seconds after the first buyer>] [buyers]
#
# Needs the driver env (GATEWAY_URL, COGNITO_*), checks/.env and checks/tunnels.sh running.
set -euo pipefail
RUN=$1 TEMPLATE=${2:-} OFFSET=${3:-0} BUYERS=${4:-2500}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/driver/state/runs/$RUN"
export AWS_REGION=${AWS_REGION:-us-east-1} PYTHONUNBUFFERED=1

cd "$ROOT/driver"
.venv/bin/python -m driver buy --run "$RUN" --rate "$BUYERS" --duration 1 > "$DIR/buy.log" 2>&1 &
BUY=$!
until grep -q ' buyers at ' "$DIR/buy.log"; do
  kill -0 $BUY 2>/dev/null || { tail -5 "$DIR/buy.log"; exit 1; }
  sleep 1
done
echo "$RUN: first buyer at $(date -u +%H:%M:%S)"

if [ -n "$TEMPLATE" ]; then
  sleep "$OFFSET"
  EXP=$(aws fis start-experiment --experiment-template-id "$TEMPLATE" --tags run="$RUN" --query experiment.id --output text)
  echo "$EXP" > "$DIR/fis-experiment.txt"
  echo "$RUN: FIS $EXP started at $(date -u +%H:%M:%S) (+${OFFSET}s)"
fi

wait $BUY || true
echo "$RUN: buy ended at $(date -u +%H:%M:%S)"

if [ -n "$TEMPLATE" ]; then
  until [[ "$(aws fis get-experiment --id "$EXP" --query experiment.state.status --output text)" =~ ^(completed|stopped|failed)$ ]]; do sleep 10; done
  aws fis get-experiment --id "$EXP" --query 'experiment.[state.status,state.reason,startTime,endTime]' --output text | tee "$DIR/fis-state.txt"
fi

# Fresh tunnels for the checker: SSM closes a session after 20 idle minutes, and C2's reboot drops them.
cd "$ROOT/checks"
bash tunnels.sh > "$DIR/tunnels.log" 2>&1 &
TUNNELS=$!
trap 'kill $TUNNELS 2>/dev/null; sleep 1; pkill -f "session-manager-plugin.*localPortNumber"' EXIT
for _ in $(seq 1 30); do
  nc -z 127.0.0.1 16379 && nc -z 127.0.0.1 13306 && nc -z 127.0.0.1 16380 && break
  sleep 2
done 2>/dev/null
set -a; . ./.env; set +a
.venv/bin/python -m checker --event-id "$(jq -r .event_id "$DIR/event.json")" 2>&1 | tee "$DIR/checker.txt"

START=$(jq -r '.[-1].started' "$DIR/windows.json")
for m in CPUUtilization DatabaseConnections; do
  echo "$m max: $(aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name $m \
    --dimensions Name=DBInstanceIdentifier,Value=db-ticketing --statistics Maximum --period 60 \
    --start-time "$START" --end-time "$(date -u +%FT%TZ)" --query 'max(Datapoints[].Maximum)' --output text)"
done | tee "$DIR/rds.txt"
