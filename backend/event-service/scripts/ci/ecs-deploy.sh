#!/usr/bin/env bash
# Deploy step 2: point the API and outbox-worker ECS services at the task definitions
# registered by ecs-migrate.sh, wait for both rollouts, and fail if ECS rolled either back.
set -euo pipefail

: "${TASK_DEFINITION_ARN:?}" "${WORKER_TASK_DEFINITION_ARN:?}"
: "${ECS_CLUSTER:?}" "${ECS_SERVICE:?}" "${ECS_WORKER_SERVICE:?}"

update() {  # update <service> <task definition ARN>
  aws ecs update-service \
    --cluster "$ECS_CLUSTER" \
    --service "$1" \
    --task-definition "$2" \
    --query 'service.serviceName' --output text
}

update "$ECS_SERVICE" "$TASK_DEFINITION_ARN"
update "$ECS_WORKER_SERVICE" "$WORKER_TASK_DEFINITION_ARN"
echo "Rolling out; waiting for both services to become stable..."

aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" "$ECS_WORKER_SERVICE"

# With the deployment circuit breaker, a failed rollout also ends "stable" -- on the OLD
# task definition. So check which one is actually running. The rollout state lags a little
# behind "stable", so wait for it to leave IN_PROGRESS before judging.
check() {  # check <service> <expected task definition ARN>
  local running rollout attempt
  for attempt in $(seq 1 30); do
    read -r running rollout <<<"$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$1" \
      --query 'services[0].deployments[?status==`PRIMARY`] | [0].[taskDefinition, rolloutState]' --output text)"
    [[ "$rollout" != "IN_PROGRESS" ]] && break
    sleep 10
  done
  if [[ "$running" != "$2" || "$rollout" != "COMPLETED" ]]; then
    echo "$1 did not complete: running $running (rollout $rollout)." >&2
    echo "FAILED means ECS rolled the release back: read the stopped tasks' reason in ECS." >&2
    return 1
  fi
  echo "Deployed $2 to $1"
}

failed=0
check "$ECS_SERVICE" "$TASK_DEFINITION_ARN" || failed=1
check "$ECS_WORKER_SERVICE" "$WORKER_TASK_DEFINITION_ARN" || failed=1
exit "$failed"
