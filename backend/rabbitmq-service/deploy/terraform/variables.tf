variable "name" {
  description = "Name of the broker instance, and the prefix of its role, instance profile, security group and secret."
  type        = string
  default     = "rabibi"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "availability_zone" {
  description = "AZ of the default-VPC subnet the instance runs in. The data volume is created in the same AZ."
  type        = string
  default     = "us-east-1a"
}

variable "instance_type" {
  description = "Must be arm64 (Graviton): the AMI is AL2023 arm64. 2 GiB is the minimum: the container is capped at 1 GiB."
  type        = string
  default     = "t4g.small"
}

variable "data_volume_size_gib" {
  description = "RabbitMQ's data volume. config/rabbitmq.conf assumes 10 GiB (disk_free_limit 1 GiB)."
  type        = number
  default     = 10
}

variable "amqp_allowed_sg_ids" {
  description = "Security groups whose members may connect to AMQP (5672), e.g. the services' ECS task security groups."
  type        = list(string)
  default     = []
}

variable "amqp_allowed_cidrs" {
  description = "CIDR blocks that may connect to AMQP (5672), e.g. a peered VPC. Never 0.0.0.0/0."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.amqp_allowed_cidrs, "0.0.0.0/0")
    error_message = "Don't open AMQP to the internet."
  }
}

variable "management_allowed_cidrs" {
  description = "CIDR blocks that may open the management UI (15672) on the public IP, e.g. [\"203.0.113.7/32\"]. Empty: the UI is reached only through SSM port forwarding. Plain HTTP, so the admin password crosses the network unencrypted."
  type        = list(string)
  default     = []
}

variable "ecr_repository_name" {
  type    = string
  default = "rabbitmq-service"
}

variable "gitlab_project_path" {
  description = "Only this GitLab project's main branch may assume the deploy role."
  type        = string
  default     = "cs302-2026/g2-team5/backend/rabbitmq-service"
}

variable "create_gitlab_oidc_provider" {
  description = "An account can have only one https://gitlab.com OIDC provider. Set false if it already exists; it is then looked up."
  type        = bool
  default     = true
}

variable "allow_destroy_data" {
  description = "Must be true before `tofu destroy` can delete the ECR repository while it still holds images. (The data volume has its own guard: prevent_destroy in main.tf.)"
  type        = bool
  default     = false
}
