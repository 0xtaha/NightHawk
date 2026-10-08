provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}

locals {
  # Mirrors config/network.yaml's AWS-destined rules (destination prefixed
  # "aws-", e.g. "aws-s3"; scope alone is not the right filter since
  # "backend-object-storage" and "remote-gateway" are Docker/self-hosted
  # traffic despite not being loopback scope - see design.md).
  # tests/test_terraform_boundaries.py asserts this literal's rule count
  # stays equal to the network contract's AWS-scoped rule count, so a
  # future rule added to config/network.yaml that isn't mirrored here fails
  # the test suite instead of silently under-provisioning security groups.
  network_rules = [
    {
      id          = "aws-object-storage"
      source      = "signal-backend"
      destination = "aws-s3"
      protocol    = "tcp"
      port        = 443
      scope       = "private"
    },
  ]
}

module "network" {
  source = "../../modules/aws-vpc-network"

  name_prefix   = var.name_prefix
  vpc_cidr      = var.vpc_cidr
  az_count      = var.az_count
  network_rules = local.network_rules
  tags          = var.tags
}

module "eks" {
  source = "../../modules/aws-eks"

  name_prefix           = var.name_prefix
  vpc_id                = module.network.vpc_id
  cluster_subnet_ids    = concat(module.network.public_subnet_ids, module.network.private_subnet_ids)
  node_subnet_ids       = module.network.private_subnet_ids
  cluster_version       = var.cluster_version
  ebs_csi_addon_version = var.ebs_csi_addon_version
  stateful_node_group   = var.stateful_node_group
  stateless_node_group  = var.stateless_node_group
  tags                  = var.tags
}

output "vpc_id" {
  description = "ID of the managed VPC."
  value       = module.network.vpc_id
}

output "public_subnet_ids" {
  description = "Public subnet IDs."
  value       = module.network.public_subnet_ids
}

output "private_subnet_ids" {
  description = "Private subnet IDs."
  value       = module.network.private_subnet_ids
}

output "security_group_ids" {
  description = "Security-group ID per network-contract source identity."
  value       = module.network.security_group_ids
}

output "oidc_provider_arn" {
  description = "Copy into aws-storage's aws_identity.oidc_provider_arn."
  value       = module.eks.oidc_provider_arn
}

output "oidc_issuer_url" {
  description = "Copy into aws-storage's aws_identity.oidc_issuer_url."
  value       = module.eks.oidc_issuer_url
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS cluster API server endpoint."
  value       = module.eks.cluster_endpoint
}
