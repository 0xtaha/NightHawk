# Spec Delta

## Purpose

Defines how workloads in the cluster obtain the platform's secrets and
certificates from the operator's Vault without any Vault credential being
stored in the cluster, a chart, or an inventory, and how a rotated secret
reaches a running workload.

## ADDED Requirements

### Requirement: Secrets are synchronized from Vault by the cluster
Every secret a workload needs SHALL be delivered as a Kubernetes Secret
synchronized from the secret's declared Vault location by an operator in
the cluster. No rendered file, chart value, manifest, or inventory SHALL
contain a secret value.

#### Scenario: Workload starts
- **WHEN** a workload that needs a declared secret is deployed
- **THEN** the secret is available to it from a Kubernetes Secret whose
  content equals the value in Vault

#### Scenario: Rendered output
- **WHEN** everything rendered for a deployment is searched for the values
  of the platform's secrets
- **THEN** none is found

### Requirement: No Vault credential in the cluster
The cluster SHALL authenticate to Vault with its service accounts'
identities. No Vault token, AppRole secret, or other long-lived Vault
credential SHALL be stored in the cluster or supplied to a chart.

#### Scenario: Searching the cluster
- **WHEN** the cluster's Secrets and ConfigMaps are inspected after a
  deployment
- **THEN** none holds a Vault token or AppRole secret

#### Scenario: Vault does not recognise the cluster
- **WHEN** Vault has no authentication role for the cluster
- **THEN** secrets are not synchronized, workloads that need them do not
  start, and the deployment fails naming the missing role

### Requirement: Least-privilege access per workload
Each workload's identity SHALL be able to read only the secrets that
workload uses, and SHALL NOT be able to write to Vault.

#### Scenario: One backend's identity
- **WHEN** a backend's identity requests another backend's storage keys
- **THEN** Vault denies the request

#### Scenario: Writing
- **WHEN** any in-cluster identity attempts to write a secret
- **THEN** Vault denies the request

### Requirement: Certificates are issued in the cluster from Vault's authority
Server and client certificates used by workloads SHALL be issued by the
cluster's certificate manager from Vault's PKI. The private key SHALL be
generated in the cluster, and only a signing request SHALL be sent to
Vault.

#### Scenario: Gateway server certificate
- **WHEN** the gateway is deployed
- **THEN** it serves a certificate for the gateway and Grafana host names
  signed by the authority in Vault

#### Scenario: Renewal
- **WHEN** a certificate approaches expiry
- **THEN** a new one is issued and used without an operator action

#### Scenario: Key location
- **WHEN** a certificate is issued
- **THEN** Vault received a signing request and never held the private key

### Requirement: Rotation reaches running workloads
A secret rotated in Vault SHALL reach the workloads that use it within a
stated period, and a workload that does not re-read its files SHALL be
restarted or reloaded by the system.

#### Scenario: Rotated gateway credential
- **WHEN** a tenant's ingestion credential is rotated in Vault
- **THEN** within the stated period the gateway accepts the new value and
  rejects the old one

#### Scenario: Rotated storage identity
- **WHEN** a backend's storage identity is rotated
- **THEN** the backend and the object storage both use the new identity
  and stored telemetry remains readable

### Requirement: Policy is derived inside the cluster without recoverable secrets
The gateway's authorization policy SHALL be derived inside the cluster from
the synchronized credentials, and SHALL contain no recoverable secret, as
on every other deployment.

#### Scenario: Policy content
- **WHEN** the policy the auth service loaded is inspected
- **THEN** it contains no credential value

#### Scenario: A credential is missing
- **WHEN** a declared credential has no value in Vault
- **THEN** the auth service does not start serving and the deployment
  reports which credential is missing
