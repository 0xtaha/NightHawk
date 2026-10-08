# Self-hosted Kubernetes environment root (zero-resource placeholder)

This root exists only so the compatibility matrix and Terraform tooling can
track a pinned `terraform`/`aws` version pair for the self-hosted
Kubernetes deployment profile. It declares **no resources**.

## Why this root has no resources

Per `docs/01-architecture.md`'s Storage amendments section, the
self-hosted Kubernetes profile's k3s cluster, Cilium, MetalLB, Longhorn,
Traefik, cert-manager, and SeaweedFS deployment are entirely owned by
Ansible and Helm. Terraform never competes with that tooling to manage
these local/self-hosted resources, so this root's `main.tf` contains only
a `terraform {}` block with pinned `required_version`/`required_providers`
- no `resource` blocks.

`tests/test_terraform_boundaries.py` enforces this boundary by scanning
every `.tf` file under this directory for `resource "..." "..." {` blocks
and failing the build if one is ever added. Do not add a resource here;
if self-hosted infrastructure needs Terraform-managed AWS resources in the
future, that is a design change requiring an OpenSpec proposal, not a
silent edit to this root.

## Static validation

From this directory:

```text
terraform init -backend=false -input=false
terraform validate
terraform plan -input=false
```

Expected result: configuration is valid, and the plan reports "No changes"
since no resources are declared.
