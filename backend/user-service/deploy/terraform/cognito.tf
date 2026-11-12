# Cognito pre token generation trigger: copies `cognito:groups` into a colon-free `groups` claim
# that API Gateway can map into X-User-Groups. Plan and rollout: ../../cognito-groups-plan.md
#
# This creates the function only. Attaching it to the shared pool is a one-off console step
# (plan Phase 4): `update-user-pool` resets every setting it isn't given, and OpenTofu can only
# attach triggers by owning the whole pool, which no single service should.
#
# A failing trigger blocks every sign-in on the platform. Rollback: remove the trigger in the
# console (User pool → Extensions).

locals {
  pre_token_generation_name = "cognito-pre-token-generation"
  cognito_user_pool_arn     = "arn:aws:cognito-idp:${var.region}:${local.account_id}:userpool/${var.cognito_user_pool_id}"
}

data "archive_file" "pre_token_generation" {
  type        = "zip"
  source_file = "${path.module}/../../cognito_triggers/pre_token_generation.py"
  output_path = "${path.module}/.build/pre_token_generation.zip"
}

resource "aws_iam_role" "pre_token_generation" {
  name = local.pre_token_generation_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Its own logs only: the function calls nothing else.
resource "aws_iam_role_policy_attachment" "pre_token_generation_logs" {
  role       = aws_iam_role.pre_token_generation.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_log_group" "pre_token_generation" {
  name              = "/aws/lambda/${local.pre_token_generation_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "pre_token_generation" {
  function_name    = local.pre_token_generation_name
  description      = "Cognito V2 pre token generation: adds a colon-free groups claim for API Gateway"
  role             = aws_iam_role.pre_token_generation.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "pre_token_generation.handler"
  filename         = data.archive_file.pre_token_generation.output_path
  source_code_hash = data.archive_file.pre_token_generation.output_base64sha256
  memory_size      = 128
  timeout          = 3 # Cognito waits at most 5 s before failing the sign-in

  depends_on = [
    aws_cloudwatch_log_group.pre_token_generation,
    aws_iam_role_policy_attachment.pre_token_generation_logs,
  ]
}

# Only this user pool may invoke it.
resource "aws_lambda_permission" "pre_token_generation_cognito" {
  statement_id  = "AllowCognitoUserPool"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pre_token_generation.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = local.cognito_user_pool_arn
}

# Any error means some sign-in was at risk. Shows in the CloudWatch console; add an SNS
# topic to alarm_actions to get emailed.
resource "aws_cloudwatch_metric_alarm" "pre_token_generation_errors" {
  alarm_name          = "${local.pre_token_generation_name}-errors"
  alarm_description   = "The Cognito pre token generation trigger failed: sign-ins may be blocked. Rollback: remove the trigger (User pool → Extensions)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.pre_token_generation.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

output "pre_token_generation_function" {
  description = "Pick this in Cognito → User pool → Extensions → Pre token generation (event version V2_0)."
  value       = aws_lambda_function.pre_token_generation.function_name
}
