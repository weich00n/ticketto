#!/usr/bin/env bash
# ticketto-check: is this broker healthy and set up the way topology/messages.yaml says?
# Installed in the image as /usr/local/bin/ticketto-check. Run it inside the container:
#   docker exec rabbitmq ticketto-check           (EC2 broker host; the deploy runs it)
#   docker compose exec rabbitmq ticketto-check   (local)
#
# Checks, printing every problem found:
#   1. the node is running and has no memory or disk alarm
#   2. every queue in the catalogue exists and is a quorum queue (persisted to disk)
#   3. no consumer uses auto-ack. With auto-ack the broker forgets a message as soon as it
#      is sent, so a consumer that crashes mid-way loses it. Consumers must ack manually.
# Warnings only (they don't change the exit status):
#   - queues that aren't in the catalogue, e.g. left behind after a consumer was removed
#   - service users that weren't created because the broker had no password for them
#
# Options:
#   --skip-consumers   checks 1 and 2 only. The deploy uses this to decide on a rollback:
#                      a service using auto-ack is that service's bug, and rolling the
#                      broker back would not fix it.
#
# Exit status: 0 = all good, 1 = at least one problem.
set -euo pipefail

EXPECTED_QUEUES=/etc/rabbitmq/ticketto-queues.txt
SERVICE_USERS=/etc/rabbitmq/ticketto-service-users.tsv
check_consumers=true
case "${1:-}" in
  "") ;;
  --skip-consumers) check_consumers=false ;;
  *)
    echo "usage: ticketto-check [--skip-consumers]" >&2
    exit 2
    ;;
esac
problems=0

fail() {
  echo "FAIL: $*"
  problems=$((problems + 1))
}

if ! rabbitmq-diagnostics -q check_running >/dev/null 2>&1; then
  echo "FAIL: the RabbitMQ node is not running"
  exit 1
fi
if ! alarms=$(rabbitmq-diagnostics -q check_local_alarms 2>&1); then
  fail "resource alarm in effect: $alarms"
fi

# ---- 2. queues ----
declare -A queue_type=()
while IFS=$'\t' read -r name type; do
  [[ -n "$name" ]] && queue_type["$name"]=$type
done < <(rabbitmqctl -q list_queues name type --no-table-headers)

declare -A expected=()
while read -r name; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  expected["$name"]=1
  if [[ -z "${queue_type[$name]+set}" ]]; then
    fail "queue $name is missing"
  elif [[ "${queue_type[$name]}" != quorum ]]; then
    fail "queue $name is ${queue_type[$name]}, expected quorum"
  fi
done <"$EXPECTED_QUEUES"

for name in "${!queue_type[@]}"; do
  # amq.gen-* are server-named temporary queues (exclusive, deleted with their connection).
  if [[ -z "${expected[$name]+set}" && "$name" != amq.* ]]; then
    echo "WARN: queue $name is not in topology/messages.yaml (did a client declare it?)"
  fi
done

# ---- service users (warnings) ----
declare -A user_exists=()
while IFS=$'\t' read -r name _; do
  [[ -n "$name" ]] && user_exists["$name"]=1
done < <(rabbitmqctl -q list_users --no-table-headers)
while IFS=$'\t' read -r name _; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  if [[ -z "${user_exists[$name]+set}" ]]; then
    echo "WARN: service user $name does not exist (no password for it at start-up)"
  fi
done <"$SERVICE_USERS"

# ---- 3. acknowledgement mode ----
# rabbitmqctl prints columns in its own order (queue, tag, ack), not the order requested, so
# find the ack flag by its value rather than by position.
if [[ "$check_consumers" == true ]]; then
  while IFS=$'\t' read -r queue second third; do
    if [[ "$third" == true || "$third" == false ]]; then
      tag=$second ack_required=$third
    else
      tag=$third ack_required=$second
    fi
    if [[ "$ack_required" == false ]]; then
      fail "consumer '$tag' on queue $queue uses auto-ack; consume with manual acks"
    fi
  done < <(rabbitmqctl -q list_consumers queue_name consumer_tag ack_required --no-table-headers)
fi

if ((problems > 0)); then
  echo "$problems problem(s) found"
  exit 1
fi
summary="OK: node running, ${#expected[@]} catalogue queues present (all quorum)"
if [[ "$check_consumers" == true ]]; then
  echo "$summary, no auto-ack consumers"
else
  echo "$summary (consumers not checked)"
fi
