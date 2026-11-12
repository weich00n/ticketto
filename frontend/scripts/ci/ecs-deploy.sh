#!/usr/bin/env bash
# Registers a task definition for the new image, rolls the ECS service onto it, waits until
# the service is stable and fails if ECS rolled the deployment back.
set -euo pipefail

: "${IMAGE:?}" "${AWS_REGION:?}" "${ECS_CLUSTER:?}" "${ECS_SERVICE:?}"
: "${ECS_EXECUTION_ROLE_ARN:?}" "${COGNITO_USER_POOL_ID:?}" "${COGNITO_CLIENT_ID:?}"
: "${COGNITO_SECRET_ARN:?}"
API_GATEWAY_URL=${API_GATEWAY_URL:-}
STRIPE_PUBLISHABLE_KEY=${STRIPE_PUBLISHABLE_KEY:-}
USE_DEMO_DATA=${USE_DEMO_DATA:-true}

# ARNs contain ':' and '/', so '|' is the sed delimiter.
sed -e "s|__IMAGE__|$IMAGE|g" \
    -e "s|__AWS_REGION__|$AWS_REGION|g" \
    -e "s|__ECS_EXECUTION_ROLE_ARN__|$ECS_EXECUTION_ROLE_ARN|g" \
    -e "s|__COGNITO_USER_POOL_ID__|$COGNITO_USER_POOL_ID|g" \
    -e "s|__COGNITO_CLIENT_ID__|$COGNITO_CLIENT_ID|g" \
    -e "s|__COGNITO_SECRET_ARN__|$COGNITO_SECRET_ARN|g" \
    -e "s|__API_GATEWAY_URL__|$API_GATEWAY_URL|g" \
    -e "s|__USE_DEMO_DATA__|$USE_DEMO_DATA|g" \
    -e "s|__STRIPE_PUBLISHABLE_KEY__|$STRIPE_PUBLISHABLE_KEY|g" \
    deploy/task-definition.json > task-definition.rendered.json
if grep -o '__[A-Z_]*__' task-definition.rendered.json; then
  echo "Unfilled placeholders above: add the matching CI/CD variable" >&2
  exit 1
fi

TASK_DEFINITION_ARN=$(aws ecs register-task-definition \
  --cli-input-json file://task-definition.rendered.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)
echo "Registered $TASK_DEFINITION_ARN"

aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_SERVICE" \
  --task-definition "$TASK_DEFINITION_ARN" \
  --query 'service.serviceName' --output text
echo "Rolling out; waiting for the service to become stable..."

aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"

# With the deployment circuit breaker, a failed rollout also ends "stable" -- on the OLD
# task definition. So check which one is actually running. The rollout state lags a little
# behind "stable", so wait for it to leave IN_PROGRESS before judging.
for attempt in $(seq 1 30); do
  read -r running rollout <<<"$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
    --query 'services[0].deployments[?status==`PRIMARY`] | [0].[taskDefinition, rolloutState]' --output text)"
  [[ "$rollout" != "IN_PROGRESS" ]] && break
  sleep 10
done
if [[ "$running" != "$TASK_DEFINITION_ARN" || "$rollout" != "COMPLETED" ]]; then
  echo "$ECS_SERVICE did not complete: running $running (rollout $rollout)." >&2
  echo "FAILED means ECS rolled the release back: read the stopped tasks' reason in ECS." >&2
  exit 1
fi
echo "Deployed $TASK_DEFINITION_ARN to $ECS_SERVICE"
