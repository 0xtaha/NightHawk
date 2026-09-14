mock_provider "aws" {
  override_during = plan
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_region" {
    defaults = { region = "eu-west-1" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:eu-west-1:123456789012:key/11111111-1111-1111-1111-111111111111" }
  }
}

mock_provider "aws" {
  alias           = "china"
  override_during = plan
  mock_data "aws_partition" {
    defaults = { partition = "aws-cn", dns_suffix = "amazonaws.com.cn" }
  }
  mock_data "aws_region" {
    defaults = { region = "cn-north-1" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

override_resource {
  override_during = plan
  target          = aws_iam_role.backend["mimir"]
  values          = { arn = "arn:aws:iam::123456789012:role/test-mimir-storage" }
}
override_resource {
  override_during = plan
  target          = aws_iam_role.backend["loki"]
  values          = { arn = "arn:aws:iam::123456789012:role/test-loki-storage" }
}
override_resource {
  override_during = plan
  target          = aws_iam_role.backend["tempo"]
  values          = { arn = "arn:aws:iam::123456789012:role/test-tempo-storage" }
}
override_resource {
  override_during = plan
  target          = aws_iam_role.backend["pyroscope"]
  values          = { arn = "arn:aws:iam::123456789012:role/test-pyroscope-storage" }
}

variables {
  name_prefix = "nighthawk-unit"
  aws_identity = {
    oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/TEST"
    oidc_issuer_url   = "https://oidc.eks.eu-west-1.amazonaws.com/id/TEST"
  }
  workloads = {
    mimir     = { namespace = "observability", service_accounts = ["mimir", "mimir-ruler"] }
    loki      = { namespace = "observability", service_accounts = ["loki"] }
    tempo     = { namespace = "observability", service_accounts = ["tempo"] }
    pyroscope = { namespace = "observability", service_accounts = ["pyroscope"] }
  }
}

run "secure_storage_contract" {
  command = plan

  assert {
    condition     = output.storage.provider == "aws" && length(output.storage.bindings) == 6
    error_message = "The default four-backend deployment must expose six separate buckets."
  }
  assert {
    condition = alltrue([
      for binding in values(output.storage.bindings) :
      binding.protocol == "s3" && binding.endpoint == "https://s3.eu-west-1.amazonaws.com" &&
      binding.region == "eu-west-1" && !binding.force_path_style &&
      binding.tls.enabled && binding.tls.ca_secret_ref == null &&
      binding.identity.type == "irsa" && binding.capabilities.workload_identity
    ])
    error_message = "Bindings must match the HTTPS/IRSA storage contract."
  }
  assert {
    condition     = length(toset([for binding in values(output.storage.bindings) : binding.identity.ref])) == 4
    error_message = "Each signal backend needs a distinct identity."
  }
  assert {
    condition = alltrue([
      for bucket in aws_s3_bucket.backend : !bucket.force_destroy
      ]) && alltrue([
      for block in aws_s3_bucket_public_access_block.backend :
      block.block_public_acls && block.block_public_policy && block.ignore_public_acls && block.restrict_public_buckets
    ])
    error_message = "Buckets must retain data on deletion and block public access."
  }
  assert {
    condition = alltrue([
      for controls in aws_s3_bucket_ownership_controls.backend :
      one(controls.rule).object_ownership == "BucketOwnerEnforced"
      ]) && alltrue([
      for encryption in aws_s3_bucket_server_side_encryption_configuration.backend :
      one(one(encryption.rule).apply_server_side_encryption_by_default).sse_algorithm == "aws:kms"
      ]) && alltrue([
      for key in aws_kms_key.backend : key.enable_key_rotation && key.deletion_window_in_days == 30
    ])
    error_message = "Bucket ownership and rotating KMS encryption must be enforced."
  }
  assert {
    condition = alltrue([
      for name, policy in aws_iam_role_policy.backend :
      toset(jsondecode(policy.policy).Statement[0].Resource) == toset([
        for bucket, owner in local.buckets : local.bucket_arns[bucket] if owner == name
      ]) &&
      toset(jsondecode(policy.policy).Statement[1].Resource) == toset([
        for bucket, owner in local.buckets : "${local.bucket_arns[bucket]}/*" if owner == name
      ]) &&
      !contains(jsondecode(policy.policy).Statement[1].Action, "s3:DeleteObjectVersion")
    ])
    error_message = "A backend must access only its own buckets and must not purge noncurrent versions."
  }
  assert {
    condition = length(aws_kms_key.backend) == 4 && alltrue([
      for name, policy in aws_iam_role_policy.backend :
      jsondecode(policy.policy).Statement[2].Resource == aws_kms_key.backend[name].arn &&
      toset(jsondecode(policy.policy).Statement[2].Action) == toset(["kms:Decrypt", "kms:GenerateDataKey"]) &&
      jsondecode(policy.policy).Statement[2].Condition.StringEquals["kms:ViaService"] == local.s3_host &&
      toset(jsondecode(policy.policy).Statement[2].Condition.StringLike["kms:EncryptionContext:aws:s3:arn"]) == toset(flatten([
        for bucket, owner in local.buckets :
        [local.bucket_arns[bucket], "${local.bucket_arns[bucket]}/*"] if owner == name
      ]))
    ])
    error_message = "KMS grants must be restricted to the backend key, S3 service, and assigned bucket contexts."
  }
  assert {
    condition = alltrue([
      for name, role in aws_iam_role.backend :
      jsondecode(role.assume_role_policy).Statement[0].Action == "sts:AssumeRoleWithWebIdentity" &&
      jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["${local.issuer}:aud"] == "sts.amazonaws.com" &&
      toset(jsondecode(role.assume_role_policy).Statement[0].Condition.StringEquals["${local.issuer}:sub"]) == toset([
        for account in var.workloads[name].service_accounts : "system:serviceaccount:${var.workloads[name].namespace}:${account}"
      ])
    ])
    error_message = "IRSA trust must pin both audience and exact service-account subjects."
  }
  assert {
    condition = alltrue([
      for policy in aws_s3_bucket_policy.backend :
      jsondecode(policy.policy).Statement[0].Condition.Bool["aws:SecureTransport"] == "false" &&
      contains(keys(jsondecode(policy.policy).Statement[2].Condition), "StringNotEqualsIfExists") &&
      jsondecode(policy.policy).Statement[3].Condition.Null["s3:x-amz-server-side-encryption-customer-algorithm"] == "false"
    ])
    error_message = "TLS and KMS overrides must not permit plaintext transport or alternate encryption keys."
  }
  assert {
    condition = alltrue([
      for lifecycle in aws_s3_bucket_lifecycle_configuration.backend :
      length(lifecycle.rule) == 1 &&
      one(lifecycle.rule).id == "abort-incomplete-uploads" &&
      length(one(lifecycle.rule).expiration) == 0 &&
      length(one(lifecycle.rule).transition) == 0
    ])
    error_message = "Default lifecycle may abort incomplete uploads but cannot expire/archive current telemetry."
  }
}

run "versioned_storage" {
  command = plan
  variables {
    versioning        = { enabled = true, noncurrent_expiration_days = 2 }
    enable_loki_ruler = true
  }
  assert {
    condition     = length(output.storage.bindings) == 7 && output.storage.bindings["loki-ruler"].capabilities.versioning
    error_message = "The optional ruler bucket and versioning capability must be exported."
  }
  assert {
    condition = alltrue(flatten([
      for lifecycle in aws_s3_bucket_lifecycle_configuration.backend : [
        for rule in lifecycle.rule :
        rule.id == "retired-versions" ? (
          one(rule.noncurrent_version_expiration).noncurrent_days == 2 &&
          one(rule.expiration).expired_object_delete_marker &&
          length(rule.transition) == 0 && length(rule.noncurrent_version_transition) == 0
        ) : true
      ]
    ]))
    error_message = "Versioned cleanup must use the explicit delay, without expiring or archiving current objects."
  }
}

run "missing_noncurrent_policy" {
  command = plan
  variables {
    versioning        = { enabled = true }
    enable_loki_ruler = true
  }
  expect_failures = [var.versioning]
}

run "fractional_noncurrent_policy" {
  command = plan
  variables {
    versioning        = { enabled = true, noncurrent_expiration_days = 1.5 }
    enable_loki_ruler = true
  }
  expect_failures = [var.versioning]
}

run "shared_service_account" {
  command = plan
  variables {
    workloads = {
      mimir = { namespace = "observability", service_accounts = ["shared"] }
      loki  = { namespace = "observability", service_accounts = ["shared"] }
    }
  }
  expect_failures = [var.workloads]
}

run "wildcard_subject" {
  command = plan
  variables {
    workloads = { mimir = { namespace = "observability", service_accounts = ["*"] } }
  }
  expect_failures = [var.workloads]
}

run "unknown_backend" {
  command = plan
  variables {
    workloads = { unknown = { namespace = "observability", service_accounts = ["unknown"] } }
  }
  expect_failures = [var.workloads]
}

run "invalid_prefix" {
  command = plan
  variables {
    name_prefix = "INVALID/PREFIX"
  }
  expect_failures = [var.name_prefix]
}

run "noncurrent_policy_without_versioning" {
  command = plan
  variables {
    versioning = { enabled = false, noncurrent_expiration_days = 2 }
  }
  expect_failures = [var.versioning]
}

run "empty_workloads" {
  command = plan
  variables { workloads = {} }
  expect_failures = [var.workloads]
}

run "ruler_without_loki" {
  command = plan
  variables {
    workloads         = { tempo = { namespace = "observability", service_accounts = ["tempo"] } }
    enable_loki_ruler = true
  }
  expect_failures = [var.enable_loki_ruler]
}

run "mismatched_oidc_account" {
  command = plan
  variables {
    workloads = { tempo = { namespace = "observability", service_accounts = ["tempo"] } }
    aws_identity = {
      oidc_provider_arn = "arn:aws:iam::999999999999:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/TEST"
      oidc_issuer_url   = "https://oidc.eks.eu-west-1.amazonaws.com/id/TEST"
    }
  }
  expect_failures = [aws_iam_role.backend]
}

run "mismatched_oidc_issuer" {
  command = plan
  variables {
    workloads = { tempo = { namespace = "observability", service_accounts = ["tempo"] } }
    aws_identity = {
      oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/TEST"
      oidc_issuer_url   = "https://oidc.eks.eu-west-1.amazonaws.com/id/OTHER"
    }
  }
  expect_failures = [aws_iam_role.backend]
}

run "china_partition" {
  command   = plan
  providers = { aws = aws.china }
  variables {
    workloads = { tempo = { namespace = "observability", service_accounts = ["tempo"] } }
    aws_identity = {
      oidc_provider_arn = "arn:aws-cn:iam::123456789012:oidc-provider/oidc.eks.cn-north-1.amazonaws.com.cn/id/TEST"
      oidc_issuer_url   = "https://oidc.eks.cn-north-1.amazonaws.com.cn/id/TEST"
    }
  }
  override_resource {
    target          = aws_iam_role.backend["tempo"]
    override_during = plan
    values          = { arn = "arn:aws-cn:iam::123456789012:role/test-tempo-storage" }
  }
  assert {
    condition     = output.storage.bindings["tempo-traces"].endpoint == "https://s3.cn-north-1.amazonaws.com.cn"
    error_message = "The endpoint must use the configured partition's DNS suffix."
  }
  assert {
    condition     = startswith(output.storage.bindings["tempo-traces"].identity.ref, "arn:aws-cn:")
    error_message = "Workload identity must belong to the configured AWS partition."
  }
}
