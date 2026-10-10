provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}

locals {
  # Mirrors config/network.yaml's AWS-destined rules (destination prefixed
  # "aws-", e.g. "aws-s3"; scope alone is not the right filter since
  # "backend-object-storage" and "remote-gateway" are Docker/self-hosted
  # traffic despite not being loopback scope - see design.md).
  # tests/test_terraform_boundaries.py compares every field of every rule
  # here with the network contract, so a rule that is added, removed, or
  # changed in config/network.yaml without being mirrored fails the test
  # suite instead of silently diverging.
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

  name_prefix        = var.name_prefix
  cluster_subnet_ids = concat(module.network.public_subnet_ids, module.network.private_subnet_ids)
  node_subnet_ids    = module.network.private_subnet_ids
  # Every node carries the groups derived from the network contract.
  node_security_group_ids      = values(module.network.security_group_ids)
  endpoint_public_access       = var.endpoint_public_access
  public_access_cidrs          = var.public_access_cidrs
  cluster_admin_principal_arns = var.cluster_admin_principal_arns
  secrets_kms_key_arn          = var.secrets_kms_key_arn
  dns_controller               = var.dns_controller
  certificate_controller       = var.certificate_controller
  autoscaler_controller        = var.autoscaler_controller
  cluster_version              = var.cluster_version
  ebs_csi_addon_version        = var.ebs_csi_addon_version
  stateful_node_group          = var.stateful_node_group
  stateless_node_group         = var.stateless_node_group
  tags                         = var.tags
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

output "cluster_security_group_id" {
  description = "Security group EKS creates for the cluster."
  value       = module.eks.cluster_security_group_id
}

output "secrets_kms_key_arn" {
  description = "KMS key that encrypts Kubernetes secrets."
  value       = module.eks.secrets_kms_key_arn
}

output "controller_role_arns" {
  description = "IRSA role ARN per enabled controller, for the later workload installation."
  value       = module.eks.controller_role_arns
}

output "node_security_group_ids" {
  description = "Security groups attached to every node: the cluster's own group and the contract-derived groups."
  value       = module.eks.node_security_group_ids
}
