# Spec Delta

## MODIFIED Requirements

### Requirement: Supported OS distribution and architecture matrix
The compatibility matrix SHALL declare the explicit set of supported
operating system distributions, versions, and CPU architectures for
self-hosted Docker hosts and for k3s/Longhorn nodes, and SHALL distinguish
between the two if the supported sets differ. Each entry SHALL record what
its support rests on: role tests in a container, a run on a real host, or
nothing yet. The Docker host set SHALL include Debian-family and
RHEL-family systems.

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

#### Scenario: Evidence recorded
- **WHEN** the compatibility matrix is validated
- **THEN** every operating system entry states the evidence behind it, and
  validation fails for an entry that states none

#### Scenario: RHEL family
- **WHEN** a Rocky Linux 9 or AlmaLinux 9 host is checked as a Docker host
- **THEN** it is reported as supported, and as unsupported when checked as a
  cluster node

## ADDED Requirements

### Requirement: Pinned automation tooling
The compatibility matrix SHALL record an exact version for `ansible-core`,
for each Ansible collection the automation uses, and for the lint and
role-test tools, and a repository check SHALL fail when the automation's own
requirement files differ from the matrix.

#### Scenario: Requirement files match
- **WHEN** the drift check runs and every requirement file names the pinned
  versions
- **THEN** the check passes

#### Scenario: Divergent collection version
- **WHEN** a requirement file names a different version than the matrix
- **THEN** the drift check reports the file and the pin

### Requirement: Checksums for host-installed artifacts
For every artifact a role downloads and installs on a host, the
compatibility matrix SHALL record a checksum for each supported
architecture, or, for a package repository, the fingerprint of its signing
key.

#### Scenario: Complete entries
- **WHEN** the compatibility matrix is validated
- **THEN** the k3s binary and the Alloy archive have a checksum for each
  supported architecture, and the Docker package repository has a signing
  key fingerprint

#### Scenario: Missing checksum
- **WHEN** an artifact entry lacks a checksum for a supported architecture
- **THEN** validation fails and names the artifact and architecture
