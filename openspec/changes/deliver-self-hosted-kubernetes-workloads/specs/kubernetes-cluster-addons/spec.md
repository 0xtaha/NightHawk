# Spec Delta

## Purpose

Installs the add-ons and operators a self-hosted cluster needs before the
platform can run on it: load balancing, storage, ingress, certificate
issuance, secret delivery, and, for production, the Kafka and PostgreSQL
operators.

## ADDED Requirements

### Requirement: Pinned add-on installation
The system SHALL install MetalLB, Longhorn, Traefik, cert-manager, and the
Vault Secrets Operator at the chart versions the compatibility matrix pins,
and SHALL refuse to install a chart whose content differs from the locked
digest.

#### Scenario: Installing on a new cluster
- **WHEN** the add-ons are installed on a cluster that has only its network
  plugin
- **THEN** each add-on is running at its pinned version and reports healthy

#### Scenario: Chart content changed upstream
- **WHEN** a chart at a pinned version no longer matches its locked digest
- **THEN** nothing is installed from it and the failure names the chart

#### Scenario: Running again
- **WHEN** the installation is run again with nothing changed
- **THEN** no release is upgraded and no workload restarts

### Requirement: Add-ons use what the cluster preflight checked
The load balancer SHALL hand out only the address pool the cluster layout
check accepted, and the storage add-on SHALL use only the disks declared
for storage nodes.

#### Scenario: Address pool
- **WHEN** the load balancer is installed
- **THEN** its address pool equals the pool declared in the inventory

#### Scenario: Storage disks
- **WHEN** the storage add-on is installed on a production cluster
- **THEN** it stores data only on the declared disks of the storage nodes

### Requirement: Development clusters skip what one node cannot use
On a single-node development cluster the system SHALL NOT install the
replicated storage add-on or the Kafka and PostgreSQL operators, and SHALL
say which storage class is used instead.

#### Scenario: Single-node development cluster
- **WHEN** add-ons are installed for the development shape
- **THEN** workloads use the cluster's local storage class and no
  replicated storage, Kafka, or PostgreSQL operator is installed

### Requirement: Production operators
For the production shape the system SHALL install the pinned Kafka operator
and the pinned PostgreSQL operator before any workload that needs them.

#### Scenario: Production cluster
- **WHEN** add-ons are installed for the production shape
- **THEN** both operators are running before the platform's releases start

### Requirement: Ingress controller is the only external listener
The ingress controller SHALL be the only component that receives an
external address, and SHALL listen only on the gateway entry points the
network contract defines.

#### Scenario: Services with external addresses
- **WHEN** the add-ons and the platform are installed
- **THEN** exactly one service has an external address, and its ports are
  the selected gateway entry points

### Requirement: Add-on failures are reported by name
The installation SHALL wait for each add-on to become healthy within a
stated time and SHALL fail naming the add-on that did not.

#### Scenario: An add-on does not become healthy
- **WHEN** an add-on is not healthy within the stated time
- **THEN** the installation stops before the next release and names it
