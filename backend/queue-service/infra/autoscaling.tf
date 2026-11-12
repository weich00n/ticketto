# Autoscaling for queue-api: Application Auto Scaling ── CPU target tracking ──► desired count.
#
# More than one queue-api task is safe: queue state lives in Redis (changed only in Lua scripts),
# every task signs with the same JWT key, and new tasks register in the ALB target group on their
# own. The admitter and the consumer are not scaled: exactly one admitter may run.

resource "aws_appautoscaling_target" "api" {
  count = var.create_services ? 1 : 0

  service_namespace  = "ecs"
  resource_id        = "service/${data.aws_ecs_cluster.shared.cluster_name}/${aws_ecs_service.this["api"].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.api_min_count
  max_capacity       = var.api_max_count
}

resource "aws_appautoscaling_policy" "api_cpu" {
  count = var.create_services ? 1 : 0

  name               = "${local.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api[0].service_namespace
  resource_id        = aws_appautoscaling_target.api[0].resource_id
  scalable_dimension = aws_appautoscaling_target.api[0].scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = var.api_cpu_target_percent
    scale_out_cooldown = 60
    scale_in_cooldown  = 120
  }
}
