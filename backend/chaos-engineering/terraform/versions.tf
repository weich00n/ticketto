terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Settings come from backend.hcl (copy backend.hcl.example):
  #   tofu init -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "ticketto"
      Component = "chaos-engineering"
      ManagedBy = "opentofu"
      Source    = "backend/chaos-engineering/terraform"
    }
  }
}
