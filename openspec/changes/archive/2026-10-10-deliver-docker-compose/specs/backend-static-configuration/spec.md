# Spec Delta

## Purpose

Renders each signal backend's own configuration from the platform document,
so storage, tenancy, and retention settings on a running backend always match
the validated contract.

## ADDED Requirements

### Requirement: Backend configuration for every enabled signal
Contract rendering SHALL produce a configuration document for each signal
backend whose signal at least one datastream enables, for the monolithic
Compose profile, deterministically and without any secret value.

#### Scenario: All signals enabled
- **WHEN** contracts are rendered for a document enabling all four signals
- **THEN** a configuration is written for Mimir, Loki, Tempo, and Pyroscope

#### Scenario: Repeated render
- **WHEN** contracts are rendered twice from the same inputs
- **THEN** each backend configuration is byte-identical

#### Scenario: Unsupported deployment
- **WHEN** backend configuration is requested for a deployment whose
  architecture this change does not implement
- **THEN** rendering of those files is skipped with an explicit message and
  the rest of the contract render still succeeds

### Requirement: Storage follows the bindings
Each backend configuration SHALL use exactly the buckets, endpoint, region,
addressing style, and trust settings of that backend's storage bindings, and
SHALL obtain storage credentials from a mounted file or the environment,
never from the rendered document.

#### Scenario: Buckets match bindings
- **WHEN** a backend configuration is rendered
- **THEN** every bucket it names is one of that backend's bindings and every
  binding of that backend is used

#### Scenario: No credential in the document
- **WHEN** a backend configuration is rendered
- **THEN** it contains no access key or secret key value

### Requirement: Tenancy is enforced by the backend
Each backend configuration SHALL enable multitenancy and SHALL disable
cross-tenant query federation wherever the pinned version offers a setting
for it.

#### Scenario: Request without a tenant
- **WHEN** a request without a tenant header reaches a running backend
  directly on the private network
- **THEN** the backend rejects it

#### Scenario: Federation disabled
- **WHEN** a backend configuration is rendered
- **THEN** every federation setting the pinned version exposes is set to
  disabled, and a backend with no such setting is listed in the documentation

### Requirement: Runtime overrides are loaded and retention can act
Each backend configuration SHALL load that backend's rendered runtime
override file and SHALL enable the compaction or retention workers that the
overrides' retention values depend on.

#### Scenario: Overrides visible at runtime
- **WHEN** the stack is running
- **THEN** each backend reports the rendered per-tenant overrides as loaded

#### Scenario: Changed overrides
- **WHEN** an override file is re-rendered with a changed value and replaced
- **THEN** the backend reports the new value without a restart

### Requirement: Version-gated backend configuration
Backend configuration rendering SHALL succeed only for the backend versions
and architectures it was verified against, consistent with override
rendering.

#### Scenario: Unreviewed pin
- **WHEN** the compatibility matrix pins a different version or Compose
  architecture for a backend
- **THEN** rendering fails and names that backend
