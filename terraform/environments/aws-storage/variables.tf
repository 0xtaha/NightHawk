variable "region" {
  description = "AWS region for telemetry storage."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.region))
    error_message = "Specify an AWS region such as eu-west-1."
  }
}

variable "account_id" {
  description = "Expected AWS account; guards against deployment using the wrong credentials."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must contain exactly 12 digits."
  }
}

variable "name_prefix" {
  description = "Globally unique bucket and IAM role prefix."
  type        = string
  nullable    = false
}

variable "workloads" {
  description = "Explicit backend namespaces/service accounts, matching the workload deployment."
  type = map(object({
    namespace        = string
    service_accounts = set(string)
  }))
  nullable = false
}

variable "aws_identity" {
  description = "Existing EKS OIDC provider in the expected account."
  type = object({
    oidc_provider_arn = string
    oidc_issuer_url   = string
  })
  nullable = false
}

variable "versioning" {
  description = "Explicit telemetry versioning policy; no implicit backup retention."
  type = object({
    enabled                    = bool
    noncurrent_expiration_days = optional(number)
  })
  nullable = false
}

variable "enable_loki_ruler" {
  description = "Provision Loki rule storage only if Loki rulers will be deployed."
  type        = bool
  nullable    = false
}

variable "tags" {
  description = "Environment-specific tags."
  type        = map(string)
  default     = {}
  nullable    = false
}
