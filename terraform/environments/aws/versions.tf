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
