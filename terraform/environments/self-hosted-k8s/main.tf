# This root deliberately declares zero managed resources.
#
# k3s/Cilium/MetalLB/Longhorn/Traefik/cert-manager bootstrap and SeaweedFS
# provisioning for the self-hosted Kubernetes profile are entirely owned by
# Ansible and Helm, per docs/01-architecture.md's Storage amendments
# section ("Terraform never competes to manage these local resources").
# See README.md for the full ownership boundary and
# tests/test_terraform_boundaries.py for the guard that enforces this file
# never gains a `resource` block.

terraform {
  required_version = "= 1.13.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.12.0"
    }
  }
}
