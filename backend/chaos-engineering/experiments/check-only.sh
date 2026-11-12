#!/bin/bash
# The checker and RDS load for a run that already ended (run-one.sh's last half).
#   experiments/check-only.sh <run>
set -euo pipefail
RUN=$1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/driver/state/runs/$RUN"
export AWS_REGION=${AWS_REGION:-us-east-1}

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
