# FIS role, the abort switch and the experiment templates.
#
#   C1  stop the RabbitMQ broker for broker_stop_duration   (FIS starts it again)
#   C2  reboot reservation-service's Redis instance
#   C3  stop every reservation-service task
#   C4  stop queue-admitter
#   C5  stop checkout-service-worker
#   C6  stop ticket-service-worker
#   C7  stop one of the reservation-service tasks
#   C8  stop every checkout-service API task
#
#   aws fis start-experiment --experiment-template-id "$(tofu output -json experiment_templates | jq -r .c1)"
#   aws fis stop-experiment --id <experiment id>
#
# Nothing runs until someone starts it. Every template stops on its own when the abort alarm fires.

# ---------- FIS role ----------

resource "aws_iam_role" "fis" {
  name = "chaos-engineering-fis"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "fis.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "fis" {
  name = "run-chaos-experiments"
  role = aws_iam_role.fis.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Finding the targets can't be limited to a resource.
        Sid      = "FindTargets"
        Effect   = "Allow"
        Action   = ["ecs:ListTasks", "ecs:DescribeTasks", "ecs:DescribeServices", "ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Sid    = "StopTasks"
        Effect = "Allow"
        Action = "ecs:StopTask"
        Resource = [
          "arn:aws:ecs:${var.region}:${local.account_id}:task/${var.ecs_cluster_name}/*",
        ]
      },
      {
        Sid      = "StopAndStartBroker"
        Effect   = "Allow"
        Action   = ["ec2:StopInstances", "ec2:StartInstances"]
        Resource = data.aws_instance.broker.arn
      },
      {
        Sid      = "RebootReservationRedis"
        Effect   = "Allow"
        Action   = "ec2:RebootInstances"
        Resource = data.aws_instance.reservation_redis.arn
      },
      {
        # The broker's volume is KMS-encrypted; starting it again needs a grant for EC2.
        Sid       = "StartEncryptedBroker"
        Effect    = "Allow"
        Action    = "kms:CreateGrant"
        Resource  = "*"
        Condition = { Bool = { "kms:GrantIsForAWSResource" = "true" } }
      },
      {
        Sid      = "WriteExperimentLog"
        Effect   = "Allow"
        Action   = ["logs:CreateLogDelivery", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "fis" {
  name              = "/fis/chaos-engineering"
  retention_in_days = 30
}

# ---------- abort switch ----------

# No service has a load balancer to count 5xx on, so count ERROR log lines across all of them.
resource "aws_cloudwatch_log_metric_filter" "errors" {
  for_each = toset(var.watched_log_groups)

  name           = "chaos-errors"
  log_group_name = each.value
  pattern        = "ERROR"

  metric_transformation {
    name          = "ErrorLogLines"
    namespace     = "Ticketto/chaos"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "abort" {
  alarm_name          = "chaos-engineering-abort"
  alarm_description   = "Stops any running chaos experiment: too many ERROR lines across the platform."
  namespace           = "Ticketto/chaos"
  metric_name         = "ErrorLogLines"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  threshold           = var.abort_error_threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  depends_on = [aws_cloudwatch_log_metric_filter.errors]
}

# ---------- experiments ----------

locals {
  # Experiments that stop tasks of one ECS service: all of them, or COUNT(n). ECS starts
  # replacements straight away.
  stop_service = {
    c3 = { cluster = var.ecs_cluster_name, service = "reservation-service", tasks = "ALL", description = "C3: stop every reservation-service task" }
    c4 = { cluster = var.ecs_cluster_name, service = "queue-admitter", tasks = "ALL", description = "C4: stop queue-admitter" }
    c5 = { cluster = var.ecs_cluster_name, service = "checkout-service-worker", tasks = "ALL", description = "C5: stop checkout-service-worker" }
    c6 = { cluster = var.ecs_cluster_name, service = "ticket-service-worker", tasks = "ALL", description = "C6: stop ticket-service-worker" }
    c7 = { cluster = var.ecs_cluster_name, service = "reservation-service", tasks = "COUNT(1)", description = "C7: stop one reservation-service task" }
    c8 = { cluster = var.ecs_cluster_name, service = "checkout-service", tasks = "ALL", description = "C8: stop every checkout-service API task" }
  }
}

resource "aws_fis_experiment_template" "stop_service" {
  for_each = local.stop_service

  description = each.value.description
  role_arn    = aws_iam_role.fis.arn
  tags        = { Name = "chaos-${each.key}" }

  stop_condition {
    source = "aws:cloudwatch:alarm"
    value  = aws_cloudwatch_metric_alarm.abort.arn
  }

  target {
    name           = "Tasks"
    resource_type  = "aws:ecs:task"
    selection_mode = each.value.tasks
    parameters = {
      cluster = each.value.cluster
      service = each.value.service
    }
  }

  action {
    name      = "stop-tasks"
    action_id = "aws:ecs:stop-task"
    target {
      key   = "Tasks"
      value = "Tasks"
    }
  }

  log_configuration {
    log_schema_version = 2
    cloudwatch_logs_configuration {
      log_group_arn = "${aws_cloudwatch_log_group.fis.arn}:*"
    }
  }
}

resource "aws_fis_experiment_template" "stop_broker" {
  description = "C1: stop the RabbitMQ broker for ${var.broker_stop_duration}"
  role_arn    = aws_iam_role.fis.arn
  tags        = { Name = "chaos-c1" }

  stop_condition {
    source = "aws:cloudwatch:alarm"
    value  = aws_cloudwatch_metric_alarm.abort.arn
  }

  target {
    name           = "Broker"
    resource_type  = "aws:ec2:instance"
    selection_mode = "ALL"
    resource_arns  = [data.aws_instance.broker.arn]
  }

  # FIS starts the broker again after the duration, and also when the experiment is stopped early.
  action {
    name      = "stop-broker"
    action_id = "aws:ec2:stop-instances"
    parameter {
      key   = "startInstancesAfterDuration"
      value = var.broker_stop_duration
    }
    target {
      key   = "Instances"
      value = "Broker"
    }
  }

  log_configuration {
    log_schema_version = 2
    cloudwatch_logs_configuration {
      log_group_arn = "${aws_cloudwatch_log_group.fis.arn}:*"
    }
  }
}

resource "aws_fis_experiment_template" "reboot_reservation_redis" {
  description = "C2: reboot ${var.reservation_redis_instance_name}"
  role_arn    = aws_iam_role.fis.arn
  tags        = { Name = "chaos-c2" }

  stop_condition {
    source = "aws:cloudwatch:alarm"
    value  = aws_cloudwatch_metric_alarm.abort.arn
  }

  target {
    name           = "Redis"
    resource_type  = "aws:ec2:instance"
    selection_mode = "ALL"
    resource_arns  = [data.aws_instance.reservation_redis.arn]
  }

  action {
    name      = "reboot-redis"
    action_id = "aws:ec2:reboot-instances"
    target {
      key   = "Instances"
      value = "Redis"
    }
  }

  log_configuration {
    log_schema_version = 2
    cloudwatch_logs_configuration {
      log_group_arn = "${aws_cloudwatch_log_group.fis.arn}:*"
    }
  }
}
