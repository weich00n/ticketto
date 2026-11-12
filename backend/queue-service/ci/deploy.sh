#!/bin/sh
# Deploy $IMAGE to ECS (run by the GitLab deploy job; see infra/README.md):
#   1. new revision of queue-migrate-<env> with $IMAGE, run it, stop on failure
#   2. new revisions of consumer, admitter, api with $IMAGE; update each service, wait
#
# Needs: IMAGE, ECS_CLUSTER, ECS_SUBNETS, ECS_SECURITY_GROUP, and the environment
# name in DEPLOY_ENV (defaults to GitLab's CI_ENVIRONMENT_NAME).
set -eu
: "${IMAGE:?}" "${ECS_CLUSTER:?}" "${ECS_SUBNETS:?}" "${ECS_SECURITY_GROUP:?}"
ENV_NAME="${DEPLOY_ENV:-${CI_ENVIRONMENT_NAME:?set DEPLOY_ENV or run in a GitLab environment}}"
NETWORK="awsvpcConfiguration={subnets=[$ECS_SUBNETS],securityGroups=[$ECS_SECURITY_GROUP],assignPublicIp=${ECS_ASSIGN_PUBLIC_IP:-DISABLED}}"

# Copy the latest revision of a task definition family with the new image; print its ARN.
register() {
  aws ecs describe-task-definition --task-definition "$1" --query taskDefinition > td.json
  python3 - <<'EOF'
import json, os
td = json.load(open("td.json"))
for key in ("taskDefinitionArn", "revision", "status", "requiresAttributes", "compatibilities",
            "registeredAt", "registeredBy", "deregisteredAt"):
    td.pop(key, None)
for container in td["containerDefinitions"]:
    container["image"] = os.environ["IMAGE"]
json.dump(td, open("td-new.json", "w"))
EOF
  aws ecs register-task-definition --cli-input-json file://td-new.json \
    --query taskDefinition.taskDefinitionArn --output text
}

echo "== migrate ($ENV_NAME)"
MIGRATE=$(register "queue-migrate-$ENV_NAME")
TASK=$(aws ecs run-task --cluster "$ECS_CLUSTER" --launch-type FARGATE \
  --task-definition "$MIGRATE" --network-configuration "$NETWORK" \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER" --tasks "$TASK"
CODE=$(aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "$TASK" \
  --query 'tasks[0].containers[0].exitCode' --output text)
if [ "$CODE" != "0" ]; then
  echo "migration failed (exit code $CODE); see CloudWatch /ecs/queue-service-$ENV_NAME/migrate"
  exit 1
fi

for name in consumer admitter api; do
  echo "== queue-$name"
  REVISION=$(register "queue-$name-$ENV_NAME")
  aws ecs update-service --cluster "$ECS_CLUSTER" --service "queue-$name" \
    --task-definition "$REVISION" > /dev/null
  aws ecs wait services-stable --cluster "$ECS_CLUSTER" --services "queue-$name"
done
echo "== deployed $IMAGE"
