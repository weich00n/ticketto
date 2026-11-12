variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  description = "Names the services, roles, security group, ECR repository and secrets."
  type        = string
  default     = "notification-service"
}

variable "availability_zones" {
  description = "The services' subnets (default VPC)."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

variable "cluster_name" {
  description = "The shared ECS cluster. Looked up, never changed."
  type        = string
  default     = "TicketPlatform"
}

variable "desired_count" {
  description = "Running API tasks. Keep 0 until the first image is in ECR and the migration has run, or ECS keeps trying, and failing, to start a container."
  type        = number
  default     = 0
}

variable "worker_desired_count" {
  description = "Running worker tasks. The same as desired_count. 0 pauses the worker; messages wait on the queues."
  type        = number
  default     = 0
}

variable "ses_sender" {
  description = "The SES-verified address (or domain) emails are sent from, e.g. you@example.com."
  type        = string
}

variable "email_from_name" {
  type    = string
  default = "Ticketto"
}

variable "rds_instance_identifier" {
  description = "The shared MySQL instance. notification-service gets its own database (db_notification) in it: ../create-database.sh."
  type        = string
  default     = "db-ticketing"
}

variable "user_service_security_group_name" {
  description = "user-service's ECS task security group; this code lets the worker in on user_service_ports."
  type        = string
  default     = "user-service"
}

variable "user_service_ports" {
  description = "user-service ports the worker may reach. 8001 serves only /internal/v1. While switching from 8000, apply once with [8000, 8001]."
  type        = list(number)
  default     = [8001]
}

variable "user_service_url" {
  description = "user-service's private address (Cloud Map)."
  type        = string
  default     = "http://user-service.ticketto.local:8001"
}

variable "event_service_url" {
  description = "Optional. event-service's private address, for titles of events published before this service started."
  type        = string
  default     = ""
}

variable "broker_secret_name" {
  description = "Created by rabbitmq-service's OpenTofu; its `url` key is the broker login."
  type        = string
  default     = "rabibi/service/notification-service"
}

variable "gitlab_project_path" {
  description = "Only this project's main branch may assume the deploy role."
  type        = string
  default     = "cs302-2026/g2-team5/backend/notification-service"
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "allow_destroy_data" {
  description = "true before `tofu destroy` if the ECR repository still holds images."
  type        = bool
  default     = false
}

variable "api_id" {
  description = "The shared HTTP API (\"api\"). api.tf adds only the notifications routes to it."
  type        = string
  default     = "x0vxfeenrl"
}

variable "api_authorizer_id" {
  description = "The API's Cognito JWT authorizer (302-proj-cognito)"
  type        = string
  default     = "9ky1ji"
}

variable "api_port" {
  description = "The API's port, and its own listener on the shared internal ALB (frontend/deploy/gateway). Each service there has its own port. Matches deploy/task-definition.json and the Dockerfile."
  type        = number
  default     = 8082
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
  default     = "ticketto-notification-api"
}

variable "api_timeout_seconds" {
  description = "How long API Gateway waits for the API (30 s at most)"
  type        = number
  default     = 10
}
