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

variable "endpoint_public_access" {
  description = "Whether the cluster API is reachable from outside the VPC. There is no default: state it. The private endpoint is always enabled."
  type        = bool
  nullable    = false
}

variable "public_access_cidrs" {
  description = "CIDR ranges allowed to reach the public API endpoint. Required and non-empty when endpoint_public_access is true; must be empty otherwise. A range covering every address is refused."
  type        = list(string)
  nullable    = false

  validation {
    condition     = alltrue([for cidr in var.public_access_cidrs : can(cidrhost(cidr, 0))])
    error_message = "public_access_cidrs must contain valid CIDR ranges."
  }

  validation {
    condition     = alltrue([for cidr in var.public_access_cidrs : !endswith(cidr, "/0")])
    error_message = "public_access_cidrs must not contain a range covering every address (0.0.0.0/0 or ::/0)."
  }

  validation {
    condition     = var.endpoint_public_access ? length(var.public_access_cidrs) > 0 : length(var.public_access_cidrs) == 0
    error_message = "List at least one range in public_access_cidrs when endpoint_public_access is true, and none when it is false."
  }
}

variable "cluster_admin_principal_arns" {
  description = "IAM principals granted cluster administration through access entries. The creating principal gets no access on its own, so at least one is required."
  type        = list(string)
  nullable    = false

  validation {
    condition     = length(var.cluster_admin_principal_arns) > 0
    error_message = "cluster_admin_principal_arns must name at least one principal; nobody else can administer the cluster."
  }

  validation {
    condition = alltrue([
      for arn in var.cluster_admin_principal_arns :
      can(regex("^arn:aws(-us-gov|-cn)?:iam::[0-9]{12}:(role|user)/[A-Za-z0-9+=,.@_/-]+$", arn))
    ])
    error_message = "cluster_admin_principal_arns must be IAM role or user ARNs."
  }

  validation {
    condition     = length(distinct(var.cluster_admin_principal_arns)) == length(var.cluster_admin_principal_arns)
    error_message = "cluster_admin_principal_arns must not repeat a principal."
  }
}

variable "secrets_kms_key_arn" {
  description = "KMS key that encrypts Kubernetes secrets. When null, the module creates one with rotation enabled."
  type        = string
  default     = null

  validation {
    condition     = var.secrets_kms_key_arn == null || can(regex("^arn:aws(-us-gov|-cn)?:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Za-z0-9-]+$", var.secrets_kms_key_arn))
    error_message = "secrets_kms_key_arn must be a KMS key ARN."
  }
}

variable "node_security_group_ids" {
  description = "Security groups derived from the network contract (aws-vpc-network's security_group_ids) to attach to every node, in addition to the cluster's own group."
  type        = list(string)
  nullable    = false
}

variable "dns_controller" {
  description = "Enable an IRSA role for DNS record management (for example external-dns), limited to the listed Route 53 hosted zones. Null disables it."
  type = object({
    service_account = object({ namespace = string, name = string })
    hosted_zone_ids = list(string)
  })
  default = null

  validation {
    condition     = var.dns_controller == null || length(try(var.dns_controller.hosted_zone_ids, [])) > 0
    error_message = "dns_controller needs at least one hosted zone ID; a controller with no scope is refused."
  }

  validation {
    condition     = var.dns_controller == null || alltrue([for zone in try(var.dns_controller.hosted_zone_ids, []) : can(regex("^Z[A-Z0-9]+$", zone))])
    error_message = "dns_controller.hosted_zone_ids must be Route 53 hosted zone IDs such as Z0123456789ABC."
  }
}

variable "certificate_controller" {
  description = "Enable an IRSA role for certificate DNS-01 validation (for example cert-manager), limited to TXT records in the listed Route 53 hosted zones. Null disables it."
  type = object({
    service_account = object({ namespace = string, name = string })
    hosted_zone_ids = list(string)
  })
  default = null

  validation {
    condition     = var.certificate_controller == null || length(try(var.certificate_controller.hosted_zone_ids, [])) > 0
    error_message = "certificate_controller needs at least one hosted zone ID; a controller with no scope is refused."
  }

  validation {
    condition     = var.certificate_controller == null || alltrue([for zone in try(var.certificate_controller.hosted_zone_ids, []) : can(regex("^Z[A-Z0-9]+$", zone))])
    error_message = "certificate_controller.hosted_zone_ids must be Route 53 hosted zone IDs such as Z0123456789ABC."
  }
}

variable "autoscaler_controller" {
  description = "Enable an IRSA role for node autoscaling (for example Cluster Autoscaler), able to resize only this cluster's node groups. Null disables it."
  type = object({
    service_account = object({ namespace = string, name = string })
  })
  default = null
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
