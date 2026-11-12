# Renamed when the Lambdas went. These keep the existing resources, so nothing is replaced.

moved {
  from = aws_security_group.lambda
  to   = aws_security_group.worker
}

moved {
  from = aws_vpc_security_group_egress_rule.lambda_all
  to   = aws_vpc_security_group_egress_rule.worker_all
}

moved {
  from = aws_vpc_security_group_ingress_rule.user_service_from_lambda
  to   = aws_vpc_security_group_ingress_rule.user_service_from_worker
}

# The rule became one per port when the worker moved to user-service's internal port 8001.
moved {
  from = aws_vpc_security_group_ingress_rule.user_service_from_worker
  to   = aws_vpc_security_group_ingress_rule.user_service_from_worker["8000"]
}

moved {
  from = aws_vpc_security_group_ingress_rule.rds_from_lambda
  to   = aws_vpc_security_group_ingress_rule.rds_from_worker
}
