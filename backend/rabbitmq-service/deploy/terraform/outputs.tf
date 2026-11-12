# Paste into GitLab: backend/rabbitmq-service → Settings → CI/CD → Variables (see ../README.md).
output "gitlab_ci_variables" {
  value = {
    AWS_REGION         = var.region
    AWS_ROLE_ARN       = aws_iam_role.deploy.arn
    ECR_REPOSITORY_URI = aws_ecr_repository.broker.repository_url
    BROKER_INSTANCE_ID = aws_instance.broker.id
  }
}

output "broker_instance_id" {
  description = "Open a shell with: aws ssm start-session --target <id>"
  value       = aws_instance.broker.id
}

output "broker_private_ip" {
  description = "Services connect to <ip>:5672. Also in the secret's host and url keys."
  value       = aws_instance.broker.private_ip
}

output "broker_security_group_id" {
  value = aws_security_group.broker.id
}

output "rabbitmq_secret_arn" {
  description = "The ADMIN user's secret. Don't give it to services: they get service_secret_arns."
  value       = aws_secretsmanager_secret.broker.arn
}

output "service_secret_arns" {
  description = "Give each service its own: {\"name\": \"RABBITMQ_URL\", \"valueFrom\": \"<arn>:url::\"}"
  value       = { for k, s in aws_secretsmanager_secret.service : k => s.arn }
}

output "account_id" {
  value = data.aws_caller_identity.current.account_id
}
