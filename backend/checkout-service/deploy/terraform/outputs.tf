output "security_group_id" {
  value = aws_security_group.checkout.id
}

output "subnet_ids" {
  description = "Comma-separated, for the migration task's --network-configuration (README step 5)"
  value       = join(",", data.aws_subnets.this.ids)
}

output "ecr_repository_url" {
  value = aws_ecr_repository.checkout.repository_url
}

output "execution_role_arn" {
  value = local.execution_role_arn
}

output "stripe_secret_arn" {
  value = aws_secretsmanager_secret.stripe.arn
}

output "api_task_definition_arn" {
  value = aws_ecs_task_definition.this["api"].arn
}

output "worker_task_definition_arn" {
  value = aws_ecs_task_definition.this["worker"].arn
}

output "orders_api_url" {
  description = "Where the frontend calls checkout (needs a Cognito access token)"
  value       = "${data.aws_apigatewayv2_api.shared.api_endpoint}/api/v1/orders"
}

output "stripe_webhook_url" {
  description = "The endpoint URL to register in the Stripe dashboard"
  value       = "${data.aws_apigatewayv2_api.shared.api_endpoint}/webhooks/stripe"
}

output "gitlab_deploy_role_arn" {
  value = aws_iam_role.gitlab_deploy.arn
}

# The checkout-service project's CI/CD variables (deploy/README.md §8), ready to paste:
# Protected, environment scope "production". None is a password. DEPLOY_ENABLED is set
# separately (scope All).
output "gitlab_ci_variables" {
  value = {
    AWS_REGION             = var.aws_region
    AWS_ROLE_ARN           = aws_iam_role.gitlab_deploy.arn
    ECR_REPOSITORY_URI     = aws_ecr_repository.checkout.repository_url
    ECS_CLUSTER            = var.ecs_cluster
    ECS_SERVICE            = local.services.api.name
    ECS_WORKER_SERVICE     = local.services.worker.name
    ECS_SUBNETS            = join(",", data.aws_subnets.this.ids)
    ECS_SECURITY_GROUP     = aws_security_group.checkout.id
    ECS_ASSIGN_PUBLIC_IP   = "ENABLED" # public subnets, no NAT
    ECS_EXECUTION_ROLE_ARN = local.execution_role_arn
    RDS_ENDPOINT           = var.rds_endpoint
    DB_SECRET_ARN          = data.aws_secretsmanager_secret.db.arn
    RABBITMQ_SECRET_ARN    = local.placeholders["__RABBITMQ_SECRET_ARN__"] # ends in :url:: (JSON secret)
    STRIPE_SECRET_ARN      = aws_secretsmanager_secret.stripe.arn
    # reservation-service registers in Cloud Map, so CI deploys don't depend on its task IP.
    RESERVATION_BASE_URL = var.reservation_base_url
  }
}
