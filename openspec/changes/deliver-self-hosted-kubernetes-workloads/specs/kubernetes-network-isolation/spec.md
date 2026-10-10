# Spec Delta

## Purpose

Restricts traffic inside the cluster to what the network contract allows,
so that a workload can reach only the services it needs and nothing can
reach a backend except through the gateway.

## ADDED Requirements

### Requirement: Default deny in every platform namespace
Every namespace the platform creates SHALL deny all inbound and outbound
pod traffic that no policy allows.

#### Scenario: Unlisted connection
- **WHEN** a pod in a platform namespace connects to a platform service no
  policy allows it to reach
- **THEN** the connection is refused

#### Scenario: New pod without a policy
- **WHEN** a pod with no matching allowance is started in a platform
  namespace
- **THEN** it can neither receive nor open any connection except name
  resolution

### Requirement: Allowances come from the network contract
Every allowance SHALL correspond to a rule of the network contract, and the
contract SHALL list every flow the platform needs inside the cluster,
including name resolution, object storage, Kafka, ring and gossip traffic,
the cluster API where a workload needs it, and Vault.

#### Scenario: Generated policies
- **WHEN** the policies are generated
- **THEN** each allowed port and peer maps to exactly one contract rule,
  and each in-cluster contract rule is covered by a policy

#### Scenario: A flow is missing from the contract
- **WHEN** a workload needs a connection the contract does not list
- **THEN** the connection is refused until the contract is extended

### Requirement: Backends are reachable only through the gateway
Ingestion and query ports of the signal backends SHALL accept connections
only from the gateway and from the backends' own components.

#### Scenario: Another workload
- **WHEN** a pod that is not the gateway connects to a backend's ingestion
  or query port
- **THEN** the connection is refused

#### Scenario: Collector
- **WHEN** a collector delivers telemetry
- **THEN** it reaches only the gateway, never a backend directly

### Requirement: Object storage and databases are reachable only by their users
Object storage SHALL accept connections only from the signal backends and
its own components, and the database only from the workloads that declare
it.

#### Scenario: Gateway to object storage
- **WHEN** the gateway connects to object storage
- **THEN** the connection is refused

### Requirement: Egress is limited
A workload SHALL be able to leave the cluster only where the contract
allows it, such as the secret operator and the certificate manager reaching
Vault.

#### Scenario: Backend to the internet
- **WHEN** a backend pod connects to an address outside the cluster
- **THEN** the connection is refused
