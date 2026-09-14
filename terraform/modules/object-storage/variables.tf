variable "cloud_vendor" {
  description = "Cloud adapter selector. AWS is implemented; other vendors fail explicitly."
  type        = string
  nullable    = false
  validation {
    condition     = var.cloud_vendor == "aws"
    error_message = "Only the AWS object-storage adapter is implemented. Local storage is managed by SeaweedFS deployment orchestration."
  }
}

variable "name_prefix" {
  description = "Globally unique storage/identity prefix."
  type        = string
  nullable    = false
}

variable "workloads" {
  description = "Backend names mapped to their Kubernetes namespace and service accounts."
  type = map(object({
    namespace        = string
    service_accounts = set(string)
  }))
  nullable = false
}

variable "aws_identity" {
  description = "AWS-specific identity input; future adapters must supply their own identity implementation."
  type = object({
    oidc_provider_arn = string
    oidc_issuer_url   = string
  })
  nullable = false
}

variable "versioning" {
  description = "Enable versioning only with an explicit noncurrent-version expiration policy."
  type = object({
    enabled                    = bool
    noncurrent_expiration_days = optional(number)
  })
  default  = { enabled = false }
  nullable = false
}

variable "enable_loki_ruler" {
  description = "Whether to provision the optional Loki ruler bucket."
  type        = bool
  default     = false
  nullable    = false
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
  nullable    = false
}
