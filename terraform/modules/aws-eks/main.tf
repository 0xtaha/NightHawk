locals {
  tags = merge(var.tags, { ManagedBy = "terraform", Platform = "nighthawk" })

  # Amazon Root CA 1's SHA-1 thumbprint is the same for every EKS cluster's
  # OIDC issuer in every region (AWS/eksctl document this as a static value,
  # since EKS issuer certificates all chain to that same root). Deriving it
  # dynamically would require the "tls" provider for no added safety.
  eks_oidc_root_ca_thumbprint = "9e99a48a9960b14926bb7f3b02e22da2b0ab7280"
}

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

resource "aws_eks_cluster" "this" {
  name     = "${var.name_prefix}-eks"
  role_arn = aws_iam_role.cluster.arn
  version  = var.cluster_version
  tags     = local.tags

  vpc_config {
    subnet_ids = var.cluster_subnet_ids
  }

  depends_on = [aws_iam_role_policy_attachment.cluster]
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

resource "aws_iam_role_policy_attachment" "node_ebs_csi" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_eks_node_group" "stateful" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.name_prefix}-stateful"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.node_subnet_ids
  capacity_type   = "ON_DEMAND"
  instance_types  = var.stateful_node_group.instance_types
  tags            = local.tags

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
  capacity_type   = var.stateless_node_group.capacity_type
  instance_types  = var.stateless_node_group.instance_types
  tags            = local.tags

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

  depends_on = [
    aws_eks_node_group.stateful,
    aws_eks_node_group.stateless,
    aws_iam_role_policy_attachment.node_ebs_csi,
  ]
}
