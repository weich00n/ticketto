# Values to copy into GitLab CI/CD variables (scoped to this environment) and to
# give to other teams.

output "gitlab_variables" {
  description = "Set these in GitLab → Settings → CI/CD → Variables, scoped to this environment."
  value = {
    AWS_REGION           = var.region
    AWS_ROLE_ARN         = aws_iam_role.gitlab_deploy.arn
    ECS_CLUSTER          = data.aws_ecs_cluster.shared.cluster_name
    ECS_SUBNETS          = join(",", var.subnet_ids)
    ECS_SECURITY_GROUP   = aws_security_group.app.id
    ECS_ASSIGN_PUBLIC_IP = var.assign_public_ip ? "ENABLED" : "DISABLED"
  }
}

output "config_bucket" {
  description = "Upload app.env and migrate.env here."
  value = {
    app_env     = "s3://${aws_s3_bucket.config.bucket}/${local.config_path}/app.env"
    migrate_env = "s3://${aws_s3_bucket.config.bucket}/${local.config_path}/migrate.env"
  }
}

output "secrets_to_fill" {
  description = "Secret names to set with `aws secretsmanager put-secret-value`."
  value = {
    jwt_private_key = aws_secretsmanager_secret.jwt_private_key.name
    gitlab_registry = aws_secretsmanager_secret.gitlab_registry.name
  }
}

output "app_security_group_id" {
  description = "Ask the RDS owners to allow TCP 3306 from this security group."
  value       = aws_security_group.app.id
}
