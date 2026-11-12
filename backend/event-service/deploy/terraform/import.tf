# Adopts the resources that were first created by hand in the console, so the first apply
# changes them to match this code instead of recreating them. Once an apply has succeeded
# these blocks do nothing and can be deleted.

import {
  to = aws_ecr_repository.event_service
  id = "event-service"
}

import {
  to = aws_cloudwatch_log_group.event_service
  id = "/ecs/event-service"
}

import {
  to = aws_iam_role.execution
  id = "event-service-execution"
}

import {
  to = aws_iam_role_policy_attachment.execution
  id = "event-service-execution/arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

import {
  to = aws_iam_role_policy.read_secrets
  id = "event-service-execution:read-secrets"
}

import {
  to = aws_iam_role.gitlab_deploy
  id = "gitlab-event-service-deploy"
}

import {
  to = aws_iam_role_policy.gitlab_deploy
  id = "gitlab-event-service-deploy:deploy"
}

import {
  to = aws_ecs_service.api
  id = "TicketPlatform/event-service"
}

import {
  to = aws_ecs_service.worker
  id = "TicketPlatform/event-service-worker"
}
