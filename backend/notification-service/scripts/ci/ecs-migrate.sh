#!/usr/bin/env bash
# Deploy step 1: register task definitions for the new image (the API and the worker), then run
# `alembic upgrade head` once as a one-off ECS task and fail unless it exits 0. It uses the API's
# task definition and security group, because it only needs the database. Writes
# TASK_DEFINITION_ARN and WORKER_TASK_DEFINITION_ARN to deploy.env for the deploy job (GitLab
# dotenv artifact).
set -euo pipefail

: "${IMAGE:?}" "${AWS_REGION:?}" "${ECS_CLUSTER:?}" "${ECS_SUBNETS:?}" "${ECS_SECURITY_GROUP:?}"
: "${ECS_EXECUTION_ROLE_ARN:?}" "${ECS_TASK_ROLE_ARN:?}" "${RDS_ENDPOINT:?}" "${DB_SECRET_ARN:?}"
: "${RABBITMQ_SECRET_ARN:?}" "${USER_SERVICE_URL:?}" "${EMAIL_FROM:?}"
: "${SES_CONFIGURATION_SET:?}"
EVENT_SERVICE_URL=${EVENT_SERVICE_URL:-} # optional: titles of events published before this service

# Render a task definition template, register it and print its ARN. ARNs contain ':' and '/', so
# '|' is the sed delimiter.
register() {
  local template=$1 rendered=$2
  sed -e "s|__IMAGE__|$IMAGE|g" \
      -e "s|__AWS_REGION__|$AWS_REGION|g" \
      -e "s|__ECS_EXECUTION_ROLE_ARN__|$ECS_EXECUTION_ROLE_ARN|g" \
      -e "s|__ECS_TASK_ROLE_ARN__|$ECS_TASK_ROLE_ARN|g" \
      -e "s|__RDS_ENDPOINT__|$RDS_ENDPOINT|g" \
      -e "s|__DB_SECRET_ARN__|$DB_SECRET_ARN|g" \
      -e "s|__RABBITMQ_SECRET_ARN__|$RABBITMQ_SECRET_ARN|g" \
      -e "s|__USER_SERVICE_URL__|$USER_SERVICE_URL|g" \
      -e "s|__EVENT_SERVICE_URL__|$EVENT_SERVICE_URL|g" \
      -e "s|__EMAIL_FROM__|$EMAIL_FROM|g" \
      -e "s|__SES_CONFIGURATION_SET__|$SES_CONFIGURATION_SET|g" \
      "$template" > "$rendered"
  if grep -o '__[A-Z_]*__' "$rendered" >&2; then
    echo "Unfilled placeholders above in $template: add the matching CI/CD variable" >&2
    return 1
  fi
  aws ecs register-task-definition --cli-input-json "file://$rendered" \
    --query 'taskDefinition.taskDefinitionArn' --output text
}

TASK_DEFINITION_ARN=$(register deploy/task-definition.json task-definition.rendered.json)
echo "Registered $TASK_DEFINITION_ARN"
WORKER_TASK_DEFINITION_ARN=$(register deploy/worker-task-definition.json \
  worker-task-definition.rendered.json)
echo "Registered $WORKER_TASK_DEFINITION_ARN"
{
  echo "TASK_DEFINITION_ARN=$TASK_DEFINITION_ARN"
  echo "WORKER_TASK_DEFINITION_ARN=$WORKER_TASK_DEFINITION_ARN"
} > deploy.env

# Public IP: the default VPC has no NAT, so without one the task cannot pull its image.
# The security group still admits nothing from outside.
TASK_ARN=$(aws ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --launch-type FARGATE \
  --task-definition "$TASK_DEFINITION_ARN" \
  --network-configuration "awsvpcConfiguration={subnets=[$ECS_SUBNETS],securityGroups=[$ECS_SECURITY_GROUP],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"notification-service","command":["alembic","upgrade","head"]}]}' \
  --started-by "gitlab-pipeline-${CI_PIPELINE_ID:-manual}" \
  --query 'tasks[0].taskArn' --output text)
if [[ -z "$TASK_ARN" || "$TASK_ARN" == "None" ]]; then
  echo "run-task did not start the migration task" >&2
  exit 1
fi
echo "Migration task $TASK_ARN started; waiting for it to finish..."

aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER" --tasks "$TASK_ARN"

# shellcheck disable=SC2016  # the backticks are JMESPath, not the shell
read -r EXIT_CODE REASON <<<"$(aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].[containers[?name==`notification-service`].exitCode | [0], stoppedReason]' --output text)"
TASK_ID=${TASK_ARN##*/}
echo "Logs: CloudWatch group /ecs/notification-service, stream notification-service/notification-service/$TASK_ID"
if [[ "$EXIT_CODE" != "0" ]]; then
  echo "Migration failed: exit code $EXIT_CODE ($REASON)" >&2
  exit 1
fi
echo "Migration succeeded"
