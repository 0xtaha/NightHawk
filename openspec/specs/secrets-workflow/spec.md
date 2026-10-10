# secrets-workflow

## Purpose

Provides the secret lifecycle on an operator-provided HashiCorp Vault
(connection, authentication, storage, rotation, and runtime materialization)
that every tenant, gateway, and storage credential referenced by the platform
configuration contract relies on, so secrets are never kept in the repository,
never replaced by accident, and never silently missing.

## Requirements

### Requirement: Secret rotation without reference changes
The system SHALL provide a command that replaces the value behind an existing
secret reference with a new value as a new version in Vault, without
requiring any change to the `secret_ref` values in platform documents that
point to it, and without changing any other key stored at the same path.

#### Scenario: Rotating an existing secret
- **WHEN** an operator supplies a new value for a secret reference whose path
  and key already hold a value
- **THEN** Vault holds the new value as a newer version at the same path and
  key, every other key at that path is unchanged, and the previously
  validated platform document's `secret_ref` continues to resolve

#### Scenario: Rotating a nonexistent secret
- **WHEN** an operator requests rotation for a reference whose path or key
  holds no value
- **THEN** the system fails explicitly instead of creating a new secret

#### Scenario: Concurrent change
- **WHEN** the path is changed by someone else between the system reading it
  and writing the rotated value
- **THEN** the write is rejected, nothing is overwritten, and the command
  fails asking the operator to retry

### Requirement: Restricted runtime materialization
The system SHALL provide a command that reads the secrets referenced by a
validated platform document from Vault into a runtime materialization
directory that is excluded from version control, created with restrictive
permissions, and removable by an explicit cleanup action.

#### Scenario: Materializing secrets for a validated platform document
- **WHEN** an operator runs the materialization command against a platform
  document that passes configuration validation
- **THEN** the system reads every referenced secret into a target directory
  created with owner-only permissions, and that directory is covered by the
  repository's ignore rules

#### Scenario: Refusing to materialize an invalid configuration
- **WHEN** an operator runs the materialization command against a platform
  document that fails configuration validation
- **THEN** the system fails explicitly and reads no secrets

#### Scenario: Referenced secret is absent
- **WHEN** a referenced path or key holds no value in Vault
- **THEN** the command fails naming every absent reference and leaves no
  partial materialization behind

#### Scenario: Explicit cleanup
- **WHEN** an operator runs the cleanup command for a prior materialization
  directory
- **THEN** the system removes the contents of that directory and reports
  success, leaving no residual secret material

### Requirement: Ordinary validation never decrypts secrets
The system SHALL NOT contact Vault or read any secret during ordinary
configuration `validate` or `render-contracts` operations; those operations
SHALL continue to check only that every `secret_ref` resolves to a path and
key declared in the platform document's `secrets` map.

#### Scenario: Validating with unresolved on-disk secrets
- **WHEN** an operator runs `validate` against a platform document whose
  referenced secrets hold no value in Vault and have never been materialized
  on disk
- **THEN** validation succeeds or fails based solely on reference consistency
  within the document, and no attempt is made to look up any secret

#### Scenario: Validating without a Vault
- **WHEN** an operator runs `validate` or `render-contracts` with no Vault
  reachable and no Vault credential present
- **THEN** the outcome depends solely on reference consistency within the
  document, and no connection to Vault is attempted

### Requirement: Vault connection declared in the platform document
The platform document SHALL declare the Vault server address, the key-value
mount, and the PKI mount and roles, and each entry of its `secrets` map SHALL
name a path within the key-value mount and a key at that path. The system
SHALL refuse a plaintext Vault address unless it is a loopback address.

#### Scenario: Complete declaration
- **WHEN** a platform document declares the Vault address, mounts, roles, and
  a path and key for every secret
- **THEN** validation succeeds

#### Scenario: Plaintext address
- **WHEN** a platform document declares an `http` Vault address that is not
  loopback
- **THEN** validation fails and names the address

#### Scenario: Earlier document shape
- **WHEN** a platform document uses the earlier file-and-key secret shape
- **THEN** validation fails stating that the document's schema version is no
  longer supported and what replaced it

### Requirement: Vault authentication without stored credentials
The system SHALL authenticate to Vault with a token or an AppRole supplied
through the environment or a file with owner-only permissions, and SHALL NOT
accept a Vault credential in the platform document or as a command-line
argument, write one to disk, or print one.

#### Scenario: Token from the environment
- **WHEN** a Vault token is present in the environment
- **THEN** commands that need Vault use it

#### Scenario: AppRole from files
- **WHEN** an operator supplies role and secret identifier files
- **THEN** the system logs in with them and uses the resulting token only for
  that command

#### Scenario: No credential
- **WHEN** a command needs Vault and no credential is supplied
- **THEN** it fails before contacting Vault and names the ways to supply one

#### Scenario: Credential file readable by others
- **WHEN** a supplied credential file is readable by group or others
- **THEN** the command fails naming the file and does not use it

#### Scenario: Rejected credential
- **WHEN** Vault rejects the credential or denies a path
- **THEN** the command fails naming the path and the denied operation, and
  prints no credential

### Requirement: Storing a secret never replaces existing values
The system SHALL provide a command that stores a value at a secret
reference's path and key in Vault, reading the value from standard input or a
file and never from a command-line argument. Storing SHALL keep every other
key at that path, SHALL refuse a key that already holds a value, and SHALL be
rejected rather than overwrite when the path changed concurrently.

#### Scenario: First key at a path
- **WHEN** an operator stores a value at a path that does not exist
- **THEN** the path is created holding that key

#### Scenario: Further key at an existing path
- **WHEN** an operator stores a new key at a path that already holds other
  keys
- **THEN** the path afterwards holds the new key and every key it held before

#### Scenario: Key already holds a value
- **WHEN** an operator stores a key that already holds a value
- **THEN** the system fails explicitly, changes nothing, and names rotation
  as the way to replace a value

#### Scenario: Undeclared reference
- **WHEN** an operator stores a value for a reference the platform document
  does not declare
- **THEN** the system fails explicitly and writes nothing

### Requirement: Production Vault safeguards
For a platform document whose `profile` is `production`, the system SHALL
refuse to read or write secrets when the Vault address is plaintext or
loopback, or when the supplied credential carries Vault's root policy.

#### Scenario: Loopback Vault in production
- **WHEN** a `production` document declares a loopback Vault address
- **THEN** validation fails and names the address

#### Scenario: Root token in production
- **WHEN** a command runs against a `production` document with a token that
  carries the root policy
- **THEN** the command fails before reading or writing any secret

#### Scenario: Scoped credential in production
- **WHEN** a command runs against a `production` document with a TLS Vault
  address and a non-root credential
- **THEN** the command proceeds

### Requirement: Vault prerequisite check
The system SHALL provide a command that verifies the declared Vault is
reachable and unsealed, reports a version the compatibility matrix supports,
accepts the supplied credential, has the declared key-value mount, PKI
mount with a certificate authority, and PKI roles, and that those roles would
sign every hostname and collector identity the platform document declares;
every command that reads or writes secrets or requests a certificate SHALL
fail before doing so when that check fails.

#### Scenario: All prerequisites satisfied
- **WHEN** the check runs against a correctly configured Vault
- **THEN** it reports success and names the Vault version

#### Scenario: Sealed or unreachable Vault
- **WHEN** Vault is unreachable or sealed
- **THEN** the check fails and says which

#### Scenario: Unsupported version
- **WHEN** Vault reports a version outside the supported range
- **THEN** the check fails naming the reported and supported versions

#### Scenario: Missing mount or role
- **WHEN** a declared mount or PKI role does not exist or is not readable
  with the supplied credential
- **THEN** the check fails and names each one

#### Scenario: Role does not cover a declared name
- **WHEN** a PKI role in Vault would refuse a hostname or collector identity
  the platform document declares
- **THEN** the check fails, names the role and each refused name, and points
  to the rendered role definitions

### Requirement: Rendered Vault access requirements
The system SHALL render, from a validated platform document and without
contacting Vault, the access policy and the PKI role definitions the
platform needs: read and write only on the declared secret paths,
certificate signing limited to the declared hostnames and collector
identities, certificate revocation in the declared PKI mount, and inspection
of its own credential.

#### Scenario: Policy covers exactly the declared paths
- **WHEN** access requirements are rendered
- **THEN** the policy grants creating, reading, and updating each declared
  secret path, signing with and reading the two declared PKI roles, revoking
  a certificate in the declared PKI mount, and looking up its own token, and
  nothing else

#### Scenario: Roles limited to declared names
- **WHEN** access requirements are rendered
- **THEN** the server role allows only the declared gateway, Grafana, and
  storage hostnames with server usage, and the collector role allows only
  the declared certificate identities with client usage

#### Scenario: Deterministic output
- **WHEN** access requirements are rendered twice from the same document
- **THEN** the output is byte-identical

### Requirement: Development Vault bootstrap
The system SHALL provide a command that configures a development Vault with
the mounts, a certificate authority, the PKI roles, and the policy from the
rendered access requirements, and SHALL refuse to run for a `production`
platform document or without an explicit confirmation that the Vault is
disposable.

#### Scenario: Bootstrapping a development Vault
- **WHEN** an operator runs the bootstrap with the confirmation against an
  empty development Vault and a `development` document
- **THEN** the prerequisite check then succeeds against that Vault

#### Scenario: Running it again
- **WHEN** the bootstrap is run again against the same Vault
- **THEN** it changes nothing and keeps the existing certificate authority

#### Scenario: Production document
- **WHEN** the bootstrap is run for a `production` platform document
- **THEN** it fails explicitly and changes nothing

#### Scenario: Missing confirmation
- **WHEN** the bootstrap is run without the confirmation
- **THEN** it fails explicitly and changes nothing
