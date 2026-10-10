# Plan-only, with a mocked provider. Run with the example values:
#   terraform test -var-file=terraform.tfvars.example
mock_provider "aws" {
  override_during = plan
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_region" {
    defaults = { region = "eu-west-1" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-west-1a", "eu-west-1b", "eu-west-1c"] }
  }
  mock_resource "aws_vpc_endpoint" {
    defaults = { prefix_list_id = "pl-00000000000000000" }
  }
  mock_resource "aws_security_group" {
    defaults = { id = "sg-0000000000000c0de" }
  }
  mock_resource "aws_eks_cluster" {
    defaults = {
      arn      = "arn:aws:eks:eu-west-1:123456789012:cluster/example-eks"
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
  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:eu-west-1:123456789012:key/00000000-0000-0000-0000-000000000000"
      key_id = "00000000-0000-0000-0000-000000000000"
    }
  }
  mock_resource "aws_launch_template" {
    defaults = { latest_version = 1 }
  }
}

run "example_values_plan_and_nodes_carry_the_contract_derived_group" {
  command = plan

  assert {
    condition     = keys(output.security_group_ids) == ["signal-backend"]
    error_message = "Expected one contract-derived security group, for the signal-backend source."
  }

  assert {
    condition     = contains(output.node_security_group_ids, output.security_group_ids["signal-backend"])
    error_message = "Every node must carry the contract-derived security group."
  }

  assert {
    condition     = length(output.node_security_group_ids) == 2 && output.node_security_group_ids[0] == output.cluster_security_group_id
    error_message = "Every node must also carry the cluster's own security group, and nothing else."
  }

  assert {
    condition     = keys(output.controller_role_arns) == ["ebs-csi"]
    error_message = "With the example values only the EBS CSI role is created."
  }
}
