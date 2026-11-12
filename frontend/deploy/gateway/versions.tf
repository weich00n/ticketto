terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {}
}

provider "aws" {
  region = "us-east-1"
  default_tags {
    tags = {
      Project   = "ticketto"
      Component = "shared-gateway"
      ManagedBy = "opentofu"
      Source    = "frontend/deploy/gateway"
    }
  }
}
