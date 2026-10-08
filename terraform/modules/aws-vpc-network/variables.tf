variable "name_prefix" {
  description = "Prefix applied to VPC, subnet, and security-group names."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-32 lowercase letters/digits/hyphens, start with a letter, and end with a letter or digit."
  }
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
  nullable    = false

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block."
  }
}

variable "az_count" {
  description = "Number of distinct availability zones to span."
  type        = number
  default     = 2
  nullable    = false

  validation {
    condition     = var.az_count >= 2 && floor(var.az_count) == var.az_count
    error_message = "az_count must be a whole number of at least 2 for a multi-AZ VPC."
  }
}

variable "network_rules" {
  description = <<-EOT
    Network-contract rules (mirrors config/network.yaml's schema) that this
    module derives security-group rules from. Only AWS-relevant rules
    (scope != "loopback") may be passed here; the calling root is
    responsible for filtering config/network.yaml down to this list.
    Currently the only supported destination is "aws-s3" (private access to
    the S3 Gateway Endpoint); any other destination fails validation until
    this module's mapping is extended.
  EOT
  type = list(object({
    id          = string
    source      = string
    destination = string
    protocol    = string
    port        = number
    scope       = string
  }))
  nullable = false

  validation {
    condition     = alltrue([for rule in var.network_rules : rule.scope != "loopback"])
    error_message = "network_rules must not include loopback-scope rules; those describe Docker/self-hosted traffic this module never provisions."
  }

  validation {
    condition     = alltrue([for rule in var.network_rules : contains(["aws-s3"], rule.destination)])
    error_message = "network_rules destination must be a supported AWS target (currently only \"aws-s3\"); extend this module's mapping before adding new destinations."
  }

  validation {
    condition     = alltrue([for rule in var.network_rules : contains(["tcp", "udp"], rule.protocol)])
    error_message = "network_rules protocol must be \"tcp\" or \"udp\"."
  }
}

variable "tags" {
  description = "Additional tags on managed AWS resources."
  type        = map(string)
  default     = {}
  nullable    = false
}
