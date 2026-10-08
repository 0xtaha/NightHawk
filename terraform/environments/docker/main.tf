# This root deliberately declares zero managed resources.
#
# Docker Compose bootstrap and local SeaweedFS provisioning for the Docker
# profile are entirely owned by Ansible/Compose orchestration, per
# docs/01-architecture.md's Storage amendments section ("Terraform never
# competes to manage these local resources"). See README.md for the full
# ownership boundary and tests/test_terraform_boundaries.py for the guard
# that enforces this file never gains a `resource` block.

terraform {
  required_version = "= 1.13.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.12.0"
    }
  }
}
