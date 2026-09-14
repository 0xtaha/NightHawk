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
}

variables {
  cloud_vendor = "aws"
  name_prefix  = "nighthawk-facade"
  aws_identity = {
    oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/TEST"
    oidc_issuer_url   = "https://oidc.eks.eu-west-1.amazonaws.com/id/TEST"
  }
  workloads = { tempo = { namespace = "observability", service_accounts = ["tempo"] } }
}

run "aws_contract" {
  command = plan
  assert {
    condition     = output.storage.provider == "aws" && keys(output.storage.bindings) == ["tempo-traces"]
    error_message = "The facade must select AWS and retain logical binding names."
  }
}

run "reject_azure" {
  command = plan
  variables { cloud_vendor = "azure" }
  expect_failures = [var.cloud_vendor]
}

run "reject_gcp" {
  command = plan
  variables { cloud_vendor = "gcp" }
  expect_failures = [var.cloud_vendor]
}

run "reject_local_storage" {
  command = plan
  variables { cloud_vendor = "seaweedfs" }
  expect_failures = [var.cloud_vendor]
}
