variable "name_prefix" {
  description = "Prefix applied to the EKS cluster, node group, and IAM resource names."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be 3-32 lowercase letters/digits/hyphens, start with a letter, and end with a letter or digit."
  }
}

variable "cluster_version" {
  description = "EKS-managed Kubernetes control-plane version. Pinned in config/versions.yaml's kubernetes_platform.eks.version; tracked_consumers enforces this literal stays in sync."
  type        = string
  default     = "1.37"
  nullable    = false

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+$", var.cluster_version))
    error_message = "cluster_version must be an EKS minor version such as \"1.37\" (AWS manages patch versions within it)."
  }
}

variable "ebs_csi_addon_version" {
  description = <<-EOT
    EBS CSI driver addon version, including the "-eksbuildN" suffix AWS
    publishes per cluster version. The numeric prefix is pinned in
    config/versions.yaml's kubernetes_platform.ebs_csi_driver.version;
    tracked_consumers enforces that prefix stays in sync. Confirm the exact
    available suffix for your chosen cluster_version with
    `aws eks describe-addon-versions --addon-name aws-ebs-csi-driver
    --kubernetes-version <cluster_version>` before applying - AWS does not
    publish every addon version for every cluster version.
  EOT
  type        = string
  default     = "v1.66.0-eksbuild.1"
  nullable    = false

  validation {
    condition     = can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+-eksbuild\\.[0-9]+$", var.ebs_csi_addon_version))
    error_message = "ebs_csi_addon_version must look like \"v1.66.0-eksbuild.1\"."
  }
}

variable "vpc_id" {
  description = "VPC ID hosting the cluster (output of aws-vpc-network)."
  type        = string
  nullable    = false
}

variable "cluster_subnet_ids" {
  description = "Subnet IDs for the EKS control plane's elastic network interfaces (public + private, per AWS guidance)."
  type        = list(string)
  nullable    = false

  validation {
    condition     = length(var.cluster_subnet_ids) >= 2
    error_message = "EKS requires subnets in at least two availability zones."
  }
}

variable "node_subnet_ids" {
  description = "Private subnet IDs for managed node groups."
  type        = list(string)
  nullable    = false

  validation {
    condition     = length(var.node_subnet_ids) >= 1
    error_message = "At least one private subnet is required for node groups."
  }
}

variable "stateful_node_group" {
  description = "Sizing for the always-on-demand stateful node group."
  type = object({
    desired_size   = number
    min_size       = number
    max_size       = number
    instance_types = optional(list(string), ["m6i.large"])
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
  })
  default  = { desired_size = 1, min_size = 0, max_size = 4 }
  nullable = false

  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.stateless_node_group.capacity_type)
    error_message = "stateless_node_group.capacity_type must be \"ON_DEMAND\" or \"SPOT\"."
  }
}

variable "tags" {
  description = "Additional tags on managed AWS resources."
  type        = map(string)
  default     = {}
  nullable    = false
}
