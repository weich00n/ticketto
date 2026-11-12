# The Ticketto frontend on ECS Fargate, in the account's default VPC.
#
#   browser ──HTTPS──► CloudFront ──HTTP──► ALB ──:3000──► ECS task (Next.js)
#   GitLab main ── OIDC ──► deploy role: push to ECR, register the task definition, update the service
#
# CloudFront serves HTTPS on var.domain_name (and www.), with an ACM certificate, as well as on its
# own *.cloudfront.net address. The sign-in cookies are `secure`, and browsers drop them over plain
# HTTP. The domain must point at CloudFront, never at the ALB: the ALB takes traffic from CloudFront only.
# The pipeline owns the task definition and its image: see ../README.md.

locals {
  container_name = "frontend"
}

data "aws_caller_identity" "current" {}

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

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

# Created once by the broker stack (or by hand) for the whole account.
data "aws_iam_openid_connect_provider" "gitlab" {
  url = "https://gitlab.com"
}

data "aws_secretsmanager_secret" "cognito" {
  name = var.cognito_secret_name
}

# ---------- security groups ----------

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "Frontend load balancer: HTTP from CloudFront only"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = "${var.name}-alb" }
}

resource "aws_security_group" "service" {
  name        = var.name
  description = "Frontend ECS tasks: the app port from the load balancer only"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = var.name }
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from CloudFront"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
}

resource "aws_vpc_security_group_egress_rule" "alb_to_service" {
  security_group_id            = aws_security_group.alb.id
  description                  = "App port to the tasks"
  ip_protocol                  = "tcp"
  from_port                    = var.container_port
  to_port                      = var.container_port
  referenced_security_group_id = aws_security_group.service.id
}

resource "aws_vpc_security_group_ingress_rule" "service_from_alb" {
  security_group_id            = aws_security_group.service.id
  description                  = "App port from the load balancer"
  ip_protocol                  = "tcp"
  from_port                    = var.container_port
  to_port                      = var.container_port
  referenced_security_group_id = aws_security_group.alb.id
}

# Tasks run in public subnets with a public IP, as the other services do: they reach ECR,
# CloudWatch Logs, Secrets Manager, Cognito and API Gateway without a NAT gateway.
resource "aws_vpc_security_group_egress_rule" "service_all" {
  security_group_id = aws_security_group.service.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# ---------- image repository and logs ----------

resource "aws_ecr_repository" "frontend" {
  name                 = var.name
  image_tag_mutability = "MUTABLE" # the pipeline re-tags latest

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "frontend" {
  repository = aws_ecr_repository.frontend.name
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

resource "aws_cloudwatch_log_group" "frontend" {
  name              = "/ecs/${var.name}"
  retention_in_days = 30
}

# ---------- task execution role ----------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Used by ECS to start the containers. There is no task role: the app uses Cognito with
# the app client, not the task's AWS identity.
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "read_cognito_secret" {
  name = "read-cognito-secret"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = data.aws_secretsmanager_secret.cognito.arn
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
        Resource = aws_ecr_repository.frontend.arn
      },
      {
        # RegisterTaskDefinition can't be limited by resource.
        Effect = "Allow"
        Action = [
          "ecs:RegisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
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

# ---------- load balancer ----------

resource "aws_lb" "frontend" {
  name               = var.name
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default.ids
}

resource "aws_lb_target_group" "frontend" {
  name                 = var.name
  port                 = var.container_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = data.aws_vpc.default.id
  deregistration_delay = 30

  health_check {
    path                = var.health_check_path
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.frontend.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }
}

# ---------- ECS service ----------

# The latest revision the pipeline registered. A service can't exist before one does.
data "aws_ecs_task_definition" "frontend" {
  count           = var.create_service ? 1 : 0
  task_definition = var.name
}

resource "aws_ecs_service" "frontend" {
  count                             = var.create_service ? 1 : 0
  name                              = var.name
  cluster                           = data.aws_ecs_cluster.platform.arn
  task_definition                   = data.aws_ecs_task_definition.frontend[0].arn
  desired_count                     = var.desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 30

  # ecs-deploy.sh relies on this to detect a bad release.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.frontend.arn
    container_name   = local.container_name
    container_port   = var.container_port
  }

  # Each deploy registers a new task definition; scaling is done in ECS.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.http]
}

# ---------- CloudFront: HTTPS in front of the load balancer ----------

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

# Cookies, query strings and headers all reach the app.
data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled     = true
  comment     = "Ticketto frontend"
  price_class = "PriceClass_100"

  origin {
    origin_id   = "alb"
    domain_name = aws_lb.frontend.dns_name

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id         = "alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = true
  }

  # Built files have content hashes in their names, so they are safe to cache.
  ordered_cache_behavior {
    path_pattern           = "/_next/static/*"
    target_origin_id       = "alb"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id
    compress               = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  aliases = local.domain_names

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.frontend.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

# ---------- custom domain ----------

locals {
  domain_names = [var.domain_name, "www.${var.domain_name}"]
}

# Created by the Route 53 registrar when the domain was registered.
data "aws_route53_zone" "frontend" {
  name         = var.domain_name
  private_zone = false
}

# CloudFront only takes certificates from us-east-1, which is var.region.
resource "aws_acm_certificate" "frontend" {
  domain_name               = var.domain_name
  subject_alternative_names = ["www.${var.domain_name}"]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for o in aws_acm_certificate.frontend.domain_validation_options : o.domain_name => o
  }

  zone_id         = data.aws_route53_zone.frontend.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "frontend" {
  certificate_arn         = aws_acm_certificate.frontend.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# allow_overwrite takes over the apex A record first made by hand, which pointed at the ALB.
resource "aws_route53_record" "frontend" {
  for_each = toset(local.domain_names)

  zone_id         = data.aws_route53_zone.frontend.zone_id
  name            = each.value
  type            = "A"
  allow_overwrite = true

  alias {
    name                   = aws_cloudfront_distribution.frontend.domain_name
    zone_id                = aws_cloudfront_distribution.frontend.hosted_zone_id
    evaluate_target_health = false
  }
}
