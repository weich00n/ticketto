# This service's Redis: one EC2 instance running Redis in Docker. It was created by hand and
# adopted into this stack (import.tf), so several values below are what the console chose, not
# choices of ours.
#
# Replacing the instance loses whatever is on its root volume, which is deleted with it, and
# moves its private IP, so never accept a plan that replaces it. Its role and instance profile
# (redisReservationDefault) are still not managed here.
#
# user_data is ignored: it only runs at first boot. ../redis/ec2-user-data.sh is what to paste
# when building a new box.

resource "aws_security_group" "redis" {
  name = var.redis_security_group_name
  # Changing the description replaces the group, so it keeps the original text, typo included.
  description = "security group for reservation redid"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_egress_rule" "redis_all" {
  security_group_id = aws_security_group.redis.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "redisinsight" {
  security_group_id = aws_security_group.redis.id
  description       = "RedisInsight"
  ip_protocol       = "tcp"
  from_port         = 5540
  to_port           = 5540
  cidr_ipv4         = var.redisinsight_cidr
}

resource "aws_instance" "redis" {
  ami                         = "ami-0567589d25e06853e"
  instance_type               = "t2.nano"
  subnet_id                   = "subnet-03d7b093b082015ab"
  vpc_security_group_ids      = [aws_security_group.redis.id]
  iam_instance_profile        = "redisReservationDefault"
  associate_public_ip_address = true # changing this replaces the instance

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = false # changing this replaces the instance
  }

  tags = { Name = var.redis_instance_name }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [ami, user_data, user_data_base64]
  }
}
