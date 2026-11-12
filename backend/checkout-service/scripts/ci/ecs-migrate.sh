#!/usr/bin/env bash
# Deploy step 1: register task definitions for the new image (API and worker), then run
# `alembic upgrade head` once as a one-off ECS task and fail unless it exits 0.
# Writes both task definition ARNs to deploy.env for the deploy job (GitLab dotenv artifact).
# ECS_ASSIGN_PUBLIC_IP: ENABLED for public subnets without NAT (the team's default VPC), so the
# migration task can reach ECR and Secrets Manager; unset (DISABLED) for private subnets.
set -euo pipefail

: "${IMAGE:?}" "${AWS_REGION:?}" "${ECS_CLUSTER:?}" "${ECS_SUBNETS:?}" "${ECS_SECURITY_GROUP:?}"
: "${ECS_EXECUTION_ROLE_ARN:?}" "${RDS_ENDPOINT:?}" "${DB_SECRET_ARN:?}" "${RABBITMQ_SECRET_ARN:?}"
: "${STRIPE_SECRET_ARN:?}" "${RESERVATION_BASE_URL:?}"

# Render a task definition template. ARNs contain ':' and '/', so '|' is the sed delimiter.
render() {  # render <template> <output>
  sed -e "s|__IMAGE__|$IMAGE|g" \
      -e "s|__AWS_REGION__|$AWS_REGION|g" \
      -e "s|__ECS_EXECUTION_ROLE_ARN__|$ECS_EXECUTION_ROLE_ARN|g" \
      -e "s|__RDS_ENDPOINT__|$RDS_ENDPOINT|g" \
      -e "s|__DB_SECRET_ARN__|$DB_SECRET_ARN|g" \
      -e "s|__RABBITMQ_SECRET_ARN__|$RABBITMQ_SECRET_ARN|g" \
      -e "s|__STRIPE_SECRET_ARN__|$STRIPE_SECRET_ARN|g" \
      -e "s|__RESERVATION_BASE_URL__|$RESERVATION_BASE_URL|g" \
      "$1" > "$2"
  if grep -o '__[A-Z_]*__' "$2"; then
    echo "Unfilled placeholders above in $2: add the matching CI/CD variable" >&2
    exit 1
  fi
}

register() {  # register <rendered file> -> prints the new revision's ARN
  aws ecs register-task-definition --cli-input-json "file://$1" \
    --query 'taskDefinition.taskDefinitionArn' --output text
}

render deploy/task-definition.json task-definition.rendered.json
render deploy/worker-task-definition.json worker-task-definition.rendered.json
TASK_DEFINITION_ARN=$(register task-definition.rendered.json)
WORKER_TASK_DEFINITION_ARN=$(register worker-task-definition.rendered.json)
echo "Registered $TASK_DEFINITION_ARN and $WORKER_TASK_DEFINITION_ARN"
{
  echo "TASK_DEFINITION_ARN=$TASK_DEFINITION_ARN"
  echo "WORKER_TASK_DEFINITION_ARN=$WORKER_TASK_DEFINITION_ARN"
} > deploy.env

TASK_ARN=$(aws ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --launch-type FARGATE \
  --task-definition "$TASK_DEFINITION_ARN" \
  --network-configuration "awsvpcConfiguration={subnets=[$ECS_SUBNETS],securityGroups=[$ECS_SECURITY_GROUP],assignPublicIp=${ECS_ASSIGN_PUBLIC_IP:-DISABLED}}" \
  --overrides '{"containerOverrides":[{"name":"checkout-service","command":["alembic","upgrade","head"]}]}' \
  --started-by "gitlab-pipeline-${CI_PIPELINE_ID:-manual}" \
  --query 'tasks[0].taskArn' --output text)
if [[ -z "$TASK_ARN" || "$TASK_ARN" == "None" ]]; then
  echo "run-task did not start the migration task" >&2
  exit 1
fi
echo "Migration task $TASK_ARN started; waiting for it to finish..."

aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER" --tasks "$TASK_ARN"

read -r EXIT_CODE REASON <<<"$(aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].[containers[?name==`checkout-service`].exitCode | [0], stoppedReason]' --output text)"
TASK_ID=${TASK_ARN##*/}
echo "Logs: CloudWatch group /ecs/checkout-service, stream checkout-service/checkout-service/$TASK_ID"
if [[ "$EXIT_CODE" != "0" ]]; then
  echo "Migration failed: exit code $EXIT_CODE ($REASON)" >&2
  exit 1
fi
echo "Migration succeeded"
