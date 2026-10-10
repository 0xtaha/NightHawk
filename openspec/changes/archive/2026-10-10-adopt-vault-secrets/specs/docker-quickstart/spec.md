# Spec Delta

## MODIFIED Requirements

### Requirement: Prerequisite check before any change
The quickstart SHALL verify the container runtime, the Compose command, the
declared Vault (reachable, unsealed, a supported version, an accepted
credential, and the declared mounts and roles present), and free host ports
before creating any file, container, or secret, and SHALL name every missing
or mismatched prerequisite.

#### Scenario: Missing prerequisite
- **WHEN** the quickstart runs and a prerequisite is missing or a required
  port is in use
- **THEN** it exits non-zero naming the prerequisite and creates nothing

#### Scenario: No Vault available
- **WHEN** the quickstart runs and Vault is unreachable or no credential is
  supplied
- **THEN** it exits non-zero, creates nothing, and points to the documented
  way to start and bootstrap a development Vault

### Requirement: One-command bring-up
Given a Vault that passes the prerequisite check, the quickstart SHALL
generate every secret and storage identity the example platform document
references that does not yet exist in Vault, obtain every certificate that
does not yet exist, render all configuration, start the stack, provision
Grafana, and exit zero only when every service is healthy.

#### Scenario: First run
- **WHEN** an operator runs the quickstart in a clean checkout with the
  prerequisites present and a bootstrapped development Vault
- **THEN** the stack is running, Grafana has the tenant's organization and
  data sources, and the command prints the gateway and Grafana addresses

#### Scenario: Second run
- **WHEN** the quickstart is run again
- **THEN** it reuses the existing secrets and certificates, leaves stored
  telemetry intact, and ends in the same healthy state

#### Scenario: A service does not become healthy
- **WHEN** a service fails to become healthy within the documented timeout
- **THEN** the quickstart exits non-zero and names the service

#### Scenario: Development Vault was recreated
- **WHEN** the quickstart is run against a Vault that no longer holds the
  secrets of an earlier run
- **THEN** it exits non-zero before starting anything, states that existing
  volumes hold data protected by the lost storage identities, and names the
  teardown that clears them

### Requirement: Secrets stay local and ignored
Everything the quickstart generates that is secret SHALL be stored in Vault
or under directories excluded from version control with owner-only
permissions, and the quickstart SHALL never print a secret other than the
generated Grafana administrator password, which it prints once on first
creation. The quickstart SHALL NOT write a Vault credential to disk.

#### Scenario: Working tree after a run
- **WHEN** the quickstart has completed
- **THEN** version control reports no new untracked file containing private
  key or credential material

#### Scenario: Vault credential is not persisted
- **WHEN** the quickstart has completed
- **THEN** no file it wrote, including the Compose environment file and the
  materialization directory, contains the Vault credential
