# Paste into GitLab: backend/notification-service → Settings → CI/CD → Variables, each one
# Protected with environment scope "production":
#   tofu output -json gitlab_ci_variables
output "gitlab_ci_variables" {
  value = {
    AWS_REGION             = var.region
    AWS_ROLE_ARN           = aws_iam_role.deploy.arn
    ECR_REPOSITORY_URI     = aws_ecr_repository.service.repository_url
    ECS_CLUSTER            = data.aws_ecs_cluster.this.cluster_name
    ECS_SERVICE            = aws_ecs_service.api.name
    ECS_WORKER_SERVICE     = aws_ecs_service.worker.name
    ECS_SUBNETS            = join(",", data.aws_subnets.service.ids)
    ECS_SECURITY_GROUP     = aws_security_group.api.id # the migration task only needs the database
    ECS_EXECUTION_ROLE_ARN = aws_iam_role.execution.arn
    ECS_TASK_ROLE_ARN      = aws_iam_role.worker_task.arn
    RDS_ENDPOINT           = data.aws_db_instance.shared.address
    DB_SECRET_ARN          = aws_secretsmanager_secret.db.arn
    RABBITMQ_SECRET_ARN    = data.aws_secretsmanager_secret.broker.arn
    USER_SERVICE_URL       = var.user_service_url
    EVENT_SERVICE_URL      = var.event_service_url
    EMAIL_FROM             = local.email_from
    SES_CONFIGURATION_SET  = aws_sesv2_configuration_set.tracking.configuration_set_name
  }
}

output "email_events_log_group" {
  description = "SES's event for each email: search it for the ses_message_id in an \"email sent\" line."
  value       = aws_cloudwatch_log_group.email_events.name
}

output "worker_security_group_id" {
  description = "Add to amqp_allowed_sg_ids in rabbitmq-service's terraform.tfvars, then `tofu apply` there."
  value       = aws_security_group.worker.id
}

output "db_secret_arn" {
  value = aws_secretsmanager_secret.db.arn
}
