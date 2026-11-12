# notification-service: two Fargate services in the shared cluster, in the account's default VPC.
# ecs.tf has the services, api.tf the gateway routes, email_tracking.tf the SES delivery events.
# This file has what they share.
#
#   worker ──► RabbitMQ rabibi :5672          basic_get on notification-service.* (its own user)
#          ──► user-service.ticketto.local    who to email (internal API, :8001)
#          ──► RDS db-ticketing :3306         database db_notification
#          ──► SES API (public, through the task's public IP)
#   API    ──► RDS db-ticketing :3306
#   GitLab main ── OIDC ──► deploy role: push the image, run the migration, update both services
#
# This code owns the infrastructure. The pipeline owns which image the services run, so
# `tofu apply` never changes the running code. First deploy and day-to-day: ../README.md

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  db_name    = "db_notification"
  db_user    = "notification_service"
  email_from = "${var.email_from_name} <${var.ses_sender}>"
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

# The worker's. Nothing connects in to it; it only calls out. The description still says Lambda
# because changing one replaces the group, and rabbitmq-service's tfvars hard-code its ID.
resource "aws_security_group" "worker" {
  name        = var.name
  description = "${var.name} Lambda: outbound only"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = var.name }
}

resource "aws_vpc_security_group_egress_rule" "worker_all" {
  security_group_id = aws_security_group.worker.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# The broker lets this group in on 5672 through rabbitmq-service's amqp_allowed_sg_ids
# (its terraform.tfvars), the same as every other service: add `worker_security_group_id`.

# 8001 serves only /internal/v1. Its port 8000 has the routes that trust X-User-* headers, so
# this group must never be let in there.
resource "aws_vpc_security_group_ingress_rule" "user_service_from_worker" {
  for_each = toset([for port in var.user_service_ports : tostring(port)])

  security_group_id            = data.aws_security_group.user_service.id
  description                  = "Internal API from ${var.name}"
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
  referenced_security_group_id = aws_security_group.worker.id
}

data "aws_security_group" "user_service" {
  vpc_id = data.aws_vpc.default.id
  name   = var.user_service_security_group_name
}

# ---------- the shared RDS instance: our own database in it ----------

data "aws_db_instance" "shared" {
  db_instance_identifier = var.rds_instance_identifier
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_worker" {
  for_each = toset(data.aws_db_instance.shared.vpc_security_groups)

  security_group_id            = each.value
  description                  = "MySQL from ${var.name}"
  ip_protocol                  = "tcp"
  from_port                    = data.aws_db_instance.shared.port
  to_port                      = data.aws_db_instance.shared.port
  referenced_security_group_id = aws_security_group.worker.id
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

# ---------- image repository ----------

resource "aws_ecr_repository" "service" {
  name                 = var.name
  image_tag_mutability = "MUTABLE" # :latest moves with every build; :<sha> tags never change
  force_delete         = var.allow_destroy_data
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
        Resource  = "arn:aws:ecs:${var.region}:${local.account_id}:task-definition/${var.name}:*"
        Condition = { ArnEquals = { "ecs:cluster" = data.aws_ecs_cluster.this.arn } }
      },
      {
        Sid      = "WatchMigrationTask"
        Effect   = "Allow"
        Action   = "ecs:DescribeTasks"
        Resource = "arn:aws:ecs:${var.region}:${local.account_id}:task/${var.cluster_name}/*"
      },
      {
        Sid    = "UpdateServices"
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
        Resource  = [aws_iam_role.execution.arn, aws_iam_role.worker_task.arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
    ]
  })
}
