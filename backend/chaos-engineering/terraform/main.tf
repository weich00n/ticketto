# Chaos experiments for the Ticketto platform, run with AWS FIS (see ../PLAN.md).
#
#   FIS experiment ──► stop tasks / stop the broker / reboot Redis ──► services degrade
#   driver (laptop) ──► buyers through the API Gateway ──► outcome per buyer
#   checker (laptop, SSM tunnels) ──► MySQL + Redis invariants after each run
#   dashboard.tf ──► one CloudWatch screen to watch a run
#
# Looked up, never managed here: the VPC, the cluster, the services and their log groups, the
# broker and Redis instances, the internal ALB. The driver's security group is left from a plan to
# run the driver inside the VPC; nothing uses it now.

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
}

data "aws_vpc" "default" {
  default = true
}

# The default VPC's subnets are all public and it has no NAT, so tasks get a public IP to reach
# ECR and CloudWatch. Nothing comes in that way: the driver's security group has no ingress.
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_instance" "broker" {
  filter {
    name   = "tag:Name"
    values = [var.broker_instance_name]
  }
  filter {
    name   = "instance-state-name"
    values = ["running", "stopped"]
  }
}

data "aws_instance" "reservation_redis" {
  filter {
    name   = "tag:Name"
    values = [var.reservation_redis_instance_name]
  }
  filter {
    name   = "instance-state-name"
    values = ["running", "stopped"]
  }
}

# ---------- driver ----------

# Created now so teammates can reference it in their ingress rules; the task definition that
# uses it comes with the driver script.
resource "aws_security_group" "driver" {
  name        = "chaos-driver"
  description = "k6 chaos driver: outbound only"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = "chaos-driver" }
}

resource "aws_vpc_security_group_egress_rule" "driver_all" {
  security_group_id = aws_security_group.driver.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}
