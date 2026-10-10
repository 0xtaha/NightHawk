locals {
  tags = merge(var.tags, { ManagedBy = "terraform", Platform = "nighthawk" })

  # Amazon Root CA 1's SHA-1 thumbprint is the same for every EKS cluster's
  # OIDC issuer in every region (AWS/eksctl document this as a static value,
  # since EKS issuer certificates all chain to that same root). Deriving it
  # dynamically would require the "tls" provider for no added safety.
  eks_oidc_root_ca_thumbprint = "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"

  partition       = data.aws_partition.current.partition
  issuer          = trimprefix(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://")
  secrets_key_arn = var.secrets_kms_key_arn != null ? var.secrets_kms_key_arn : aws_kms_key.secrets[0].arn

  # The cluster's own security group keeps node-to-control-plane traffic working; the
  # contract-derived groups add what config/network.yaml declares for the workloads.
  node_security_group_ids = concat(
    [aws_eks_cluster.this.vpc_config[0].cluster_security_group_id], var.node_security_group_ids,
  )

  # Controllers that get an IRSA role. EBS CSI is always present; the rest are opt-in.
  controllers = merge(
    { ebs-csi = { namespace = "kube-system", name = "ebs-csi-controller-sa" } },
    var.dns_controller == null ? {} : { dns = var.dns_controller.service_account },
    var.certificate_controller == null ? {} : { certificate = var.certificate_controller.service_account },
    var.autoscaler_controller == null ? {} : { autoscaler = var.autoscaler_controller.service_account },
  )
  zone_arns = {
    dns         = [for zone in try(var.dns_controller.hosted_zone_ids, []) : "arn:${local.partition}:route53:::hostedzone/${zone}"]
    certificate = [for zone in try(var.certificate_controller.hosted_zone_ids, []) : "arn:${local.partition}:route53:::hostedzone/${zone}"]
  }
}

data "aws_partition" "current" {}

resource "aws_iam_role" "cluster" {
  name = "${var.name_prefix}-eks-cluster"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "eks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# Envelope encryption for Kubernetes secrets, unless the operator supplies a key.
resource "aws_kms_key" "secrets" {
  count                   = var.secrets_kms_key_arn == null ? 1 : 0
  description             = "NightHawk ${var.name_prefix} EKS secrets encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags
}

resource "aws_kms_alias" "secrets" {
  count         = var.secrets_kms_key_arn == null ? 1 : 0
  name          = "alias/${var.name_prefix}-eks-secrets"
  target_key_id = aws_kms_key.secrets[0].key_id
}

resource "aws_iam_role_policy" "cluster_secrets_key" {
  name = "secrets-encryption"
  role = aws_iam_role.cluster.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Encrypt", "kms:Decrypt", "kms:DescribeKey", "kms:CreateGrant", "kms:ListGrants"]
      Resource = local.secrets_key_arn
    }]
  })
}

resource "aws_eks_cluster" "this" {
  name     = "${var.name_prefix}-eks"
  role_arn = aws_iam_role.cluster.arn
  version  = var.cluster_version
  tags     = local.tags

  # Access is granted only through the access entries below: the principal that
  # creates the cluster gets nothing implicitly.
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  vpc_config {
    subnet_ids              = var.cluster_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = var.endpoint_public_access
    public_access_cidrs     = var.endpoint_public_access ? var.public_access_cidrs : null
  }

  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = local.secrets_key_arn
    }
  }

  depends_on = [aws_iam_role_policy_attachment.cluster, aws_iam_role_policy.cluster_secrets_key]
}

resource "aws_eks_access_entry" "admin" {
  for_each      = toset(var.cluster_admin_principal_arns)
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
  type          = "STANDARD"
  tags          = local.tags
}

resource "aws_eks_access_policy_association" "admin" {
  for_each      = aws_eks_access_entry.admin
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value.principal_arn
  policy_arn    = "arn:${local.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}

resource "aws_iam_openid_connect_provider" "this" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [local.eks_oidc_root_ca_thumbprint]
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  tags            = local.tags
}

resource "aws_iam_role" "node" {
  name = "${var.name_prefix}-eks-node"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node_worker" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "node_cni" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "node_ecr" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# A managed node group takes security groups and volume settings only through a
# launch template. One per group: encrypted root volume, cluster and contract groups.
resource "aws_launch_template" "node" {
  for_each = {
    stateful  = var.stateful_node_group.root_volume_gb
    stateless = var.stateless_node_group.root_volume_gb
  }
  name                   = "${var.name_prefix}-${each.key}"
  vpc_security_group_ids = local.node_security_group_ids
  tags                   = local.tags

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      encrypted             = true
      volume_size           = each.value
      volume_type           = "gp3"
      delete_on_termination = true
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags          = local.tags
  }
}

resource "aws_eks_node_group" "stateful" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.name_prefix}-stateful"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.node_subnet_ids

  launch_template {
    name    = aws_launch_template.node["stateful"].name
    version = aws_launch_template.node["stateful"].latest_version
  }

  capacity_type  = "ON_DEMAND"
  instance_types = var.stateful_node_group.instance_types
  tags           = local.tags

  scaling_config {
    desired_size = var.stateful_node_group.desired_size
    min_size     = var.stateful_node_group.min_size
    max_size     = var.stateful_node_group.max_size
  }

  depends_on = [
    aws_iam_role_policy_attachment.node_worker,
    aws_iam_role_policy_attachment.node_cni,
    aws_iam_role_policy_attachment.node_ecr,
  ]
}

resource "aws_eks_node_group" "stateless" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.name_prefix}-stateless"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.node_subnet_ids

  launch_template {
    name    = aws_launch_template.node["stateless"].name
    version = aws_launch_template.node["stateless"].latest_version
  }

  capacity_type  = var.stateless_node_group.capacity_type
  instance_types = var.stateless_node_group.instance_types
  tags           = local.tags

  scaling_config {
    desired_size = var.stateless_node_group.desired_size
    min_size     = var.stateless_node_group.min_size
    max_size     = var.stateless_node_group.max_size
  }

  depends_on = [
    aws_iam_role_policy_attachment.node_worker,
    aws_iam_role_policy_attachment.node_cni,
    aws_iam_role_policy_attachment.node_ecr,
  ]
}

# No Helm release or Kubernetes-provider resource is declared anywhere in
# this module: the cluster a Kubernetes/Helm provider would target does not
# exist until this same apply completes.
resource "aws_eks_addon" "ebs_csi" {
  cluster_name  = aws_eks_cluster.this.name
  addon_name    = "aws-ebs-csi-driver"
  addon_version = var.ebs_csi_addon_version
  tags          = local.tags

  # The driver's controller uses its own IRSA role; the node role carries no volume permissions.
  service_account_role_arn = aws_iam_role.controller["ebs-csi"].arn

  depends_on = [
    aws_eks_node_group.stateful,
    aws_eks_node_group.stateless,
    aws_iam_role_policy_attachment.ebs_csi,
  ]
}

# One IRSA role per controller, assumable only by the service account named for it.
resource "aws_iam_role" "controller" {
  for_each = local.controllers
  name     = "${var.name_prefix}-eks-${each.key}"
  tags     = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.this.arn }
      Condition = {
        StringEquals = {
          "${local.issuer}:aud" = "sts.amazonaws.com"
          "${local.issuer}:sub" = "system:serviceaccount:${each.value.namespace}:${each.value.name}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.controller["ebs-csi"].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# DNS records: change and list only in the declared hosted zones. Listing zones cannot be scoped.
resource "aws_iam_role_policy" "dns" {
  count = var.dns_controller == null ? 0 : 1
  name  = "dns-records"
  role  = aws_iam_role.controller["dns"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ChangeRecordsInDeclaredZones"
        Effect   = "Allow"
        Action   = ["route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets"]
        Resource = local.zone_arns.dns
      },
      {
        Sid      = "DiscoverZones"
        Effect   = "Allow"
        Action   = ["route53:ListHostedZones", "route53:ListTagsForResources"]
        Resource = "*"
      },
    ]
  })
}

# DNS-01 validation: TXT records only, in the declared hosted zones.
resource "aws_iam_role_policy" "certificate" {
  count = var.certificate_controller == null ? 0 : 1
  name  = "dns01-validation"
  role  = aws_iam_role.controller["certificate"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ChangeValidationRecordsInDeclaredZones"
        Effect   = "Allow"
        Action   = "route53:ChangeResourceRecordSets"
        Resource = local.zone_arns.certificate
        Condition = {
          "ForAllValues:StringEquals" = { "route53:ChangeResourceRecordSetsRecordTypes" = ["TXT"] }
        }
      },
      {
        Sid      = "ListRecordsInDeclaredZones"
        Effect   = "Allow"
        Action   = "route53:ListResourceRecordSets"
        Resource = local.zone_arns.certificate
      },
      {
        Sid      = "FollowChanges"
        Effect   = "Allow"
        Action   = "route53:GetChange"
        Resource = "arn:${local.partition}:route53:::change/*"
      },
      {
        Sid      = "DiscoverZones"
        Effect   = "Allow"
        Action   = "route53:ListHostedZonesByName"
        Resource = "*"
      },
    ]
  })
}

# Node autoscaling: read everywhere it must, resize only this cluster's node groups.
resource "aws_iam_role_policy" "autoscaler" {
  count = var.autoscaler_controller == null ? 0 : 1
  name  = "node-autoscaling"
  role  = aws_iam_role.controller["autoscaler"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Describe"
        Effect = "Allow"
        Action = [
          "autoscaling:DescribeAutoScalingGroups", "autoscaling:DescribeAutoScalingInstances",
          "autoscaling:DescribeLaunchConfigurations", "autoscaling:DescribeScalingActivities",
          "autoscaling:DescribeTags", "ec2:DescribeImages", "ec2:DescribeInstanceTypes",
          "ec2:DescribeLaunchTemplateVersions", "ec2:GetInstanceTypesFromInstanceRequirements",
          "eks:DescribeNodegroup",
        ]
        Resource = "*"
      },
      {
        Sid      = "ResizeThisClustersNodeGroups"
        Effect   = "Allow"
        Action   = ["autoscaling:SetDesiredCapacity", "autoscaling:TerminateInstanceInAutoScalingGroup"]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:ResourceTag/eks:cluster-name" = aws_eks_cluster.this.name }
        }
      },
    ]
  })
}
