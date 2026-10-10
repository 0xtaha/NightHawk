# Spec Delta

## ADDED Requirements

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
