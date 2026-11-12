# checkout-service on ECS Fargate in the shared team account. Works with OpenTofu or Terraform.
# State is in the team's shared S3 bucket, like the other services; setup and the deploy
# order: README.md
terraform {
  required_version = ">= 1.10" # S3-native state locking (use_lockfile)

  # Settings come from backend.hcl (copy backend.hcl.example):
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = "ticketto"
      Component = "checkout-service"
      ManagedBy = "terraform"
      Source    = "backend/checkout-service/deploy/terraform"
    }
  }
}
