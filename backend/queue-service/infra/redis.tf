# The Queue service's Redis: one EC2 instance running Redis in Docker. It was created by hand
# and adopted into this stack, so several values below are what the console chose, not choices
# of ours.
#
# All runtime state (queues, admissions) lives on the root volume, which is deleted with the
# instance, and app.env points at its private IP. Replacing the instance loses that state, so
# never accept a plan that replaces it. Changing instance_type is a stop/start: the data and
# the private IP survive, the public IP doesn't.
#
# user_data is ignored: it only runs at first boot, and what's on the instance doesn't match
# ../ops/redis/user-data.sh, so it can't rebuild this box as it stands.

data "aws_iam_policy_document" "redis_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "redis" {
  name               = "Queue-Redis"
  assume_role_policy = data.aws_iam_policy_document.redis_assume.json
}

resource "aws_iam_role_policy_attachment" "redis" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ])
  role       = aws_iam_role.redis.name
  policy_arn = each.value
}

resource "aws_iam_instance_profile" "redis" {
  name = "Queue-Redis"
  role = aws_iam_role.redis.name
}

resource "aws_security_group" "redis" {
  name = "queue-redis-sg"
  # Changing the description replaces the group, so it keeps the launch wizard's text.
  description = "launch-wizard-2 created 2026-10-20T13:59:20.826Z"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "redis_all" {
  security_group_id = aws_security_group.redis.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_instance" "redis" {
  ami                         = "ami-0567589d25e06853e"
  instance_type               = "t2.nano"
  subnet_id                   = "subnet-03d7b093b082015ab"
  vpc_security_group_ids      = [aws_security_group.redis.id]
  iam_instance_profile        = aws_iam_instance_profile.redis.name
  associate_public_ip_address = true # outbound only: the group has no public ingress
  disable_api_termination     = true

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = false # changing this replaces the instance
  }

  tags = { Name = "queue-Redis" }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [ami, user_data, user_data_base64]
  }
}
