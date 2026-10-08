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
}

variables {
  name_prefix = "nighthawk-unit"
  network_rules = [
    {
      id          = "aws-object-storage"
      source      = "signal-backend"
      destination = "aws-s3"
      protocol    = "tcp"
      port        = 443
      scope       = "private"
    }
  ]
}

run "plans_with_the_real_network_contract_rule" {
  command = plan

  assert {
    condition     = length(aws_subnet.public) == 2
    error_message = "Expected the default az_count (2) of public subnets."
  }

  assert {
    condition     = length(aws_subnet.private) == 2
    error_message = "Expected the default az_count (2) of private subnets."
  }

  assert {
    condition     = contains(keys(aws_security_group.workload), "signal-backend")
    error_message = "Expected a security group for the signal-backend source identity."
  }

  assert {
    condition     = length(aws_vpc_security_group_egress_rule.s3) == 1
    error_message = "Expected exactly one S3 egress rule for the aws-object-storage network-contract rule."
  }
}

run "rejects_loopback_scope_rules" {
  command = plan

  variables {
    network_rules = [
      {
        id          = "local-gateway"
        source      = "local-client"
        destination = "gateway"
        protocol    = "tcp"
        port        = 8443
        scope       = "loopback"
      }
    ]
  }

  expect_failures = [
    var.network_rules,
  ]
}

run "rejects_unsupported_destination" {
  command = plan

  variables {
    network_rules = [
      {
        id          = "backend-object-storage"
        source      = "signal-backend"
        destination = "object-storage"
        protocol    = "tcp"
        port        = 8333
        scope       = "private"
      }
    ]
  }

  expect_failures = [
    var.network_rules,
  ]
}
