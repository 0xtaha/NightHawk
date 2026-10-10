variable "region" {
  description = "AWS region for the compute environment."
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
  description = "Globally unique prefix for VPC, EKS, and IAM resource names."
  type        = string
  nullable    = false
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
  nullable    = false
}

variable "az_count" {
  description = "Number of distinct availability zones to span."
  type        = number
  default     = 2
  nullable    = false
}

variable "endpoint_public_access" {
  description = "Whether the cluster API is reachable from outside the VPC. No default: state it."
  type        = bool
  nullable    = false
}

variable "public_access_cidrs" {
  description = "CIDR ranges allowed to reach the public API endpoint; non-empty when it is enabled, empty otherwise."
  type        = list(string)
  nullable    = false
}

variable "cluster_admin_principal_arns" {
  description = "IAM principals granted cluster administration. The creating principal gets none on its own."
  type        = list(string)
  nullable    = false
}

variable "secrets_kms_key_arn" {
  description = "KMS key for Kubernetes secrets encryption; null lets the EKS module create one."
  type        = string
  default     = null
}

variable "dns_controller" {
  description = "Optional IRSA role for DNS record management, limited to the listed hosted zones."
  type = object({
    service_account = object({ namespace = string, name = string })
    hosted_zone_ids = list(string)
  })
  default = null
}

variable "certificate_controller" {
  description = "Optional IRSA role for certificate DNS-01 validation, limited to the listed hosted zones."
  type = object({
    service_account = object({ namespace = string, name = string })
    hosted_zone_ids = list(string)
  })
  default = null
}

variable "autoscaler_controller" {
  description = "Optional IRSA role for node autoscaling of this cluster's node groups."
  type = object({
    service_account = object({ namespace = string, name = string })
  })
  default = null
}

variable "cluster_version" {
  description = "EKS-managed Kubernetes control-plane version."
  type        = string
  default     = "1.37"
  nullable    = false
}

variable "ebs_csi_addon_version" {
  description = "EBS CSI driver addon version, including the -eksbuildN suffix."
  type        = string
  default     = "v1.66.0-eksbuild.1"
  nullable    = false
}

variable "stateful_node_group" {
  description = "Sizing for the always-on-demand stateful node group."
  type = object({
    desired_size   = number
    min_size       = number
    max_size       = number
    instance_types = optional(list(string), ["m6i.large"])
    root_volume_gb = optional(number, 50)
  })
  default  = { desired_size = 2, min_size = 2, max_size = 4 }
  nullable = false
}

variable "stateless_node_group" {
  description = "Sizing for the stateless node group; capacity_type defaults to on-demand and is opt-in to spot."
  type = object({
    desired_size   = number
    min_size       = number
    max_size       = number
    instance_types = optional(list(string), ["m6i.large"])
    capacity_type  = optional(string, "ON_DEMAND")
    root_volume_gb = optional(number, 50)
  })
  default  = { desired_size = 1, min_size = 0, max_size = 4 }
  nullable = false
}

variable "tags" {
  description = "Environment-specific tags."
  type        = map(string)
  default     = {}
  nullable    = false
}
