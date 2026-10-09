# Spec Delta

## Purpose

Provides the single-host Docker Compose deployment of the whole platform, so
it can be started, observed, and torn down on one machine with tenant
isolation, persistence, and private backends intact.

## ADDED Requirements

### Requirement: Complete single-host topology
The Compose stack SHALL run monolithic Mimir, Loki, Tempo, and Pyroscope,
SeaweedFS object storage, the gateway proxy, the auth service, Grafana, and
an Alloy collector, and SHALL NOT include Kafka.

#### Scenario: Stack composition
- **WHEN** the Compose configuration is resolved
- **THEN** it declares a service for each of those components and no Kafka
  service

#### Scenario: Images are pinned
- **WHEN** the Compose configuration is resolved
- **THEN** every service image is referenced by a digest recorded in the
  compatibility matrix, or is built from this repository on a base image
  referenced that way

### Requirement: Only the gateway is published, on loopback by default
The Compose stack SHALL publish no port other than the gateway's selected
entry points, and SHALL bind them to the loopback interface unless the
operator explicitly configures another address. Backends, object storage,
the auth service, and Grafana SHALL be reachable only on private Compose
networks.

#### Scenario: Default exposure
- **WHEN** the stack is started with the example environment
- **THEN** the only host ports listening are the gateway entry points, bound
  to the loopback address

#### Scenario: Backend not reachable from the host
- **WHEN** a client on the host connects to a backend's, the object
  store's, or the auth service's port
- **THEN** the connection is refused

#### Scenario: Auth service isolation
- **WHEN** the Compose configuration is resolved
- **THEN** the auth service shares a network only with the gateway proxy and
  the collector that scrapes its metrics

### Requirement: Readiness-ordered startup
Each long-running service SHALL declare a health check that uses a probe
present in its pinned image, and a service SHALL start only after the
services it depends on are healthy or, for initialization services, have
completed successfully.

#### Scenario: Dependent waits for readiness
- **WHEN** the stack starts from empty volumes
- **THEN** no backend starts before object storage initialization has
  completed, and the gateway does not start before the auth service is
  healthy

#### Scenario: Probe exists in the image
- **WHEN** a service's health check runs
- **THEN** it executes a binary that exists in that service's pinned image

### Requirement: Initialization services fail visibly
One-shot initialization SHALL run in services separate from long-running
ones, SHALL exit zero only when the work is complete, and a failed
initialization SHALL prevent dependent services from starting.

#### Scenario: Failed initialization
- **WHEN** an initialization service exits non-zero
- **THEN** its dependents are not started and the bring-up command reports
  the failed service

#### Scenario: Repeated initialization
- **WHEN** the stack is started a second time on existing volumes
- **THEN** initialization completes successfully without recreating or
  altering existing buckets, identities, or data

### Requirement: Persistence across restarts
Telemetry, object storage, Grafana state, and collector buffers SHALL be kept
on named volumes, so that stopping and starting the stack, or restarting any
single service, loses no acknowledged data.

#### Scenario: Stack restart
- **WHEN** telemetry is ingested, the stack is stopped and started again
- **THEN** the previously ingested telemetry is still returned by queries

#### Scenario: Single backend restart
- **WHEN** one backend container is restarted while the rest keep running
- **THEN** it becomes healthy again and earlier data for its signal is still
  queryable

### Requirement: Restart policy and least privilege
Every long-running service SHALL have a restart policy, SHALL run as a
non-root user where its pinned image supports it, and no service in the
default configuration SHALL be privileged or mount the container runtime
socket. Each exception SHALL be listed in the documentation with its reason.

#### Scenario: Default services are unprivileged
- **WHEN** the default Compose configuration is resolved
- **THEN** no service is privileged and none mounts the container runtime
  socket

#### Scenario: Host collection is opt-in
- **WHEN** an operator enables the host-collection profile
- **THEN** only the additional host collector receives the runtime socket and
  the privileges it needs

### Requirement: No secrets in the Compose configuration
The Compose files and the example environment file SHALL contain no secret
value; every secret SHALL reach its service as a mounted file from the
materialized secrets directory.

#### Scenario: Resolved configuration
- **WHEN** the Compose configuration is resolved with the example
  environment
- **THEN** it contains no credential, key, or password value

### Requirement: External S3 override
The stack SHALL provide an override that removes local object storage and its
initialization and uses the storage bindings of a platform document whose
storage provider is an external S3 service.

#### Scenario: Override resolved
- **WHEN** the Compose configuration is resolved with the S3 override
- **THEN** it contains no SeaweedFS service and the backends still start
  after their own dependencies
