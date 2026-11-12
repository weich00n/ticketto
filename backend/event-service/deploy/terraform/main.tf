# Event Service on ECS Fargate, in the account's default VPC and the team's TicketPlatform cluster.
#
#   API:    event-service         (gunicorn on :3001; API Gateway reaches it through the shared
#                                  VPC link and internal ALB, other services through Cloud Map)
#   Worker: event-service-worker  (outbox to RabbitMQ; marks started events COMPLETED)
#   GitLab main ── OIDC ──► deploy role: push to ECR, run the migration, update both services
#
# The pipeline owns the image and each task definition revision: see ../README.md.
# The database, its login secret and the broker are shared and live outside this stack.

# ---------- existing infrastructure ----------

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_ecs_cluster" "platform" {
  cluster_name = var.cluster_name
}

# Existing dedicated group already allowed by RDS; keep its internal caller rules.
data "aws_security_group" "tasks" {
  vpc_id = data.aws_vpc.default.id
  name   = var.task_security_group_name
}

# Created once for the whole account (by the broker stack).
data "aws_iam_openid_connect_provider" "gitlab" {
  url = "https://gitlab.com"
}

data "aws_secretsmanager_secret" "db" {
  name = var.db_secret_name
}

data "aws_secretsmanager_secret" "rabbitmq" {
  name = var.rabbitmq_secret_name
}

# ---------- image repository and logs ----------

resource "aws_ecr_repository" "event_service" {
  name                 = var.name
  image_tag_mutability = "MUTABLE" # the pipeline re-tags latest

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "event_service" {
  repository = aws_ecr_repository.event_service.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 newest images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "event_service" {
  name              = "/ecs/${var.name}"
  retention_in_days = 30
}

# ---------- task execution role ----------

# Used by ECS to start the containers. There is no task role: the app calls no AWS APIs.
resource "aws_iam_role" "execution" {
  name        = "${var.name}-execution"
  description = "Allows ECS tasks to call AWS services on your behalf."
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = ""
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "read_secrets" {
  name = "read-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "secretsmanager:GetSecretValue"
      Resource = [
        data.aws_secretsmanager_secret.db.arn,
        data.aws_secretsmanager_secret.rabbitmq.arn,
      ]
    }]
  })
}

# ---------- GitLab deploy role (OIDC, no stored keys) ----------

resource "aws_iam_role" "gitlab_deploy" {
  name = "gitlab-${var.name}-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.gitlab.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "gitlab.com:aud" = "https://gitlab.com"
          "gitlab.com:sub" = "project_path:${var.gitlab_project_path}:ref_type:branch:ref:main"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "gitlab_deploy" {
  name = "deploy"
  role = aws_iam_role.gitlab_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
        ]
        Resource = aws_ecr_repository.event_service.arn
      },
      {
        # RegisterTaskDefinition can't be limited by resource. RunTask starts the migration.
        Effect = "Allow"
        Action = [
          "ecs:RegisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
          "ecs:RunTask",
          "ecs:DescribeTasks",
          "ecs:UpdateService",
          "ecs:DescribeServices",
        ]
        Resource = "*"
      },
      {
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.execution.arn
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
    ]
  })
}

# ---------- ECS services ----------

# The latest revisions the pipeline registered.
data "aws_ecs_task_definition" "api" {
  task_definition = var.name
}

data "aws_ecs_task_definition" "worker" {
  task_definition = "${var.name}-worker"
}

locals {
  services = {
    api    = data.aws_ecs_task_definition.api.arn
    worker = data.aws_ecs_task_definition.worker.arn
  }
}

resource "aws_ecs_service" "api" {
  name            = var.name
  cluster         = data.aws_ecs_cluster.platform.arn
  task_definition = local.services.api
  desired_count   = var.desired_count

  enable_ecs_managed_tags = true

  service_registries {
    registry_arn   = aws_service_discovery_service.event.arn
    container_name = var.name
    container_port = var.api_port
  }

  load_balancer {
    target_group_arn = data.aws_lb_target_group.gateway.arn
    container_name   = var.name
    container_port   = var.api_port
  }

  # The ALB's health checks don't count against a new task until it has had time to start.
  health_check_grace_period_seconds = 90

  depends_on = [aws_vpc_security_group_ingress_rule.broker_from_event]

  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }

  # ecs-deploy.sh relies on this to detect a bad release.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [data.aws_security_group.tasks.id]
    assign_public_ip = true
  }

  # Each deploy registers a new task definition; scaling is done in ECS.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

# Exactly one worker. Two would send outbox messages out of order, which can bring a
# cancelled event back. So a deploy stops the old task before it starts the new one.
resource "aws_ecs_service" "worker" {
  name            = "${var.name}-worker"
  cluster         = data.aws_ecs_cluster.platform.arn
  task_definition = local.services.worker
  desired_count   = 1

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  # ECS does not allow a maximum of 100% on a service that rebalances across zones, and
  # one task has nothing to rebalance.
  availability_zone_rebalancing = "DISABLED"

  enable_ecs_managed_tags = true

  depends_on = [aws_vpc_security_group_ingress_rule.broker_from_event]

  capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [data.aws_security_group.tasks.id]
    assign_public_ip = true
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}
