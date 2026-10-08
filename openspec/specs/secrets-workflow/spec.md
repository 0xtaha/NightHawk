# secrets-workflow

## Purpose

Provides the SOPS + age secret lifecycle (generation, encryption, decryption,
rotation, and runtime materialization) that every tenant, gateway, and
storage credential referenced by the platform configuration contract relies
on, so secrets are never committed in plaintext and never silently missing.

## Requirements

### Requirement: Age recipient generation
The system SHALL provide a command that generates a new age key pair (public
recipient and private key) for use as a SOPS encryption recipient, and SHALL
refuse to overwrite an existing private key file without an explicit
confirmation flag.

#### Scenario: Generating a fresh recipient
- **WHEN** an operator runs the recipient-generation command with a target
  path that does not yet exist
- **THEN** the system writes a new age private key to that path with
  restrictive file permissions and prints the corresponding public recipient
  string

#### Scenario: Refusing to overwrite silently
- **WHEN** an operator runs the recipient-generation command with a target
  path that already contains a private key, without the overwrite flag
- **THEN** the system fails explicitly without modifying the existing file

### Requirement: Secret encryption under the `secrets/` convention
The system SHALL provide a command that encrypts a plaintext value or file
into a SOPS-encrypted file under `secrets/`, keyed by the same `file`/`key`
pair referenced from a platform document's `secrets` map, using one or more
age recipients supplied by the operator or read from a configured recipients
list.

#### Scenario: Encrypting a new secret
- **WHEN** an operator supplies a plaintext value, a destination key name, and
  at least one age recipient
- **THEN** the system writes a SOPS-encrypted file under `secrets/` containing
  that key, encrypted to the supplied recipients, and never writes the
  plaintext value to disk outside that encrypted file

#### Scenario: Rejecting an empty recipient list
- **WHEN** an operator requests encryption without supplying any age
  recipients and none are configured
- **THEN** the system fails explicitly and performs no encryption

### Requirement: Secret rotation without reference changes
The system SHALL provide a command that replaces the plaintext value behind
an existing `secrets/` file/key pair with a new value, re-encrypted to the
same recipients, without requiring any change to the `secret_ref` values in
platform documents that point to it.

#### Scenario: Rotating an existing secret
- **WHEN** an operator supplies a new plaintext value for an existing
  `file`/`key` pair under `secrets/`
- **THEN** the system re-encrypts the file with the new value under the same
  key and the previously validated platform document's `secret_ref` continues
  to resolve to the same file/key pair

#### Scenario: Rotating a nonexistent secret
- **WHEN** an operator requests rotation for a `file`/`key` pair that does not
  exist under `secrets/`
- **THEN** the system fails explicitly instead of creating a new secret file

### Requirement: Restricted runtime materialization
The system SHALL provide a command that decrypts the secrets referenced by a
validated platform document into a runtime materialization directory that is
excluded from version control, created with restrictive permissions, and
removable by an explicit cleanup action.

#### Scenario: Materializing secrets for a validated platform document
- **WHEN** an operator runs the materialization command against a platform
  document that passes configuration validation
- **THEN** the system decrypts every referenced secret into a target
  directory created with owner-only permissions, and that directory is
  covered by the repository's ignore rules

#### Scenario: Refusing to materialize an invalid configuration
- **WHEN** an operator runs the materialization command against a platform
  document that fails configuration validation
- **THEN** the system fails explicitly and decrypts no secrets

#### Scenario: Explicit cleanup
- **WHEN** an operator runs the cleanup command for a prior materialization
  directory
- **THEN** the system removes the decrypted contents of that directory and
  reports success, leaving no residual decrypted material

### Requirement: Ordinary validation never decrypts secrets
The system SHALL NOT decrypt any secret during ordinary configuration
`validate` or `render-contracts` operations; those operations SHALL continue
to check only that every `secret_ref` resolves to a known `file`/`key` pair
declared in the platform document's `secrets` map.

#### Scenario: Validating with unresolved on-disk secrets
- **WHEN** an operator runs `validate` against a platform document whose
  referenced `secrets/` files do not exist on disk
- **THEN** validation succeeds or fails based solely on reference consistency
  within the document, and no attempt is made to read or decrypt any
  `secrets/` file

### Requirement: Production recipient and trust input requirements
The system SHALL require an operator-supplied list of age recipients and
trust/DNS inputs before performing any encryption or materialization against
a platform document whose `profile` is `production`, and SHALL reject
locally auto-generated recipients for that profile.

#### Scenario: Rejecting an auto-generated recipient in production
- **WHEN** an operator attempts to encrypt a secret for a platform document
  with `profile: production` using a recipient generated by the local
  development helper without explicit operator confirmation
- **THEN** the system fails explicitly and performs no encryption

#### Scenario: Accepting operator-supplied production recipients
- **WHEN** an operator supplies explicit age recipients and required trust
  inputs for a `profile: production` platform document
- **THEN** encryption and materialization proceed using only those supplied
  recipients and inputs

### Requirement: Prerequisite tooling checks
The system SHALL provide a command that verifies the local SOPS and age
executables are present and report their resolved versions against the
compatibility matrix's pinned versions before any secrets-workflow command
performs encryption, decryption, rotation, or materialization.

#### Scenario: All prerequisites satisfied
- **WHEN** an operator runs the prerequisite check and the installed SOPS and
  age versions match the pins recorded in the compatibility matrix
- **THEN** the system reports success and the requested secrets-workflow
  command proceeds

#### Scenario: Missing or mismatched tooling
- **WHEN** an operator runs a secrets-workflow command and SOPS or age is
  missing, or an installed version does not match the compatibility matrix's
  pin
- **THEN** the system fails explicitly before performing any encryption,
  decryption, rotation, or materialization, and names the missing or
  mismatched tool and its expected version
