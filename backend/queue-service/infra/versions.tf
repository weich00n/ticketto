terraform {
  required_version = ">= 1.8" # OpenTofu

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State is a local file (terraform.tfstate in this directory). It is gitignored
  # and holds resource IDs; keep a backup, since losing it means re-importing.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      service     = "queue"
      environment = var.env
      managed-by  = "opentofu"
    }
  }
}
