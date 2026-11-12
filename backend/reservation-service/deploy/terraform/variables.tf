variable "name" {
  description = "The ECS service, and the prefix of its roles, security group, secrets, log group and ECR repository."
  type        = string
  default     = "reservation-service"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "ecs_cluster_name" {
  description = "The team's shared cluster. It already exists; this stack only adds a service to it."
  type        = string
  default     = "TicketPlatform"
}

variable "desired_count" {
  description = "Tasks when the ECS service is first created. After that the autoscaler owns the count (autoscaling.tf)."
  type        = number
  default     = 0
}

variable "rds_instance_id" {
  description = "The shared RDS instance. Looked up, never managed here."
  type        = string
  default     = "db-ticketing"
}

variable "db_name" {
  description = "This service's schema on the shared instance. create-db-user.sh creates it; Flyway creates its tables on first start."
  type        = string
  default     = "db_reservation"
}

variable "redis_instance_name" {
  description = "Name tag of this service's Redis EC2 instance."
  type        = string
  default     = "reservation-Redis-v1.1"
}

variable "redis_security_group_name" {
  type    = string
  default = "reservation-redis"
}

variable "redisinsight_cidr" {
  description = "The one address allowed to open RedisInsight on port 5540."
  type        = string
  default     = "138.75.199.89/32"
}

variable "redis_password" {
  description = "The --requirepass of the Redis container. Empty: the secret is created without a value; set it with `aws secretsmanager put-secret-value`."
  type        = string
  default     = ""
  sensitive   = true
}

variable "admission_public_key_file" {
  description = "Path to Queue's admission-token public key (PEM). Empty: the secret is created without a value; set it with `aws secretsmanager put-secret-value`."
  type        = string
  default     = ""
}

variable "broker_secret_name" {
  description = "Created by rabbitmq-service's stack: Reservation's own least-privilege broker login. The task reads its url key."
  type        = string
  default     = "rabibi/service/reservation-service"
}

variable "client_security_group_ids" {
  description = "Security groups that may call this service on 8081 (Queue and Checkout for /internal/v1). The shared gateway stack adds the ALB's own rule."
  type        = list(string)
  default     = []
}

variable "gitlab_project_path" {
  description = "Only this GitLab project's main branch may assume the deploy role."
  type        = string
  default     = "cs302-2026/g2-team5/backend/reservation-service"
}

variable "allow_destroy_data" {
  description = "Must be true before `tofu destroy` can delete the ECR repository or the config bucket while they still hold files."
  type        = bool
  default     = false
}

# ---------- autoscaling (autoscaling.tf) ----------

variable "min_count" {
  description = "Fewest running tasks. 2, so losing one task doesn't take the service down."
  type        = number
  default     = 2
}

variable "max_count" {
  type    = number
  default = 4
}

variable "cpu_target_percent" {
  description = "Average CPU across tasks that the autoscaler aims for."
  type        = number
  default     = 60
}

variable "service_discovery_namespace" {
  description = "The team's private Cloud Map namespace. Looked up, never managed here."
  type        = string
  default     = "ticketto.local"
}

variable "gateway_target_group_name" {
  description = "The shared gateway stack's target group for this service (frontend repository, deploy/gateway). Looked up, never managed here."
  type        = string
  default     = "ticketto-reservation-api"
}
