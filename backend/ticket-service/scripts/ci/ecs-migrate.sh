#!/usr/bin/env bash
# Deploy step 1: register task definitions for the new image (API and worker), then run
# `alembic upgrade head` once as a one-off ECS task and fail unless it exits 0.
# Writes both task definition ARNs to deploy.env for the deploy job (GitLab dotenv artifact).
# The CI/CD variables come from `tofu output gitlab_ci_variables` (deploy/README.md).
set -euo pipefail

: "${IMAGE:?}" "${AWS_REGION:?}" "${ECS_CLUSTER:?}" "${ECS_SUBNETS:?}" "${ECS_SECURITY_GROUP:?}"
: "${ECS_EXECUTION_ROLE_ARN:?}" "${RDS_ENDPOINT:?}" "${DB_SECRET_ARN:?}" "${RABBITMQ_SECRET_ARN:?}"
: "${QR_KEYS_SECRET_ARN:?}"
# Optional: empty leaves the worker without event-service lookups (TicketIssued's event details
# then come from messages only).
EVENT_SERVICE_URL=${EVENT_SERVICE_URL:-}

# Render a task definition template. ARNs contain ':' and '/', so '|' is the sed delimiter;
# esc() keeps '&', '|' and '\' in a value (e.g. a URL) from being read as sed syntax.
esc() { printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'; }
render() {  # render <template> <output>
  sed -e "s|__IMAGE__|$(esc "$IMAGE")|g" \
      -e "s|__AWS_REGION__|$(esc "$AWS_REGION")|g" \
      -e "s|__ECS_EXECUTION_ROLE_ARN__|$(esc "$ECS_EXECUTION_ROLE_ARN")|g" \
      -e "s|__RDS_ENDPOINT__|$(esc "$RDS_ENDPOINT")|g" \
      -e "s|__DB_SECRET_ARN__|$(esc "$DB_SECRET_ARN")|g" \
      -e "s|__RABBITMQ_SECRET_ARN__|$(esc "$RABBITMQ_SECRET_ARN")|g" \
      -e "s|__QR_KEYS_SECRET_ARN__|$(esc "$QR_KEYS_SECRET_ARN")|g" \
      -e "s|__EVENT_SERVICE_URL__|$(esc "$EVENT_SERVICE_URL")|g" \
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

# Public IP on: the team's default VPC has no NAT, so a task without one can't pull its image
# or read its secrets. The security group lets nothing in.
TASK_ARN=$(aws ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --launch-type FARGATE \
  --task-definition "$TASK_DEFINITION_ARN" \
  --network-configuration "awsvpcConfiguration={subnets=[$ECS_SUBNETS],securityGroups=[$ECS_SECURITY_GROUP],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"ticket-service","command":["alembic","upgrade","head"]}]}' \
  --started-by "gitlab-pipeline-${CI_PIPELINE_ID:-manual}" \
  --query 'tasks[0].taskArn' --output text)
if [[ -z "$TASK_ARN" || "$TASK_ARN" == "None" ]]; then
  echo "run-task did not start the migration task" >&2
  exit 1
fi
echo "Migration task $TASK_ARN started; waiting for it to finish..."

aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER" --tasks "$TASK_ARN"

read -r EXIT_CODE REASON <<<"$(aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].[containers[?name==`ticket-service`].exitCode | [0], stoppedReason]' --output text)"
TASK_ID=${TASK_ARN##*/}
echo "Logs: CloudWatch group /ecs/ticket-service, stream ticket-service/ticket-service/$TASK_ID"
if [[ "$EXIT_CODE" != "0" ]]; then
  echo "Migration failed: exit code $EXIT_CODE ($REASON)" >&2
  exit 1
fi
echo "Migration succeeded"
