# Spec Delta

## MODIFIED Requirements

### Requirement: Certificate issuance bound to declared identities
The system SHALL issue a collector client certificate only for a certificate
identity declared by a credential in the platform document, carrying that
identity as its URI subject alternative name, with an operator-supplied
validity period and client-authentication usage only. The system SHALL issue
server certificates only for hostnames the platform document declares: the
gateway hostname together with the Grafana UI hostname, or the hostnames of
the storage binding endpoints.

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

#### Scenario: Gateway server certificate names
- **WHEN** an operator requests the gateway server certificate
- **THEN** its DNS names are exactly the gateway hostname and the Grafana UI
  hostname

#### Scenario: Storage server certificate names
- **WHEN** an operator requests the storage server certificate
- **THEN** its DNS names are exactly the hostnames of the storage binding
  endpoints
