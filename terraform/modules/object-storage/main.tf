module "aws" {
  source = "../aws-s3-backends"

  name_prefix       = var.name_prefix
  workloads         = var.workloads
  aws_identity      = var.aws_identity
  versioning        = var.versioning
  enable_loki_ruler = var.enable_loki_ruler
  tags              = var.tags
}
