# The Ticketto broker on its own EC2 instance, in the account's default VPC.
#
#   EC2 (AL2023 arm64) ── docker: rabbitmq (this repo's image, from ECR)
#     /data on a separate EBS volume: replacing the instance keeps every queue and message
#     reached with SSM Session Manager: no SSH, no key pair. The only inbound rules are AMQP
#     from allowed sources and, if management_allowed_cidrs is set, the management UI
#   GitLab main ── OIDC ──► deploy role: push to ECR, run deploy/run-rabbitmq.sh over SSM
#
# Setup and day-to-day use: ../README.md

data "aws_caller_identity" "current" {}

# ---------- network: the default VPC ----------

data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "broker" {
  vpc_id            = data.aws_vpc.default.id
  availability_zone = var.availability_zone
  default_for_az    = true
}

resource "aws_security_group" "broker" {
  name        = var.name
  description = "RabbitMQ broker: AMQP from allowed sources only. Admin access is via SSM, not SSH."
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = var.name }
}

# Outbound to ECR, SSM and Secrets Manager. A default VPC has no NAT, so this goes out
# through the instance's public IP. Nothing comes in that way unless management_allowed_cidrs
# opens the management UI.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.broker.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "amqp_from_sg" {
  for_each = toset(var.amqp_allowed_sg_ids)

  security_group_id            = aws_security_group.broker.id
  description                  = "AMQP from ${each.value}"
  ip_protocol                  = "tcp"
  from_port                    = 5672
  to_port                      = 5672
  referenced_security_group_id = each.value
}

resource "aws_vpc_security_group_ingress_rule" "amqp_from_cidr" {
  for_each = toset(var.amqp_allowed_cidrs)

  security_group_id = aws_security_group.broker.id
  description       = "AMQP from ${each.value}"
  ip_protocol       = "tcp"
  from_port         = 5672
  to_port           = 5672
  cidr_ipv4         = each.value
}

# Off by default: the UI is normally reached through SSM port forwarding.
resource "aws_vpc_security_group_ingress_rule" "management_from_cidr" {
  for_each = toset(var.management_allowed_cidrs)

  security_group_id = aws_security_group.broker.id
  description       = "Management UI from ${each.value}"
  ip_protocol       = "tcp"
  from_port         = 15672
  to_port           = 15672
  cidr_ipv4         = each.value
}

# ---------- image repository ----------

resource "aws_ecr_repository" "broker" {
  name                 = var.ecr_repository_name
  image_tag_mutability = "MUTABLE" # :latest moves with every publish; :<sha> tags never change
  force_delete         = var.allow_destroy_data

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "broker" {
  repository = aws_ecr_repository.broker.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

# ---------- the admin user's credentials ----------

resource "random_password" "broker" {
  length  = 32
  special = false # it goes into an AMQP URL unescaped
}

resource "aws_secretsmanager_secret" "broker" {
  name                    = "${var.name}/rabbitmq"
  description             = "RabbitMQ admin user for the ${var.name} broker. Services read the url key."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "broker" {
  secret_id = aws_secretsmanager_secret.broker.id
  secret_string = jsonencode({
    username = "ticketto"
    password = random_password.broker.result
    host     = aws_instance.broker.private_ip
    port     = 5672
    url      = "amqp://ticketto:${random_password.broker.result}@${aws_instance.broker.private_ip}:5672/%2F"
  })
}

# ---------- one least-privilege broker user per service ----------
# The services come from topology/messages.yaml (every publisher and consumer), the same list
# the image creates users for (topology/service-users.tsv). Each service gets its own secret
# with a ready-made url, and reads only that one. run-rabbitmq.sh finds them by name prefix.

locals {
  catalogue = yamldecode(file("${path.module}/../../topology/messages.yaml")).messages
  services = toset(concat(
    [for m in values(local.catalogue) : m.publisher],
    flatten([for m in values(local.catalogue) : m.consumers]),
  ))
}

resource "random_password" "service" {
  for_each = local.services

  length  = 32
  special = false # it goes into an AMQP URL unescaped
}

resource "aws_secretsmanager_secret" "service" {
  for_each = local.services

  name                    = "${var.name}/service/${each.key}"
  description             = "${each.key}'s own RabbitMQ user on the ${var.name} broker. Inject the url key as RABBITMQ_URL."
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "service" {
  for_each = local.services

  secret_id = aws_secretsmanager_secret.service[each.key].id
  secret_string = jsonencode({
    username = each.key
    password = random_password.service[each.key].result
    host     = aws_instance.broker.private_ip
    port     = 5672
    url      = "amqp://${each.key}:${random_password.service[each.key].result}@${aws_instance.broker.private_ip}:5672/%2F"
  })
}

# ---------- instance role: SSM, the broker's secrets, pulling its own image ----------

resource "aws_iam_role" "broker" {
  name = var.name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "broker_ssm" {
  role       = aws_iam_role.broker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "broker" {
  name = "read-secrets-and-pull-image"
  role = aws_iam_role.broker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = concat([aws_secretsmanager_secret.broker.arn], [for s in aws_secretsmanager_secret.service : s.arn])
      },
      # run-rabbitmq.sh finds the service secrets by name prefix. Listing shows names, not values.
      { Effect = "Allow", Action = "secretsmanager:ListSecrets", Resource = "*" },
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = aws_ecr_repository.broker.arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "broker" {
  name = var.name
  role = aws_iam_role.broker.name
}

# ---------- instance and data volume ----------

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_ebs_volume" "data" {
  availability_zone = var.availability_zone
  size              = var.data_volume_size_gib
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "${var.name}-data" }

  # Holds every queue and message. To really delete it, change this to false first.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_instance" "broker" {
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = var.instance_type
  subnet_id                   = data.aws_subnet.broker.id
  vpc_security_group_ids      = [aws_security_group.broker.id]
  iam_instance_profile        = aws_iam_instance_profile.broker.name
  associate_public_ip_address = true # outbound only: see the security group

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_type = "gp3"
    encrypted   = true
  }

  # The password is NOT in the user data: run-rabbitmq.sh reads it from Secrets Manager.
  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    boot_script_b64  = filebase64("${path.module}/../boot-rabbitmq.sh")
    run_script_b64   = filebase64("${path.module}/../run-rabbitmq.sh")
    region           = var.region
    data_volume_id   = aws_ebs_volume.data.id
    ecr_repository   = aws_ecr_repository.broker.repository_url
    broker_secret_id = aws_secretsmanager_secret.broker.name
  })

  # Neither a newer AL2023 image nor an edited script replaces the broker on its own:
  #  - run-rabbitmq.sh is re-installed by every deploy-broker job anyway;
  #  - to roll out a changed boot script or a new AMI, replace it on purpose (/data survives):
  #      tofu apply -replace=aws_instance.broker
  lifecycle {
    ignore_changes = [ami, user_data]
  }

  tags = { Name = var.name }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.broker.id
}

# ---------- GitLab CI: OIDC, no stored keys ----------

resource "aws_iam_openid_connect_provider" "gitlab" {
  count = var.create_gitlab_oidc_provider ? 1 : 0

  url            = "https://gitlab.com"
  client_id_list = ["https://gitlab.com"]
}

data "aws_iam_openid_connect_provider" "gitlab" {
  count = var.create_gitlab_oidc_provider ? 0 : 1

  url = "https://gitlab.com"
}

locals {
  gitlab_oidc_provider_arn = (var.create_gitlab_oidc_provider
    ? aws_iam_openid_connect_provider.gitlab[0].arn
  : data.aws_iam_openid_connect_provider.gitlab[0].arn)
}

# Only pipelines on this project's main branch can assume it.
resource "aws_iam_role" "deploy" {
  name = "${var.name}-gitlab-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.gitlab_oidc_provider_arn }
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
  name = "publish-and-deploy"
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
        Resource = aws_ecr_repository.broker.arn
      },
      {
        Sid    = "RunDeployScriptOnTheBroker"
        Effect = "Allow"
        Action = "ssm:SendCommand"
        Resource = [
          aws_instance.broker.arn,
          "arn:aws:ssm:${var.region}::document/AWS-RunShellScript",
        ]
      },
      { Sid = "ReadCommandResult", Effect = "Allow", Action = "ssm:GetCommandInvocation", Resource = "*" },
    ]
  })
}
