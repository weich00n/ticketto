# Paste into GitLab: backend/reservation-service → Settings → CI/CD → Variables, each one
# protected with environment scope production (see ../README.md).
output "gitlab_ci_variables" {
  value = {
    AWS_REGION               = var.region
    AWS_ROLE_ARN             = aws_iam_role.deploy.arn
    ECR_REPOSITORY_URI       = aws_ecr_repository.service.repository_url
    ECS_CLUSTER              = var.ecs_cluster_name
    ECS_SERVICE              = aws_ecs_service.service.name
    ECS_EXECUTION_ROLE_ARN   = aws_iam_role.execution.arn
    ENV_FILE_S3_ARN          = "${aws_s3_bucket.config.arn}/${aws_s3_object.env.key}"
    DB_SECRET_ARN            = aws_secretsmanager_secret.db.arn
    ADMISSION_KEY_SECRET_ARN = aws_secretsmanager_secret.admission_key.arn
    RABBITMQ_SECRET_ARN      = data.aws_secretsmanager_secret.broker.arn
    REDIS_SECRET_ARN         = aws_secretsmanager_secret.redis.arn
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
