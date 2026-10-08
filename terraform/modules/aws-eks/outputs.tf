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
