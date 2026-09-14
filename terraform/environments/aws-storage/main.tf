terraform {
  required_version = "= 1.13.5"
  backend "s3" {}
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.12.0"
    }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}

module "storage" {
  source = "../../modules/object-storage"

  cloud_vendor      = "aws"
  name_prefix       = var.name_prefix
  workloads         = var.workloads
  aws_identity      = var.aws_identity
  versioning        = var.versioning
  enable_loki_ruler = var.enable_loki_ruler
  tags              = var.tags
}

output "storage" {
  description = "Export with terraform output -json storage for the NightHawk renderer."
  value       = module.storage.storage
}

output "role_arns" {
  description = "Apply as eks.amazonaws.com/role-arn annotations to the declared service accounts."
  value       = module.storage.role_arns
}
