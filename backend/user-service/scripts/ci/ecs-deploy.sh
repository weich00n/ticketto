#!/usr/bin/env bash
# Deploy step 2: point the API and worker ECS services at the task definitions registered by
# ecs-migrate.sh, wait for each rollout, and fail if ECS rolled back to the previous version.
set -euo pipefail

: "${TASK_DEFINITION_ARN:?}" "${WORKER_TASK_DEFINITION_ARN:?}"
: "${ECS_CLUSTER:?}" "${ECS_SERVICE:?}" "${ECS_WORKER_SERVICE:?}"

deploy_service() {
  local service=$1 task_definition=$2 running rollout
  aws ecs update-service \
    --cluster "$ECS_CLUSTER" \
    --service "$service" \
    --task-definition "$task_definition" \
    --query 'service.serviceName' --output text
  echo "Rolling out $task_definition; waiting for $service to become stable..."

  aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$service"

  # "Stable" only means the task counts match: ECS can still say IN_PROGRESS for a minute or so
  # while the old task deregisters from Cloud Map. Poll until the rollout settles (up to 10 min).
  # With the deployment circuit breaker, a failed rollout also ends on the OLD task definition,
  # so check which one is actually running.
  for _ in $(seq 40); do
    read -r running rollout <<<"$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$service" \
      --query 'services[0].deployments[?status==`PRIMARY`] | [0].[taskDefinition, rolloutState]' --output text)"
    [[ "$rollout" == "IN_PROGRESS" ]] || break
    sleep 15
  done
  if [[ "$running" != "$task_definition" || "$rollout" != "COMPLETED" ]]; then
    echo "Deployment of $service did not complete: running $running (rollout $rollout). ECS probably rolled back." >&2
    return 1
  fi
  echo "Deployed $task_definition to $service"
}

deploy_service "$ECS_SERVICE" "$TASK_DEFINITION_ARN"
deploy_service "$ECS_WORKER_SERVICE" "$WORKER_TASK_DEFINITION_ARN"
