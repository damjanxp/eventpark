terraform {
  required_version = ">= 1.11"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Chicken and egg: this stack CREATES the state bucket, so its first apply uses local state.
  # After the first apply, uncomment this block and run `terraform init -migrate-state` (README, step 4):
  # Terraform copies the local state into the bucket it just made.
  # backend "s3" {
  #   bucket       = "eventpark-tfstate-<account-id>"   # = output state_bucket
  #   key          = "bootstrap/terraform.tfstate"
  #   region       = "eu-central-1"
  #   use_lockfile = true                              # native S3 locking (no DynamoDB table)
  #   encrypt      = true
  # }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "eventpark", Env = "shared", ManagedBy = "terraform", Stack = "bootstrap" }
  }
}
