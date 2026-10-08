# Spec Delta

## Purpose

Provides the isolated, destroy-protected Terraform root that creates the
encrypted remote-state backend (S3 bucket and lock table) every other AWS
Terraform root in this platform depends on.

## ADDED Requirements

### Requirement: Self-hosted remote-state bootstrap
The system SHALL provide a Terraform execution root that provisions an
encrypted, versioned S3 bucket and a DynamoDB lock table for remote state,
using only its own local state (no prior remote backend exists before this
root is applied), and SHALL protect those resources from ordinary destroy
operations.

#### Scenario: Bootstrapping remote state for the first time
- **WHEN** an operator applies the state-bootstrap root against a fresh AWS
  account with only local state
- **THEN** the system creates an encrypted, versioned S3 bucket and a
  DynamoDB lock table, both marked to resist ordinary `terraform destroy`

#### Scenario: Ordinary teardown preserves bootstrap resources
- **WHEN** an operator runs ordinary environment teardown for any other root
  that uses this backend
- **THEN** the state-bootstrap bucket and lock table are not destroyed or
  modified

### Requirement: Other roots consume the bootstrapped backend explicitly
Other Terraform execution roots SHALL declare their own backend
configuration referencing the bootstrapped S3 bucket and lock table; the
bootstrap root SHALL NOT be a dependency that other roots apply implicitly
or automatically.

#### Scenario: A dependent root is initialized against the bootstrapped backend
- **WHEN** an operator supplies the bootstrapped bucket/table identifiers as
  explicit backend configuration for another root's `terraform init`
- **THEN** that root initializes against the shared encrypted backend without
  the state-bootstrap root being re-applied or implicitly triggered

### Requirement: Documented native S3-lockfile migration
The system SHALL document a safe migration path from DynamoDB locking to
Terraform's native S3 lockfile locking without data loss or concurrent-write
risk, consistent with the migration already documented for the existing
storage root.

#### Scenario: Migrating an existing backend to native S3 locking
- **WHEN** an operator follows the documented migration steps for a backend
  already using DynamoDB locking
- **THEN** the steps describe pausing writers, backing up state, verifying
  lockfile support, enabling native locking while retaining the DynamoDB
  table during transition, and reinitializing clients at the same
  bucket/key before removing the DynamoDB configuration
