# reservation-service on ECS Fargate, in the team's TicketPlatform cluster and the default VPC.
#
#   ECS service (Fargate, public IP for outbound only) ──► RDS db-ticketing   (3306)
#                                                      ──► Redis EC2           (6379)
#                                                      ──► RabbitMQ rabibi     (5672, the broker team's rule)
#   GitLab main ── OIDC ──► deploy role: push to ECR, register task definitions, update the service
#
# Looked up, never managed here: the VPC, the cluster, RDS, the Redis instance's role, the broker
# and its secret, the GitLab OIDC provider. Everything else belongs to this service; the Redis
# instance itself is in redis.tf.
# Setup and first deployment: ../README.md

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

# ---------- what already exists ----------

data "aws_vpc" "default" {
  default = true
}

# The default VPC's subnets are all public and it has no NAT, so tasks get a public IP to reach
# ECR, Secrets Manager and S3. Nothing comes in that way: see the security group.
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_ecs_cluster" "shared" {
  cluster_name = var.ecs_cluster_name
}

# The public routes reach the tasks through the team's shared API Gateway stack (the frontend
# repository's deploy/gateway): API Gateway ── VPC link ──► internal ALB :8081 ──► this target
# group. That stack owns the routes, the ALB, the target group and the ALB-to-task ingress rule;
# this one only attaches the ECS service to the target group.
data "aws_lb_target_group" "gateway" {
  name = var.gateway_target_group_name
}

data "aws_db_instance" "shared" {
  db_instance_identifier = var.rds_instance_id
}

data "aws_secretsmanager_secret" "broker" {
  name = var.broker_secret_name
}

data "aws_iam_openid_connect_provider" "gitlab" {
  url = "https://gitlab.com"
}

# ---------- network ----------

resource "aws_security_group" "service" {
  name        = var.name
  description = "reservation-service tasks: 8081 from allowed callers only"
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
  description                  = "HTTP 8081 from ${each.value}"
  ip_protocol                  = "tcp"
  from_port                    = 8081
  to_port                      = 8081
  referenced_security_group_id = each.value
}

# Added to groups this stack doesn't own. Each is a separate rule, so it leaves the groups'
# other rules alone, and `tofu destroy` removes only these.
resource "aws_vpc_security_group_ingress_rule" "rds_from_service" {
  for_each = toset(data.aws_db_instance.shared.vpc_security_groups)

  security_group_id            = each.value
  description                  = "MySQL from ${var.name}"
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
  referenced_security_group_id = aws_security_group.service.id
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_service" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Redis from ${var.name}"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
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
    username = "reservation_service"
    password = random_password.db.result
  })
}

resource "aws_secretsmanager_secret" "redis" {
  name                    = "${var.name}/redis"
  description             = "requirepass of ${var.redis_instance_name}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "redis" {
  count = var.redis_password == "" ? 0 : 1

  secret_id     = aws_secretsmanager_secret.redis.id
  secret_string = jsonencode({ password = var.redis_password })
}

resource "aws_secretsmanager_secret" "admission_key" {
  name                    = "${var.name}/admission-public-key"
  description             = "Queue's admission-token public key (PEM). Not sensitive; kept here so the multi-line PEM stays intact."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "admission_key" {
  count = var.admission_public_key_file == "" ? 0 : 1

  secret_id     = aws_secretsmanager_secret.admission_key.id
  secret_string = file(var.admission_public_key_file)
}

# The Redis instance reads its --requirepass from the redis secret at launch
# (../redis/ec2-user-data.sh). Its role isn't ours, so this adds one inline policy to it.
data "aws_iam_instance_profile" "redis" {
  name = aws_instance.redis.iam_instance_profile
}

resource "aws_iam_role_policy" "redis_reads_password" {
  name = "read-redis-password"
  role = data.aws_iam_instance_profile.redis.role_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.redis.arn
    }]
  })
}

# ---------- environment file: addresses only, read by ECS when a task starts ----------

resource "aws_s3_bucket" "config" {
  bucket        = "${var.name}-config-${local.account_id}"
  force_destroy = var.allow_destroy_data
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket                  = aws_s3_bucket.config.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "env" {
  bucket       = aws_s3_bucket.config.id
  key          = "production.env"
  content_type = "text/plain"
  content      = <<-EOT
    # Written by deploy/terraform. Edit there and re-apply, then re-run the deploy job.
    DB_HOST=${data.aws_db_instance.shared.address}
    DB_PORT=${data.aws_db_instance.shared.port}
    DB_NAME=${var.db_name}
    REDIS_HOST=${aws_instance.redis.private_ip}
    REDIS_PORT=6379
  EOT
}

# ---------- execution role: pull the image, read the secrets and the env file ----------

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
  name = "read-secrets-and-env-file"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = [
          aws_secretsmanager_secret.db.arn,
          aws_secretsmanager_secret.redis.arn,
          aws_secretsmanager_secret.admission_key.arn,
          data.aws_secretsmanager_secret.broker.arn,
        ]
      },
      { Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.config.arn}/${aws_s3_object.env.key}" },
      { Effect = "Allow", Action = "s3:GetBucketLocation", Resource = aws_s3_bucket.config.arn },
    ]
  })
}

# ---------- task definition and service ----------

# The same template the deploy job fills in, so the two can't drift. tofu registers only the
# first revision; every deploy after that registers its own and points the service at it.
locals {
  task_definition = jsondecode(
    replace(replace(replace(replace(replace(replace(replace(replace(
      file("${path.module}/../task-definition.json"),
      "__IMAGE__", "${aws_ecr_repository.service.repository_url}:latest"),
      "__AWS_REGION__", var.region),
      "__ECS_EXECUTION_ROLE_ARN__", aws_iam_role.execution.arn),
      "__ENV_FILE_S3_ARN__", "${aws_s3_bucket.config.arn}/${aws_s3_object.env.key}"),
      "__DB_SECRET_ARN__", aws_secretsmanager_secret.db.arn),
      "__ADMISSION_KEY_SECRET_ARN__", aws_secretsmanager_secret.admission_key.arn),
      "__RABBITMQ_SECRET_ARN__", data.aws_secretsmanager_secret.broker.arn),
    "__REDIS_SECRET_ARN__", aws_secretsmanager_secret.redis.arn)
  )
}

resource "aws_ecs_task_definition" "service" {
  family                   = local.task_definition.family
  network_mode             = local.task_definition.networkMode
  requires_compatibilities = local.task_definition.requiresCompatibilities
  cpu                      = local.task_definition.cpu
  memory                   = local.task_definition.memory
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions    = jsonencode(local.task_definition.containerDefinitions)

  runtime_platform {
    operating_system_family = local.task_definition.runtimePlatform.operatingSystemFamily
    cpu_architecture        = local.task_definition.runtimePlatform.cpuArchitecture
  }

  skip_destroy = true # keep old revisions registered, so a rollback still has something to roll back to

  depends_on = [aws_cloudwatch_log_group.service]
}

resource "aws_ecs_service" "service" {
  name            = var.name
  cluster         = data.aws_ecs_cluster.shared.arn
  task_definition = aws_ecs_task_definition.service.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = true # outbound only; there's no NAT in the default VPC
  }

  # A rollout whose tasks never get healthy goes back to the last good revision.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # reservation-service.ticketto.local resolves to every running task (autoscaling.tf).
  service_registries {
    registry_arn = aws_service_discovery_service.service.arn
  }

  # The shared gateway's ALB sends the public routes here. Without this block an apply would
  # detach the service from it, and every public route would answer 503.
  load_balancer {
    target_group_arn = data.aws_lb_target_group.gateway.arn
    container_name   = var.name
    container_port   = 8081
  }

  # Spring takes a while to start; the ALB's /health checks don't count against a task until then.
  health_check_grace_period_seconds = 90

  lifecycle {
    # task_definition: owned by the deploy job after the first apply.
    # desired_count: owned by the autoscaler (autoscaling.tf).
    ignore_changes = [task_definition, desired_count]
  }
}

# ---------- GitLab CI: OIDC, no stored keys ----------

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
  name = "push-and-deploy"
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
        # Registering a task definition can't be limited to a resource.
        Sid      = "RegisterTaskDefinition"
        Effect   = "Allow"
        Action   = ["ecs:RegisterTaskDefinition", "ecs:DescribeTaskDefinition"]
        Resource = "*"
      },
      {
        Sid      = "RollOutService"
        Effect   = "Allow"
        Action   = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = "arn:aws:ecs:${var.region}:${local.account_id}:service/${var.ecs_cluster_name}/${var.name}"
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

# The FIS experiments moved to the chaos-engineering repository (C2, C7). Keep the old
# experiment logs: they hold the 2026-10-25 "before" run of C2.
removed {
  from = aws_cloudwatch_log_group.fis
  lifecycle {
    destroy = false
  }
}
