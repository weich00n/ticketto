# Own only Event's additions to the shared HTTP API. User/Checkout routes and
# the shared stage/authorizer remain managed by their respective owners.
data "aws_apigatewayv2_api" "shared" {
  api_id = var.api_id
}

data "aws_service_discovery_dns_namespace" "shared" {
  name = var.service_discovery_namespace
  type = "DNS_PRIVATE"
}

data "aws_security_group" "broker" {
  name   = var.broker_security_group_name
  vpc_id = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "broker_from_event" {
  security_group_id            = data.aws_security_group.broker.id
  referenced_security_group_id = data.aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 5672
  to_port                      = 5672
  description                  = "AMQP from the dedicated Event Service security group"
}

# API Gateway reaches the API through the shared VPC link and internal ALB, like every other
# service. The gateway stack (frontend/deploy/gateway) owns the link, the ALB, this listener, the
# target group and the rule letting the ALB reach 3001 on the tasks. Apply that stack first.
data "aws_lb" "internal" {
  name = var.internal_alb_name
}

data "aws_lb_listener" "api" {
  load_balancer_arn = data.aws_lb.internal.arn
  port              = var.api_port
}

data "aws_lb_target_group" "gateway" {
  name = var.gateway_target_group_name
}

# ECS adds each API task here. Ticket and Notification reach the API through this name
# (event-service.ticketto.local:3001); API Gateway goes through the ALB instead.
resource "aws_service_discovery_service" "event" {
  name = var.name
  dns_config {
    namespace_id   = data.aws_service_discovery_dns_namespace.shared.id
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
  # Kept explicitly: this provider treats removing it as service replacement.
  health_check_custom_config {
    failure_threshold = 1
  }
}

locals {
  public_routes = toset([
    "GET /api/v1/events",
    "GET /api/v1/events/{event_id}",
    "GET /api/v1/events/{event_id}/image",
  ])
  organizer_routes = toset([
    "GET /api/v1/organizer/events",
    "POST /api/v1/organizer/events",
    "GET /api/v1/organizer/events/{event_id}",
    "PUT /api/v1/organizer/events/{event_id}",
    "POST /api/v1/organizer/events/{event_id}/publish",
    "POST /api/v1/organizer/events/{event_id}/cancel",
    "GET /api/v1/organizer/events/{event_id}/image",
    "PUT /api/v1/organizer/events/{event_id}/image",
  ])
  identity_mappings = {
    public = {
      "remove:header.X-User-Sub"      = "''"
      "remove:header.X-User-Username" = "''"
      "remove:header.X-User-Groups"   = "''"
      "remove:header.X-User-Id"       = "''"
    }
    organizer = {
      "overwrite:header.X-User-Sub"      = "$context.authorizer.claims.sub"
      "overwrite:header.X-User-Username" = "$context.authorizer.claims.username"
      "overwrite:header.X-User-Groups"   = "$context.authorizer.claims.groups"
      "remove:header.X-User-Id"          = "''"
    }
  }
}

resource "aws_apigatewayv2_integration" "event" {
  for_each               = local.identity_mappings
  api_id                 = data.aws_apigatewayv2_api.shared.id
  integration_type       = "HTTP_PROXY"
  integration_method     = "ANY"
  connection_type        = "VPC_LINK"
  connection_id          = var.api_vpc_link_id
  integration_uri        = data.aws_lb_listener.api.arn
  payload_format_version = "1.0"
  request_parameters     = merge(each.value, { "overwrite:path" = "$request.path" })
}

resource "aws_apigatewayv2_route" "public" {
  for_each           = local.public_routes
  api_id             = data.aws_apigatewayv2_api.shared.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.event["public"].id}"
  authorization_type = "NONE"
}

resource "aws_apigatewayv2_route" "organizer" {
  for_each           = local.organizer_routes
  api_id             = data.aws_apigatewayv2_api.shared.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.event["organizer"].id}"
  authorization_type = "JWT"
  authorizer_id      = var.jwt_authorizer_id
}
