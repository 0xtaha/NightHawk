# Spec Delta

## Purpose

Lets an operator bring the whole platform up on one machine with a single
command, using freshly generated local secrets and trust, and take it down
again without losing data unless they ask to.

## ADDED Requirements

### Requirement: Prerequisite check before any change
The quickstart SHALL verify the container runtime, the Compose command, the
pinned `sops` and `age` tools, and free host ports before creating any file
or container, and SHALL name every missing or mismatched prerequisite.

#### Scenario: Missing prerequisite
- **WHEN** the quickstart runs and a prerequisite is missing or a required
  port is in use
- **THEN** it exits non-zero naming the prerequisite and creates nothing

### Requirement: One-command bring-up
The quickstart SHALL generate every secret, storage identity, certificate
authority, and certificate the example platform document references that
does not yet exist, render all configuration, start the stack, provision
Grafana, and exit zero only when every service is healthy.

#### Scenario: First run
- **WHEN** an operator runs the quickstart in a clean checkout with the
  prerequisites present
- **THEN** the stack is running, Grafana has the tenant's organization and
  data sources, and the command prints the gateway and Grafana addresses

#### Scenario: Second run
- **WHEN** the quickstart is run again
- **THEN** it reuses the existing secrets and certificates, leaves stored
  telemetry intact, and ends in the same healthy state

#### Scenario: A service does not become healthy
- **WHEN** a service fails to become healthy within the documented timeout
- **THEN** the quickstart exits non-zero and names the service

### Requirement: Secrets stay local and ignored
Everything the quickstart generates that is secret SHALL be stored
SOPS-encrypted or under directories excluded from version control with
owner-only permissions, and the quickstart SHALL never print a secret other
than the generated Grafana administrator password, which it prints once on
first creation.

#### Scenario: Working tree after a run
- **WHEN** the quickstart has completed
- **THEN** version control reports no new untracked file containing private
  key or credential material

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
