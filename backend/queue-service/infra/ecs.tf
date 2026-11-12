# One image, four task definitions (api, admitter, consumer, migrate), three services.
#
# Releases are done by the GitLab deploy job (ci/deploy.sh), not by OpenTofu: it
# copies the latest revision of each task definition with the new image, runs the
# migration and updates the services. So:
#   - var.image is only the image of OpenTofu's own revisions; the next deploy job
#     replaces it.
#   - The services ignore task_definition changes, so `tofu apply` never rolls a
#     service back to an older image. A config change made here (CPU, memory, env
#     file...) becomes a new revision and goes live with the next deploy job.

# The services run in the team's shared cluster, which no stack here owns.
data "aws_ecs_cluster" "shared" {
  cluster_name = var.ecs_cluster_name
}

# The cluster Queue used to run in, now empty. Kept so that setting ecs_cluster_name back to
# it rolls the move back; delete it once the shared cluster has proved itself.
resource "aws_ecs_cluster" "this" {
  name = local.name

  # Off: Container Insights is billed (custom metrics + log ingestion). The free
  # default AWS/ECS metrics (CPU and memory per service) are enough for now.
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

locals {
  # Task definition families are account-wide, so they carry the environment.
  family = { for name in ["api", "admitter", "consumer", "migrate"] : name => "queue-${name}-${var.env}" }

  container_common = {
    image                 = var.image
    essential             = true
    stopTimeout           = 30
    repositoryCredentials = { credentialsParameter = aws_secretsmanager_secret.gitlab_registry.arn }
  }

  logs = {
    for name in keys(local.family) : name => {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this[name].name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = name
      }
    }
  }

  # ECS reads one JSON key of a secret with "<arn>:<json-key>::" (version stage and
  # id left empty = current version); the plain ARN gives the whole secret string.
  rabbitmq_url = {
    name = "RABBITMQ_URL"
    valueFrom = (
      var.rabbitmq_url_secret_json_key == ""
      ? var.rabbitmq_url_secret_arn
      : "${var.rabbitmq_url_secret_arn}:${var.rabbitmq_url_secret_json_key}::"
    )
  }

  app_env     = [{ type = "s3", value = local.app_env_arn }]
  migrate_env = [{ type = "s3", value = local.migrate_env_arn }]

  # The code reads the signing key from a file (JWT_PRIVATE_KEY_PATH, set in
  # app.env), so write the secret there first, then start uvicorn as usual.
  api_command = [
    "sh", "-c",
    "printf '%s\\n' \"$JWT_PRIVATE_KEY\" > /tmp/jwt_private.pem && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000",
  ]

  containers = {
    api = merge(local.container_common, {
      name             = "api"
      command          = local.api_command
      portMappings     = [{ containerPort = 8000, protocol = "tcp" }]
      environmentFiles = local.app_env
      secrets = [
        { name = "JWT_PRIVATE_KEY", valueFrom = aws_secretsmanager_secret.jwt_private_key.arn },
        local.rabbitmq_url, # not used by the API, but the settings require it
      ]
      logConfiguration = local.logs.api
    })
    admitter = merge(local.container_common, {
      name             = "admitter"
      command          = ["python", "-m", "workers.admitter"]
      environmentFiles = local.app_env
      secrets          = [local.rabbitmq_url] # required by the settings, not used
      logConfiguration = local.logs.admitter
      healthCheck = {
        command     = ["CMD", "python", "-m", "workers.healthcheck", "admitter"]
        interval    = 10
        timeout     = 10 # a new Python process on 0.25 vCPU; 5 s was too tight
        retries     = 3
        startPeriod = 60
      }
    })
    consumer = merge(local.container_common, {
      name             = "consumer"
      command          = ["python", "-m", "workers.consumer"]
      environmentFiles = local.app_env
      secrets          = [local.rabbitmq_url]
      logConfiguration = local.logs.consumer
      healthCheck = {
        command     = ["CMD", "python", "-m", "workers.healthcheck", "consumer"]
        interval    = 15
        timeout     = 10
        retries     = 3
        startPeriod = 60
      }
    })
    migrate = merge(local.container_common, {
      name             = "migrate"
      command          = ["alembic", "upgrade", "head"]
      environmentFiles = local.migrate_env
      logConfiguration = local.logs.migrate
    })
  }

  size = {
    api      = { cpu = 512, memory = 1024 }
    admitter = { cpu = 256, memory = 512 }
    consumer = { cpu = 256, memory = 512 }
    migrate  = { cpu = 256, memory = 512 }
  }
}

resource "aws_ecs_task_definition" "this" {
  for_each = local.containers

  family                   = local.family[each.key]
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.size[each.key].cpu
  memory                   = local.size[each.key].memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions    = jsonencode([each.value])

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # the GitLab release job builds amd64
  }
}

# --- Services --------------------------------------------------------------------------

locals {
  services = var.create_services ? {
    # name = [desired count, minimum healthy %, maximum %]
    consumer = [var.consumer_desired_count, 100, 200]
    # Exactly one admitter, ever: stop the old task before starting the new one.
    admitter = [1, 0, 100]
    api      = [var.api_min_count, 100, 200] # then scaled on CPU (autoscaling.tf)
  } : {}
}

resource "aws_ecs_service" "this" {
  for_each = local.services

  name                   = "queue-${each.key}"
  cluster                = data.aws_ecs_cluster.shared.arn
  task_definition        = aws_ecs_task_definition.this[each.key].arn
  launch_type            = "FARGATE"
  desired_count          = each.value[0]
  enable_execute_command = true

  deployment_minimum_healthy_percent = each.value[1]
  deployment_maximum_percent         = each.value[2]

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = var.assign_public_ip
  }

  # queue-api only: the shared internal ALB's target group (the API gateway's way in).
  health_check_grace_period_seconds = each.key == "api" && var.api_target_group_arn != "" ? 90 : null

  dynamic "load_balancer" {
    for_each = each.key == "api" && var.api_target_group_arn != "" ? [var.api_target_group_arn] : []
    content {
      target_group_arn = load_balancer.value
      container_name   = "api"
      container_port   = 8000
    }
  }

  lifecycle {
    ignore_changes = [task_definition] # owned by the GitLab deploy job after creation
  }
}
