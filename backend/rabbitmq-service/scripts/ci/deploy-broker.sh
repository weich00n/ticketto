#!/usr/bin/env bash
# CI deploy job: redeploy the broker on its EC2 instance over SSM, then check it.
#
# The instance does the work with /opt/ticketto/run-rabbitmq.sh: it pulls the image, swaps
# the container gracefully, waits for it to be healthy and rolls back to the previous image
# if it is not. This job first installs this commit's deploy/run-rabbitmq.sh there, so a
# change to that script ships with the pipeline instead of needing a new instance. It then
# waits for services to reconnect and runs the full ticketto-check, which also fails on
# auto-ack consumers.
#
# Needs: AWS_REGION, BROKER_INSTANCE_ID, CI_COMMIT_SHA, plus credentials from .aws-oidc.
set -euo pipefail

: "${AWS_REGION:?}" "${BROKER_INSTANCE_ID:?}" "${CI_COMMIT_SHA:?}"

# Run one shell command on the broker host and wait for it. Prints its output; returns 0
# only if it succeeded.
run_on_broker() {
  local comment=$1 command=$2 command_id status
  command_id=$(aws ssm send-command \
    --instance-ids "$BROKER_INSTANCE_ID" \
    --document-name AWS-RunShellScript \
    --comment "$comment" \
    --parameters "commands=[\"$command\"],executionTimeout=[\"900\"]" \
    --query Command.CommandId --output text)

  status=Pending
  for _ in $(seq 1 200); do # up to ~17 minutes
    sleep 5
    status=$(aws ssm get-command-invocation --command-id "$command_id" \
      --instance-id "$BROKER_INSTANCE_ID" --query Status --output text 2>/dev/null || echo Pending)
    case "$status" in
      Pending | InProgress | Delayed) ;;
      *) break ;;
    esac
  done

  aws ssm get-command-invocation --command-id "$command_id" --instance-id "$BROKER_INSTANCE_ID" \
    --query StandardOutputContent --output text
  aws ssm get-command-invocation --command-id "$command_id" --instance-id "$BROKER_INSTANCE_ID" \
    --query StandardErrorContent --output text >&2
  echo "($comment: $status)"
  [[ "$status" == Success ]]
}

# base64 has no quotes or spaces, so it is safe inside the JSON parameters below.
run_script_b64=$(base64 -w0 deploy/run-rabbitmq.sh)
install_run_script="echo $run_script_b64 | base64 -d > /opt/ticketto/run-rabbitmq.sh.new"
install_run_script+=" && chmod 755 /opt/ticketto/run-rabbitmq.sh.new"
install_run_script+=" && mv /opt/ticketto/run-rabbitmq.sh.new /opt/ticketto/run-rabbitmq.sh"

run_on_broker "rabbitmq-service deploy ${CI_COMMIT_SHA:0:12}" \
  "$install_run_script && /opt/ticketto/run-rabbitmq.sh $CI_COMMIT_SHA"

# Services reconnect on their own after the restart; give them a moment, then check that
# none of them consumes with auto-ack. This fails the job but does not roll back.
sleep 60
run_on_broker "rabbitmq-service post-deploy check" "docker exec rabbitmq ticketto-check"
