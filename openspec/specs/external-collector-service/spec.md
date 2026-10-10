# external-collector-service

## Purpose

Runs a collector as a host service on machines outside the platform, such as
a virtual machine or a host next to an external service, delivering to the
gateway with a credential and a client certificate.

## Requirements

### Requirement: Verified collector installation
The system SHALL install the Alloy version the compatibility matrix pins
from an archive whose checksum matches the matrix, and SHALL run it as a
dedicated unprivileged account under a systemd unit that restarts it on
failure.

#### Scenario: Installing
- **WHEN** the role is applied to a supported host
- **THEN** the pinned collector is running as its own account and starts at
  boot

#### Scenario: Checksum mismatch
- **WHEN** the downloaded archive's checksum differs from the matrix
- **THEN** nothing is installed and the role fails naming the artifact

#### Scenario: Service confinement
- **WHEN** the unit is inspected
- **THEN** the service cannot gain privileges, cannot write outside its own
  state directory, and has no access to other users' home directories

### Requirement: Configuration, credential, and certificate from the platform
The collector SHALL run the configuration rendered for its tenant,
datastream, and profile, and SHALL authenticate with that datastream's
ingestion credential and a client certificate carrying the credential's
declared identity. The private key and the credential SHALL be readable only
by the collector's account.

#### Scenario: Delivering telemetry
- **WHEN** the service is running on a host that can reach the gateway
- **THEN** it connects to the external entry point with its client
  certificate and its telemetry is accepted for its own datastream only

#### Scenario: Credential without a certificate identity
- **WHEN** the chosen ingestion credential declares no certificate identity
- **THEN** the role fails before changing the host, since the external entry
  point would refuse it

#### Scenario: File permissions
- **WHEN** the role has run
- **THEN** the credential and private key files exclude group and others

### Requirement: Changes are applied without losing buffered telemetry
A changed configuration SHALL be validated with the collector's own check
before it replaces the running one, and SHALL be applied by a reload, not a
restart, when only the configuration changed.

#### Scenario: Invalid configuration
- **WHEN** a rendered configuration fails the collector's validation
- **THEN** the running configuration is kept and the role fails

#### Scenario: Configuration change
- **WHEN** only the configuration changed
- **THEN** the service is reloaded and its write-ahead log is kept

### Requirement: Certificate renewal by re-running
Re-running the playbook SHALL replace the collector's certificate when it is
close to expiry or was signed by an authority Vault no longer has, and SHALL
leave it alone otherwise.

#### Scenario: Certificate still valid
- **WHEN** the playbook is re-run while the certificate has more than the
  renewal period left
- **THEN** no certificate is requested and the service is not touched

#### Scenario: Certificate close to expiry
- **WHEN** the certificate is within the renewal period
- **THEN** a new one is issued for the same identity and the service picks
  it up
