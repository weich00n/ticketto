# One screen to watch during an experiment: what buyers see (5xx, latency at the shared ALB), how
# many tasks are healthy, how hard each service works, and the abort switch.
#
#   https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards/dashboard/chaos-engineering
#
# Every API is behind the shared internal ALB (one target group each) since 2026-11-04, so 5xx and
# healthy-host counts come from there. Workers have no target group: their CPU is the signal.

data "aws_lb" "internal" {
  name = var.internal_alb_name
}

data "aws_lb_target_group" "api" {
  for_each = var.dashboard_target_groups
  name     = each.value
}

locals {
  lb_dim = data.aws_lb.internal.arn_suffix

  # One line per API, all on the same widget.
  per_api = {
    for metric, stat in {
      HTTPCode_Target_5XX_Count = "Sum"
      TargetResponseTime        = "p95"
      HealthyHostCount          = "Minimum"
      } : metric => [
      for svc, tg in data.aws_lb_target_group.api :
      ["AWS/ApplicationELB", metric, "TargetGroup", tg.arn_suffix, "LoadBalancer", local.lb_dim, { label = svc, stat = stat }]
    ]
  }

  per_service = {
    for metric in ["CPUUtilization", "MemoryUtilization"] : metric => [
      for svc in var.dashboard_ecs_services :
      ["AWS/ECS", metric, "ClusterName", var.ecs_cluster_name, "ServiceName", svc, { label = svc }]
    ]
  }

  widget = {
    type   = "metric"
    width  = 12
    height = 6
  }
}

resource "aws_cloudwatch_dashboard" "chaos" {
  dashboard_name = "chaos-engineering"
  dashboard_body = jsonencode({
    widgets = [
      merge(local.widget, { x = 0, y = 0, properties = {
        title   = "API 5xx a minute (what buyers see)", region = var.region, view = "timeSeries", period = 60,
        metrics = local.per_api.HTTPCode_Target_5XX_Count
      } }),
      merge(local.widget, { x = 12, y = 0, properties = {
        title   = "API response time p95 (s)", region = var.region, view = "timeSeries", period = 60,
        metrics = local.per_api.TargetResponseTime
      } }),
      merge(local.widget, { x = 0, y = 6, properties = {
        title   = "Healthy API tasks (dips when FIS stops one)", region = var.region, view = "timeSeries", period = 60,
        metrics = local.per_api.HealthyHostCount, yAxis = { left = { min = 0 } }
      } }),
      merge(local.widget, { x = 12, y = 6, properties = {
        title       = "ERROR log lines a minute vs the abort threshold", region = var.region, view = "timeSeries", period = 60,
        metrics     = [["Ticketto/chaos", "ErrorLogLines", { stat = "Sum", label = "all watched services" }]],
        annotations = { horizontal = [{ label = "abort", value = var.abort_error_threshold }] }
      } }),
      merge(local.widget, { x = 0, y = 12, properties = {
        title   = "ECS CPU % (workers included)", region = var.region, view = "timeSeries", period = 60,
        metrics = local.per_service.CPUUtilization
      } }),
      merge(local.widget, { x = 12, y = 12, properties = {
        title   = "ECS memory %", region = var.region, view = "timeSeries", period = 60,
        metrics = local.per_service.MemoryUtilization
      } }),
      { type = "alarm", x = 0, y = 18, width = 12, height = 3, properties = {
        title = "Abort switch", alarms = [aws_cloudwatch_metric_alarm.abort.arn]
      } },
      { type = "log", x = 12, y = 18, width = 12, height = 6, properties = {
        title = "FIS: what each experiment did", region = var.region, view = "table",
        query = "SOURCE '${aws_cloudwatch_log_group.fis.name}' | fields event_timestamp, id, log_type, details.action_name, coalesce(details.action_state.status, details.experiment_state.status) as status | sort event_timestamp desc | limit 50"
      } },
    ]
  })
}
