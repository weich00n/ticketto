variable "name" {
  description = "Name of the app, and the prefix of its roles, security groups, load balancer and ECS service. The container is called the same."
  type        = string
  default     = "frontend"
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
  default     = "cs302-2026/g2-team5/frontend"
}

variable "cognito_secret_name" {
  description = "Secrets Manager secret holding the Cognito app client secret under the key client_secret. Created by hand: its value never goes through OpenTofu."
  type        = string
  default     = "frontend/cognito"
}

variable "container_port" {
  type    = number
  default = 3000
}

variable "health_check_path" {
  type    = string
  default = "/api/health"
}

variable "desired_count" {
  description = "Tasks to start with. Later changes are made in ECS and ignored here."
  type        = number
  default     = 1
}

variable "create_service" {
  description = "Create the ECS service. Leave false until the pipeline has registered the task definition once: a service can't exist without one."
  type        = bool
  default     = false
}

variable "domain_name" {
  description = "The app's domain. Its hosted zone already exists in Route 53 (created when the domain was registered). www.<domain> is served too."
  type        = string
  default     = "ticketo-cs302.com"
}
