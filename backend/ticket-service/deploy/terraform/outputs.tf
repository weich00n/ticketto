# Paste into GitLab: backend/ticket-service → Settings → CI/CD → Variables, each one protected
# with environment scope production (see ../README.md). None of them is a password.
output "gitlab_ci_variables" {
  value = {
    AWS_REGION             = var.region
    AWS_ROLE_ARN           = aws_iam_role.deploy.arn
    ECR_REPOSITORY_URI     = aws_ecr_repository.service.repository_url
    ECS_CLUSTER            = var.ecs_cluster_name
    ECS_SERVICE            = aws_ecs_service.this["api"].name
    ECS_WORKER_SERVICE     = aws_ecs_service.this["worker"].name
    ECS_SUBNETS            = join(",", data.aws_subnets.default.ids)
    ECS_SECURITY_GROUP     = aws_security_group.service.id
    ECS_EXECUTION_ROLE_ARN = aws_iam_role.execution.arn
    RDS_ENDPOINT           = data.aws_db_instance.shared.address
    DB_SECRET_ARN          = aws_secretsmanager_secret.db.arn
    QR_KEYS_SECRET_ARN     = aws_secretsmanager_secret.qr_keys.arn
    RABBITMQ_SECRET_ARN    = data.aws_secretsmanager_secret.broker.arn
    SMTP_SECRET_ARN        = aws_secretsmanager_secret.smtp.arn
    EVENT_SERVICE_URL      = var.event_service_url
  }
}

output "service_security_group_id" {
  description = "Give this to the broker team for amqp_allowed_sg_ids, and to services that call this one."
  value       = aws_security_group.service.id
}

output "db_secret_arn" {
  description = "create-db-user.sh reads the password from here."
  value       = aws_secretsmanager_secret.db.arn
}

output "rds_master_secret_arn" {
  description = "RDS-managed master login, used once by create-db-user.sh."
  value       = try(data.aws_db_instance.shared.master_user_secret[0].secret_arn, null)
}

output "rds_endpoint" {
  value = data.aws_db_instance.shared.address
}

output "db_name" {
  value = var.db_name
}

output "email_sender" {
  description = "SES sends nothing from this address until its verification link is clicked: aws sesv2 get-email-identity --email-identity <this> shows VerificationStatus."
  value       = aws_sesv2_email_identity.sender.email_identity
}
