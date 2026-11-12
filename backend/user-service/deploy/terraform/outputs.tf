# Paste into GitLab: backend/user-service → Settings → CI/CD → Variables, each one Protected
# with environment scope "production":
#   tofu output -json gitlab_ci_variables
output "gitlab_ci_variables" {
  value = {
    AWS_REGION               = var.region
    AWS_ROLE_ARN             = aws_iam_role.deploy.arn
    ECR_REPOSITORY_URI       = aws_ecr_repository.service.repository_url
    ECS_CLUSTER              = data.aws_ecs_cluster.this.cluster_name
    ECS_SERVICE              = aws_ecs_service.service.name
    ECS_WORKER_SERVICE       = aws_ecs_service.worker.name
    ECS_SUBNETS              = join(",", data.aws_subnets.service.ids)
    ECS_SECURITY_GROUP       = aws_security_group.service.id
    ECS_EXECUTION_ROLE_ARN   = aws_iam_role.execution.arn
    ECS_TASK_ROLE_ARN        = aws_iam_role.task.arn
    RDS_ENDPOINT             = data.aws_db_instance.shared.address
    DB_SECRET_ARN            = aws_secretsmanager_secret.db.arn
    RABBITMQ_SECRET_ARN      = data.aws_secretsmanager_secret.broker.arn
    APP_COGNITO_USER_POOL_ID = var.cognito_user_pool_id
  }
}

output "api_base_url" {
  description = "e.g. curl -H \"Authorization: Bearer <access token>\" <api_base_url>/api/v1/users/me"
  value       = data.aws_apigatewayv2_api.shared.api_endpoint
}

output "service_dns_name" {
  description = "Inside the VPC, other services can reach user-service at http://<this>:8000"
  value       = "${var.name}.${var.service_discovery_namespace}"
}

output "db_secret_arn" {
  value = aws_secretsmanager_secret.db.arn
}

output "rds_endpoint" {
  value = data.aws_db_instance.shared.address
}
