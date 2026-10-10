# Spec Delta

## ADDED Requirements

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

### Requirement: Checksummed secrets tooling
The compatibility matrix SHALL record a checksum for each supported platform
build of the pinned `sops` and `age` tools, and any automated download of
them SHALL be verified against it.

#### Scenario: Verified download
- **WHEN** the quickstart tooling downloads `sops` or `age`
- **THEN** it installs the file only if its checksum equals the matrix value

#### Scenario: Checksum mismatch
- **WHEN** a downloaded file's checksum differs from the matrix value
- **THEN** the file is discarded and the command fails naming the tool
