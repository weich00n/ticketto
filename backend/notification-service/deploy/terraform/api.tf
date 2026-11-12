# The notifications API: a signed-in user's in-app notifications (the frontend's bell).
#
#   Browser → Next.js proxy → API Gateway (Cognito JWT check, sets X-User-Sub)
#           → shared VPC link → shared internal ALB :8082 → ECS service notification-service
#           → db_notification.notifications, filtered by that sub (app/api.py)
#
# API Gateway is the only way in, and only through these routes. The shared HTTP API, its
# $default stage and the Cognito authorizer belong to the team and are read, not managed (the
# same pattern as checkout-service's gateway.tf).
#
# Throttling for these routes belongs on the shared $default stage, which is managed in
# frontend/deploy/gateway (see PLAN.md); adding route_settings from here would be undone by
# that stack's next apply.

locals {
  notification_routes = [
    "GET /api/v1/notifications",
    "GET /api/v1/notifications/unread-count",
    "POST /api/v1/notifications/read-all",
    "POST /api/v1/notifications/{notification_id}/read",
  ]
}

# ---------- API Gateway: routes on the shared HTTP API ----------

data "aws_apigatewayv2_api" "shared" {
  api_id = var.api_id
}

# The gateway stack (frontend/deploy/gateway) owns this listener and the shared VPC link.
data "aws_lb" "internal" {
  name = var.internal_alb_name
}

data "aws_lb_listener" "api" {
  load_balancer_arn = data.aws_lb.internal.arn
  port              = var.api_port
}

# `overwrite:` replaces any X-User-Sub the client sent, so callers can't fake their identity. The
# app reads only this header. `overwrite:path` stops the stage prefix reaching the app.
resource "aws_apigatewayv2_integration" "notifications_ecs" {
  api_id                 = data.aws_apigatewayv2_api.shared.id
  integration_type       = "HTTP_PROXY"
  integration_method     = "ANY"
  connection_type        = "VPC_LINK"
  connection_id          = var.api_vpc_link_id
  integration_uri        = data.aws_lb_listener.api.arn
  payload_format_version = "1.0"
  timeout_milliseconds   = var.api_timeout_seconds * 1000

  request_parameters = {
    "overwrite:header.X-User-Sub"   = "$context.authorizer.claims.sub"
    "overwrite:path"                = "$request.path"
    "remove:header.X-User-Username" = "''"
    "remove:header.X-User-Groups"   = "''"
    "remove:header.X-User-Id"       = "''"
  }
}

resource "aws_apigatewayv2_route" "notifications" {
  for_each = toset(local.notification_routes)

  api_id             = data.aws_apigatewayv2_api.shared.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.notifications_ecs.id}"
  authorization_type = "JWT"
  authorizer_id      = var.api_authorizer_id
}
