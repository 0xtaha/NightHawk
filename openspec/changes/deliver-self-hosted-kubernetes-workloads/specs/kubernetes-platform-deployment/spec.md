# Spec Delta

## Purpose

Runs the whole platform on a self-hosted Kubernetes cluster: installs the
releases in order, proves they are healthy, keeps the same tenant
boundaries as every other deployment, and removes them without losing
data unless asked.

## ADDED Requirements

### Requirement: Ordered, health-gated deployment
The system SHALL install the platform's releases in dependency order from
the control machine, SHALL wait for each to be healthy before the next, and
SHALL report success only when every workload is ready.

#### Scenario: First deployment
- **WHEN** the platform is deployed to a prepared cluster
- **THEN** object storage, backends, the gateway, the auth service,
  Grafana, and the collectors are running and Grafana is provisioned

#### Scenario: A workload is not ready
- **WHEN** a workload does not become ready within the stated time
- **THEN** the deployment fails, names the release and the workload, and
  does not continue

#### Scenario: Deploying again
- **WHEN** the deployment is run again with nothing changed
- **THEN** no release is upgraded, no pod restarts, and Grafana reports no
  change

### Requirement: All four signals work end to end
A deployed platform SHALL accept metrics, logs, traces, and profiles
through the gateway and return them to queries, for every enabled signal
of every datastream.

#### Scenario: Round trip
- **WHEN** fixtures for the four signals are sent through the cluster
  collector for a datastream
- **THEN** each is returned by a query through the gateway with that
  datastream's query credential

#### Scenario: Grafana
- **WHEN** a provisioned data source is queried through Grafana
- **THEN** it returns that datastream's data through the gateway

### Requirement: Tenant boundaries are the same as on every deployment
The gateway on Kubernetes SHALL enforce the same credential-derived tenant
binding, permission separation, signal enablement, and certificate
identity binding as the tenant gateway capability requires.

#### Scenario: Cross-tenant access
- **WHEN** one datastream's credential is used to read or write another
  datastream
- **THEN** the request is refused

#### Scenario: Spoofed tenant header
- **WHEN** a request carries a tenant header that differs from its
  credential's tenant
- **THEN** the request is refused

#### Scenario: Certificate-bound credential on the external entry point
- **WHEN** ingestion is attempted on the external entry point without the
  credential's client certificate
- **THEN** the request is refused

### Requirement: Backends and storage are private
No backend, object storage, auth service, Kafka, or database endpoint SHALL
be reachable from outside the cluster.

#### Scenario: From outside the cluster
- **WHEN** a machine outside the cluster connects to any address of the
  cluster other than the gateway's entry points
- **THEN** no platform service answers

### Requirement: Runtime overrides and retention apply per datastream
The per-datastream runtime overrides SHALL be delivered to the backends and
picked up without a restart, as the tenant runtime overrides capability
requires.

#### Scenario: Changed override
- **WHEN** a datastream's limit is changed and the deployment is run again
- **THEN** the backend reports the new value without having restarted

### Requirement: Collectors run with separated discovery
The node collector SHALL run on every node and the cluster collector as a
separate workload, with the discovery separation the collection capability
requires, each authenticating with its datastream's ingestion credential.

#### Scenario: Cluster metrics
- **WHEN** the collectors are running
- **THEN** node and pod metrics arrive once, not duplicated between the two
  collectors

#### Scenario: Privileged profiling
- **WHEN** privileged profiling is not allowed for a datastream
- **THEN** no privileged collector workload is deployed

### Requirement: Production availability
In the production profile, the loss of any single node SHALL NOT make
ingestion or queries unavailable and SHALL NOT lose acknowledged telemetry.

#### Scenario: Replica placement
- **WHEN** the production profile is deployed
- **THEN** replicas of each stateful component are on distinct nodes

#### Scenario: Node drain
- **WHEN** one node is drained
- **THEN** disruption budgets keep a quorum of every stateful component
  running

### Requirement: Development profile states its limits
The development profile SHALL state, in the deployment's output and its
documentation, that it is a single node without high availability and that
its retention values are examples.

#### Scenario: Deployment message
- **WHEN** a development deployment completes
- **THEN** its output states that the installation is not highly available

### Requirement: Data survives restarts and redeployment
Stored telemetry, Grafana state, and object storage data SHALL survive pod
restarts and a redeployment.

#### Scenario: Backend pod deleted
- **WHEN** a backend pod is deleted and recreated
- **THEN** telemetry stored before the deletion is still returned

### Requirement: Teardown keeps data by default
The system SHALL provide a teardown that removes the platform's releases
and keeps persistent volumes, synchronized secrets' sources, and
certificates' authority, unless a purge is confirmed by naming the
cluster.

#### Scenario: Ordinary teardown
- **WHEN** the teardown is run and the platform is deployed again
- **THEN** earlier telemetry is still returned

#### Scenario: Purge without confirmation
- **WHEN** a purge is requested without the confirmation
- **THEN** nothing is deleted
