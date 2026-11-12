# Autoscaling and service discovery for reservation-service.
#
#   Application Auto Scaling ── CPU target tracking ──► ECS service desired count (min..max)
#   Cloud Map ticketto.local ── A records, one per task ──► reservation-service.ticketto.local
#
# More than one task is safe: holds are claimed in Lua, the outbox is read with SKIP LOCKED and
# inventory rows are locked, so the scheduled jobs never process the same thing twice.
# The namespace is the team's (API Gateway reaches user-service through it). Looked up, never managed here.

data "aws_service_discovery_dns_namespace" "team" {
  name = var.service_discovery_namespace
  type = "DNS_PRIVATE"
}

resource "aws_service_discovery_service" "service" {
  name = var.name

  dns_config {
    namespace_id   = data.aws_service_discovery_dns_namespace.team.id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "A"
      ttl  = 10
    }
  }
  # No health_check_custom_config: an empty block isn't stored, so every plan would replace this
  # service. ECS still deregisters a task from DNS as soon as it stops.
}

resource "aws_appautoscaling_target" "service" {
  service_namespace  = "ecs"
  resource_id        = "service/${var.ecs_cluster_name}/${aws_ecs_service.service.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.min_count
  max_capacity       = var.max_count
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.name}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.service.service_namespace
  resource_id        = aws_appautoscaling_target.service.resource_id
  scalable_dimension = aws_appautoscaling_target.service.scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = var.cpu_target_percent
    scale_out_cooldown = 60
    scale_in_cooldown  = 120
  }
}
