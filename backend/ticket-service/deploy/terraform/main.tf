# ticket-service on ECS Fargate, in the team's TicketPlatform cluster and the default VPC.
# Two services from one image: the API (ticket-service) and the worker (ticket-service-worker).
#
#   API + worker (Fargate, public IP for outbound only) ──► RDS db-ticketing  (3306)
#   worker                                              ──► RabbitMQ rabibi   (5672, the broker team's rule)
#   worker                                              ──► Amazon SES SMTP   (587, STARTTLS)
#   API Gateway ── VPC link ── shared internal ALB (gateway stack) ──► API (3006)
#   GitLab main ── OIDC ──► deploy role: push to ECR, register task definitions, run the
#                           migration task, update both services
#
# Used, never managed here: the VPC, the cluster, RDS, the broker and its ticket-service
# secret, the GitLab OIDC provider. Everything else below belongs to this service.
# Setup and first deployment: ../README.md

data "aws_caller_identity" "current" {}

locals {
  account_id     = data.aws_caller_identity.current.account_id
  worker_name    = "${var.name}-worker"
  cluster_prefix = "arn:aws:ecs:${var.region}:${local.account_id}"
  # The account's GitLab OIDC provider (shared, already exists). Built from its fixed ARN rather
  # than looked up, so applying needs no iam:GetOpenIDConnectProvider permission.
  gitlab_oidc_provider_arn = "arn:aws:iam::${local.account_id}:oidc-provider/gitlab.com"
}

# ---------- what already exists ----------

data "aws_vpc" "default" {
  default = true
}

# The default VPC's subnets are all public and it has no NAT, so tasks get a public IP to reach
# ECR, Secrets Manager, CloudWatch and the broker. Nothing comes in that way: see the security group.
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_ecs_cluster" "shared" {
  cluster_name = var.ecs_cluster_name
}

data "aws_db_instance" "shared" {
  db_instance_identifier = var.rds_instance_id
}

data "aws_secretsmanager_secret" "broker" {
  name = var.broker_secret_name
}

# API Gateway reaches the API through the shared internal ALB. The gateway stack (frontend repo,
# deploy/gateway) owns the ALB, this target group and the rule letting the ALB reach 3006 on this
# service's group; this stack only attaches the API service to the target group.
data "aws_lb_target_group" "gateway" {
  name = var.gateway_target_group_name
}

# ---------- network ----------

resource "aws_security_group" "service" {
  name        = var.name
  description = "ticket-service API and worker tasks: 3006 from allowed callers only"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = var.name }
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.service.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "http_from_clients" {
  for_each = toset(var.client_security_group_ids)

  security_group_id            = aws_security_group.service.id
  description                  = "HTTP 3006 from ${each.value}"
  ip_protocol                  = "tcp"
  from_port                    = 3006
  to_port                      = 3006
  referenced_security_group_id = each.value
}

# Added to the RDS instance's group, which this stack doesn't own. A separate rule, so it leaves
# the group's other rules alone, and `tofu destroy` removes only this one.
resource "aws_vpc_security_group_ingress_rule" "rds_from_service" {
  for_each = toset(data.aws_db_instance.shared.vpc_security_groups)

  security_group_id            = each.value
  description                  = "MySQL from ${var.name}"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
  referenced_security_group_id = aws_security_group.service.id
}

# ---------- image repository and logs ----------

resource "aws_ecr_repository" "service" {
  name                 = var.name
  image_tag_mutability = "MUTABLE" # :latest moves with every push; :<sha> tags never change
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

# Both services log here: stream prefix ticket-service/ for the API, worker/ for the worker.
resource "aws_cloudwatch_log_group" "service" {
  name              = "/ecs/${var.name}"
  retention_in_days = 14
}

# ---------- secrets ----------

# The service's own MySQL user. tofu only makes up the password; create-db-user.sh creates the
# user on RDS with it, once.
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "db" {
  name                    = "${var.name}/db"
  description             = "MySQL user for ${var.name} on ${var.rds_instance_id}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    username = var.db_username
    password = random_password.db.result
  })
}

# QR_SIGNING_KEYS: "key_id:secret". Alphanumeric only, since ',' and ':' separate keys.
# To rotate, put "k2:<new>,k1:<old>" here and redeploy; see the README's QR key section.
resource "random_password" "qr_k1" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "qr_keys" {
  name                    = "${var.name}/qr-signing-keys"
  description             = "HMAC keys that sign ticket QR codes (QR_SIGNING_KEYS)"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "qr_keys" {
  secret_id     = aws_secretsmanager_secret.qr_keys.id
  secret_string = "k1:${random_password.qr_k1.result}"

  lifecycle {
    ignore_changes = [secret_string] # set by hand after a rotation; never overwritten
  }
}

# ---------- email: Amazon SES over SMTP ----------

# The From address. Creating it makes AWS email that address a verification link; SES sends
# nothing from it until the link is clicked. While the account is in the SES sandbox, SES also
# delivers only to addresses verified the same way (../README.md, Email).
resource "aws_sesv2_email_identity" "sender" {
  email_identity = var.email_sender
}

# SES's SMTP endpoint signs in with an IAM user's access key, so the worker gets a user that can
# do nothing but send mail from that one address.
resource "aws_iam_user" "smtp" {
  name = "${var.name}-ses-smtp"
}

resource "aws_iam_user_policy" "smtp" {
  name = "send-ticket-email"
  user = aws_iam_user.smtp.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "ses:SendRawEmail" # what SMTP sending uses
      # All identities, because in the sandbox SES also checks each recipient's; the condition
      # still pins the From address.
      Resource  = "arn:aws:ses:${var.region}:${local.account_id}:identity/*"
      Condition = { StringEquals = { "ses:FromAddress" = var.email_sender } }
    }]
  })
}

# To rotate: `tofu apply -replace=aws_iam_access_key.smtp`, then redeploy the worker.
resource "aws_iam_access_key" "smtp" {
  user = aws_iam_user.smtp.name
}

# The worker's SMTP login (the key ID, and the SES SMTP password AWS derives from the key's
# secret for this region) and its From header. tofu writes it here directly, so the password
# never appears in output, in GitLab or in a file.
resource "aws_secretsmanager_secret" "smtp" {
  name                    = "${var.name}/ses-smtp"
  description             = "Amazon SES SMTP login and From address for ${local.worker_name}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "smtp" {
  secret_id = aws_secretsmanager_secret.smtp.id
  secret_string = jsonencode({
    username = aws_iam_access_key.smtp.id
    password = aws_iam_access_key.smtp.ses_smtp_password_v4
    from     = "${var.email_sender_name} <${var.email_sender}>"
  })
}

# ---------- execution role: pull the image, read the secrets, write logs ----------

resource "aws_iam_role" "execution" {
  name = "${var.name}-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
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

resource "aws_iam_role_policy" "execution" {
  name = "read-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "secretsmanager:GetSecretValue"
      Resource = [
        aws_secretsmanager_secret.db.arn,
        aws_secretsmanager_secret.qr_keys.arn,
        aws_secretsmanager_secret.smtp.arn,
        data.aws_secretsmanager_secret.broker.arn,
      ]
    }]
  })
}

# ---------- task definitions and services ----------

# The same templates the migrate job fills in, so the two can't drift. tofu registers only the
# first revisions; every deploy after that registers its own and points the services at them.
locals {
  placeholders = {
    "__IMAGE__"                  = "${aws_ecr_repository.service.repository_url}:latest"
    "__AWS_REGION__"             = var.region
    "__ECS_EXECUTION_ROLE_ARN__" = aws_iam_role.execution.arn
    "__RDS_ENDPOINT__"           = data.aws_db_instance.shared.address
    "__DB_SECRET_ARN__"          = aws_secretsmanager_secret.db.arn
    "__QR_KEYS_SECRET_ARN__"     = aws_secretsmanager_secret.qr_keys.arn
    "__RABBITMQ_SECRET_ARN__"    = data.aws_secretsmanager_secret.broker.arn
    "__SMTP_SECRET_ARN__"        = aws_secretsmanager_secret.smtp.arn
    "__EVENT_SERVICE_URL__"      = var.event_service_url
  }
  p = local.placeholders

  api_template    = file("${path.module}/../task-definition.json")
  worker_template = file("${path.module}/../worker-task-definition.json")

  # One replace() per placeholder; the precondition on aws_ecs_task_definition.this catches any
  # template placeholder missing here.
  api_task = jsondecode(
    replace(replace(replace(replace(replace(replace(replace(replace(replace(local.api_template,
      "__IMAGE__", local.p["__IMAGE__"]),
      "__AWS_REGION__", local.p["__AWS_REGION__"]),
      "__ECS_EXECUTION_ROLE_ARN__", local.p["__ECS_EXECUTION_ROLE_ARN__"]),
      "__RDS_ENDPOINT__", local.p["__RDS_ENDPOINT__"]),
      "__DB_SECRET_ARN__", local.p["__DB_SECRET_ARN__"]),
      "__QR_KEYS_SECRET_ARN__", local.p["__QR_KEYS_SECRET_ARN__"]),
      "__RABBITMQ_SECRET_ARN__", local.p["__RABBITMQ_SECRET_ARN__"]),
      "__SMTP_SECRET_ARN__", local.p["__SMTP_SECRET_ARN__"]),
    "__EVENT_SERVICE_URL__", local.p["__EVENT_SERVICE_URL__"])
  )
  worker_task = jsondecode(
    replace(replace(replace(replace(replace(replace(replace(replace(replace(local.worker_template,
      "__IMAGE__", local.p["__IMAGE__"]),
      "__AWS_REGION__", local.p["__AWS_REGION__"]),
      "__ECS_EXECUTION_ROLE_ARN__", local.p["__ECS_EXECUTION_ROLE_ARN__"]),
      "__RDS_ENDPOINT__", local.p["__RDS_ENDPOINT__"]),
      "__DB_SECRET_ARN__", local.p["__DB_SECRET_ARN__"]),
      "__QR_KEYS_SECRET_ARN__", local.p["__QR_KEYS_SECRET_ARN__"]),
      "__RABBITMQ_SECRET_ARN__", local.p["__RABBITMQ_SECRET_ARN__"]),
      "__SMTP_SECRET_ARN__", local.p["__SMTP_SECRET_ARN__"]),
    "__EVENT_SERVICE_URL__", local.p["__EVENT_SERVICE_URL__"])
  )
  tasks = { api = local.api_task, worker = local.worker_task }
}

resource "aws_ecs_task_definition" "this" {
  for_each = local.tasks

  family                   = each.value.family
  network_mode             = each.value.networkMode
  requires_compatibilities = each.value.requiresCompatibilities
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode(each.value.containerDefinitions)

  runtime_platform {
    operating_system_family = each.value.runtimePlatform.operatingSystemFamily
    cpu_architecture        = each.value.runtimePlatform.cpuArchitecture
  }

  skip_destroy = true # keep old revisions registered, so a rollback still has something to roll back to

  depends_on = [aws_cloudwatch_log_group.service]

  lifecycle {
    precondition {
      condition     = !strcontains(jsonencode(each.value), "__")
      error_message = "The ${each.key} task definition template has a __PLACEHOLDER__ that local.placeholders doesn't fill in."
    }
  }
}

# ticket-service.ticketto.local resolves to every running API task, so callers inside the VPC
# (and the API Gateway VPC link) keep finding it when ECS replaces a task. The namespace is the
# team's; it is looked up, never managed here.
data "aws_service_discovery_dns_namespace" "team" {
  name = var.service_discovery_namespace
  type = "DNS_PRIVATE"
}

resource "aws_service_discovery_service" "api" {
  name = var.name

  dns_config {
    namespace_id   = data.aws_service_discovery_dns_namespace.team.id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "A"
      ttl  = 10
    }
  }
  # No health_check_custom_config: an empty block isn't stored, so every plan would replace this
  # service. ECS still deregisters a task from DNS as soon as it stops.
}

resource "aws_ecs_service" "this" {
  for_each = { api = var.name, worker = local.worker_name }

  name            = each.value
  cluster         = data.aws_ecs_cluster.shared.arn
  task_definition = aws_ecs_task_definition.this[each.key].arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # The ALB health-checks /ready; give a new API task time to start before that counts.
  health_check_grace_period_seconds = each.key == "api" ? 90 : null

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = true # outbound only; there's no NAT in the default VPC
  }

  # Only the API listens. ECS registers each new API task's IP in the gateway's target group, so
  # replacing a task needs nothing by hand. Without this block, an apply would detach the API.
  dynamic "load_balancer" {
    for_each = each.key == "api" ? [data.aws_lb_target_group.gateway.arn] : []
    content {
      target_group_arn = load_balancer.value
      container_name   = "ticket-service" # the container in ../task-definition.json
      container_port   = 3006
    }
  }

  # A rollout whose tasks never get healthy goes back to the last good revision.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # Only the API listens; the worker makes outbound connections only.
  dynamic "service_registries" {
    for_each = each.key == "api" ? [aws_service_discovery_service.api.arn] : []
    content {
      registry_arn = service_registries.value
    }
  }

  lifecycle {
    ignore_changes = [task_definition] # owned by the deploy job after the first apply
  }
}

# ---------- GitLab CI: OIDC, no stored keys ----------

# Only pipelines on this project's main branch can assume it. The sub claim starts with the
# project ID, not the path: GitLab refuses path-based ID tokens for this project because its path
# was used by an earlier project (job failure id_token_burned_project_path). The project's
# ci_id_token_sub_claim_components setting is ["project_id", "ref_type", "ref"] to match.
resource "aws_iam_role" "deploy" {
  name = "gitlab-${var.name}-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.gitlab_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "gitlab.com:aud" = "https://gitlab.com"
          "gitlab.com:sub" = "project_id:${var.gitlab_project_id}:ref_type:branch:ref:main"
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
      {
        # Registering and reading task definitions can't be limited to a resource.
        Sid      = "TaskDefinitions"
        Effect   = "Allow"
        Action   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"]
        Resource = "*"
      },
      {
        # The one-off `alembic upgrade head` task, only in the team cluster.
        Sid       = "RunMigrationTask"
        Effect    = "Allow"
        Action    = "ecs:RunTask"
        Resource  = "${local.cluster_prefix}:task-definition/${var.name}:*"
        Condition = { ArnEquals = { "ecs:cluster" = data.aws_ecs_cluster.shared.arn } }
      },
      {
        Sid      = "WatchMigrationTask"
        Effect   = "Allow"
        Action   = "ecs:DescribeTasks"
        Resource = "${local.cluster_prefix}:task/${var.ecs_cluster_name}/*"
      },
      {
        Sid    = "RollOutServices"
        Effect = "Allow"
        Action = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = [
          "${local.cluster_prefix}:service/${var.ecs_cluster_name}/${var.name}",
          "${local.cluster_prefix}:service/${var.ecs_cluster_name}/${local.worker_name}",
        ]
      },
      {
        Sid       = "PassExecutionRole"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.execution.arn
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
    ]
  })
}
