# Spec Delta

## ADDED Requirements

### Requirement: Pinned Kubernetes tooling and locked charts
The compatibility matrix SHALL pin every tool and chart the Kubernetes
deployment uses: the Helm client with a checksum per platform, the manifest
validator, the Ansible Kubernetes collection and its client library, and
each chart with its repository, version, and content digest. A consumer
that names a different version, and a chart whose content differs from its
digest, SHALL fail the pin check or the installation.

#### Scenario: Chart version drift
- **WHEN** a release names a chart version that differs from the matrix
- **THEN** the pin check fails and names the release

#### Scenario: Helm download
- **WHEN** the Helm client is fetched
- **THEN** it is kept only if its checksum matches the matrix

#### Scenario: Chart without a digest
- **WHEN** the matrix lists a chart the deployment installs without a
  content digest
- **THEN** matrix validation fails

### Requirement: Kubernetes images pinned by digest
Every container image a Kubernetes release runs SHALL be listed in the
matrix with a digest, including images of add-ons and operators, and the
matrix SHALL record for each component whether it was started in a cluster
or only rendered.

#### Scenario: Image missing from the matrix
- **WHEN** a templated release references an image that is not in the
  matrix
- **THEN** the check fails and names the image

#### Scenario: Rendered-only component
- **WHEN** a component was rendered and validated but never started
- **THEN** its matrix entry says so
