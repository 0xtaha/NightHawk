# Spec Delta

## ADDED Requirements

### Requirement: Rendered Vault access for a cluster
For a self-hosted Kubernetes platform document the system SHALL render
what Vault must allow so that the cluster can read the platform's secrets
and have certificates signed: the authentication role for the cluster's
service accounts, a read-only policy per workload limited to the secrets
that workload uses, and the PKI roles the certificate manager may use. The
platform document SHALL declare the authentication mount; no credential is
declared. The prerequisite check SHALL report a missing or wider-than-
rendered role or policy when asked to check a cluster deployment.

#### Scenario: Rendering
- **WHEN** the contracts are rendered for a self-hosted document
- **THEN** the Vault requirements include the cluster authentication role
  and one read-only policy per workload, and nothing in them is a secret

#### Scenario: Development Vault
- **WHEN** the development bootstrap is run for a self-hosted document
- **THEN** the dev-mode Vault has the authentication mount, role, and
  policies, and a second run changes nothing

#### Scenario: Production Vault lacks the role
- **WHEN** the prerequisite check runs for a cluster deployment and Vault
  has no role for the cluster
- **THEN** the check fails and names what is missing

#### Scenario: Docker document
- **WHEN** the contracts are rendered for a Docker document
- **THEN** no cluster authentication requirement is rendered
