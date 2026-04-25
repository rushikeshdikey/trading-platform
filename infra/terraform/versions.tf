terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  # Local state for v1. Switch to S3 backend (s3 + dynamodb-lock) once you
  # have more than one operator or want remote state. See infra/terraform/README.md.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "trading-journal"
      ManagedBy = "terraform"
      Owner     = "rushikesh"
    }
  }
}
