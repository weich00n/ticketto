# Shared connectivity only. ECS services and application task definitions remain
# owned by their service repositories. See attach_services.py and README.md.
data "aws_vpc" "shared" {
  default = true
}
data "aws_subnets" "shared" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.shared.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}
data "aws_apigatewayv2_api" "shared" {
  api_id = "x0vxfeenrl"
}
# Created by notification-service's stack, so apply that part of it first: see its ecs.tf.
data "aws_security_group" "notification_api" {
  vpc_id = data.aws_vpc.shared.id
  name   = "notification-service-api"
}
data "aws_security_group" "user_api" {
  vpc_id = data.aws_vpc.shared.id
  name   = "user-service"
}
data "aws_security_group" "checkout_api" {
  vpc_id = data.aws_vpc.shared.id
  name   = "checkout-service"
}
data "aws_security_group" "event_api" {
  vpc_id = data.aws_vpc.shared.id
  name   = "event-service"
}

locals {
  services = {
    queue = {
      port    = 8000, security_group = "sg-07862c5e84a60aec9"
      cluster = "queue-service-staging", service = "queue-api", container = "api"
      health  = "/health/ready"
    }
    reservation = {
      port    = 8081, security_group = "sg-03904eefda23fcbe3"
      cluster = "TicketPlatform", service = "reservation-service", container = "reservation-service"
      health  = "/health"
    }
    ticket = {
      port    = 3006, security_group = "sg-0b2386156b128c44e"
      cluster = "TicketPlatform", service = "ticket-service", container = "ticket-service"
      health  = "/ready"
    }
    # Only the listener, target group and security group rules: notification-service's own stack
    # keeps its routes and integration (deploy/terraform/api.tf).
    notification = {
      port    = 8082, security_group = data.aws_security_group.notification_api.id
      cluster = "TicketPlatform", service = "notification-service", container = "notification-service"
      health  = "/health"
    }
    # Same split as notification: user-service's own stack keeps its routes and integration
    # (deploy/terraform/main.tf). Its tasks listen on 8000, which is Queue's listener here.
    user = {
      port    = 8000, listener_port = 8083, security_group = data.aws_security_group.user_api.id
      cluster = "TicketPlatform", service = "user-service", container = "user-service"
      health  = "/health"
    }
    # Same split again for Event and Checkout: their own stacks keep their routes and integrations
    # (deploy/terraform/gateway.tf).
    event = {
      port    = 3001, security_group = data.aws_security_group.event_api.id
      cluster = "TicketPlatform", service = "event-service", container = "event-service"
      health  = "/health"
    }
    checkout = {
      port    = 3002, security_group = data.aws_security_group.checkout_api.id
      cluster = "TicketPlatform", service = "checkout-service", container = "checkout-service"
      health  = "/health"
    }
  }
  # Listeners need a port of their own, so listener_port is for a service whose port is taken.
  listener_ports = { for name, cfg in local.services : name => try(cfg.listener_port, cfg.port) }
  public_routes = {
    "GET /api/v1/events/{event_id}/queue"        = "queue"
    "GET /api/v1/events/{event_id}/availability" = "reservation"
  }
  protected_routes = {
    "POST /api/v1/events/{event_id}/queue/entries" = "queue"
    "GET /api/v1/events/{event_id}/queue/me"       = "queue"
    "DELETE /api/v1/events/{event_id}/queue/me"    = "queue"
    "POST /api/v1/reservations"                    = "reservation"
    "GET /api/v1/reservations/{hold_id}"           = "reservation"
    "DELETE /api/v1/reservations/{hold_id}"        = "reservation"
    "GET /api/v1/tickets"                          = "ticket"
    "GET /api/v1/tickets/{ticket_id}"              = "ticket"
    "GET /api/v1/tickets/{ticket_id}/qr"           = "ticket"
    "POST /api/v1/tickets/check-in"                = "ticket"
  }
  integrations = {
    queue_public       = { service = "queue", authenticated = false }
    queue_buyer        = { service = "queue", authenticated = true }
    reservation_public = { service = "reservation", authenticated = false }
    reservation_buyer  = { service = "reservation", authenticated = true }
    ticket_buyer       = { service = "ticket", authenticated = true }
  }
  identity_headers = {
    "overwrite:header.X-User-Sub"      = "$context.authorizer.claims.sub"
    "overwrite:header.X-User-Username" = "$context.authorizer.claims.username"
    "overwrite:header.X-User-Groups"   = "$context.authorizer.claims.groups"
    "remove:header.X-User-Id"          = "''"
  }
  public_headers = {
    "remove:header.X-User-Sub"      = "''"
    "remove:header.X-User-Username" = "''"
    "remove:header.X-User-Groups"   = "''"
    "remove:header.X-User-Id"       = "''"
  }
}

resource "aws_security_group" "link" {
  name        = "ticketto-shared-vpc-link"
  description = "API Gateway to the internal service load balancer"
  vpc_id      = data.aws_vpc.shared.id
}
resource "aws_security_group" "alb" {
  name        = "ticketto-internal-api-alb"
  description = "Internal API listeners reachable only through the gateway VPC link"
  vpc_id      = data.aws_vpc.shared.id
}
resource "aws_vpc_security_group_egress_rule" "link" {
  for_each                     = local.services
  security_group_id            = aws_security_group.link.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = local.listener_ports[each.key]
  to_port                      = local.listener_ports[each.key]
}
resource "aws_vpc_security_group_ingress_rule" "alb" {
  for_each                     = local.services
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.link.id
  ip_protocol                  = "tcp"
  from_port                    = local.listener_ports[each.key]
  to_port                      = local.listener_ports[each.key]
}
resource "aws_vpc_security_group_egress_rule" "alb" {
  for_each                     = local.services
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = each.value.security_group
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}
resource "aws_vpc_security_group_ingress_rule" "service" {
  for_each                     = local.services
  security_group_id            = each.value.security_group
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  description                  = "HTTP from the shared internal API load balancer"
}

resource "aws_lb" "api" {
  name               = "ticketto-internal-api"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = sort(data.aws_subnets.shared.ids)
}
resource "aws_lb_target_group" "service" {
  for_each             = local.services
  name                 = "ticketto-${each.key}-api"
  port                 = each.value.port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = data.aws_vpc.shared.id
  deregistration_delay = 30
  health_check {
    path                = each.value.health
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}
resource "aws_lb_listener" "service" {
  for_each          = local.services
  load_balancer_arn = aws_lb.api.arn
  port              = local.listener_ports[each.key]
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.service[each.key].arn
  }
}
resource "aws_apigatewayv2_vpc_link" "shared" {
  name               = "ticketto-shared-services"
  security_group_ids = [aws_security_group.link.id]
  # 1a, 1b and 1c: not every default-VPC AZ supports API Gateway.
  subnet_ids = [
    "subnet-09dd1849716094883",
    "subnet-0db093130a81f0686",
    "subnet-0c5c0553070ae8457",
  ]
}
resource "aws_apigatewayv2_integration" "service" {
  for_each               = local.integrations
  api_id                 = data.aws_apigatewayv2_api.shared.id
  integration_type       = "HTTP_PROXY"
  integration_method     = "ANY"
  connection_type        = "VPC_LINK"
  connection_id          = aws_apigatewayv2_vpc_link.shared.id
  integration_uri        = aws_lb_listener.service[each.value.service].arn
  payload_format_version = "1.0"
  request_parameters = merge(
    each.value.authenticated ? local.identity_headers : local.public_headers,
    { "overwrite:path" = "$request.path" }
  )
}
resource "aws_apigatewayv2_route" "public" {
  for_each           = local.public_routes
  api_id             = data.aws_apigatewayv2_api.shared.id
  route_key          = each.key
  target             = "integrations/${aws_apigatewayv2_integration.service["${each.value}_public"].id}"
  authorization_type = "NONE"
}
resource "aws_apigatewayv2_route" "protected" {
  for_each           = local.protected_routes
  api_id             = data.aws_apigatewayv2_api.shared.id
  route_key          = each.key
  target             = "integrations/${aws_apigatewayv2_integration.service["${each.value}_buyer"].id}"
  authorization_type = "JWT"
  authorizer_id      = "9ky1ji"
}

output "service_attachments" {
  value = { for name, cfg in local.services : name => merge(cfg, {
    target_group_arn = aws_lb_target_group.service[name].arn
  }) }
}
output "api_gateway_url" {
  value = data.aws_apigatewayv2_api.shared.api_endpoint
}
