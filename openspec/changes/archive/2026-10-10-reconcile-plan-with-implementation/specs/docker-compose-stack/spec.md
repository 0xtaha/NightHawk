# Spec Delta

## MODIFIED Requirements

### Requirement: Readiness-ordered startup
Each long-running service SHALL have its readiness established before any
service that depends on it starts: by a health check that uses a probe
present in its pinned image, or, where the pinned image contains no usable
probe binary, by an initialization service that waits for it from another
image. A service SHALL start only after the services it depends on are
healthy or, for initialization services, have completed successfully. The
documentation SHALL list, for each service, which of the two establishes its
readiness.

#### Scenario: Dependent waits for readiness
- **WHEN** the stack starts from empty volumes
- **THEN** no backend starts before object storage initialization has
  completed, and the gateway does not start before the auth service is
  healthy

#### Scenario: Probe exists in the image
- **WHEN** a service's health check runs
- **THEN** it executes a binary that exists in that service's pinned image

#### Scenario: Image without a probe binary
- **WHEN** a long-running service's pinned image has no usable probe binary
- **THEN** the service declares no health check, and every service that
  depends on it is gated on an initialization service that completes only
  once it is ready
