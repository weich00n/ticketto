variable "region" {
  type    = string
  default = "us-east-1"
}

variable "ecs_cluster_name" {
  description = "The team's shared cluster: every service, queue included since early November."
  type        = string
  default     = "TicketPlatform"
}

variable "broker_instance_name" {
  description = "Name tag of the RabbitMQ EC2 instance (rabbitmq-service's stack). Services reach it by private IP, which survives a stop and start."
  type        = string
  default     = "rabibi"
}

variable "reservation_redis_instance_name" {
  description = "Name tag of reservation-service's Redis EC2 instance (reservation-service's stack)."
  type        = string
  default     = "reservation-Redis-v1.1"
}

variable "broker_stop_duration" {
  description = "C1: how long the broker stays stopped before FIS starts it again (ISO 8601)."
  type        = string
  default     = "PT3M"
}

variable "watched_log_groups" {
  description = "Service log groups whose ERROR lines feed the abort alarm."
  type        = list(string)
  default = [
    "/ecs/checkout-service",
    "/ecs/event-service",
    "/ecs/reservation-service",
    "/ecs/ticket-service",
    "/ecs/queue-service-staging/api",
    "/ecs/queue-service-staging/admitter",
    "/ecs/queue-service-staging/consumer",
  ]
}

variable "abort_error_threshold" {
  description = <<-EOT
    ERROR lines a minute, summed over every watched service, above which a running experiment is
    stopped (3 minutes in a row). Failures cause some errors by design (a stopped broker makes every
    publisher log one), so this is set to catch a runaway cascade, not the expected noise. Calibrate
    it from a no-fault baseline and from the first run of each experiment.
  EOT
  type        = number
  default     = 300
}

variable "internal_alb_name" {
  description = "The shared internal ALB every API sits behind (gateway VPC link -> ALB -> target group)."
  type        = string
  default     = "ticketto-internal-api"
}

variable "dashboard_target_groups" {
  description = "Dashboard: each API's target group on the internal ALB, by service."
  type        = map(string)
  default = {
    checkout     = "ticketto-checkout-api"
    event        = "ticketto-event-api"
    queue        = "ticketto-queue-api"
    reservation  = "ticketto-reservation-api"
    ticket       = "ticketto-ticket-api"
    user         = "ticketto-user-api"
    notification = "ticketto-notification-api"
  }
}

variable "dashboard_ecs_services" {
  description = "Dashboard: ECS services whose CPU and memory are shown, workers included."
  type        = list(string)
  default = [
    "checkout-service", "checkout-service-worker",
    "event-service", "event-service-worker",
    "queue-api", "queue-admitter", "queue-consumer",
    "reservation-service",
    "ticket-service", "ticket-service-worker",
    "user-service",
  ]
}
