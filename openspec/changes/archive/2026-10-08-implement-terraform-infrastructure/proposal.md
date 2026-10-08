# Proposal

## Why

`Plan.md` Phase 2 ("Implement Terraform infrastructure and state bootstrap")
is the next unblocked foundation gap after Phase 1. Today the only Terraform
present is the AWS storage facade (`terraform/modules/object-storage`,
`terraform/modules/aws-s3-backends`, `terraform/environments/aws-storage`),
and its own README states it "requires existing EKS/OIDC and state-bootstrap
resources" that nothing in the repository yet creates. Without a compute
environment (VPC, EKS, IRSA-capable OIDC provider) and a safely isolated
remote-state root, the storage root cannot be authorized end to end, and
every later phase that depends on a running cluster (collection/gateway,
Kubernetes delivery, dashboards, acceptance) stays blocked.

## What Changes

- Add a separate, isolated **Terraform state-bootstrap root**
  (`terraform/environments/aws-state-bootstrap`) that provisions the
  encrypted S3 bucket and DynamoDB lock table other AWS roots use as their
  remote backend. It uses local state for itself (there is no earlier root to
  bootstrap it from), is destroy-protected, and documents the same native
  S3-lockfile migration path already described for the storage root.
- Add an **`aws-vpc-network` Terraform module**: VPC across multiple
  availability zones, public/private subnets, NAT/routing, and the security
  groups required by the compute and storage tiers. Security-group ingress
  rules are derived from `config/network.yaml` (the existing network
  contract), not independently declared ports.
- Add an **`aws-eks` Terraform module**: an EKS cluster with managed node
  groups (on-demand for stateful workloads, optional spot for suitable
  stateless workloads), the EBS CSI driver, and an IRSA-capable OIDC
  provider. It outputs `oidc_provider_arn`/`oidc_issuer_url` in the exact
  shape `aws-s3-backends`' `aws_identity` variable already expects, and
  explicitly does not install any Helm release (Terraform must not
  initialize a Kubernetes/Helm provider against a cluster that does not yet
  exist).
- Add an **`environments/aws` execution root** that composes
  `aws-vpc-network` and `aws-eks` with their own backend configuration,
  independent from (and not automatically chained into) `aws-storage`; an
  operator copies its `oidc_provider_arn`/`oidc_issuer_url`/subnet outputs
  into the storage root's inputs, matching that root's existing
  operator-supplied-input pattern.
- Add **`environments/self-hosted-k8s`** and **`environments/docker`**
  execution roots that formalize the Terraform/Ansible ownership boundary
  for the two profiles with no cloud account: both apply zero resources,
  document that k3s bootstrap, SeaweedFS, and all host-local provisioning
  stay Ansible/Helm-owned per `docs/01-architecture.md` ("Terraform never
  competes to manage these local resources"), and fail validation if any
  resource block is added, rather than shipping an empty root with no
  guardrail against future scope creep.
- **MODIFIED**: extend the compatibility matrix's pinned inventory to cover
  the EKS-managed Kubernetes control-plane version and the EBS CSI driver
  addon version, both newly required by `aws-eks` and currently absent from
  the matrix's enumerated component list.
- Add `terraform/environments/aws-state-bootstrap/versions.tf`,
  `terraform/modules/aws-vpc-network/versions.tf`, and
  `terraform/modules/aws-eks/versions.tf` as new `tracked_consumers` entries
  so `nighthawk check-pins` covers them the same way it already covers the
  storage modules.

## Capabilities

### New Capabilities
- `terraform-state-bootstrap`: the isolated remote-state bootstrap root
  (S3 + DynamoDB locking, destroy-protected, documented lockfile migration).
- `aws-compute-infrastructure`: the `aws-vpc-network` and `aws-eks` modules
  plus the `environments/aws` root, including network-contract-derived
  security groups and the OIDC/IRSA handoff to the existing storage root.
- `terraform-environment-boundaries`: the `environments/self-hosted-k8s` and
  `environments/docker` roots that apply nothing and enforce the
  Terraform/Ansible ownership split for non-cloud profiles.

### Modified Capabilities
- `compatibility-matrix`: "Complete pinned inventory" must also require an
  exact, source-verified pin for the EKS-managed Kubernetes control-plane
  version and the EBS CSI driver addon version.

## Impact

- New Terraform: `terraform/environments/aws-state-bootstrap/`,
  `terraform/modules/aws-vpc-network/`, `terraform/modules/aws-eks/`,
  `terraform/environments/aws/`, `terraform/environments/self-hosted-k8s/`,
  `terraform/environments/docker/`.
- Modified: `config/versions.yaml` (new pins + `tracked_consumers` entries),
  `config/versions.schema.json` (new pin fields), `docs/01-architecture.md`
  and `docs/03-diagrams.md` (reflect implemented Terraform compute/bootstrap
  roots), and the existing `terraform/environments/aws-storage/README.md`
  cross-references to the new environments.
- No changes to `nighthawk/` Python code, `config/tenants.example.yaml`, or
  any already-archived secrets-workflow/compatibility-matrix behavior beyond
  the pinned-inventory addition above.
- Out of scope: Ansible host/cluster automation (Phase 5), Helm chart
  installation, Kafka/Strimzi deployment, and any billable `terraform apply`
  against real AWS infrastructure — this change produces statically
  validated, plan-only Terraform, consistent with Phase 1's precedent.
