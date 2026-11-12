#!/usr/bin/env bash
# Manual rollback: move the API and worker back to the task definition revision before the
# one each service is running now, and wait until both are stable.
# Schema changes are additive, so the previous image runs against the migrated database.
# To go back further, set ROLLBACK_TASK_DEFINITION_ARN and ROLLBACK_WORKER_TASK_DEFINITION_ARN.
set -euo pipefail

: "${ECS_CLUSTER:?}" "${ECS_SERVICE:?}" "${ECS_WORKER_SERVICE:?}"

previous() {  # previous <service> -> ARN of the revision before the running one
  local current family revision
  current=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$1" \
    --query 'services[0].taskDefinition' --output text)
  family=${current##*/}; family=${family%:*}
  revision=${current##*:}
  if (( revision <= 1 )); then
    echo "$1 is on revision 1; nothing to roll back to" >&2
    exit 1
  fi
  aws ecs describe-task-definition --task-definition "$family:$((revision - 1))" \
    --query 'taskDefinition.taskDefinitionArn' --output text
}

API_TARGET=${ROLLBACK_TASK_DEFINITION_ARN:-$(previous "$ECS_SERVICE")}
WORKER_TARGET=${ROLLBACK_WORKER_TASK_DEFINITION_ARN:-$(previous "$ECS_WORKER_SERVICE")}
echo "Rolling $ECS_SERVICE back to $API_TARGET and $ECS_WORKER_SERVICE back to $WORKER_TARGET"

aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
  --task-definition "$API_TARGET" --query 'service.serviceName' --output text
aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_WORKER_SERVICE" \
  --task-definition "$WORKER_TARGET" --query 'service.serviceName' --output text
aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" "$ECS_WORKER_SERVICE"
echo "Rollback complete"
