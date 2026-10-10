# Spec Delta

## REMOVED Requirements

### Requirement: Development certificate authority
**Reason**: The platform no longer creates or holds a certificate authority.
Vault's PKI engine is the authority in every profile. Replaced by
"Certificate authority held in Vault".
**Migration**: Remove the CA and CA key secret references from the platform
document and declare the PKI mount and roles instead. For local use, the
development Vault bootstrap creates the authority inside Vault. Certificates
issued by the earlier local CA are not trusted afterwards and are issued
again.

## MODIFIED Requirements

### Requirement: Gateway credential generation
The system SHALL provide a command that generates a high-entropy gateway
credential secret for a credential declared in the platform document and
stores it only in Vault under the credential's secret reference.
The system SHALL reject operator-supplied gateway credential secrets below a
documented minimum length when rendering the gateway policy.

#### Scenario: Generating a declared credential
- **WHEN** an operator generates the secret for a credential declared in a
  valid platform document
- **THEN** the system stores a newly generated secret in Vault at that
  credential's path and key and never prints or writes the plaintext
  elsewhere

#### Scenario: Refusing to overwrite
- **WHEN** an operator generates a secret for a credential whose path and key
  already hold a value, without the rotation command
- **THEN** the system fails explicitly without changing the existing value

#### Scenario: Weak supplied secret
- **WHEN** the gateway policy is rendered and a credential's materialized
  secret is shorter than the documented minimum
- **THEN** rendering fails and names the credential without printing the
  secret

### Requirement: Certificate issuance bound to declared identities
The system SHALL obtain a collector client certificate from Vault's PKI
engine only for a certificate identity declared by a credential in the
platform document, carrying that identity as its URI subject alternative
name, with an operator-supplied validity period and client-authentication
usage only. The system SHALL obtain server certificates only for hostnames
the platform document declares: the gateway hostname together with the
Grafana UI hostname, or the hostnames of the storage binding endpoints. The
private key SHALL be generated where the command runs and SHALL NOT be sent
to Vault.

#### Scenario: Issuing for a declared identity
- **WHEN** an operator requests a collector certificate for a credential that
  declares a certificate identity, with an explicit validity period
- **THEN** the system writes a private key and a certificate signed by
  Vault's certificate authority with owner-only permissions, whose URI
  identity equals the declared identity

#### Scenario: Undeclared identity
- **WHEN** an operator requests a collector certificate for an identity that
  no credential declares
- **THEN** the system fails explicitly, requests nothing from Vault, and
  issues nothing

#### Scenario: Missing validity period
- **WHEN** an operator requests a certificate without a validity period
- **THEN** the system fails explicitly rather than applying a default

#### Scenario: Gateway server certificate names
- **WHEN** an operator requests the gateway server certificate
- **THEN** its DNS names are exactly the gateway hostname and the Grafana UI
  hostname

#### Scenario: Storage server certificate names
- **WHEN** an operator requests the storage server certificate
- **THEN** its DNS names are exactly the hostnames of the storage binding
  endpoints

#### Scenario: Private key stays local
- **WHEN** a certificate is requested
- **THEN** only a signing request leaves the machine and Vault never receives
  the private key

#### Scenario: Vault grants less than requested
- **WHEN** Vault returns a certificate whose names, identity, usage, or
  validity differ from the request
- **THEN** the system fails explicitly and writes neither key nor certificate

### Requirement: Certificate revocation
The system SHALL support revoking a single issued certificate by listing its
fingerprint in the platform document, and revoking every certificate for a
collector by removing its declared identity. The system SHALL provide a
command that also revokes a certificate in Vault and reports the fingerprint
to list. The gateway SHALL enforce revocation from its policy and SHALL NOT
depend on reaching Vault to do so.

#### Scenario: Revoking by fingerprint
- **WHEN** an operator adds a certificate's fingerprint to the revoked list
  and re-renders the gateway policy
- **THEN** the rendered policy denies that certificate while other
  certificates remain accepted

#### Scenario: Malformed fingerprint
- **WHEN** the revoked list contains a value that is not a well-formed
  fingerprint
- **THEN** platform document validation fails and names the offending entry

#### Scenario: Revoking in Vault
- **WHEN** an operator runs the revocation command for an issued certificate
- **THEN** Vault records the certificate as revoked and the command prints
  the fingerprint to add to the platform document

#### Scenario: Vault unavailable at request time
- **WHEN** the gateway decides a request while Vault is unreachable
- **THEN** the decision is unaffected, and a certificate listed as revoked is
  still denied

## ADDED Requirements

### Requirement: Certificate authority held in Vault
The certificate authority that signs gateway, storage, and collector
certificates SHALL be the one in the declared Vault PKI mount. The system
SHALL obtain only its public certificate, SHALL NOT read, store, or
materialize its private key, and SHALL use that public certificate as the
gateway's trust for client certificates.

#### Scenario: Gateway trust
- **WHEN** runtime configuration is rendered
- **THEN** the gateway's client certificate trust is the public certificate
  of the declared PKI mount's authority

#### Scenario: No CA key reference
- **WHEN** a platform document declares a reference to a certificate
  authority private key
- **THEN** validation fails and names the field

#### Scenario: Authority replaced
- **WHEN** the PKI mount's authority changes and runtime configuration is
  rendered again
- **THEN** the gateway's trust is updated to the new public certificate and
  the command reports that existing certificates must be issued again
