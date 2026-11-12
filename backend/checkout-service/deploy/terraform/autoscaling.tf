# Task counts: Application Auto Scaling owns each service's desired count, between min and max.
#
#   API     CPU target tracking ──► desired count, api_min_count..api_max_count
#   worker  pinned: min = max = worker_desired_count (no policy)
#
# More than one API task is safe: the API is HTTP-only and keeps no state between requests (all
# background work is in the worker), and each task registers in the shared ALB's target group, so
# the gateway reaches every healthy one. The worker is not scaled: it runs the outbox relay and the scheduled jobs.
#
# Terraform ignores desired_count on the services (main.tf), so an apply never resets a
# scaled-out API. Changing min/max moves the running count into the new range, which is also
# how the services are stopped (all three variables 0) and started again.

resource "aws_appautoscaling_target" "this" {
  for_each = var.deploy_services ? local.services : {}

  service_namespace  = "ecs"
  resource_id        = "service/${var.ecs_cluster}/${aws_ecs_service.this[each.key].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = each.value.min
  max_capacity       = each.value.max
}

resource "aws_appautoscaling_policy" "api_cpu" {
  count = var.deploy_services ? 1 : 0

  name               = "checkout-service-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.this["api"].service_namespace
  resource_id        = aws_appautoscaling_target.this["api"].resource_id
  scalable_dimension = aws_appautoscaling_target.this["api"].scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = var.api_cpu_target_percent
    scale_out_cooldown = 60
    scale_in_cooldown  = 120
  }
}
