#!/usr/bin/env bash
# Run, or replace, the Ticketto broker container on the broker instance (deploy/terraform).
#
# Installed as /opt/ticketto/run-rabbitmq.sh, with its settings in /opt/ticketto/rabbitmq.env:
# first by the instance's user data, then again by every deploy-broker job, so the copy on
# the instance is always this repo's. deploy/boot-rabbitmq.sh calls it with the tag in
# /data/rabbitmq-service.tag (on the data volume, so a replaced instance runs the same image);
# the pipeline's deploy-broker job calls it over SSM with a new tag.
#
# Usage: run-rabbitmq.sh <image-tag>
#   0. read the admin password, and every service user's password from the secrets under
#      SERVICE_SECRET_PREFIX (default: <RABBITMQ_SECRET_ID's folder>/service/, e.g.
#      rabibi/service/event-service). The image creates those users at start-up.
#   1. pull <ECR_REPOSITORY_URI>:<tag>
#   2. stop the running broker gracefully, then start the new image with the same hostname
#      and data directory, so every queue and message carries over
#   3. wait for Docker to report it healthy, then run `ticketto-check --skip-consumers`
#   4. on failure, start the previous image again and exit 1
set -euo pipefail # no -x: it would print the password into the log

TAG=${1:?usage: run-rabbitmq.sh <image-tag>}
# Overridable only so the script can be rehearsed away from the broker host.
CONFIG=${BROKER_CONFIG_FILE:-/opt/ticketto/rabbitmq.env}
STATE=${BROKER_STATE_FILE:-/data/rabbitmq-service.tag} # the tag that last passed its checks
BROKER_DATA_DIR=${BROKER_DATA_DIR:-/data/rabbitmq}

# shellcheck source=/dev/null
source "$CONFIG"
: "${ECR_REPOSITORY_URI:?set in $CONFIG}" "${AWS_REGION:?set in $CONFIG}"
: "${RABBITMQ_SECRET_ID:?set in $CONFIG}"

log() { echo "[run-rabbitmq] $*"; }

secret=$(aws secretsmanager get-secret-value --region "$AWS_REGION" \
  --secret-id "$RABBITMQ_SECRET_ID" --query SecretString --output text)
RABBITMQ_ADMIN_USER=$(jq -r .username <<<"$secret")
RABBITMQ_ADMIN_PASS=$(jq -r .password <<<"$secret")
export RABBITMQ_ADMIN_USER RABBITMQ_ADMIN_PASS

# Service users: rabibi/service/<service> -> RABBITMQ_PASS_<SERVICE>, passed by name only.
SERVICE_SECRET_PREFIX=${SERVICE_SECRET_PREFIX:-${RABBITMQ_SECRET_ID%/*}/service/}
service_env=()
while read -r secret_name; do
  [[ -z "$secret_name" || "$secret_name" == None ]] && continue
  service=${secret_name##*/}
  variable="RABBITMQ_PASS_$(tr 'a-z-' 'A-Z_' <<<"$service")"
  password=$(aws secretsmanager get-secret-value --region "$AWS_REGION" --secret-id "$secret_name" \
    --query SecretString --output text | jq -r .password)
  export "$variable=$password"
  service_env+=(-e "$variable")
done < <(aws secretsmanager list-secrets --region "$AWS_REGION" \
  --filters Key=name,Values="$SERVICE_SECRET_PREFIX" --query 'SecretList[].Name' --output text |
  tr '\t' '\n')
log "service users with a password: $(( ${#service_env[@]} / 2 ))"

aws ecr get-login-password --region "$AWS_REGION" |
  docker login --username AWS --password-stdin "${ECR_REPOSITORY_URI%%/*}" >/dev/null

start() {
  local image="$ECR_REPOSITORY_URI:$1"
  docker pull --quiet "$image" >/dev/null
  if docker container inspect rabbitmq >/dev/null 2>&1; then
    # Graceful: a hard kill within seconds of a queue being created can lose that queue.
    docker stop --time 60 rabbitmq >/dev/null
    docker rm rabbitmq >/dev/null
  fi
  # `-e NAME` without a value passes the variable from this shell, so the password never
  # appears on a command line. --hostname must stay `broker`: the data is stored under it.
  docker run -d --name rabbitmq --hostname broker --restart unless-stopped \
    --stop-timeout 60 --memory 1g \
    -p 5672:5672 -p 15672:15672 \
    -e RABBITMQ_ADMIN_USER -e RABBITMQ_ADMIN_PASS "${service_env[@]}" \
    -v "$BROKER_DATA_DIR":/var/lib/rabbitmq \
    "$image" >/dev/null
}

healthy() {
  local status
  for _ in $(seq 1 36); do # up to 3 minutes
    status=$(docker inspect --format '{{.State.Health.Status}}' rabbitmq 2>/dev/null || true)
    case "$status" in
      healthy) docker exec rabbitmq ticketto-check --skip-consumers && return 0 || return 1 ;;
      unhealthy) return 1 ;;
    esac
    sleep 5
  done
  return 1
}

previous=$(cat "$STATE" 2>/dev/null || true)
log "deploying $TAG (previous: ${previous:-none})"
start "$TAG"
if healthy; then
  echo "$TAG" >"$STATE"
  log "broker healthy on $TAG"
  exit 0
fi

log "$TAG failed its checks. Last 40 log lines:"
docker logs --tail 40 rabbitmq 2>&1 || true
if [[ -n "$previous" && "$previous" != "$TAG" ]]; then
  log "rolling back to $previous"
  start "$previous"
  if healthy; then
    log "rolled back to $previous"
  else
    log "the rollback to $previous is unhealthy too: investigate now"
  fi
fi
exit 1
