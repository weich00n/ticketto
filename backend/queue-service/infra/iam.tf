# --- Task execution role: ECS pulls the image, reads config and secrets, writes logs ---

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "EnvFiles"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.config.arn}/${local.config_path}/*"]
  }
  statement {
    sid       = "EnvFilesBucket"
    actions   = ["s3:GetBucketLocation"]
    resources = [aws_s3_bucket.config.arn]
  }
  statement {
    sid     = "Secrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.jwt_private_key.arn,
      aws_secretsmanager_secret.gitlab_registry.arn,
      var.rabbitmq_url_secret_arn, # owned by the broker team
    ]
  }
  dynamic "statement" {
    for_each = var.rabbitmq_secret_kms_key_arn == "" ? [] : [var.rabbitmq_secret_kms_key_arn]
    content {
      sid       = "RabbitMqSecretKey"
      actions   = ["kms:Decrypt"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role_policy" "execution" {
  name   = "config-and-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# --- Task role: what the running app may do. It calls no AWS APIs; this only
# allows ECS Exec (a shell in a task, e.g. for `python -m workers.dlq`). ---------------

resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "ecs-exec"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
