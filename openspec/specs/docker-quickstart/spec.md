# docker-quickstart

## Purpose

Lets an operator bring the whole platform up on one machine with a single
command, using freshly generated local secrets and trust, and take it down
again without losing data unless they ask to.

## Requirements

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

### Requirement: Example retention is labelled as an example
The quickstart's tenant and datastream SHALL come from the example platform
document, and the documentation and command output SHALL state that its
retention values are examples and not production defaults.

#### Scenario: Output wording
- **WHEN** the quickstart completes
- **THEN** its output identifies the sample tenant and states that its
  retention values are examples

### Requirement: Teardown preserves data
The teardown command SHALL stop and remove containers and networks while
keeping all named volumes, generated secrets, and certificates. Removing data
SHALL require a separate, explicitly named purge option.

#### Scenario: Ordinary teardown
- **WHEN** an operator runs teardown and then the quickstart again
- **THEN** telemetry ingested before the teardown is still queryable

#### Scenario: Purge
- **WHEN** an operator runs teardown with the purge option and confirms it
- **THEN** the stack's volumes are removed and the command states what was
  deleted

### Requirement: Measured startup
The documentation SHALL state the host resources and network assumptions for
the quickstart and SHALL report measured cold-start and warm-start times from
an actual run, without claiming a startup target that was not measured.

#### Scenario: Recorded measurement
- **WHEN** the quickstart documentation is read
- **THEN** it gives measured cold and warm start times together with the
  machine and runtime they were measured on

### Requirement: The gateway is published on loopback only
The quickstart SHALL publish the gateway only on a loopback address and on
the port the platform document declares for the local entry point, and SHALL
refuse any other bind address before generating or starting anything.

#### Scenario: Default address
- **WHEN** the quickstart runs without a bind address
- **THEN** the gateway is published on `127.0.0.1` at the local entry point's
  declared port

#### Scenario: Non-loopback address
- **WHEN** an operator supplies a bind address that is not a loopback address
- **THEN** the quickstart exits non-zero, changes nothing, and states that
  the local entry point accepts ingestion without a client certificate

#### Scenario: Declared port differs from the example
- **WHEN** the platform document declares a different port for the local
  entry point
- **THEN** the gateway is published on that port and the printed addresses
  use it

### Requirement: Credential choice during rotation
The quickstart SHALL accept an explicit choice of credential for its
collector and for Grafana's data sources, and SHALL require that choice when
the platform document declares more than one ingestion credential for the
sample datastream, or more than one query credential for any datastream
Grafana has data sources for.

#### Scenario: One credential per permission
- **WHEN** the sample datastream has one ingestion credential and one query
  credential
- **THEN** the quickstart uses them without any choice being supplied

#### Scenario: Overlapping credentials with a choice
- **WHEN** the sample datastream has two ingestion credentials and the
  operator names one
- **THEN** the collector is rendered with the named credential and the stack
  ends healthy

#### Scenario: Overlapping credentials without a choice
- **WHEN** the sample datastream has two credentials of one permission and
  the operator names neither
- **THEN** the quickstart exits non-zero before starting anything and lists
  the credentials to choose from

#### Scenario: Chosen credential cannot be used
- **WHEN** the operator names a credential the platform document does not
  declare, or an ingestion credential of a datastream other than the sample
  datastream
- **THEN** the quickstart exits non-zero before starting anything and names
  the credential

#### Scenario: Query credential of another datastream
- **WHEN** a datastream other than the sample datastream declares two query
  credentials and the operator names one
- **THEN** Grafana's data sources for that datastream use the named
  credential

### Requirement: Rotated secrets reach Grafana
When the value of a query credential's secret has changed since the previous
run, the quickstart SHALL resend data source passwords to Grafana, so that a
secret rotated in place does not leave Grafana with a rejected password.

#### Scenario: Secret rotated in place
- **WHEN** a query credential's secret is rotated under the same credential
  ID and the quickstart is run again
- **THEN** queries through that datastream's Grafana data sources succeed

#### Scenario: Nothing rotated
- **WHEN** the quickstart is run again with no secret changed
- **THEN** Grafana provisioning reports no changes

### Requirement: Host collection configuration on request
The quickstart SHALL render the collector configuration that the optional
host collection service mounts when the operator requests host collection,
with host filesystem, process, and system paths pointing at the mounted host
root, and SHALL NOT render or start it otherwise.

#### Scenario: Host collection requested
- **WHEN** an operator requests host collection
- **THEN** the rendered directory the host collection service mounts contains
  a complete collector configuration whose host metrics read from the
  mounted host root

#### Scenario: Host collection not requested
- **WHEN** the quickstart runs without requesting host collection
- **THEN** no host collection configuration is rendered and the service is
  not started

#### Scenario: Runtime cannot support host collection
- **WHEN** an operator requests host collection under a runtime the
  documentation lists as unsupported for it
- **THEN** the quickstart exits non-zero before starting anything and names
  the supported runtime
