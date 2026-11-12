variable "name" {
  description = "The API's ECS service, and the prefix of the worker service, roles, security group, secrets, log group and ECR repository."
  type        = string
  default     = "ticket-service"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "ecs_cluster_name" {
  description = "The team's shared cluster. It already exists; this stack only adds services to it."
  type        = string
  default     = "TicketPlatform"
}

variable "desired_count" {
  description = "Running tasks for the API and for the worker. 0 until the pipeline has pushed a first image to ECR (../README.md, First deployment); 0 again to stop paying for compute."
  type        = number
  default     = 0
}

variable "rds_instance_id" {
  description = "The shared RDS instance. Looked up, never managed here."
  type        = string
  default     = "db-ticketing"
}

variable "db_name" {
  description = "This service's schema on the shared instance. create-db-user.sh creates it; the pipeline's migrate job creates the tables."
  type        = string
  default     = "db_ticket"
}

variable "db_username" {
  type    = string
  default = "ticket_service"
}

variable "broker_secret_name" {
  description = "Created by rabbitmq-service's stack: Ticket's own least-privilege broker login. The worker reads its url key."
  type        = string
  default     = "rabibi/service/ticket-service"
}

variable "service_discovery_namespace" {
  description = "The team's private Cloud Map namespace. Looked up, never managed here."
  type        = string
  default     = "ticketto.local"
}

variable "client_security_group_ids" {
  description = "Security groups that may call the API on 3006, e.g. Checkout's for /internal/v1. Not the gateway's ALB: the gateway stack manages that rule itself."
  type        = list(string)
  default     = []
}

variable "gateway_target_group_name" {
  description = "The API's target group on the shared internal ALB, created by the gateway stack (frontend repo, deploy/gateway). Looked up, never managed here."
  type        = string
  default     = "ticketto-ticket-api"
}

variable "gitlab_project_id" {
  description = "Only this GitLab project's main branch may assume the deploy role. The project's path was used before, so GitLab won't issue path-based ID tokens for it; the project's sub claim is set to start with project_id instead (../README.md, GitLab sign-in)."
  type        = number
  default     = 87547400 # cs302-2026/g2-team5/backend/ticket-service
}

variable "allow_destroy_data" {
  description = "Must be true before `tofu destroy` can delete the ECR repository while it still holds images."
  type        = bool
  default     = false
}

variable "event_service_url" {
  description = "Event Service's private address for the worker, e.g. http://<host>:3001. Empty: event details on ticket emails come from messages only."
  type        = string
  default     = ""
}

variable "email_sender" {
  description = "From address of ticket emails. tofu makes it an Amazon SES email identity; AWS then emails it a verification link, and SES sends nothing from it until that link is clicked."
  type        = string
  default     = "ticketto.alerts@gmail.com"

  validation {
    condition     = can(regex("^[^@\\s<>]+@[^@\\s<>]+\\.[^@\\s<>]+$", var.email_sender))
    error_message = "email_sender must be a plain email address, without a display name."
  }
}

variable "email_sender_name" {
  description = "Display name shown in front of email_sender."
  type        = string
  default     = "Ticketto"
}
