variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  description = "Name of the ECS service, ECR repository, log group and Cloud Map entry, and the prefix of the roles, security groups and secret."
  type        = string
  default     = "user-service"
}

variable "cluster_name" {
  description = "The shared ECS cluster user-service runs in. It already exists; this module doesn't create it."
  type        = string
  default     = "TicketPlatform"
}

variable "availability_zones" {
  description = "Default-VPC subnets the tasks use."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}

variable "desired_count" {
  description = "Running tasks. Keep 0 until the first image is in ECR and the migration has run, or ECS keeps trying, and failing, to start a container."
  type        = number
  default     = 0
}

# ---------- shared resources this code only looks up ----------

variable "rds_instance_identifier" {
  description = "The shared MySQL instance. user-service gets its own database (db_user) inside it: see ../README.md."
  type        = string
  default     = "db-ticketing"
}

variable "cognito_user_pool_id" {
  type    = string
  default = "us-east-1_2g8EYCEa1"
}

variable "api_id" {
  description = "The shared HTTP API (\"api\"). This code adds only user-service's routes to it."
  type        = string
  default     = "x0vxfeenrl"
}

variable "api_authorizer_id" {
  description = "The API's Cognito JWT authorizer (302-proj-cognito)."
  type        = string
  default     = "9ky1ji"
}

variable "api_vpc_link_id" {
  description = "The shared VPC link from API Gateway to the internal ALB (ticketto-shared-services), owned by the gateway stack."
  type        = string
  default     = "891jty"
}

variable "internal_alb_name" {
  description = "The shared internal ALB, owned by the gateway stack (frontend/deploy/gateway)."
  type        = string
  default     = "ticketto-internal-api"
}

variable "alb_listener_port" {
  description = "user-service's own listener on the shared ALB. It forwards to the tasks' port 8000, but can't be 8000 itself: that's queue-service's listener."
  type        = number
  default     = 8083
}

variable "gateway_target_group_name" {
  description = "The gateway stack's target group for this service. Looked up, never managed here."
  type        = string
  default     = "ticketto-user-api"
}

variable "service_discovery_namespace" {
  description = "Private DNS namespace in the default VPC. Services register as <name>.<namespace>."
  type        = string
  default     = "ticketto.local"
}

variable "create_service_discovery_namespace" {
  description = "true: this code creates the namespace (the first service to use it). false: it already exists and is looked up. Once another service registers in it, set false and `tofu state rm` it before destroying, or the destroy fails."
  type        = bool
  default     = true
}

variable "gitlab_project_path" {
  description = "Only this GitLab project's main branch may assume the deploy role."
  type        = string
  default     = "cs302-2026/g2-team5/backend/user-service"
}

# ---------- sizing / teardown ----------

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "allow_destroy_data" {
  description = "Must be true before `tofu destroy` can delete the ECR repository while it still holds images."
  type        = bool
  default     = false
}
