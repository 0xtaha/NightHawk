# Spec Delta

## Purpose

Produces, from the platform document, the Helm values and Kubernetes
manifests a self-hosted deployment installs, for the development and the
production profile, deterministically and without secrets.

## ADDED Requirements

### Requirement: Values for every release from the platform document
For a self-hosted Kubernetes platform document the renderer SHALL produce
the values for every release of the selected profile, and SHALL derive
tenants, datastreams, enabled signals, host names, storage bindings, and
version pins from the platform document and the compatibility matrix
rather than from values an operator edits.

#### Scenario: Rendering a profile
- **WHEN** the contracts are rendered for a self-hosted document
- **THEN** one values file per release of the document's profile is
  written, with the namespace, release name, chart, and chart version of
  each

#### Scenario: Adding a datastream
- **WHEN** a datastream is added to the document and the contracts are
  rendered again
- **THEN** the gateway routes, runtime overrides, and Grafana desired
  state for it appear and no values file needs manual editing

#### Scenario: Docker document
- **WHEN** the contracts are rendered for a Docker document
- **THEN** no Kubernetes output is written

### Requirement: Two visibly distinct profiles
The renderer SHALL produce a development or a production deployment
according to the platform document's profile, and the deployment SHALL be
refused when the cluster's declared shape does not match it.

#### Scenario: Development profile
- **WHEN** a development document is rendered
- **THEN** every backend runs as a single replica without Kafka, and the
  output states that the profile is not highly available

#### Scenario: Production profile
- **WHEN** a production document is rendered
- **THEN** every stateful component has at least the replica count its
  consistency model needs, a disruption budget, topology spread across
  nodes, and resource requests and limits

#### Scenario: Profile and cluster shape differ
- **WHEN** a production rendering is deployed to a cluster declared as the
  development shape, or the reverse
- **THEN** the deployment is refused before any release is installed and
  names the mismatch

### Requirement: Deterministic and free of secrets
Rendering SHALL be deterministic, SHALL NOT contact Vault, and SHALL NOT
write any secret value. Secrets appear in the output only as references to
their Vault location.

#### Scenario: Rendering twice
- **WHEN** the same inputs are rendered twice
- **THEN** the outputs are identical byte for byte

#### Scenario: Without Vault
- **WHEN** rendering runs with no Vault reachable
- **THEN** it succeeds

### Requirement: Images are pinned by digest
Every container image in the rendered values SHALL be referenced by the
digest the compatibility matrix records.

#### Scenario: Image references
- **WHEN** the rendered releases are templated
- **THEN** every container image reference carries a digest that is in the
  matrix

### Requirement: Autoscaling only where verified
The renderer SHALL enable horizontal autoscaling only for components the
matrix records as horizontally scalable, and SHALL NOT scale Grafana beyond
one replica without a shared database.

#### Scenario: Stateful component
- **WHEN** the production profile is rendered
- **THEN** no autoscaler targets a component that is not recorded as
  horizontally scalable

#### Scenario: Grafana replicas
- **WHEN** Grafana is rendered with more than one replica
- **THEN** it is configured with the shared database and highly available
  alerting, and rendering fails if no database is declared

### Requirement: Rendered output passes static validation
Every rendered release SHALL template without error and every resulting
manifest SHALL validate against the schema of the pinned Kubernetes
version.

#### Scenario: Validating both profiles
- **WHEN** both profiles are templated and validated
- **THEN** no template error and no schema violation is reported
