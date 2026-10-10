output "oidc_provider_arn" {
  description = "IAM OIDC provider ARN; matches aws-s3-backends' aws_identity.oidc_provider_arn exactly."
  value       = aws_iam_openid_connect_provider.this.arn
}

output "oidc_issuer_url" {
  description = "HTTPS OIDC issuer URL; matches aws-s3-backends' aws_identity.oidc_issuer_url exactly."
  value       = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  description = "EKS cluster API server endpoint."
  value       = aws_eks_cluster.this.endpoint
}

output "cluster_security_group_id" {
  description = "Security group EKS creates for the cluster; attached to every node."
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

output "secrets_kms_key_arn" {
  description = "KMS key that encrypts Kubernetes secrets: the operator's, or the one this module created."
  value       = local.secrets_key_arn
}

output "controller_role_arns" {
  description = "IRSA role ARN per enabled controller (ebs-csi always; dns, certificate, autoscaler when enabled), to annotate each controller's service account with when it is installed."
  value       = { for name, role in aws_iam_role.controller : name => role.arn }
}

output "node_security_group_ids" {
  description = "Security groups attached to every node: the cluster's own group, then the contract-derived groups."
  value       = local.node_security_group_ids
}
