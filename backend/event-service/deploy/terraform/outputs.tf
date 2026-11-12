# The values for the GitLab CI/CD variables (deploy/README.md).
output "api_gateway_url" {
  value = data.aws_apigatewayv2_api.shared.api_endpoint
}

output "event_service_internal_url" {
  value = "http://${var.name}.${var.service_discovery_namespace}:${var.api_port}"
}

output "gitlab_variables" {
  value = {
    AWS_REGION             = var.region
    AWS_ROLE_ARN           = aws_iam_role.gitlab_deploy.arn
    ECR_REPOSITORY_URI     = aws_ecr_repository.event_service.repository_url
    ECS_CLUSTER            = var.cluster_name
    ECS_SERVICE            = aws_ecs_service.api.name
    ECS_WORKER_SERVICE     = aws_ecs_service.worker.name
    ECS_EXECUTION_ROLE_ARN = aws_iam_role.execution.arn
    DB_SECRET_ARN          = data.aws_secretsmanager_secret.db.arn
    RABBITMQ_SECRET_ARN    = data.aws_secretsmanager_secret.rabbitmq.arn
  }
}
