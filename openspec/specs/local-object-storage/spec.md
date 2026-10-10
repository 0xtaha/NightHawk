# local-object-storage

## Purpose

Provisions the SeaweedFS object storage that local profiles use, with
separate buckets and least-privilege identities per signal backend, served
only over TLS on a private network.

## Requirements

### Requirement: Buckets and identities from the platform document
Storage initialization SHALL create one bucket per storage binding and one
storage identity per distinct binding identity, with exactly the names the
platform document declares, and SHALL be safe to run repeatedly.

#### Scenario: First initialization
- **WHEN** storage initialization runs against empty storage
- **THEN** every declared bucket exists afterwards and each declared identity
  can authenticate

#### Scenario: Repeat initialization
- **WHEN** storage initialization runs again
- **THEN** it succeeds and no existing object is removed

### Requirement: Least-privilege identities
A storage identity SHALL be able to read, write, and list only the buckets
bound to its signal backend, and anonymous access SHALL be disabled.

#### Scenario: Cross-bucket access denied
- **WHEN** the identity of one signal backend lists, reads, or writes another
  backend's bucket
- **THEN** the object store denies the request

#### Scenario: Anonymous access denied
- **WHEN** a request without credentials reaches the object store
- **THEN** it is denied

### Requirement: Storage identity generation
The system SHALL provide a command that generates the access key and secret
key for a storage identity declared in the platform document and stores them
only in Vault under that identity's secret reference, refusing to replace an
existing value.

#### Scenario: Generating a declared identity
- **WHEN** an operator generates the keys for a declared storage identity
- **THEN** a new access key and secret key are stored in Vault at its
  reference and neither is printed

#### Scenario: Undeclared identity
- **WHEN** an operator requests keys for a secret that is not a storage
  identity of the platform document
- **THEN** the system fails explicitly and writes nothing

### Requirement: TLS-only storage endpoint
The object store's S3 endpoint SHALL be served only over TLS with a
certificate valid for the hostname in the storage bindings, trusted through
the bindings' CA reference.

#### Scenario: Backend connects with verification
- **WHEN** a backend connects to the storage endpoint
- **THEN** it verifies the server certificate against the declared CA and
  the connection succeeds

#### Scenario: Plaintext refused
- **WHEN** a client attempts a plaintext connection to the S3 endpoint
- **THEN** the request does not succeed

### Requirement: Storage state persists
Object data and the metadata needed to find it SHALL be kept on named
volumes.

#### Scenario: Object store restart
- **WHEN** the object store is restarted after objects were written
- **THEN** those objects are still readable
