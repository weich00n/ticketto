#!/bin/bash
# Opens the SSM port-forwards the checker needs. Since 2026-11-03 RDS and both Redis instances are
# VPC-only, so make-env.sh points every URL at 127.0.0.1:
#
#   127.0.0.1:16379  reservation Redis   (SSM on its own EC2 instance)
#   127.0.0.1:13306  RDS db-ticketing    (ECS Exec through a queue-api task)
#   127.0.0.1:16380  queue Redis         (ECS Exec through a queue-api task)
#
# queue-api is the one service with ECS Exec on, and its security group is already let into RDS and
# queue Redis, so no security group changes are needed. Forwards drop when the Redis instance
# reboots (C2) or the queue-api task is replaced: run this again. Ctrl-C closes them.
set -euo pipefail
export AWS_REGION=${AWS_REGION:-us-east-1}
CLUSTER=${ECS_CLUSTER:-TicketPlatform}
RES_REDIS=$(aws ec2 describe-instances --filters Name=tag:Name,Values=reservation-Redis-v1.1 Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
QUEUE_REDIS=$(aws ec2 describe-instances --filters Name=tag:Name,Values=queue-Redis Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)
RDS_HOST=$(aws rds describe-db-instances --db-instance-identifier db-ticketing --query 'DBInstances[0].Endpoint.Address' --output text)
TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name queue-api --desired-status RUNNING --query 'taskArns[0]' --output text)
RUNTIME=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[?name==`api`].runtimeId | [0]' --output text)
VIA_TASK="ecs:${CLUSTER}_${TASK##*/}_${RUNTIME}"

forward() { # target, local port, remote host (empty = the target itself), remote port
  if [ -z "$3" ]; then
    aws ssm start-session --target "$1" --document-name AWS-StartPortForwardingSession \
      --parameters "portNumber=$4,localPortNumber=$2" >/dev/null &
  else
    aws ssm start-session --target "$1" --document-name AWS-StartPortForwardingSessionToRemoteHost \
      --parameters "host=$3,portNumber=$4,localPortNumber=$2" >/dev/null &
  fi
}

# Each forward is the aws CLI plus a session-manager-plugin child: stop both, or the port stays taken.
trap 'for p in $(jobs -p); do pkill -P "$p"; kill "$p"; done 2>/dev/null' EXIT
forward "$RES_REDIS" 16379 "" 6379
forward "$VIA_TASK" 13306 "$RDS_HOST" 3306
forward "$VIA_TASK" 16380 "$QUEUE_REDIS" 6379
echo "reservation Redis 127.0.0.1:16379 ($RES_REDIS), RDS 127.0.0.1:13306 and queue Redis 127.0.0.1:16380 (via queue-api task ${TASK##*/})"
wait
