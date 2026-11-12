terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
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
      Component = "user-service"
      ManagedBy = "opentofu"
      Source    = "backend/user-service/deploy/terraform"
    }
  }
}
