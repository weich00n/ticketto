# One security group for all four Queue task definitions. Only queue-api listens
# (8000); the workers make outbound connections only.

resource "aws_security_group" "app" {
  name        = "queue-app-${var.env}-sg"
  description = "Queue service ECS tasks"
  vpc_id      = var.vpc_id
  tags        = { Name = "queue-app-${var.env}-sg" }
}

resource "aws_vpc_security_group_egress_rule" "app_all" {
  security_group_id = aws_security_group.app.id
  description       = "Image pulls, Secrets Manager, S3, logs, RDS, Redis, RabbitMQ"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# Buyers come through the platform API gateway, its shared VPC link and the shared
# internal ALB. The rule letting that ALB call queue-api on :8000 is on our security
# group but owned by the gateway's own OpenTofu project (frontend/deploy/gateway), so
# it is deliberately not managed here: two projects managing one rule fight over it.

# Direct access that bypasses the gateway (debugging only; normally empty).
resource "aws_vpc_security_group_ingress_rule" "api" {
  for_each          = toset(var.api_ingress_cidrs)
  security_group_id = aws_security_group.app.id
  description       = "queue-api direct (bypasses the gateway)"
  ip_protocol       = "tcp"
  from_port         = 8000
  to_port           = 8000
  cidr_ipv4         = each.value
}

# On the Redis instance's security group (redis.tf): let the tasks in.
resource "aws_vpc_security_group_ingress_rule" "redis_from_app" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Redis from Queue ECS tasks (${var.env})"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.app.id
}

# RDS: the shared instance's security group is not managed here. Its owners must
# allow TCP 3306 from aws_security_group.app (see the app_security_group_id output).
