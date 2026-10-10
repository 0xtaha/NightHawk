# Spec Delta

## REMOVED Requirements

### Requirement: Checksummed secrets tooling
**Reason**: `sops` and `age` are no longer used or downloaded.
**Migration**: Remove the `sops` and `age` entries and their checksums from
the matrix and from the tool download command. The supported Vault version
and the test image digest are recorded under "Complete pinned inventory".

## MODIFIED Requirements

### Requirement: Complete pinned inventory
The compatibility matrix SHALL record an explicit, exact version pin for
every component the architecture requires across all three deployment
profiles: Mimir, Loki, Tempo, Pyroscope, SeaweedFS (and its Helm chart), k3s,
Cilium, MetalLB, Longhorn, Traefik, cert-manager, Strimzi and the Kafka
version it manages, the Grafana Helm charts used by the platform, Grafana
Alloy, Terraform, the AWS Terraform provider, the EKS-managed Kubernetes
control-plane version, and the EBS CSI driver addon version. A pin
SHALL include a resolvable version identifier and, where the tool or chart
publishes one, a source reference (release URL, changelog, or digest)
supporting that it was verified rather than guessed. For Vault, which the
operator provides, the matrix SHALL instead record the range of server
versions the platform supports and the exact version and image digest the
platform's tests run against.

#### Scenario: Validating a complete matrix
- **WHEN** the compatibility matrix is validated and every required
  component listed above has an exact version pin and a source reference
- **THEN** validation succeeds

#### Scenario: Detecting a missing component pin
- **WHEN** the compatibility matrix is validated and a required component has
  no entry
- **THEN** validation fails and names the missing component

#### Scenario: Vault entry
- **WHEN** the compatibility matrix is validated
- **THEN** it has a supported Vault version range, and a tested version
  within that range with an image digest

#### Scenario: Tested version outside the supported range
- **WHEN** the tested Vault version is not within the supported range
- **THEN** validation fails and names both
