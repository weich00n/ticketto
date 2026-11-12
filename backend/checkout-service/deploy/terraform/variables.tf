variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "aws_profile" {
  description = "AWS CLI profile to sign in with (null = the default credential chain)"
  type        = string
  default     = null
}

variable "vpc_id" {
  description = "The default VPC the other services run in"
  type        = string
  default     = "vpc-0f6186a17c195aecd"
}

variable "ecs_cluster" {
  type    = string
  default = "TicketPlatform"
}

variable "rds_endpoint" {
  type    = string
  default = "db-ticketing.ckt0mcwoq5s8.us-east-1.rds.amazonaws.com"
}

# Existing security groups that checkout must reach. Terraform adds one inbound rule to each
# (source = the checkout-service group) and does not manage anything else on them.
variable "db_security_group_id" {
  type    = string
  default = "sg-0b4b57071a37498e5"
}

variable "rabbitmq_security_group_id" {
  type    = string
  default = "sg-0b055d49a3f0521a9"
}

variable "reservation_security_group_id" {
  type    = string
  default = "sg-03904eefda23fcbe3"
}

variable "db_secret_name" {
  description = "Secret with keys username/password for the checkout_service DB user. Created by hand (README step 1) so the password never enters Terraform state"
  type        = string
  default     = "checkout-service/db"
}

variable "rabbitmq_secret_name" {
  description = "checkout's own RabbitMQ user, created by rabbitmq-service's OpenTofu; key url"
  type        = string
  default     = "rabibi/service/checkout-service"
}

variable "reservation_base_url" {
  description = "How checkout reaches reservation-service: its Cloud Map name, so it survives task replacement"
  type        = string
  default     = "http://reservation-service.ticketto.local:8081"
}

variable "execution_role_arn" {
  description = "Existing task execution role to use. Leave null to create checkout-service-execution (needs IAM write access)"
  type        = string
  default     = null
}

variable "api_id" {
  description = "The shared HTTP API (\"api\"). gateway.tf adds only checkout's routes to it."
  type        = string
  default     = "x0vxfeenrl"
}

variable "api_authorizer_id" {
  description = "The API's Cognito JWT authorizer (302-proj-cognito)"
  type        = string
  default     = "9ky1ji"
}

variable "api_port" {
  description = "The API's port, and its own listener on the shared internal ALB (frontend/deploy/gateway). Matches deploy/task-definition.json."
  type        = number
  default     = 3002
}

variable "api_vpc_link_id" {
  description = "The shared VPC link from API Gateway to the internal ALB (ticketto-shared-services), owned by the gateway stack."
  type        = string
  default     = "891jty"
}

variable "internal_alb_name" {
  description = "The shared internal ALB, owned by the gateway stack."
  type        = string
  default     = "ticketto-internal-api"
}

variable "gateway_target_group_name" {
  description = "The API's target group on the internal ALB, owned by the gateway stack."
  type        = string
  default     = "ticketto-checkout-api"
}

variable "gitlab_project_path" {
  description = "GitLab project whose main branch may assume gitlab-checkout-service-deploy"
  type        = string
  default     = "cs302-2026/g2-team5/backend/checkout-service"
}

variable "image_tag" {
  description = "Tag of the image in the checkout-service ECR repository to run"
  type        = string
  default     = "latest"
}

variable "deploy_services" {
  description = "Create the two ECS services. Keep false until the image is pushed and the migration has run"
  type        = bool
  default     = false
}

variable "api_min_count" {
  description = "Fewest API tasks. 2, so losing one task doesn't take checkout down. 0 (with api_max_count 0) stops the API."
  type        = number
  default     = 2
}

variable "api_max_count" {
  description = "Most API tasks the autoscaler may run (autoscaling.tf)."
  type        = number
  default     = 4

  validation {
    condition     = var.api_max_count >= var.api_min_count
    error_message = "api_max_count must be at least api_min_count."
  }
}

variable "api_cpu_target_percent" {
  description = "Average API CPU the autoscaler aims for."
  type        = number
  default     = 60
}

variable "worker_desired_count" {
  description = "Worker tasks: 1 (it runs the outbox relay and the scheduled jobs), or 0 to stop it. Not autoscaled."
  type        = number
  default     = 1
}

variable "log_retention_days" {
  type    = number
  default = 30
}
