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
      Component = var.name
      ManagedBy = "opentofu"
      Source    = "backend/reservation-service/deploy/terraform"
    }
  }
}
