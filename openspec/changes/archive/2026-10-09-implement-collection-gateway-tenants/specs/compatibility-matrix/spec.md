# Spec Delta

## MODIFIED Requirements

### Requirement: Complete pinned inventory
The compatibility matrix SHALL record an explicit, exact version pin for
every component the architecture requires across all three deployment
profiles: Mimir, Loki, Tempo, Pyroscope, SeaweedFS (and its Helm chart), k3s,
Cilium, MetalLB, Longhorn, Traefik, cert-manager, Strimzi and the Kafka
version it manages, the Grafana Helm charts used by the platform, Grafana
Alloy, Terraform, the AWS Terraform provider, the EKS-managed Kubernetes
control-plane version, the EBS CSI driver addon version, SOPS, and age. A pin
SHALL include a resolvable version identifier and, where the tool or chart
publishes one, a source reference (release URL, changelog, or digest)
supporting that it was verified rather than guessed.

#### Scenario: Validating a complete matrix
- **WHEN** the compatibility matrix is validated and every required
  component listed above has an exact version pin and a source reference
- **THEN** validation succeeds

#### Scenario: Detecting a missing component pin
- **WHEN** the compatibility matrix is validated and a required component has
  no entry
- **THEN** validation fails and names the missing component
