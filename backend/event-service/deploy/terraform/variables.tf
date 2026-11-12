variable "name" {
  description = "Name of the service: the prefix of its roles, ECR repository, log group and ECS services."
  type        = string
  default     = "event-service"
}

variable "api_id" {
  description = "Existing shared HTTP API; this stack owns only Event routes."
  type        = string
  default     = "x0vxfeenrl"
}

variable "api_port" {
  description = "The API's port, and its own listener on the shared internal ALB (frontend/deploy/gateway). Matches deploy/task-definition.json."
  type        = number
  default     = 3001
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
  default     = "ticketto-event-api"
}

variable "jwt_authorizer_id" {
  description = "Existing Cognito JWT authorizer on the shared HTTP API."
  type        = string
  default     = "9ky1ji"
}

variable "service_discovery_namespace" {
  type    = string
  default = "ticketto.local"
}

variable "task_security_group_name" {
  description = "Existing dedicated Event group, already allowed by RDS."
  type        = string
  default     = "event-service"
}

variable "broker_security_group_name" {
  type    = string
  default = "rabibi"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "cluster_name" {
  description = "The existing ECS cluster the team's services run in."
  type        = string
  default     = "TicketPlatform"
}

variable "gitlab_project_path" {
  description = "GitLab project whose main branch may deploy. The deploy role trusts only this."
  type        = string
  default     = "cs302-2026/g2-team5/backend/event-service"
}

variable "db_secret_name" {
  description = "Secrets Manager secret with the database login (keys username and password). Created by hand: its value never goes through OpenTofu."
  type        = string
  default     = "event-service/db"
}

variable "rabbitmq_secret_name" {
  description = "This service's own broker user, created by the rabbitmq-service stack (key url)."
  type        = string
  default     = "rabibi/service/event-service"
}

variable "desired_count" {
  description = "Tasks each service starts with. Later changes are made in ECS and ignored here. The worker must stay at 1."
  type        = number
  default     = 1
}
