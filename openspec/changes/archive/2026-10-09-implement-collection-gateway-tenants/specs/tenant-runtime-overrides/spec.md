# Spec Delta

## Purpose

Translates each tenant/datastream pair's declared retention and limits into
the per-tenant runtime override documents that Mimir, Loki, Tempo, and
Pyroscope load, so backend enforcement always reflects the platform contract.

## ADDED Requirements

### Requirement: Overrides for every pair and enabled signal
The system SHALL render, for each of the four signal backends, a runtime
override document containing one entry per tenant/datastream pair that enables
that backend's signal, keyed by the pair's backend ID, and no entry for a pair
that does not enable it.

#### Scenario: Two customers with two datastreams each
- **WHEN** overrides are rendered for a platform document with two tenants of
  two datastreams each, all enabling metrics with different retention values
- **THEN** the metrics override document has four entries, each carrying its
  own pair's retention

#### Scenario: Disabled signal
- **WHEN** a datastream does not enable traces
- **THEN** the traces override document has no entry for its backend ID

### Requirement: No implicit retention or limits
A rendered override document SHALL carry no default tenant entry and no value
that is not derived from the platform document.

#### Scenario: No default entry
- **WHEN** overrides are rendered
- **THEN** no document contains a wildcard, default, or fallback tenant entry

### Requirement: Declared limits are enforced or reported
For every backend, each retention value and limit in the platform contract
SHALL either be rendered as an override setting that the pinned backend
version enforces per tenant, or be listed in a rendered report of limits that
the backend does not enforce. A declared limit SHALL NOT be silently omitted.

#### Scenario: Every declared value is accounted for
- **WHEN** overrides are rendered for a datastream
- **THEN** each of its declared retention and limit values appears either in
  an override document or in the unenforced-limits report

#### Scenario: Metrics ingestion budget in backend units
- **WHEN** a platform document declares a metrics ingestion budget
- **THEN** it is declared in samples per second and rendered unchanged as the
  metrics backend's per-tenant ingestion rate

#### Scenario: Metrics budget declared in bytes
- **WHEN** a platform document declares the metrics ingestion budget in bytes
  per second
- **THEN** validation fails and names the expected field

### Requirement: Version-gated override settings
Override rendering for a backend SHALL succeed only when the compatibility
matrix pins the backend version, and storage mode where applicable, for which
that backend's override settings were verified; any other pin SHALL fail until
the mapping is reviewed.

#### Scenario: Reviewed pin
- **WHEN** the compatibility matrix carries the reviewed version for every
  backend
- **THEN** all four override documents render

#### Scenario: Unreviewed pin
- **WHEN** the compatibility matrix pins a different version for one backend
- **THEN** rendering fails and names that backend and the reviewed version

### Requirement: Deterministic override output
Override documents SHALL be rendered deterministically as part of contract
rendering.

#### Scenario: Repeated render
- **WHEN** contracts are rendered twice from the same inputs
- **THEN** each override document is byte-identical across the two renders
