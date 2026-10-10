# aws-compute-infrastructure

## Purpose

Provides the AWS VPC and EKS compute infrastructure, including the IRSA/OIDC
identity handoff and network-contract-derived security groups, that the
existing storage facade and future Kubernetes workloads depend on.

## Requirements

### Requirement: Multi-AZ VPC with network-contract-derived security groups
The system SHALL provide a Terraform module that provisions a VPC spanning
multiple availability zones with public/private subnets and routing, and
SHALL derive security-group rules from the project's network contract
(`config/network.yaml`), in the direction each rule declares, rather than
declaring independent, potentially divergent ports. Each security group
derived from the contract SHALL be attached to the workloads its rules
describe, and the subnets SHALL carry the role tags load balancers use to
discover them. A repository check SHALL fail when a rule mirrored into
Terraform differs from the contract in any declared field.

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

#### Scenario: Security group is attached to nodes
- **WHEN** the compute environment root is planned
- **THEN** every node group's instances carry the contract-derived security
  group for their workload source in addition to the cluster's own security
  group

#### Scenario: Mirrored rule drifts in one field
- **WHEN** a rule mirrored into the compute root keeps its ID but its port,
  protocol, source, destination, or scope differs from the network contract
- **THEN** the repository check fails and names the rule and the field

#### Scenario: Subnets are discoverable by load balancers
- **WHEN** the VPC module is planned
- **THEN** public subnets carry the public load balancer role tag and private
  subnets carry the internal load balancer role tag

### Requirement: Managed EKS cluster with IRSA
The system SHALL provide a Terraform module that provisions an EKS cluster
with managed node groups, the EBS CSI driver, and an IRSA-capable OIDC
provider, and SHALL NOT install or initialize any Helm/Kubernetes-provider
resource against the cluster it creates. The EBS CSI driver SHALL obtain its
AWS permissions through its own IRSA role, and the node role SHALL NOT carry
volume-management permissions.

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

#### Scenario: EBS CSI driver uses its own role
- **WHEN** the EKS module is planned
- **THEN** the EBS CSI addon references a role assumable only by the
  driver's controller service account, and no volume-management policy is
  attached to the node role

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

### Requirement: Explicit cluster access
The EKS module SHALL require the operator to state how the cluster API is
reached and who administers it: whether the public endpoint is enabled, the
CIDR ranges allowed to reach it when it is, and the principals granted
cluster access. The module SHALL NOT grant cluster administration to the
creating principal implicitly and SHALL NOT expose the public endpoint to
all addresses.

#### Scenario: Private-only endpoint
- **WHEN** an operator disables the public endpoint
- **THEN** the planned cluster has private endpoint access enabled and public
  endpoint access disabled

#### Scenario: Public endpoint with an allowlist
- **WHEN** an operator enables the public endpoint with a list of CIDR ranges
- **THEN** the planned cluster allows exactly those ranges

#### Scenario: Public endpoint without an allowlist
- **WHEN** an operator enables the public endpoint with no CIDR ranges, or
  with a range covering all addresses
- **THEN** validation fails before any resource is planned

#### Scenario: Declared administrators only
- **WHEN** an operator supplies a list of administrator principals
- **THEN** the plan contains one access entry per principal and the cluster
  does not grant access to the creating principal on its own

#### Scenario: No administrators
- **WHEN** an operator supplies an empty list of administrator principals
- **THEN** validation fails before any resource is planned

### Requirement: Encryption at rest
The EKS module SHALL encrypt Kubernetes secrets with a KMS key and SHALL
encrypt every node's root volume. The key SHALL be either supplied by the
operator or created by the module with rotation enabled.

#### Scenario: Secrets encryption
- **WHEN** the EKS module is planned
- **THEN** the cluster's encryption configuration covers Kubernetes secrets
  with a KMS key

#### Scenario: Node volumes
- **WHEN** the EKS module is planned
- **THEN** every node group's root volume is encrypted

#### Scenario: Operator-supplied key
- **WHEN** an operator supplies a KMS key identifier
- **THEN** the module uses that key and creates none

### Requirement: Controller identities
The EKS module SHALL provide an IRSA role for each cluster controller that
needs AWS permissions and that the operator enables: DNS record management,
certificate DNS-01 validation, and node autoscaling. Each role SHALL be
assumable only by the service account the operator names for it and SHALL be
limited to the hosted zones or node groups it manages. The module SHALL
output each role's identifier for the later workload installation.

#### Scenario: DNS controller scoped to declared zones
- **WHEN** an operator enables the DNS controller with a service account and
  a list of hosted zones
- **THEN** the planned role can change records only in those zones and can be
  assumed only by that service account

#### Scenario: Controller enabled without a scope
- **WHEN** an operator enables the DNS or certificate controller with no
  hosted zones
- **THEN** validation fails before any resource is planned

#### Scenario: Autoscaler scoped to this cluster
- **WHEN** an operator enables the node autoscaling controller
- **THEN** the planned role can change the size only of this cluster's node
  groups

#### Scenario: Controller not enabled
- **WHEN** an operator enables no controller
- **THEN** the plan contains no controller role
