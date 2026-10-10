# Spec Delta

## ADDED Requirements

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
