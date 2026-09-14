data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  backend_buckets = {
    mimir     = ["mimir-blocks", "mimir-ruler", "mimir-alertmanager"]
    loki      = var.enable_loki_ruler ? ["loki-chunks", "loki-ruler"] : ["loki-chunks"]
    tempo     = ["tempo-traces"]
    pyroscope = ["pyroscope-profiles"]
  }
  buckets = merge([
    for backend in keys(var.workloads) : {
      for bucket in lookup(local.backend_buckets, backend, []) : bucket => backend
    }
  ]...)
  bucket_names = { for bucket in keys(local.buckets) : bucket => "${var.name_prefix}-${bucket}" }
  bucket_arns  = { for bucket, name in local.bucket_names : bucket => "arn:${data.aws_partition.current.partition}:s3:::${name}" }
  issuer       = trimprefix(var.aws_identity.oidc_issuer_url, "https://")
  s3_host      = "s3.${data.aws_region.current.region}.${data.aws_partition.current.dns_suffix}"
  tags         = merge(var.tags, { ManagedBy = "terraform", Platform = "nighthawk" })
}

resource "aws_kms_key" "backend" {
  for_each                = var.workloads
  description             = "NightHawk ${each.key} object storage"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags

  # The account principal enables IAM delegation for this key, not other keys.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AccountIAMDelegation"
      Effect    = "Allow"
      Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root" }
      Action    = "kms:*"
      Resource  = "*"
    }]
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket" "backend" {
  for_each      = local.buckets
  bucket        = local.bucket_names[each.key]
  force_destroy = false
  tags          = merge(local.tags, { Backend = each.value })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "backend" {
  for_each                = local.buckets
  bucket                  = aws_s3_bucket.backend[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "backend" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.backend[each.key].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backend" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.backend[each.key].id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.backend[each.value].arn
    }
  }
}

resource "aws_s3_bucket_versioning" "backend" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.backend[each.key].id
  versioning_configuration {
    status = var.versioning.enabled ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backend" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.backend[each.key].id

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  dynamic "rule" {
    for_each = var.versioning.enabled ? [var.versioning.noncurrent_expiration_days] : []
    content {
      id     = "retired-versions"
      status = "Enabled"
      filter {}
      noncurrent_version_expiration {
        noncurrent_days = rule.value
      }
      expiration {
        expired_object_delete_marker = true
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.backend]
}

resource "aws_s3_bucket_policy" "backend" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.backend[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.bucket_arns[each.key], "${local.bucket_arns[each.key]}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "DenyExplicitNonKMS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${local.bucket_arns[each.key]}/*"
        Condition = {
          Null            = { "s3:x-amz-server-side-encryption" = "false" }
          StringNotEquals = { "s3:x-amz-server-side-encryption" = "aws:kms" }
        }
      },
      {
        Sid       = "DenyExplicitWrongKMSKey"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${local.bucket_arns[each.key]}/*"
        Condition = {
          StringEquals            = { "s3:x-amz-server-side-encryption" = "aws:kms" }
          StringNotEqualsIfExists = { "s3:x-amz-server-side-encryption-aws-kms-key-id" = aws_kms_key.backend[each.value].arn }
        }
      },
      {
        Sid       = "DenyCustomerProvidedEncryptionKeys"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${local.bucket_arns[each.key]}/*"
        Condition = { Null = { "s3:x-amz-server-side-encryption-customer-algorithm" = "false" } }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.backend]
}

resource "aws_iam_role" "backend" {
  for_each = var.workloads
  name     = "${var.name_prefix}-${each.key}-storage"
  tags     = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = var.aws_identity.oidc_provider_arn }
      Condition = {
        StringEquals = {
          "${local.issuer}:aud" = "sts.amazonaws.com"
          "${local.issuer}:sub" = [
            for account in sort(tolist(each.value.service_accounts)) :
            "system:serviceaccount:${each.value.namespace}:${account}"
          ]
        }
      }
    }]
  })

  lifecycle {
    precondition {
      condition     = var.aws_identity.oidc_provider_arn == "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/${local.issuer}"
      error_message = "The OIDC provider must match the issuer, AWS partition, and account used by this module."
    }
  }
}

resource "aws_iam_role_policy" "backend" {
  for_each = var.workloads
  name     = "object-storage"
  role     = aws_iam_role.backend[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "BucketMetadata"
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"]
        Resource = [for bucket, owner in local.buckets : local.bucket_arns[bucket] if owner == each.key]
      },
      {
        Sid      = "TelemetryObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = [for bucket, owner in local.buckets : "${local.bucket_arns[bucket]}/*" if owner == each.key]
      },
      {
        Sid      = "BucketEncryption"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.backend[each.key].arn
        Condition = {
          StringEquals = { "kms:ViaService" = local.s3_host }
          StringLike = {
            "kms:EncryptionContext:aws:s3:arn" = flatten([
              for bucket, owner in local.buckets :
              [local.bucket_arns[bucket], "${local.bucket_arns[bucket]}/*"] if owner == each.key
            ])
          }
        }
      }
    ]
  })
}
