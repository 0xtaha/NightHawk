# compatibility-matrix

## Purpose

Defines the single authoritative, version-pinned, source-verified record of
every external component, tool, and chart NightHawk depends on across the
Docker, self-hosted Kubernetes, and AWS deployment profiles, and the
validation that keeps those pins complete and internally consistent.

## Requirements

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

### Requirement: Supported OS distribution and architecture matrix
The compatibility matrix SHALL declare the explicit set of supported
operating system distributions, versions, and CPU architectures for
self-hosted Docker hosts and for k3s/Longhorn nodes, and SHALL distinguish
between the two if the supported sets differ.

#### Scenario: Declaring distinct support sets
- **WHEN** the set of OS distributions/versions verified for k3s and
  Longhorn is narrower than the set verified for plain Docker hosts
- **THEN** the compatibility matrix records both sets separately rather than
  a single combined list

#### Scenario: Rejecting an undeclared target
- **WHEN** a consumer of the matrix checks an OS distribution/version/
  architecture that is not present in either declared set
- **THEN** the check reports that target as unsupported rather than silently
  assuming compatibility

### Requirement: Schema-validated matrix document
The compatibility matrix SHALL be a structured document validated against a
schema that rejects unknown fields, missing required pin fields, and
malformed version strings, consistent with how the platform and network
contracts are validated.

#### Scenario: Rejecting a malformed version string
- **WHEN** the compatibility matrix document contains a component entry whose
  version field is not a valid version string for that component's versioning
  scheme
- **THEN** validation fails and identifies the offending component and field

#### Scenario: Rejecting an unknown field
- **WHEN** the compatibility matrix document contains a field not defined by
  its schema
- **THEN** validation fails rather than silently ignoring the unknown field

### Requirement: Pin-change review gate
Changing a pinned version or storage/architecture mode for a component already
marked `runtime_verified: true` SHALL require the change to first reset that
component's verification flag to `false` in the same update, so a version
bump cannot silently inherit a prior version's runtime verification.

#### Scenario: Bumping a verified component's version
- **WHEN** an update changes the pinned version of a component whose
  `runtime_verified` flag is currently `true`
- **THEN** the same update SHALL set that component's `runtime_verified` flag
  to `false`, or validation fails

#### Scenario: Bumping an unverified component's version
- **WHEN** an update changes the pinned version of a component whose
  `runtime_verified` flag is already `false`
- **THEN** validation succeeds without requiring any additional flag change

### Requirement: Consumers resolve pins from the single matrix
Any documented deployment automation (Ansible roles/playbooks, Helm value
files, Terraform modules) that references a pinned tool, chart, or component
version SHALL resolve that version from the compatibility matrix rather than
declaring an independent, potentially divergent pin.

#### Scenario: Detecting a divergent pin
- **WHEN** a deployment automation file declares a version for a component
  that is also pinned in the compatibility matrix, and the two values differ
- **THEN** the drift check reports the divergence and names both the
  automation file and the matrix entry

### Requirement: Container images pinned by digest
The compatibility matrix SHALL record, for every container image the Compose
profile runs or builds on, the image reference and an immutable digest, and
the drift check SHALL fail when a Compose file references an image that
differs from the matrix.

#### Scenario: Digest recorded for every image
- **WHEN** the compatibility matrix is validated
- **THEN** each Compose image entry has a repository, a tag matching the
  component's pinned version, and a digest

#### Scenario: Divergent image reference
- **WHEN** a Compose file references an image or digest that differs from the
  matrix entry
- **THEN** the drift check reports the file and the matrix entry

### Requirement: Chart and backend pins agree
For every Helm chart the matrix pins that packages a backend the matrix also
pins, matrix validation SHALL fail when the chart's declared application
version differs from the backend's pinned version, unless the matrix records
that difference for the chart together with a reason.

#### Scenario: Versions agree
- **WHEN** a chart's application version equals its backend's pinned version
- **THEN** validation succeeds

#### Scenario: Unrecorded difference
- **WHEN** a chart's application version differs from its backend's pinned
  version and no exception is recorded
- **THEN** validation fails and names the chart and both versions

#### Scenario: Recorded difference
- **WHEN** a chart's application version differs from its backend's pinned
  version and the matrix records the difference with a reason
- **THEN** validation succeeds

#### Scenario: Stale exception
- **WHEN** the matrix records a difference for a chart whose application
  version equals its backend's pinned version
- **THEN** validation fails and names the chart

### Requirement: Checksummed Terraform download
The compatibility matrix SHALL record a checksum for each supported platform
build of the pinned Terraform, and any automated download of it SHALL be
verified against that checksum.

#### Scenario: Verified download
- **WHEN** the tooling downloads Terraform
- **THEN** it installs the executable only if the downloaded archive's
  checksum equals the matrix value

#### Scenario: Checksum mismatch
- **WHEN** the downloaded archive's checksum differs from the matrix value
- **THEN** nothing is installed and the command fails naming Terraform

#### Scenario: Terraform absent when its tests run
- **WHEN** the documented test command runs on a machine without the pinned
  Terraform
- **THEN** the Terraform contract tests are reported as skipped with the
  reason, not silently omitted and not failed
