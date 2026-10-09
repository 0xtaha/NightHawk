# Spec Delta

## Purpose

Provides telemetry to observe the platform with: an instrumented service that
behaves like a customer application, and deterministic fixtures that tests
can ingest and then find again.

## ADDED Requirements

### Requirement: Instrumented sample service
The stack SHALL offer an optional sample service that produces metrics, logs,
traces, and profiles through the collector, with traces, logs, and profiles
sharing identifiers that Grafana's correlations can follow.

#### Scenario: All four signals arrive
- **WHEN** the sample service has run for the documented warm-up period
- **THEN** each of the sample datastream's four Grafana data sources returns
  data from it

#### Scenario: Sample service is optional
- **WHEN** the stack is started without the sample profile
- **THEN** no sample service runs and the platform services are healthy

### Requirement: Deterministic fixtures
The system SHALL provide a fixture emitter that sends a fixed, documented set
of metric samples, log lines, spans, and a profile, carrying a caller-chosen
run identifier, so a test can query for exactly what it sent.

#### Scenario: Fixture round trip
- **WHEN** fixtures are emitted with a run identifier
- **THEN** a query for that identifier returns the emitted items for each
  signal and nothing from another run

#### Scenario: Emission failure
- **WHEN** the collector or gateway rejects a fixture
- **THEN** the emitter exits non-zero and names the signal and response
  status

### Requirement: Sensitive fixtures for redaction checks
The fixture set SHALL include values under each of the sample datastream's
drop fields, as metric labels, log labels, log body key/value pairs, span
and resource attributes, and profile labels, each with a unique marker
value.

#### Scenario: Markers are absent after ingestion
- **WHEN** sensitive fixtures are emitted through the collector and every
  signal is queried
- **THEN** no marker value placed under a drop field is found in any query
  result

#### Scenario: Documented limit is demonstrated
- **WHEN** a marker is emitted in free text without a key pattern
- **THEN** the test records that it is retained, matching the documented
  redaction limit
