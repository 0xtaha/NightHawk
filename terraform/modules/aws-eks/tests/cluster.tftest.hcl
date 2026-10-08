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
      identity = [{
        oidc = [{
          issuer = "https://oidc.eks.eu-west-1.amazonaws.com/id/TEST"
        }]
      }]
    }
  }
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = {
      arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/TEST"
    }
  }
}

variables {
  name_prefix        = "nighthawk-unit"
  vpc_id             = "vpc-00000000000000000"
  cluster_subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"]
  node_subnet_ids    = ["subnet-00000000000000001", "subnet-00000000000000002"]
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
