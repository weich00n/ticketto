# What happens to each email after SES accepts it. send_email only says SES took it, so without
# this an email that never arrives leaves no trace. The worker sends through this configuration
# set, SES publishes the email's events to EventBridge, and this rule keeps them in CloudWatch
# Logs. Find an email's events with the ses_message_id from its "email sent" log line.

data "aws_cloudwatch_event_bus" "default" {
  name = "default" # the only bus SES can publish to
}

resource "aws_sesv2_configuration_set" "tracking" {
  configuration_set_name = var.name
}

resource "aws_sesv2_configuration_set_event_destination" "eventbridge" {
  configuration_set_name = aws_sesv2_configuration_set.tracking.configuration_set_name
  event_destination_name = "eventbridge"

  event_destination {
    enabled = true
    matching_event_types = [
      "SEND", "REJECT", "RENDERING_FAILURE", "DELIVERY", "DELIVERY_DELAY", "BOUNCE", "COMPLAINT",
    ]

    event_bridge_destination {
      event_bus_arn = data.aws_cloudwatch_event_bus.default.arn
    }
  }
}

# The events include recipients' addresses, so they aren't kept for long. EventBridge can only
# write to log groups named /aws/events/...
resource "aws_cloudwatch_log_group" "email_events" {
  name              = "/aws/events/${var.name}-emails"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_resource_policy" "email_events" {
  policy_name = "${var.name}-email-events"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.email_events.arn}:*"
    }]
  })
}

resource "aws_cloudwatch_event_rule" "email_events" {
  name        = "${var.name}-email-events"
  description = "SES events for emails sent through the ${var.name} configuration set"
  event_pattern = jsonencode({
    source = ["aws.ses"]
    detail = { mail = { tags = { "ses:configuration-set" = [var.name] } } }
  })
}

resource "aws_cloudwatch_event_target" "email_events" {
  rule = aws_cloudwatch_event_rule.email_events.name
  arn  = aws_cloudwatch_log_group.email_events.arn
}
