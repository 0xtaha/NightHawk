# Spec Delta

## ADDED Requirements

### Requirement: Explicit query credential during rotation
When a datastream declares more than one query credential, the system SHALL
use the one the operator names for that datastream and SHALL fail explicitly
when none is named, rather than selecting one by any implicit order.

#### Scenario: Single query credential
- **WHEN** desired state is rendered for a datastream with one query
  credential
- **THEN** its data sources use that credential and no choice is required

#### Scenario: Overlap with a named credential
- **WHEN** a datastream declares two query credentials and the operator names
  the newer one
- **THEN** every data source of that datastream uses the named credential's
  user name and secret reference, and applying the state updates them

#### Scenario: Overlap without a named credential
- **WHEN** a datastream declares two query credentials and the operator names
  neither
- **THEN** the command fails before contacting Grafana and lists the
  datastream and its candidate credentials

#### Scenario: Named credential is not a query credential of any datastream
- **WHEN** the operator names a credential that is undeclared or is not a
  query credential
- **THEN** the command fails before contacting Grafana and names the
  credential
