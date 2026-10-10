# Spec Delta

## ADDED Requirements

### Requirement: Backend configuration for self-hosted Kubernetes
The renderer SHALL produce each enabled backend's configuration for a
self-hosted Kubernetes deployment in the mode the profile selects:
monolithic for development and distributed for production. In both modes
the configuration SHALL keep the properties required of every backend
configuration: storage from the bindings, tenancy enforced, runtime
overrides loaded, retention able to act, and version gating.

#### Scenario: Development mode
- **WHEN** backend configuration is rendered for the development profile
- **THEN** each backend's configuration is monolithic and has no ingest
  log dependency

#### Scenario: Production mode
- **WHEN** backend configuration is rendered for the production profile
- **THEN** backends whose pinned version requires an ingest log are
  configured for the cluster's Kafka, with a separate topic and credential
  per backend

#### Scenario: Unsupported version
- **WHEN** a backend's pinned version is not one the Kubernetes
  configuration was reviewed for
- **THEN** rendering fails and names the backend and version
