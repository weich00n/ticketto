# checkout-service behind the team's shared HTTP API ("api"), the same way every other service is:
# shared VPC link ──► shared internal ALB :3002 ──► API tasks. This adds only checkout's
# integrations and routes; the API, its $default stage (auto-deploy), the Cognito authorizer,
# the VPC link, the ALB, this listener, the target group and the rule letting the ALB reach
# 3002 on the tasks belong to the gateway stack (frontend/deploy/gateway). Apply that first.

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

# ---------- integrations and routes ----------

data "aws_apigatewayv2_api" "shared" {
  api_id = var.api_id
}

# `overwrite:` replaces any X-User-* header the client sent, so callers can't fake their
# identity (checkout trusts X-User-Sub). `username` is only in access tokens: the frontend
# must send the access token. Same mapping as user-service.
resource "aws_apigatewayv2_integration" "orders" {
  api_id                 = data.aws_apigatewayv2_api.shared.id
  integration_type       = "HTTP_PROXY"
  integration_method     = "ANY"
  connection_type        = "VPC_LINK"
  connection_id          = var.api_vpc_link_id
  integration_uri        = data.aws_lb_listener.api.arn
  payload_format_version = "1.0"

  request_parameters = {
    "overwrite:header.X-User-Sub"      = "$context.authorizer.claims.sub"
    "overwrite:header.X-User-Username" = "$context.authorizer.claims.username"
    "remove:header.X-User-Groups"      = "''"
  }
}

resource "aws_apigatewayv2_route" "orders" {
  for_each = toset(["ANY /api/v1/orders", "ANY /api/v1/orders/{proxy+}"])

  api_id             = data.aws_apigatewayv2_api.shared.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.orders.id}"
  authorization_type = "JWT"
  authorizer_id      = var.api_authorizer_id
}

# Stripe calls the webhook with no Cognito token; its only authentication is the
# Stripe-Signature header, which checkout verifies against the raw body (HTTP_PROXY passes the
# body through unchanged). The route has no authorizer, so its own integration strips any
# identity header a caller sends instead of mapping claims that don't exist here.
resource "aws_apigatewayv2_integration" "stripe_webhook" {
  api_id                 = data.aws_apigatewayv2_api.shared.id
  integration_type       = "HTTP_PROXY"
  integration_method     = "ANY"
  connection_type        = "VPC_LINK"
  connection_id          = var.api_vpc_link_id
  integration_uri        = data.aws_lb_listener.api.arn
  payload_format_version = "1.0"

  request_parameters = {
    "remove:header.X-User-Sub"      = "''"
    "remove:header.X-User-Username" = "''"
    "remove:header.X-User-Groups"   = "''"
  }
}

resource "aws_apigatewayv2_route" "stripe_webhook" {
  api_id             = data.aws_apigatewayv2_api.shared.id
  route_key          = "POST /webhooks/stripe"
  target             = "integrations/${aws_apigatewayv2_integration.stripe_webhook.id}"
  authorization_type = "NONE"
}

# /health, /ready and anything else checkout serves are deliberately not routed: they stay
# inside the VPC.
