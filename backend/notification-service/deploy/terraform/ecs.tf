# The worker and the notifications API on ECS Fargate, in the shared TicketPlatform cluster.
#
#   ECS service notification-service-worker: one always-on task (app/worker.py)
#     ──► RabbitMQ, user-service, RDS and SES, in the security group main.tf already allows in
#   API Gateway ── shared VPC link ── shared internal ALB :8082 (api.tf)
#     ──► ECS service notification-service (app/main.py) ──► RDS
#
# The gateway stack (frontend repo, deploy/gateway) owns the ALB, the API's target group and
# listener, and the rule letting the ALB reach 8082 on the API's group. This stack only attaches
# the API service to the target group, the same as ticket-service and reservation-service.
#
# First deploy: `tofu apply -target=aws_security_group.api`, then apply the gateway stack, then
# `tofu apply` here. The gateway stack needs the group to exist, and this one its target group.

locals {
  ecs_tasks_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_ecs_cluster" "this" {
  cluster_name = var.cluster_name
}

data "aws_lb_target_group" "gateway" {
  name = var.gateway_target_group_name
}

# Created by rabbitmq-service's OpenTofu; its `url` key is the broker login. Only the ARN is read
# here: ECS fetches the value when a task starts.
data "aws_secretsmanager_secret" "broker" {
  name = var.broker_secret_name
}

# ---------- network ----------

# Nothing but the ALB may reach the API: it trusts X-User-Sub, so anything that got to port 8082
# could read any user's notifications. The gateway stack adds that one ingress rule. Never add
# a 0.0.0.0/0 rule here, or share this group with another service.
resource "aws_security_group" "api" {
  name        = "${var.name}-api"
  description = "${var.name} API tasks: ${var.api_port} from the shared internal ALB only"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = "${var.name}-api" }
}

# Outbound to ECR, Secrets Manager, CloudWatch Logs and RDS. The default VPC has no NAT, so this
# goes out through the task's public IP.
resource "aws_vpc_security_group_egress_rule" "api_all" {
  security_group_id = aws_security_group.api.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_api" {
  for_each = toset(data.aws_db_instance.shared.vpc_security_groups)

  security_group_id            = each.value
  description                  = "MySQL from ${var.name} API"
  ip_protocol                  = "tcp"
  from_port                    = data.aws_db_instance.shared.port
  to_port                      = data.aws_db_instance.shared.port
  referenced_security_group_id = aws_security_group.api.id
}

# ---------- logs and IAM ----------

resource "aws_cloudwatch_log_group" "service" {
  name              = "/ecs/${var.name}"
  retention_in_days = var.log_retention_days
}

# Used by ECS before the app starts: pull the image, write logs, read the DB and broker secrets.
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = local.ecs_tasks_trust
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution" {
  name = "read-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = [aws_secretsmanager_secret.db.arn, data.aws_secretsmanager_secret.broker.arn]
    }]
  })
}

# Used by the worker's own code, to send email. The API has no task role: it needs nothing.
resource "aws_iam_role" "worker_task" {
  name               = "${var.name}-worker-task"
  assume_role_policy = local.ecs_tasks_trust
}

# SES checks ses:SendEmail against every identity in the email: the sender's and, in the
# sandbox, each recipient's (recipients must be verified identities there). So the resource
# is any identity, and the condition keeps the role to sending from our one address.
resource "aws_iam_role_policy" "worker_ses" {
  name = "send-email"
  role = aws_iam_role.worker_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "ses:SendEmail"
      # SES checks both: sending through a configuration set needs permission on it too.
      Resource = [
        "arn:aws:ses:${var.region}:${local.account_id}:identity/*",
        aws_sesv2_configuration_set.tracking.arn,
      ]
      Condition = { StringEquals = { "ses:FromAddress" = var.ses_sender } }
    }]
  })
}

# ---------- task definitions (first revision only) ----------

# A service needs a task definition to exist before it can be created, so these register the
# first revision from the same files the pipeline fills in. After that the pipeline registers
# every revision, and OpenTofu ignores these.
locals {
  api_task_definition = jsondecode(
    replace(replace(replace(replace(replace(
      file("${path.module}/../task-definition.json"),
      "__IMAGE__", "${aws_ecr_repository.service.repository_url}:latest"),
      "__AWS_REGION__", var.region),
      "__ECS_EXECUTION_ROLE_ARN__", aws_iam_role.execution.arn),
      "__RDS_ENDPOINT__", data.aws_db_instance.shared.address),
    "__DB_SECRET_ARN__", aws_secretsmanager_secret.db.arn)
  )
  worker_task_definition = jsondecode(
    replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(
      file("${path.module}/../worker-task-definition.json"),
      "__IMAGE__", "${aws_ecr_repository.service.repository_url}:latest"),
      "__AWS_REGION__", var.region),
      "__ECS_EXECUTION_ROLE_ARN__", aws_iam_role.execution.arn),
      "__ECS_TASK_ROLE_ARN__", aws_iam_role.worker_task.arn),
      "__RDS_ENDPOINT__", data.aws_db_instance.shared.address),
      "__DB_SECRET_ARN__", aws_secretsmanager_secret.db.arn),
      "__RABBITMQ_SECRET_ARN__", data.aws_secretsmanager_secret.broker.arn),
      "__USER_SERVICE_URL__", var.user_service_url),
      "__EVENT_SERVICE_URL__", var.event_service_url),
      "__EMAIL_FROM__", local.email_from),
    "__SES_CONFIGURATION_SET__", aws_sesv2_configuration_set.tracking.configuration_set_name)
  )
}

resource "aws_ecs_task_definition" "api_first" {
  family                   = local.api_task_definition.family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = local.api_task_definition.cpu
  memory                   = local.api_task_definition.memory
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode(local.api_task_definition.containerDefinitions)

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  lifecycle {
    ignore_changes = all
  }
}

resource "aws_ecs_task_definition" "worker_first" {
  family                   = local.worker_task_definition.family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = local.worker_task_definition.cpu
  memory                   = local.worker_task_definition.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn
  container_definitions    = jsonencode(local.worker_task_definition.containerDefinitions)

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  lifecycle {
    ignore_changes = all
  }
}

# ---------- services ----------

resource "aws_ecs_service" "api" {
  name            = var.name
  cluster         = data.aws_ecs_cluster.this.arn
  task_definition = aws_ecs_task_definition.api_first.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # Start the new task before stopping the old one; roll back if new tasks keep failing.
  # scripts/ci/ecs-deploy.sh relies on the circuit breaker to detect a bad release.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = data.aws_subnets.service.ids
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = true # no NAT: needed to reach ECR and Secrets Manager
  }

  load_balancer {
    target_group_arn = data.aws_lb_target_group.gateway.arn
    container_name   = local.api_task_definition.containerDefinitions[0].name
    container_port   = var.api_port
  }

  # The ALB's health checks don't count against a new task until it has had time to start.
  health_check_grace_period_seconds = 90

  lifecycle {
    ignore_changes = [task_definition] # the pipeline's to change
  }

  depends_on = [aws_iam_role_policy.execution, aws_cloudwatch_log_group.service]
}

# No port, no load balancer: nothing calls it. Two tasks overlapping during a deployment are
# safe: emails are claimed with SKIP LOCKED, and messages are deduplicated.
resource "aws_ecs_service" "worker" {
  name            = "${var.name}-worker"
  cluster         = data.aws_ecs_cluster.this.arn
  task_definition = aws_ecs_task_definition.worker_first.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = data.aws_subnets.service.ids
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = true # no NAT: needed to reach ECR, Secrets Manager and SES
  }

  lifecycle {
    ignore_changes = [task_definition] # the pipeline's to change
  }

  depends_on = [aws_iam_role_policy.execution, aws_cloudwatch_log_group.service]
}
