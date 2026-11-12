# user-service on ECS Fargate in the account's default VPC, in the shared TicketPlatform cluster.
#
#   Browser ── token ──► API Gateway "api" (shared; Cognito JWT authorizer)
#                          │ sets X-User-Sub / -Username / -Groups from the token's claims
#                          ▼ shared VPC link ──► shared internal ALB :8083 ──► task :8000
#   notification-service ──► Cloud Map user-service.ticketto.local:8001 ──► task (/internal/v1 only)
#   task ──► RDS db-ticketing (shared instance; database db_user) · Cognito admin APIs
#   GitLab main ── OIDC ──► deploy role: push to ECR, register task definitions, run the
#                           migration task, update the service
#
# This code owns the service's infrastructure. The GitLab pipeline owns the task definition
# (image, settings) and registers a new revision on every deploy, so `tofu apply` never
# touches the running revision. Setup and first deploy: ../../plan.md

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  db_name    = "db_user"
  db_user    = "user_service"
}

# ---------- network: the default VPC ----------

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "service" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
  filter {
    name   = "availability-zone"
    values = var.availability_zones
  }
}

# The app trusts the X-User-* headers, so nothing may reach it except through API Gateway, whose
# only path in is the shared ALB on 8000 (the gateway stack owns that rule). Other services get
# 8001, which serves only /internal/v1 (app/internal_port.py; their stacks own those rules).
# Never let another service in on 8000, and never add a 0.0.0.0/0 rule here.
resource "aws_security_group" "service" {
  name = var.name
  # Out of date, but AWS can't edit a description: changing it would replace the group.
  description = "${var.name} tasks: ${local.container_port} from the API Gateway VPC link only"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = var.name }
}

# Outbound to ECR, Secrets Manager, CloudWatch Logs, Cognito and RDS. A default VPC has no
# NAT, so this goes out through the task's public IP.
resource "aws_vpc_security_group_egress_rule" "service_all" {
  security_group_id = aws_security_group.service.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# ---------- the shared RDS instance: our own database in it ----------

data "aws_db_instance" "shared" {
  db_instance_identifier = var.rds_instance_identifier
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_service" {
  for_each = toset(data.aws_db_instance.shared.vpc_security_groups)

  security_group_id            = each.value
  description                  = "MySQL from ${var.name}"
  ip_protocol                  = "tcp"
  from_port                    = data.aws_db_instance.shared.port
  to_port                      = data.aws_db_instance.shared.port
  referenced_security_group_id = aws_security_group.service.id
}

# The database and MySQL user are created by ../create-database.sh with these credentials.
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "db" {
  name                    = "${var.name}/db"
  description             = "MySQL user for ${var.name} on ${var.rds_instance_identifier}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    username = local.db_user
    password = random_password.db.result
    host     = data.aws_db_instance.shared.address
    port     = data.aws_db_instance.shared.port
    dbname   = local.db_name
  })
}

# ---------- image repository and logs ----------

resource "aws_ecr_repository" "service" {
  name                 = var.name
  image_tag_mutability = "MUTABLE" # :latest moves with every build; :<sha> tags never change
  force_delete         = var.allow_destroy_data

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "service" {
  repository = aws_ecr_repository.service.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "service" {
  name              = "/ecs/${var.name}"
  retention_in_days = var.log_retention_days
}

# ---------- IAM: what ECS needs to start the task, and what the app may do ----------

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

# The worker's broker login. rabbitmq-service's OpenTofu creates it (`tofu apply` there first);
# its `url` key is amqp://user-service:<password>@<broker>:5672/%2F.
data "aws_secretsmanager_secret" "broker" {
  name = "rabibi/service/${var.name}"
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
  name = "read-db-secret"
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

# Used by the app's own code: the three Cognito admin calls in app/cognito.py.
resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = local.ecs_tasks_trust
}

resource "aws_iam_role_policy" "task" {
  name = "cognito-admin"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "cognito-idp:AdminGetUser",
        "cognito-idp:AdminAddUserToGroup",
        "cognito-idp:AdminUpdateUserAttributes",
      ]
      Resource = "arn:aws:cognito-idp:${var.region}:${local.account_id}:userpool/${var.cognito_user_pool_id}"
    }]
  })
}

# ---------- cluster and service discovery ----------

# Shared with the other services and owned outside this module: look it up, never change it.
data "aws_ecs_cluster" "this" {
  cluster_name = var.cluster_name
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  count = var.create_service_discovery_namespace ? 1 : 0

  name        = var.service_discovery_namespace
  description = "Ticketto services: <service>.${var.service_discovery_namespace}"
  vpc         = data.aws_vpc.default.id
}

data "aws_service_discovery_dns_namespace" "this" {
  count = var.create_service_discovery_namespace ? 0 : 1

  name = var.service_discovery_namespace
  type = "DNS_PRIVATE"
}

locals {
  namespace_id = (var.create_service_discovery_namespace
    ? aws_service_discovery_private_dns_namespace.this[0].id
  : data.aws_service_discovery_dns_namespace.this[0].id)
}

# ECS adds each running task here and removes it when it stops. Other services reach
# /internal/v1 through this name; API Gateway goes through the ALB instead.
resource "aws_service_discovery_service" "service" {
  name          = var.name
  force_destroy = true # deregister leftover instances on destroy

  dns_config {
    namespace_id   = local.namespace_id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "A"
      ttl  = 10
    }
    dns_records {
      type = "SRV"
      ttl  = 10
    }
  }

  # ECS reports task health to Cloud Map. failure_threshold is deprecated (AWS always uses 1),
  # but an empty block is dropped on create and then forces a replacement on every plan.
  health_check_custom_config {
    failure_threshold = 1
  }
}

# ---------- the shared internal ALB: looked up, never changed ----------

# The gateway stack (frontend/deploy/gateway) owns the ALB, this listener, the target group, the
# shared VPC link and the rule letting the ALB reach the tasks. Apply that stack first.
data "aws_lb" "internal" {
  name = var.internal_alb_name
}

data "aws_lb_listener" "api" {
  load_balancer_arn = data.aws_lb.internal.arn
  port              = var.alb_listener_port
}

data "aws_lb_target_group" "gateway" {
  name = var.gateway_target_group_name
}

# ---------- task definition (first revision only) and service ----------

# The service needs a task definition to exist before it can be created, so this registers
# the first revision from ../task-definition.json, the same file the pipeline fills in.
# After that the pipeline registers every revision, and OpenTofu ignores this one.
locals {
  task_definition = jsondecode(
    replace(replace(replace(replace(replace(replace(replace(
      file("${path.module}/../task-definition.json"),
      "__IMAGE__", "${aws_ecr_repository.service.repository_url}:latest"),
      "__AWS_REGION__", var.region),
      "__ECS_EXECUTION_ROLE_ARN__", aws_iam_role.execution.arn),
      "__ECS_TASK_ROLE_ARN__", aws_iam_role.task.arn),
      "__RDS_ENDPOINT__", data.aws_db_instance.shared.address),
      "__DB_SECRET_ARN__", aws_secretsmanager_secret.db.arn),
    "__APP_COGNITO_USER_POOL_ID__", var.cognito_user_pool_id)
  )
  container      = local.task_definition.containerDefinitions[0]
  container_port = 8000 # matches portMappings in ../task-definition.json and the Dockerfile
}

resource "aws_ecs_task_definition" "first" {
  family                   = local.task_definition.family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = local.task_definition.cpu
  memory                   = local.task_definition.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions    = jsonencode(local.task_definition.containerDefinitions)

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  lifecycle {
    ignore_changes = all
  }
}

resource "aws_ecs_service" "service" {
  name            = var.name
  cluster         = data.aws_ecs_cluster.this.arn
  task_definition = aws_ecs_task_definition.first.arn
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
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = true # no NAT: needed to reach ECR and Secrets Manager
  }

  service_registries {
    registry_arn   = aws_service_discovery_service.service.arn
    container_name = local.container.name
    container_port = local.container_port
  }

  load_balancer {
    target_group_arn = data.aws_lb_target_group.gateway.arn
    container_name   = local.container.name
    container_port   = local.container_port
  }

  # The ALB's health checks don't count against a new task until it has had time to start.
  health_check_grace_period_seconds = 90

  lifecycle {
    ignore_changes = [task_definition] # the pipeline's to change
  }
}

# ---------- the worker: RabbitMQ consumer (python -m app.worker), same image ----------

# Consumes user-service.OrderConfirmed and user-service.EventCancelled. No port, no Cloud
# Map entry: nothing calls it. The broker must let its security group in on 5672
# (rabbitmq-service's amqp_allowed_sg_ids), which is the same group as the API's.
locals {
  worker_task_definition = jsondecode(
    replace(replace(replace(replace(replace(replace(replace(replace(
      file("${path.module}/../worker-task-definition.json"),
      "__IMAGE__", "${aws_ecr_repository.service.repository_url}:latest"),
      "__AWS_REGION__", var.region),
      "__ECS_EXECUTION_ROLE_ARN__", aws_iam_role.execution.arn),
      "__ECS_TASK_ROLE_ARN__", aws_iam_role.task.arn),
      "__RDS_ENDPOINT__", data.aws_db_instance.shared.address),
      "__DB_SECRET_ARN__", aws_secretsmanager_secret.db.arn),
      "__RABBITMQ_SECRET_ARN__", data.aws_secretsmanager_secret.broker.arn),
    "__APP_COGNITO_USER_POOL_ID__", var.cognito_user_pool_id)
  )
}

resource "aws_ecs_task_definition" "worker_first" {
  family                   = local.worker_task_definition.family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = local.worker_task_definition.cpu
  memory                   = local.worker_task_definition.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions    = jsonencode(local.worker_task_definition.containerDefinitions)

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  lifecycle {
    ignore_changes = all
  }
}

resource "aws_ecs_service" "worker" {
  name            = "${var.name}-worker"
  cluster         = data.aws_ecs_cluster.this.arn
  task_definition = aws_ecs_task_definition.worker_first.arn
  desired_count   = var.desired_count # scaled with the API
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = data.aws_subnets.service.ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = true # no NAT: needed to reach ECR and Secrets Manager
  }

  lifecycle {
    ignore_changes = [task_definition] # the pipeline's to change
  }
}

# ---------- API Gateway: user-service's routes on the shared HTTP API ----------

data "aws_apigatewayv2_api" "shared" {
  api_id = var.api_id
}

# `overwrite:` replaces any X-User-* header the client sent, so callers can't fake their
# identity. `username` is only in access tokens: the frontend must send the access token.
#
# X-User-Groups comes from the colon-free `groups` claim (comma-separated, "" for no groups)
# that the Cognito pre-token-generation Lambda adds (cognito.tf). HTTP APIs reject any claim
# name with a colon, so `cognito:groups` itself can't be mapped. If the trigger is ever
# detached the claim is missing and the header is sent empty, never client-supplied.
# `overwrite:path` stops the stage prefix reaching the app.
resource "aws_apigatewayv2_integration" "service" {
  api_id                 = data.aws_apigatewayv2_api.shared.id
  integration_type       = "HTTP_PROXY"
  integration_method     = "ANY"
  connection_type        = "VPC_LINK"
  connection_id          = var.api_vpc_link_id
  integration_uri        = data.aws_lb_listener.api.arn
  payload_format_version = "1.0"

  request_parameters = {
    "overwrite:header.X-User-Sub"      = "$context.authorizer.claims.sub"
    "overwrite:header.X-User-Username" = "$context.authorizer.claims.username"
    "overwrite:header.X-User-Groups"   = "$context.authorizer.claims.groups"
    "overwrite:path"                   = "$request.path"
    "remove:header.X-User-Id"          = "''"
  }
}

# /internal/v1/users/* and /health are deliberately not routed: they stay inside the VPC.
resource "aws_apigatewayv2_route" "service" {
  for_each = toset(["ANY /api/v1/users", "ANY /api/v1/users/{proxy+}"])

  api_id             = data.aws_apigatewayv2_api.shared.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.service.id}"
  authorization_type = "JWT"
  authorizer_id      = var.api_authorizer_id
}

# ---------- GitLab CI: OIDC, no stored keys ----------

# The account's https://gitlab.com provider already exists (rabbitmq-service created it).
data "aws_iam_openid_connect_provider" "gitlab" {
  url = "https://gitlab.com"
}

# Only pipelines on this project's main branch can assume it.
resource "aws_iam_role" "deploy" {
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

resource "aws_iam_role_policy" "deploy" {
  name = "push-migrate-and-deploy"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "EcrLogin", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
        ]
        Resource = aws_ecr_repository.service.arn
      },
      # These two can't be limited to a resource.
      {
        Sid      = "TaskDefinitions"
        Effect   = "Allow"
        Action   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"]
        Resource = "*"
      },
      {
        Sid       = "RunMigrationTask"
        Effect    = "Allow"
        Action    = "ecs:RunTask"
        Resource  = "arn:aws:ecs:${var.region}:${local.account_id}:task-definition/${local.task_definition.family}:*"
        Condition = { ArnEquals = { "ecs:cluster" = data.aws_ecs_cluster.this.arn } }
      },
      {
        Sid      = "WatchMigrationTask"
        Effect   = "Allow"
        Action   = "ecs:DescribeTasks"
        Resource = "arn:aws:ecs:${var.region}:${local.account_id}:task/${var.cluster_name}/*"
      },
      {
        Sid    = "UpdateService"
        Effect = "Allow"
        Action = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = [
          "arn:aws:ecs:${var.region}:${local.account_id}:service/${var.cluster_name}/${var.name}",
          "arn:aws:ecs:${var.region}:${local.account_id}:service/${var.cluster_name}/${var.name}-worker",
        ]
      },
      {
        Sid       = "PassTaskRoles"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
    ]
  })
}
