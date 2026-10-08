# Spec Delta

## Purpose

Provides the AWS VPC and EKS compute infrastructure, including the IRSA/OIDC
identity handoff and network-contract-derived security groups, that the
existing storage facade and future Kubernetes workloads depend on.

## ADDED Requirements

### Requirement: Multi-AZ VPC with network-contract-derived security groups
The system SHALL provide a Terraform module that provisions a VPC spanning
multiple availability zones with public/private subnets and routing, and
SHALL derive security-group ingress rules from the project's network
contract (`config/network.yaml`) rather than declaring independent,
potentially divergent ports.

#### Scenario: Provisioning a multi-AZ VPC
- **WHEN** an operator applies the VPC module with a region and availability
  zone count
- **THEN** the module creates subnets spanning the requested number of
  distinct availability zones with appropriate public/private routing

#### Scenario: Security groups reflect the network contract
- **WHEN** the network contract declares a rule's source, destination,
  protocol, port, and scope
- **THEN** the corresponding security-group rule in this module's output
  matches that declaration, and a port not present in the network contract
  is not opened

### Requirement: Managed EKS cluster with IRSA
The system SHALL provide a Terraform module that provisions an EKS cluster
with managed node groups, the EBS CSI driver, and an IRSA-capable OIDC
provider, and SHALL NOT install or initialize any Helm/Kubernetes-provider
resource against the cluster it creates.

#### Scenario: Provisioning an EKS cluster
- **WHEN** an operator applies the EKS module with a VPC/subnet input and
  node-group sizing
- **THEN** the module creates the EKS cluster, its managed node groups, the
  EBS CSI driver, and an IRSA-capable OIDC provider

#### Scenario: On-demand versus spot capacity selection
- **WHEN** a node group is declared for stateful workloads
- **THEN** it SHALL use on-demand capacity; a node group may use spot
  capacity only when declared for stateless workloads

#### Scenario: No Helm or Kubernetes-provider resources are created here
- **WHEN** the EKS module's plan or apply output is inspected
- **THEN** it contains no Helm release or Kubernetes-provider-managed
  resource, since the cluster the provider would target does not exist at
  the start of that same operation

### Requirement: OIDC identity output matches the storage root's expected input
The EKS module's OIDC provider ARN and issuer URL outputs SHALL match the
exact shape the existing AWS storage root's `aws_identity` variable expects,
so an operator can supply them without reshaping the values.

#### Scenario: Wiring EKS output into the storage root
- **WHEN** an operator copies this module's `oidc_provider_arn` and
  `oidc_issuer_url` outputs into the storage root's `aws_identity` variable
- **THEN** the storage root accepts the values without modification or
  reformatting

### Requirement: Independent compute environment root
The system SHALL provide a Terraform execution root composing the VPC and
EKS modules with its own backend configuration, applied independently of the
existing storage root; it SHALL NOT automatically chain an apply of the
storage root or any other root.

#### Scenario: Applying the compute environment independently
- **WHEN** an operator applies the compute environment root
- **THEN** only the VPC and EKS resources it declares are planned or applied,
  and no other execution root's resources are affected
