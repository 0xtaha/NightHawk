output "storage" {
  description = "Non-secret NightHawk storage bindings, consumable with --storage-output."
  value = {
    provider = "aws"
    bindings = {
      for bucket, backend in local.buckets : bucket => {
        protocol         = "s3"
        endpoint         = "https://${local.s3_host}"
        region           = data.aws_region.current.region
        bucket           = aws_s3_bucket.backend[bucket].bucket
        force_path_style = false
        tls = {
          enabled       = true
          ca_secret_ref = null
        }
        identity = {
          type = "irsa"
          ref  = aws_iam_role.backend[backend].arn
        }
        capabilities = {
          versioning        = var.versioning.enabled
          lifecycle         = true
          workload_identity = true
        }
      }
    }
  }
  depends_on = [
    aws_iam_role_policy.backend,
    aws_s3_bucket_policy.backend,
    aws_s3_bucket_server_side_encryption_configuration.backend,
    aws_s3_bucket_ownership_controls.backend,
    aws_s3_bucket_lifecycle_configuration.backend
  ]
}

output "role_arns" {
  description = "Backend role ARNs for service-account annotations."
  value       = { for backend, role in aws_iam_role.backend : backend => role.arn }
}
