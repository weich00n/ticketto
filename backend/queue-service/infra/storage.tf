# .env files (S3), the two secrets ECS needs (Secrets Manager) and log groups.
#
# Secret *values* and the .env files are never written by OpenTofu, so no password
# ends up in the state file. Set them with the CLI after the first apply (README.md).

data "aws_caller_identity" "current" {}

locals {
  name        = "queue-service-${var.env}"
  config_path = "queue-service/${var.env}"
}

# --- .env files ------------------------------------------------------------------------

resource "aws_s3_bucket" "config" {
  bucket = "${local.name}-config-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket                  = aws_s3_bucket.config.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "config" {
  bucket = aws_s3_bucket.config.id
  versioning_configuration {
    status = "Enabled" # roll back a bad edit of app.env
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "config" {
  bucket = aws_s3_bucket.config.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

locals {
  app_env_arn     = "${aws_s3_bucket.config.arn}/${local.config_path}/app.env"
  migrate_env_arn = "${aws_s3_bucket.config.arn}/${local.config_path}/migrate.env"
}

# --- Secrets (empty; values set with the CLI) ------------------------------------------

resource "aws_secretsmanager_secret" "jwt_private_key" {
  name        = "${local.config_path}/jwt-private-key"
  description = "RS256 private key (PEM) that signs admission tokens. Injected into queue-api only."
}

resource "aws_secretsmanager_secret" "gitlab_registry" {
  name        = "${local.config_path}/gitlab-registry"
  description = "GitLab deploy token (read_registry) as {\"username\": ..., \"password\": ...}, for ECS image pulls."
}

# --- Logs ------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "this" {
  for_each          = toset(["api", "admitter", "consumer", "migrate"])
  name              = "/ecs/${local.name}/${each.key}"
  retention_in_days = var.log_retention_days
}
