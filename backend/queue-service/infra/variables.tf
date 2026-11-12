variable "env" {
  description = "Environment name: staging or prod."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.env)
    error_message = "env must be staging or prod."
  }
}

variable "region" {
  description = "AWS region (the shared RDS instance is in us-east-1)."
  type        = string
  default     = "us-east-1"
}

# --- Existing network (referenced, not created) ---------------------------------------

variable "vpc_id" {
  description = "VPC that holds RDS and the Redis EC2 instance."
  type        = string
}

variable "subnet_ids" {
  description = "Subnets for the ECS tasks. Without a NAT gateway they must be public subnets, and assign_public_ip must stay true."
  type        = list(string)
}

variable "assign_public_ip" {
  description = "Give tasks a public IP. Needed with no NAT (to pull the image and reach AWS APIs)."
  type        = bool
  default     = true
}

variable "ecs_cluster_name" {
  description = "ECS cluster the services run in (looked up, not created). Changing it recreates all three services."
  type        = string
  default     = "TicketPlatform"
}

variable "api_target_group_arn" {
  description = "The platform team's target group for queue-api on the shared internal ALB (registered by ECS). Empty = no load balancer."
  type        = string
  default     = ""
}

variable "api_ingress_cidrs" {
  description = "Extra addresses allowed to call queue-api directly on port 8000, bypassing the gateway (e.g. [\"203.0.113.7/32\"] for debugging). Keep empty: X-User-Sub is trusted as sent."
  type        = list(string)
  default     = []
}

# --- RabbitMQ (central broker; its team owns the secret) ------------------------------

variable "rabbitmq_url_secret_arn" {
  description = "Full ARN of the Secrets Manager secret holding the RabbitMQ connection string (incl. the random -XXXXXX suffix). Injected as RABBITMQ_URL into api, admitter and consumer."
  type        = string
}

variable "rabbitmq_url_secret_json_key" {
  description = "If the secret is JSON, the key whose value is the connection string (e.g. \"connection_string\"). Empty if the secret is the plain URL."
  type        = string
  default     = ""
}

variable "rabbitmq_secret_kms_key_arn" {
  description = "Customer-managed KMS key that encrypts the RabbitMQ secret, if any (required when the secret is in another AWS account). Empty for the default aws/secretsmanager key."
  type        = string
  default     = ""
}

# --- Image -----------------------------------------------------------------------------

variable "image" {
  description = "Initial image, e.g. registry.gitlab.com/cs302-2026/g2-team5/backend/queue-service/main:<sha>. The GitLab deploy job replaces it on every release."
  type        = string
}

# --- GitLab ----------------------------------------------------------------------------

variable "gitlab_project_path" {
  description = "GitLab project allowed to assume the deploy role."
  type        = string
  default     = "cs302-2026/g2-team5/backend/queue-service"
}

variable "gitlab_deploy_branch" {
  description = "Only pipelines on this branch may deploy."
  type        = string
  default     = "main"
}

variable "gitlab_oidc_audience" {
  description = "Audience (client ID) of the gitlab.com OIDC provider. Must equal the deploy job's id_tokens aud. If the account's existing provider lists another value, use that."
  type        = string
  default     = "sts.amazonaws.com"
}

variable "create_gitlab_oidc_provider" {
  description = "Create the gitlab.com OIDC identity provider. Set false if the account already has one (it is per account, shared by staging and prod)."
  type        = bool
  default     = true
}

# --- Rollout ---------------------------------------------------------------------------

variable "create_services" {
  description = "Create the three ECS services. First apply with false, fill in the secrets and .env files, run the migration, then apply with true."
  type        = bool
  default     = true
}

variable "api_min_count" {
  description = "Fewest queue-api tasks. 2, so losing one task doesn't take the queue down. tofu apply also resets the running count to this."
  type        = number
  default     = 2
}

variable "api_max_count" {
  description = "Most queue-api tasks the autoscaler may run (autoscaling.tf)."
  type        = number
  default     = 4
}

variable "api_cpu_target_percent" {
  description = "Average queue-api CPU the autoscaler aims for."
  type        = number
  default     = 60
}

variable "consumer_desired_count" {
  description = "Number of queue-consumer tasks (1 or 2; handlers are idempotent)."
  type        = number
  default     = 1
}

variable "log_retention_days" {
  type    = number
  default = 30
}
