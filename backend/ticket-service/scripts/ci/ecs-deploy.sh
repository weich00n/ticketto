#!/usr/bin/env bash
# Deploy step 2: point the API and worker ECS services at the task definitions registered by
# ecs-migrate.sh, wait until ECS has finished both rollouts, and fail if either was rolled back.
#
# `aws ecs wait services-stable` is not enough: it can return while a rollout is still
# IN_PROGRESS, because the deployment circuit breaker keeps watching the new tasks before ECS
# marks the rollout COMPLETED (or rolls it back). That happened to the worker, which has no load
# balancer to report healthy, on 26 Oct, 2 Nov and 3 Nov. So this polls each service's PRIMARY
# deployment until ECS gives an outcome. If a rollout doesn't complete, it prints what ECS says
# happened (deployments, recent service events, and why the recent tasks stopped).
set -euo pipefail

: "${TASK_DEFINITION_ARN:?}" "${WORKER_TASK_DEFINITION_ARN:?}"
: "${ECS_CLUSTER:?}" "${ECS_SERVICE:?}" "${ECS_WORKER_SERVICE:?}"
DEPLOY_TIMEOUT_SECONDS=${DEPLOY_TIMEOUT_SECONDS:-1200}
POLL_SECONDS=${POLL_SECONDS:-15}

update() {  # update <service> <task definition ARN>
  aws ecs update-service \
    --cluster "$ECS_CLUSTER" \
    --service "$1" \
    --task-definition "$2" \
    --query 'service.serviceName' --output text
}

describe() {  # describe <service> <JMESPath query> [output]
  aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$1" \
    --query "$2" --output "${3:-text}"
}

# outcome <service> <expected ARN> -> "<verdict> <PRIMARY task definition> <rolloutState>"
# verdict: done, failed (rolled back, or the rollout FAILED) or waiting.
outcome() {
  local running rollout
  # shellcheck disable=SC2016  # the backticks are JMESPath literals, not shell
  read -r running rollout <<<"$(describe "$1" \
    'services[0].deployments[?status==`PRIMARY`] | [0].[taskDefinition, rolloutState]' \
    2>/dev/null || true)"
  if [[ -z "${running:-}" || "$running" == "None" ]]; then
    echo "waiting - -"  # describe failed for a moment; ask again
  elif [[ "$running" != "$2" ]]; then
    echo "failed $running ${rollout:--}"  # the circuit breaker made the old revision PRIMARY
  elif [[ "$rollout" == "COMPLETED" ]]; then
    echo "done $running $rollout"
  elif [[ "$rollout" == "FAILED" ]]; then
    echo "failed $running $rollout"
  else
    echo "waiting $running ${rollout:--}"
  fi
}

explain() {  # explain <service>: what ECS says happened, for the job log
  local service=$1 events
  {
    echo "----- $service: deployments (status, task definition, rollout, reason, running/desired, failed tasks)"
    # shellcheck disable=SC2016  # JMESPath literals
    describe "$service" 'services[0].deployments[].[status, taskDefinition, rolloutState, rolloutStateReason, join(`/`, [to_string(runningCount), to_string(desiredCount)]), failedTasks]' table || true
    echo "----- $service: latest service events"
    events=$(describe "$service" 'services[0].events[:12].[createdAt, message]' || true)
    printf '%s\n' "$events"
    # The events name the tasks they are about as "(task <id>)": say why the latest ones stopped.
    local -a tasks=()
    mapfile -t tasks < <(grep -o 'task [0-9a-f]\{32\}' <<<"$events" | awk '{print $2}' | sort -u | head -5)
    if ((${#tasks[@]})); then
      echo "----- $service: recent tasks (status, stop code, stopped reason, exit code, container reason)"
      aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "${tasks[@]}" \
        --query 'tasks[].[taskArn, lastStatus, stopCode, stoppedReason, containers[0].exitCode, containers[0].reason]' \
        --output table || true
    fi
    echo "The container's own output is in CloudWatch, log group /ecs/ticket-service."
  } >&2
}

update "$ECS_SERVICE" "$TASK_DEFINITION_ARN"
update "$ECS_WORKER_SERVICE" "$WORKER_TASK_DEFINITION_ARN"
echo "Rolling out; waiting up to $((DEPLOY_TIMEOUT_SECONDS / 60)) minutes for ECS to finish both rollouts..."

services=("$ECS_SERVICE" "$ECS_WORKER_SERVICE")
declare -A expected=([$ECS_SERVICE]=$TASK_DEFINITION_ARN [$ECS_WORKER_SERVICE]=$WORKER_TASK_DEFINITION_ARN)
declare -A result=()
deadline=$((SECONDS + DEPLOY_TIMEOUT_SECONDS))
while :; do
  pending=0
  for service in "${services[@]}"; do
    [[ -n "${result[$service]:-}" ]] && continue
    read -r verdict running rollout <<<"$(outcome "$service" "${expected[$service]}")"
    case $verdict in
      done)
        result[$service]="done"
        echo "Deployed ${expected[$service]} to $service"
        ;;
      failed)
        result[$service]=failed
        echo "$service: the rollout of ${expected[$service]} failed; ECS is running $running (rollout $rollout)" >&2
        ;;
      *) pending=1 ;;
    esac
  done
  ((pending)) || break
  if ((SECONDS >= deadline)); then
    for service in "${services[@]}"; do
      if [[ -z "${result[$service]:-}" ]]; then
        result[$service]=timeout
        echo "$service: the rollout of ${expected[$service]} is still not complete after $((DEPLOY_TIMEOUT_SECONDS / 60)) minutes" >&2
      fi
    done
    break
  fi
  sleep "$POLL_SECONDS"
done

failed=0
for service in "${services[@]}"; do
  if [[ "${result[$service]}" != "done" ]]; then
    failed=1
    explain "$service"
  fi
done
exit "$failed"
