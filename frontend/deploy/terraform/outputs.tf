output "app_url" {
  description = "The public HTTPS address of the app."
  value       = "https://${var.domain_name}"
}

output "cloudfront_url" {
  description = "The same app on CloudFront's own address."
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "alb_dns_name" {
  value = aws_lb.frontend.dns_name
}

# The values for the GitLab CI/CD variables (deploy/README.md step 4).
output "gitlab_variables" {
  value = {
    AWS_REGION             = var.region
    AWS_ROLE_ARN           = aws_iam_role.gitlab_deploy.arn
    ECR_REPOSITORY_URI     = aws_ecr_repository.frontend.repository_url
    ECS_CLUSTER            = var.cluster_name
    ECS_SERVICE            = var.name
    ECS_EXECUTION_ROLE_ARN = aws_iam_role.execution.arn
    COGNITO_SECRET_ARN     = data.aws_secretsmanager_secret.cognito.arn
  }
}
