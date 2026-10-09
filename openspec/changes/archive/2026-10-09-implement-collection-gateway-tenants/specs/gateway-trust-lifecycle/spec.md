# Spec Delta

## Purpose

Provides the generation, issuance, rotation, and revocation of the gateway
credentials and certificates that tenants' collectors and Grafana data sources
authenticate with, so trust material is created reproducibly and can be
replaced without an ingestion outage.

## ADDED Requirements

### Requirement: Gateway credential generation
The system SHALL provide a command that generates a high-entropy gateway
credential secret for a credential declared in the platform document and
stores it only in SOPS-encrypted form under the credential's secret reference.
The system SHALL reject operator-supplied gateway credential secrets below a
documented minimum length when rendering the gateway policy.

#### Scenario: Generating a declared credential
- **WHEN** an operator generates the secret for a credential declared in a
  valid platform document
- **THEN** the system encrypts a newly generated secret to the supplied
  recipients at that credential's file/key reference and never prints or
  writes the plaintext elsewhere

#### Scenario: Refusing to overwrite
- **WHEN** an operator generates a secret for a credential whose file/key
  already holds a value, without the rotation command
- **THEN** the system fails explicitly without changing the existing value

#### Scenario: Weak supplied secret
- **WHEN** the gateway policy is rendered and a credential's materialized
  secret is shorter than the documented minimum
- **THEN** rendering fails and names the credential without printing the
  secret

### Requirement: Development certificate authority
The system SHALL provide a command that creates a certificate authority for
non-production use, storing its private key only in SOPS-encrypted form. For a
`production` profile the system SHALL require an operator-supplied client CA
and SHALL refuse to create or use a locally generated one unless explicitly
confirmed.

#### Scenario: Creating a development CA
- **WHEN** an operator creates a CA for a `development` platform document
- **THEN** the system stores the CA private key SOPS-encrypted at the
  document's CA key reference and the CA certificate at its CA reference

#### Scenario: Production refuses a local CA
- **WHEN** an operator attempts to create a local CA for a `production`
  platform document without the explicit confirmation flag
- **THEN** the system fails explicitly and creates nothing

### Requirement: Certificate issuance bound to declared identities
The system SHALL issue a collector client certificate only for a certificate
identity declared by a credential in the platform document, carrying that
identity as its URI subject alternative name, with an operator-supplied
validity period and client-authentication usage only. The system SHALL issue
gateway server certificates only for the hostname declared in the platform
document's gateway settings.

#### Scenario: Issuing for a declared identity
- **WHEN** an operator requests a collector certificate for a credential that
  declares a certificate identity, with an explicit validity period
- **THEN** the system writes a private key and certificate with owner-only
  permissions whose URI identity equals the declared identity

#### Scenario: Undeclared identity
- **WHEN** an operator requests a collector certificate for an identity that
  no credential declares
- **THEN** the system fails explicitly and issues nothing

#### Scenario: Missing validity period
- **WHEN** an operator requests a certificate without a validity period
- **THEN** the system fails explicitly rather than applying a default

### Requirement: Rotation without an ingestion gap
The system SHALL support replacing a gateway credential by declaring a second
credential for the same tenant/datastream and permission, so both are accepted
during a transition, and SHALL support replacing a collector certificate with
a new one carrying the same identity without any policy change.

#### Scenario: Overlapping credentials
- **WHEN** a platform document declares two ingestion credentials for the same
  tenant/datastream
- **THEN** the document is valid and the rendered gateway policy accepts
  either credential for that pair's backend ID

#### Scenario: Completing a rotation
- **WHEN** the older credential is removed from the platform document and the
  policy is re-rendered and loaded
- **THEN** the older credential is rejected and the newer one continues to be
  accepted

#### Scenario: Renewed certificate
- **WHEN** a collector switches to a newly issued certificate with the same
  declared identity
- **THEN** the gateway accepts it with no change to the platform document

### Requirement: Certificate revocation
The system SHALL support revoking a single issued certificate by listing its
fingerprint in the platform document, and revoking every certificate for a
collector by removing its declared identity.

#### Scenario: Revoking by fingerprint
- **WHEN** an operator adds a certificate's fingerprint to the revoked list
  and re-renders the gateway policy
- **THEN** the rendered policy denies that certificate while other
  certificates remain accepted

#### Scenario: Malformed fingerprint
- **WHEN** the revoked list contains a value that is not a well-formed
  fingerprint
- **THEN** platform document validation fails and names the offending entry

### Requirement: Documented lifecycle procedures
The documentation SHALL give the concrete command sequence, expected outcome,
and rollback for first-time issuance, credential rotation, certificate
renewal, and revocation.

#### Scenario: Rotation runbook
- **WHEN** an operator follows the documented credential rotation procedure
- **THEN** each step names the exact command to run and the observable result
  that confirms it succeeded
