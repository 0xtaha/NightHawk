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
  mock_resource "aws_eks_cluster" {
    defaults = {
      arn      = "arn:aws:eks:eu-west-1:123456789012:cluster/nighthawk-unit-eks"
      endpoint = "https://TEST.gr7.eu-west-1.eks.amazonaws.com"
      name     = "nighthawk-unit-eks"
      identity = [{
        oidc = [{
          issuer = "https://oidc.eks.eu-west-1.amazonaws.com/id/TEST"
        }]
      }]
    }
  }
  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:eu-west-1:123456789012:key/00000000-0000-0000-0000-000000000000"
      key_id = "00000000-0000-0000-0000-000000000000"
    }
  }
  mock_resource "aws_launch_template" {
    defaults = { latest_version = 1 }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = {
      arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/TEST"
    }
  }
}

# Controller roles get a known ARN at plan time, distinct from the cluster and node roles.
override_resource {
  target          = aws_iam_role.controller
  override_during = plan
  values = {
    arn = "arn:aws:iam::123456789012:role/nighthawk-unit-eks-controller"
  }
}

variables {
  name_prefix                  = "nighthawk-unit"
  cluster_subnet_ids           = ["subnet-00000000000000001", "subnet-00000000000000002"]
  node_subnet_ids              = ["subnet-00000000000000001", "subnet-00000000000000002"]
  node_security_group_ids      = ["sg-0000000000000000a"]
  endpoint_public_access       = false
  public_access_cidrs          = []
  cluster_admin_principal_arns = ["arn:aws:iam::123456789012:role/platform-admin"]
}

run "plans_the_cluster_with_pinned_versions" {
  command = plan

  assert {
    condition     = aws_eks_cluster.this.version == "1.37"
    error_message = "Expected the pinned EKS Kubernetes version."
  }

  assert {
    condition     = aws_eks_addon.ebs_csi.addon_version == "v1.66.0-eksbuild.1"
    error_message = "Expected the pinned EBS CSI driver addon version."
  }

  assert {
    condition     = aws_eks_node_group.stateful.capacity_type == "ON_DEMAND"
    error_message = "The stateful node group must always use on-demand capacity."
  }

  assert {
    condition     = aws_eks_node_group.stateless.capacity_type == "ON_DEMAND"
    error_message = "The stateless node group defaults to on-demand capacity unless opted into spot."
  }
}

run "allows_opting_the_stateless_group_into_spot" {
  command = plan

  variables {
    stateless_node_group = {
      desired_size  = 1
      min_size      = 0
      max_size      = 4
      capacity_type = "SPOT"
    }
  }

  assert {
    condition     = aws_eks_node_group.stateless.capacity_type == "SPOT"
    error_message = "Expected spot capacity when explicitly opted in."
  }

  assert {
    condition     = aws_eks_node_group.stateful.capacity_type == "ON_DEMAND"
    error_message = "Opting the stateless group into spot must not affect the stateful group."
  }
}

run "rejects_an_invalid_stateless_capacity_type" {
  command = plan

  variables {
    stateless_node_group = {
      desired_size  = 1
      min_size      = 0
      max_size      = 4
      capacity_type = "RESERVED"
    }
  }

  expect_failures = [
    var.stateless_node_group,
  ]
}

# ---- cluster access ---------------------------------------------------------------------

run "private_only_endpoint" {
  command = plan

  assert {
    condition     = aws_eks_cluster.this.vpc_config[0].endpoint_private_access == true
    error_message = "The private endpoint must always be enabled."
  }

  assert {
    condition     = aws_eks_cluster.this.vpc_config[0].endpoint_public_access == false
    error_message = "The public endpoint must be disabled when the operator disables it."
  }
}

run "public_endpoint_with_an_allowlist" {
  command = plan

  variables {
    endpoint_public_access = true
    public_access_cidrs    = ["203.0.113.0/24", "198.51.100.7/32"]
  }

  assert {
    condition     = aws_eks_cluster.this.vpc_config[0].endpoint_public_access == true
    error_message = "Expected the public endpoint to be enabled."
  }

  assert {
    condition     = aws_eks_cluster.this.vpc_config[0].public_access_cidrs == toset(["203.0.113.0/24", "198.51.100.7/32"])
    error_message = "The public endpoint must allow exactly the listed ranges."
  }

  assert {
    condition     = aws_eks_cluster.this.vpc_config[0].endpoint_private_access == true
    error_message = "The private endpoint stays enabled alongside the public one."
  }
}

run "public_endpoint_without_an_allowlist_is_refused" {
  command = plan

  variables {
    endpoint_public_access = true
    public_access_cidrs    = []
  }

  expect_failures = [var.public_access_cidrs]
}

run "public_endpoint_open_to_every_address_is_refused" {
  command = plan

  variables {
    endpoint_public_access = true
    public_access_cidrs    = ["0.0.0.0/0"]
  }

  expect_failures = [var.public_access_cidrs]
}

run "ranges_without_a_public_endpoint_are_refused" {
  command = plan

  variables {
    endpoint_public_access = false
    public_access_cidrs    = ["203.0.113.0/24"]
  }

  expect_failures = [var.public_access_cidrs]
}

run "declared_administrators_only" {
  command = plan

  variables {
    cluster_admin_principal_arns = [
      "arn:aws:iam::123456789012:role/platform-admin",
      "arn:aws:iam::123456789012:user/break-glass",
    ]
  }

  assert {
    condition     = aws_eks_cluster.this.access_config[0].authentication_mode == "API"
    error_message = "Access must be granted through access entries only."
  }

  assert {
    condition     = aws_eks_cluster.this.access_config[0].bootstrap_cluster_creator_admin_permissions == false
    error_message = "The creating principal must not be granted administration implicitly."
  }

  assert {
    condition = toset(keys(aws_eks_access_entry.admin)) == toset([
      "arn:aws:iam::123456789012:role/platform-admin", "arn:aws:iam::123456789012:user/break-glass",
    ])
    error_message = "Expected exactly one access entry per declared administrator."
  }

  assert {
    condition = alltrue([
      for association in aws_eks_access_policy_association.admin :
      association.policy_arn == "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
      && association.access_scope[0].type == "cluster"
    ])
    error_message = "Each administrator must get the cluster-admin access policy at cluster scope."
  }
}

run "no_administrators_is_refused" {
  command = plan

  variables {
    cluster_admin_principal_arns = []
  }

  expect_failures = [var.cluster_admin_principal_arns]
}

# ---- encryption at rest -----------------------------------------------------------------

run "module_created_secrets_key" {
  command = plan

  assert {
    condition     = length(aws_kms_key.secrets) == 1 && aws_kms_key.secrets[0].enable_key_rotation == true
    error_message = "Without an operator key, the module must create one with rotation enabled."
  }

  assert {
    condition     = aws_eks_cluster.this.encryption_config[0].resources == toset(["secrets"])
    error_message = "Kubernetes secrets must be covered by the encryption configuration."
  }

  assert {
    condition     = aws_eks_cluster.this.encryption_config[0].provider[0].key_arn == aws_kms_key.secrets[0].arn
    error_message = "The cluster must encrypt secrets with the module's key."
  }
}

run "operator_supplied_secrets_key" {
  command = plan

  variables {
    secrets_kms_key_arn = "arn:aws:kms:eu-west-1:123456789012:key/11111111-2222-3333-4444-555555555555"
  }

  assert {
    condition     = length(aws_kms_key.secrets) == 0 && length(aws_kms_alias.secrets) == 0
    error_message = "No key may be created when the operator supplies one."
  }

  assert {
    condition     = aws_eks_cluster.this.encryption_config[0].provider[0].key_arn == "arn:aws:kms:eu-west-1:123456789012:key/11111111-2222-3333-4444-555555555555"
    error_message = "The cluster must use the operator's key."
  }

  assert {
    condition     = output.secrets_kms_key_arn == "arn:aws:kms:eu-west-1:123456789012:key/11111111-2222-3333-4444-555555555555"
    error_message = "The output must name the key in use."
  }
}

run "node_volumes_are_encrypted_and_nodes_carry_the_contract_groups" {
  command = plan

  variables {
    node_security_group_ids = ["sg-0000000000000000a", "sg-0000000000000000b"]
  }

  assert {
    condition     = toset(keys(aws_launch_template.node)) == toset(["stateful", "stateless"])
    error_message = "Expected one launch template per node group."
  }

  assert {
    condition = alltrue([
      for template in aws_launch_template.node : template.block_device_mappings[0].ebs[0].encrypted == "true"
    ])
    error_message = "Every node group's root volume must be encrypted."
  }

  assert {
    condition = alltrue([
      for template in aws_launch_template.node :
      contains(template.vpc_security_group_ids, "sg-0000000000000000a")
      && contains(template.vpc_security_group_ids, "sg-0000000000000000b")
      && contains(template.vpc_security_group_ids, aws_eks_cluster.this.vpc_config[0].cluster_security_group_id)
      && length(template.vpc_security_group_ids) == 3
    ])
    error_message = "Every node must carry the contract-derived groups and the cluster's own group."
  }

  assert {
    condition = (
      aws_eks_node_group.stateful.launch_template[0].name == "nighthawk-unit-stateful"
      && aws_eks_node_group.stateless.launch_template[0].name == "nighthawk-unit-stateless"
      && aws_launch_template.node["stateful"].name == "nighthawk-unit-stateful"
      && aws_launch_template.node["stateless"].name == "nighthawk-unit-stateless"
    )
    error_message = "Each node group must use its own launch template."
  }
}

# ---- controller identities ---------------------------------------------------------------

run "ebs_csi_driver_uses_its_own_role" {
  command = plan

  assert {
    condition     = aws_eks_addon.ebs_csi.service_account_role_arn == "arn:aws:iam::123456789012:role/nighthawk-unit-eks-controller"
    error_message = "The EBS CSI addon must use its own IRSA role."
  }

  assert {
    condition     = aws_iam_role_policy_attachment.ebs_csi.role == "nighthawk-unit-eks-ebs-csi"
    error_message = "The volume-management policy must be attached to the driver's role."
  }

  assert {
    condition = toset([
      aws_iam_role_policy_attachment.node_worker.policy_arn, aws_iam_role_policy_attachment.node_cni.policy_arn,
      aws_iam_role_policy_attachment.node_ecr.policy_arn,
      ]) == toset([
      "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy", "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
      "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    ])
    error_message = "The node role must carry only the worker, CNI, and registry policies."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.controller["ebs-csi"].assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:sub"]
      == "system:serviceaccount:kube-system:ebs-csi-controller-sa"
    )
    error_message = "Only the driver's controller service account may assume its role."
  }

  assert {
    condition     = keys(output.controller_role_arns) == ["ebs-csi"]
    error_message = "With no optional controller enabled, only the EBS CSI role exists."
  }

  assert {
    condition     = length(aws_iam_role_policy.dns) == 0 && length(aws_iam_role_policy.certificate) == 0 && length(aws_iam_role_policy.autoscaler) == 0
    error_message = "No optional controller policy may exist when none is enabled."
  }
}

run "dns_and_certificate_controllers_are_scoped_to_declared_zones" {
  command = plan

  variables {
    dns_controller = {
      service_account = { namespace = "external-dns", name = "external-dns" }
      hosted_zone_ids = ["Z0123456789ABC"]
    }
    certificate_controller = {
      service_account = { namespace = "cert-manager", name = "cert-manager" }
      hosted_zone_ids = ["Z0123456789ABC", "Z0987654321XYZ"]
    }
  }

  assert {
    condition     = toset(keys(output.controller_role_arns)) == toset(["ebs-csi", "dns", "certificate"])
    error_message = "Each enabled controller must have a role in the outputs."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.dns[0].policy).Statement[0].Resource == ["arn:aws:route53:::hostedzone/Z0123456789ABC"]
      && contains(jsondecode(aws_iam_role_policy.dns[0].policy).Statement[0].Action, "route53:ChangeResourceRecordSets")
    )
    error_message = "The DNS controller may change records only in the declared zone."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.dns[0].policy).Statement :
      !contains(flatten([statement.Action]), "route53:ChangeResourceRecordSets") || statement.Resource != "*"
    ])
    error_message = "No statement may allow changing records in every zone."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.controller["dns"].assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.eu-west-1.amazonaws.com/id/TEST:sub"]
      == "system:serviceaccount:external-dns:external-dns"
    )
    error_message = "Only the named service account may assume the DNS controller role."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role_policy.certificate[0].policy).Statement[0].Resource
      == ["arn:aws:route53:::hostedzone/Z0123456789ABC", "arn:aws:route53:::hostedzone/Z0987654321XYZ"]
      && jsondecode(aws_iam_role_policy.certificate[0].policy).Statement[0].Condition["ForAllValues:StringEquals"]["route53:ChangeResourceRecordSetsRecordTypes"] == ["TXT"]
    )
    error_message = "The certificate controller may change only TXT records in the declared zones."
  }
}

run "dns_controller_without_zones_is_refused" {
  command = plan

  variables {
    dns_controller = {
      service_account = { namespace = "external-dns", name = "external-dns" }
      hosted_zone_ids = []
    }
  }

  expect_failures = [var.dns_controller]
}

run "certificate_controller_without_zones_is_refused" {
  command = plan

  variables {
    certificate_controller = {
      service_account = { namespace = "cert-manager", name = "cert-manager" }
      hosted_zone_ids = []
    }
  }

  expect_failures = [var.certificate_controller]
}

run "autoscaler_can_resize_only_this_clusters_node_groups" {
  command = plan

  variables {
    autoscaler_controller = {
      service_account = { namespace = "kube-system", name = "cluster-autoscaler" }
    }
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.autoscaler[0].policy).Statement :
      statement.Condition.StringEquals["aws:ResourceTag/eks:cluster-name"] == "nighthawk-unit-eks"
      if length(setintersection(statement.Action, ["autoscaling:SetDesiredCapacity", "autoscaling:TerminateInstanceInAutoScalingGroup"])) > 0
    ])
    error_message = "Write actions must be conditioned on this cluster's node group tag."
  }

  assert {
    condition = length([
      for statement in jsondecode(aws_iam_role_policy.autoscaler[0].policy).Statement : statement
      if contains(statement.Action, "autoscaling:SetDesiredCapacity")
    ]) == 1
    error_message = "Expected exactly one statement granting resize."
  }

  assert {
    condition     = toset(keys(output.controller_role_arns)) == toset(["ebs-csi", "autoscaler"])
    error_message = "The autoscaler role must be in the outputs."
  }
}
