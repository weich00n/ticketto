data "aws_caller_identity" "current" {}

data "aws_vpc" "this" {
  id = var.vpc_id
}

# All subnets of the default VPC are public (internet gateway, no NAT), so tasks get a public IP
# to reach ECR, Secrets Manager, CloudWatch and Stripe, the same as the other services.
data "aws_subnets" "this" {
  filter {
    name   = "vpc-id"
    values = [var.vpc_id]
  }
}

data "aws_secretsmanager_secret" "db" {
  name = var.db_secret_name
}

data "aws_secretsmanager_secret" "rabbitmq" {
  name = var.rabbitmq_secret_name
}

# ---------- network ----------

resource "aws_security_group" "checkout" {
  name        = "checkout-service"
  description = "checkout-service ECS tasks (API + worker)"
  vpc_id      = var.vpc_id

  tags = { Name = "checkout-service" }
}

# The API trusts the X-User-Sub header, so it only accepts traffic from inside the VPC.
resource "aws_vpc_security_group_ingress_rule" "checkout_api" {
  security_group_id = aws_security_group.checkout.id
  description       = "checkout API from inside the VPC"
  ip_protocol       = "tcp"
  from_port         = 3002
  to_port           = 3002
  cidr_ipv4         = data.aws_vpc.this.cidr_block
}

resource "aws_vpc_security_group_egress_rule" "checkout_all" {
  security_group_id = aws_security_group.checkout.id
  description       = "ECR, Secrets Manager, logs, RDS, RabbitMQ, reservation-service, Stripe"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "db_from_checkout" {
  security_group_id            = var.db_security_group_id
  description                  = "MySQL from checkout-service"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
  referenced_security_group_id = aws_security_group.checkout.id
}

resource "aws_vpc_security_group_ingress_rule" "rabbitmq_from_checkout" {
  security_group_id            = var.rabbitmq_security_group_id
  description                  = "AMQP from checkout-service"
  ip_protocol                  = "tcp"
  from_port                    = 5672
  to_port                      = 5672
  referenced_security_group_id = aws_security_group.checkout.id
}

resource "aws_vpc_security_group_ingress_rule" "reservation_from_checkout" {
  security_group_id            = var.reservation_security_group_id
  description                  = "reservation-service internal API from checkout-service"
  ip_protocol                  = "tcp"
  from_port                    = 8081
  to_port                      = 8081
  referenced_security_group_id = aws_security_group.checkout.id
}

# ---------- image, logs, secrets ----------

resource "aws_ecr_repository" "checkout" {
  name                 = "checkout-service"
  image_tag_mutability = "MUTABLE" # a deploy re-tags latest

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_cloudwatch_log_group" "checkout" {
  name              = "/ecs/checkout-service"
  retention_in_days = var.log_retention_days
}

# Created with empty keys; the app boots without them. Fill the real Stripe test keys with
# `aws secretsmanager put-secret-value` (README); Terraform then leaves the value alone.
resource "aws_secretsmanager_secret" "stripe" {
  name        = "checkout-service/stripe"
  description = "Stripe keys for checkout-service: STRIPE_API_KEY and STRIPE_WEBHOOK_SECRET"
}

resource "aws_secretsmanager_secret_version" "stripe_placeholder" {
  secret_id     = aws_secretsmanager_secret.stripe.id
  secret_string = jsonencode({ STRIPE_API_KEY = "", STRIPE_WEBHOOK_SECRET = "" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# ---------- execution role ----------

resource "aws_iam_role" "execution" {
  count = var.execution_role_arn == null ? 1 : 0

  name = "checkout-service-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  count = var.execution_role_arn == null ? 1 : 0

  role       = aws_iam_role.execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  count = var.execution_role_arn == null ? 1 : 0

  name = "read-checkout-secrets"
  role = aws_iam_role.execution[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = [data.aws_secretsmanager_secret.db.arn, data.aws_secretsmanager_secret.rabbitmq.arn, aws_secretsmanager_secret.stripe.arn]
    }]
  })
}

# ---------- GitLab deploy role ----------

# The pipeline's build-and-push, migrate and deploy jobs sign in with GitLab OIDC and assume
# this role; only this project's main branch can. The gitlab.com OIDC provider is shared
# (created once for the account by a teammate), so it is read, not managed.
data "aws_iam_openid_connect_provider" "gitlab" {
  url = "https://gitlab.com"
}

resource "aws_iam_role" "gitlab_deploy" {
  name = "gitlab-checkout-service-deploy"
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

# Same permissions as the other services' gitlab-<service>-deploy roles (deploy/README.md §7).
resource "aws_iam_role_policy" "gitlab_deploy" {
  name = "deploy"
  role = aws_iam_role.gitlab_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
        ]
        Resource = aws_ecr_repository.checkout.arn
      },
      {
        # RegisterTaskDefinition can't be limited by resource.
        Effect = "Allow"
        Action = [
          "ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition",
          "ecs:RunTask", "ecs:DescribeTasks",
          "ecs:UpdateService", "ecs:DescribeServices",
        ]
        Resource = "*"
      },
      {
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = local.execution_role_arn
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
    ]
  })
}

# ---------- task definitions ----------

locals {
  execution_role_arn = coalesce(var.execution_role_arn, try(aws_iam_role.execution[0].arn, null))
  image              = "${aws_ecr_repository.checkout.repository_url}:${var.image_tag}"

  # deploy/*.json stay the single source of truth (the GitLab pipeline renders the same files).
  placeholders = {
    "__IMAGE__"                  = local.image
    "__AWS_REGION__"             = var.aws_region
    "__ECS_EXECUTION_ROLE_ARN__" = local.execution_role_arn
    "__RDS_ENDPOINT__"           = var.rds_endpoint
    "__DB_SECRET_ARN__"          = data.aws_secretsmanager_secret.db.arn
    "__RABBITMQ_SECRET_ARN__"    = "${data.aws_secretsmanager_secret.rabbitmq.arn}:url::" # JSON secret, key url
    "__STRIPE_SECRET_ARN__"      = aws_secretsmanager_secret.stripe.arn
    "__RESERVATION_BASE_URL__"   = var.reservation_base_url
  }

  # Each value is JSON-encoded and spliced in without its quotes, so the JSON stays valid.
  templates = {
    api    = file("${path.module}/../task-definition.json")
    worker = file("${path.module}/../worker-task-definition.json")
  }
  rendered = {
    for name, text in local.templates : name => jsondecode(join("", [
      for part in regexall("__[A-Z_]+__|[^_]+|_", text) :
      contains(keys(local.placeholders), part) ? trim(jsonencode(local.placeholders[part]), "\"") : part
    ]))
  }
}

resource "aws_ecs_task_definition" "this" {
  for_each = local.rendered

  family                   = each.value.family
  network_mode             = each.value.networkMode
  requires_compatibilities = each.value.requiresCompatibilities
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = each.value.executionRoleArn
  container_definitions    = jsonencode(each.value.containerDefinitions)

  runtime_platform {
    operating_system_family = each.value.runtimePlatform.operatingSystemFamily
    cpu_architecture        = each.value.runtimePlatform.cpuArchitecture
  }

  depends_on = [aws_cloudwatch_log_group.checkout]
}

# ---------- services ----------

# Each service's task count is owned by Application Auto Scaling (autoscaling.tf), between min
# and max: the API scales on CPU, the worker is pinned (min = max). `desired` is only the count
# a service starts with when it is first created.
locals {
  services = {
    api    = { name = "checkout-service", desired = var.api_min_count, min = var.api_min_count, max = var.api_max_count }
    worker = { name = "checkout-service-worker", desired = var.worker_desired_count, min = var.worker_desired_count, max = var.worker_desired_count }
  }
}

resource "aws_ecs_service" "this" {
  for_each = var.deploy_services ? local.services : {}

  name            = each.value.name
  cluster         = var.ecs_cluster
  task_definition = aws_ecs_task_definition.this[each.key].arn
  desired_count   = each.value.desired
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.this.ids
    security_groups  = [aws_security_group.checkout.id]
    assign_public_ip = true # no NAT in this VPC
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # API Gateway reaches the API through the shared ALB's target group (gateway.tf); the worker
  # has no port and nothing calls it.
  dynamic "load_balancer" {
    for_each = each.key == "api" ? [data.aws_lb_target_group.gateway.arn] : []
    content {
      target_group_arn = load_balancer.value
      container_name   = "checkout-service"
      container_port   = var.api_port
    }
  }

  # The ALB's health checks don't count against a new task until it has had time to start.
  health_check_grace_period_seconds = each.key == "api" ? 90 : null

  wait_for_steady_state = true

  # The GitLab pipeline owns releases: it registers new task definition revisions and rolls
  # the services onto them. Terraform's revision is only the first one the service starts on,
  # so a later apply doesn't roll a CI deploy back. The running count belongs to the
  # autoscaler (autoscaling.tf), so an apply never resets a scaled-out API either; change
  # the counts through the min/max variables instead.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}
