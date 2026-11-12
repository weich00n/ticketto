#!/usr/bin/env bash
# Stop or start user-service's running tasks, the part that costs money, through OpenTofu.
#
#   ./deploy/service.sh stop          0 tasks: no Fargate or public-IP charges
#   ./deploy/service.sh start         1 task
#   ./deploy/service.sh start 2       2 tasks
#   ./deploy/service.sh status        what is running now
#
# The count is written to deploy/terraform/terraform.tfvars, so a later plain `tofu apply`
# keeps it instead of undoing it. Everything else (database, secret, routes, image) stays,
# so starting again takes about a minute. The shared RDS instance is NOT touched.
set -euo pipefail

cd "$(dirname "$0")/terraform"
CLUSTER=$(tofu output -json gitlab_ci_variables | jq -r .ECS_CLUSTER)
SERVICE=$(tofu output -json gitlab_ci_variables | jq -r .ECS_SERVICE)

status() {
  aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
    --query 'services[0].{desired:desiredCount,running:runningCount,pending:pendingCount}' --output table
}

case "${1:-}" in
  stop) COUNT=0 ;;
  start) COUNT=${2:-1} ;;
  status) status; exit 0 ;;
  *) echo "usage: $0 stop | start [count] | status" >&2; exit 1 ;;
esac
if ! [[ "$COUNT" =~ ^[0-9]+$ ]]; then
  echo "count must be a whole number, got '$COUNT'" >&2
  exit 1
fi

# Replace the desired_count line in terraform.tfvars, keeping any other settings there.
touch terraform.tfvars
{ grep -v '^[[:space:]]*desired_count[[:space:]]*=' terraform.tfvars || true; echo "desired_count = $COUNT"; } > terraform.tfvars.tmp
mv terraform.tfvars.tmp terraform.tfvars

tofu apply -target=aws_ecs_service.service # asks you to type "yes"; only touches the ECS service

echo "Waiting for ECS to reach $COUNT running task(s)..."
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"
status
