# The shared gateway stack owns stage access logging; keep existing auto-deploy.
import {
  to = aws_apigatewayv2_stage.shared
  id = "x0vxfeenrl/$default"
}
resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/apigateway/ticketto"
  retention_in_days = 14
}
resource "aws_apigatewayv2_stage" "shared" {
  api_id      = data.aws_apigatewayv2_api.shared.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    detailed_metrics_enabled = false
  }
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.gateway.arn
    format = jsonencode({
      requestId         = "$context.requestId"
      routeKey          = "$context.routeKey"
      status            = "$context.status"
      integrationError  = "$context.integrationErrorMessage"
      integrationStatus = "$context.integrationStatus"
      responseLatency   = "$context.responseLatency"
    })
  }
}
