variable "name_prefix" {
  description = "Globally unique bucket prefix; also used for IAM role names."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-32 lowercase letters/digits/hyphens, start with a letter, and end with a letter or digit."
  }
}

variable "workloads" {
  description = "Backend to namespace/service-account bindings. Each backend gets its own IRSA role and buckets."
  type = map(object({
    namespace        = string
    service_accounts = set(string)
  }))
  nullable = false

  validation {
    condition = try(
      length(var.workloads) > 0 &&
      alltrue([
        for name, workload in var.workloads :
        contains(["mimir", "loki", "tempo", "pyroscope"], name) &&
        can(regex("^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$", workload.namespace)) &&
        length(workload.service_accounts) > 0 &&
        alltrue([
          for account in workload.service_accounts :
          can(regex("^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$", account))
        ])
      ]),
      false
    )
    error_message = "Specify supported backend names with a DNS-label namespace and at least one DNS-label service account (1-63 characters, no wildcards)."
  }

  validation {
    condition = try(
      length(flatten([
        for workload in values(var.workloads) : [
          for account in workload.service_accounts : "${workload.namespace}/${account}"
        ]
        ])) == length(toset(flatten([
          for workload in values(var.workloads) : [
            for account in workload.service_accounts : "${workload.namespace}/${account}"
          ]
      ]))),
      false
    )
    error_message = "A Kubernetes service account must not be shared by different backends."
  }
}

variable "aws_identity" {
  description = "Existing EKS IAM OIDC provider ARN and its HTTPS issuer URL, in the current AWS account."
  type = object({
    oidc_provider_arn = string
    oidc_issuer_url   = string
  })
  nullable = false

  validation {
    condition = (
      can(regex("^arn:aws(-us-gov|-cn)?:iam::[0-9]{12}:oidc-provider/oidc\\.eks\\.[a-z0-9-]+\\.amazonaws\\.com(\\.cn)?/id/[A-Za-z0-9]+$", var.aws_identity.oidc_provider_arn)) &&
      can(regex("^https://oidc\\.eks\\.[a-z0-9-]+\\.amazonaws\\.com(\\.cn)?/id/[A-Za-z0-9]+$", var.aws_identity.oidc_issuer_url))
    )
    error_message = "aws_identity must contain an EKS OIDC provider ARN and its HTTPS issuer URL without a trailing slash."
  }
}

variable "versioning" {
  description = "Explicit policy for noncurrent data. Current telemetry objects are never expired by S3 lifecycle."
  type = object({
    enabled                    = bool
    noncurrent_expiration_days = optional(number)
  })
  default  = { enabled = false }
  nullable = false

  validation {
    condition = var.versioning.enabled ? try(
      var.versioning.noncurrent_expiration_days >= 1 &&
      floor(var.versioning.noncurrent_expiration_days) == var.versioning.noncurrent_expiration_days,
      false
    ) : var.versioning.noncurrent_expiration_days == null
    error_message = "Versioning requires explicit positive whole noncurrent_expiration_days; omit that value when versioning is disabled."
  }
}

variable "enable_loki_ruler" {
  description = "Create an additional Loki ruler bucket when Loki is enabled."
  type        = bool
  default     = false
  nullable    = false

  validation {
    condition     = !var.enable_loki_ruler || contains(keys(var.workloads), "loki")
    error_message = "enable_loki_ruler requires a Loki workload."
  }
}

variable "tags" {
  description = "Additional tags on managed AWS resources."
  type        = map(string)
  default     = {}
  nullable    = false
}
