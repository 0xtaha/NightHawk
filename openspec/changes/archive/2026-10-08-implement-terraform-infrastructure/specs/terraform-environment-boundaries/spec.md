# Spec Delta

## Purpose

Formalizes the Terraform/Ansible ownership boundary for the self-hosted
Kubernetes and Docker profiles, where no cloud account or provisionable
infrastructure exists for Terraform to manage.

## ADDED Requirements

### Requirement: Self-hosted and Docker Terraform roots apply no resources
The system SHALL provide `environments/self-hosted-k8s` and
`environments/docker` Terraform execution roots that declare zero managed
resources, documenting that host bootstrap (k3s, Cilium, MetalLB, Longhorn,
Traefik, cert-manager) and SeaweedFS provisioning for these profiles remain
entirely owned by Ansible and Helm, consistent with
`docs/01-architecture.md`'s statement that Terraform never competes to
manage those local resources.

#### Scenario: Validating a self-hosted or Docker root
- **WHEN** an operator runs `terraform validate` and `terraform plan` against
  either root
- **THEN** the plan reports zero resources to add, change, or destroy

#### Scenario: Rejecting an invented resource
- **WHEN** a future change adds a cloud-provider-managed resource (for
  example, a VM or firewall rule) to either root
- **THEN** a repository check (static review or CI lint) flags the addition
  as outside this capability's declared scope, requiring an explicit
  decision to expand it rather than silent scope creep

### Requirement: Documented ownership boundary
Each of these roots SHALL document, in its own README, which concerns are
intentionally out of its scope and which tool (Ansible, Helm) owns them
instead, so an operator does not mistake an empty root for an unfinished one.

#### Scenario: Reading the self-hosted root's documentation
- **WHEN** an operator reads the `environments/self-hosted-k8s` README
- **THEN** it explicitly states that k3s/Cilium/MetalLB/Longhorn/Traefik/
  cert-manager bootstrap and SeaweedFS provisioning are owned by Ansible and
  Helm, not this root

#### Scenario: Reading the Docker root's documentation
- **WHEN** an operator reads the `environments/docker` README
- **THEN** it explicitly states that Docker Compose bootstrap and local
  SeaweedFS provisioning are owned by Ansible/Compose orchestration, not
  this root
