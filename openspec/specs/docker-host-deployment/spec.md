# docker-host-deployment

## Purpose

Installs a pinned container runtime on a prepared host and deploys the
Compose stack to it as a single-node installation, with the gateway's
certificate-requiring entry point published for other machines.

## Requirements

### Requirement: Pinned container runtime
The system SHALL install the Docker Engine version the compatibility matrix
pins, from a package repository whose signing key matches the fingerprint in
the matrix, and SHALL hold that version against unattended upgrades.

#### Scenario: Installing
- **WHEN** the role is applied to a supported host without Docker
- **THEN** the pinned engine and the Compose plugin are installed and the
  service is running

#### Scenario: Signing key mismatch
- **WHEN** the repository's signing key does not match the pinned fingerprint
- **THEN** nothing is installed from it and the role fails naming the key

#### Scenario: Another version present
- **WHEN** a different engine version is already installed
- **THEN** the role fails and states both versions, unless the operator
  explicitly asked for the change

### Requirement: Deployment is rendered on the control machine
The system SHALL provide a command that performs everything the local
quickstart does before starting containers, for a remote host: check the
prerequisites, create missing secrets in Vault, obtain certificates, and
write the rendered configuration and the secret files, addressed for the
remote host's paths and account. The remote host SHALL NOT need access to
Vault.

#### Scenario: Rendering for a remote host
- **WHEN** an operator renders a deployment for a remote host
- **THEN** a rendered tree and a secret tree are written on the control
  machine, the Compose environment file names the remote paths, and no
  container is started

#### Scenario: Rendering again
- **WHEN** the same deployment is rendered again with nothing changed
- **THEN** no secret is created, no certificate is issued, and no file is
  rewritten

#### Scenario: Vault credential stays on the control machine
- **WHEN** a deployment is rendered and copied
- **THEN** no file placed on the remote host contains a Vault credential

### Requirement: Production deployment requirements
A deployment for another machine SHALL require a platform document whose
profile is production, and therefore a TLS Vault address that is not
loopback and a credential without the root policy. It SHALL require the
certificate-requiring external entry point to be selected in the document.

#### Scenario: Development document
- **WHEN** an operator deploys a development-profile document to a remote
  host
- **THEN** the deployment is refused before anything is copied

#### Scenario: External entry point not selected
- **WHEN** the platform document does not select the external entry point
- **THEN** the deployment is refused and names the missing entry point

### Requirement: Remote deployment of the stack
The system SHALL copy the rendered configuration, the secret files, and what
is needed to build the platform's own image to the host, with secret files
readable only by the account the stack runs as, start the stack, and report
success only when every service is healthy.

#### Scenario: First deployment
- **WHEN** a deployment is applied to a prepared host
- **THEN** the stack is running, Grafana is provisioned, and the external
  entry point answers on the address the operator stated

#### Scenario: Deployment after a re-render
- **WHEN** a changed rendering is applied to a host that is already running
  the stack
- **THEN** only changed files are replaced, services that do not watch their
  files are reloaded, and stored telemetry is kept

#### Scenario: A service is not healthy
- **WHEN** a service does not become healthy within the stated time
- **THEN** the playbook fails and names the service

### Requirement: Loopback entry point stays on loopback
On a remote host the loopback-scoped entry point SHALL be published on the
host's loopback address only, and the external entry point SHALL be
published only on an address the operator states.

#### Scenario: Published ports
- **WHEN** the stack is running on a remote host
- **THEN** the loopback entry point is not reachable from another machine,
  and the external entry point requires a client certificate for ingestion

#### Scenario: No external address stated
- **WHEN** no address is stated for the external entry point
- **THEN** the deployment is refused

### Requirement: Single node is stated as not highly available
The documentation and the playbook's final message SHALL state that this
deployment is a single node without high availability, and what is lost if
the host is lost.

#### Scenario: Deployment message
- **WHEN** a deployment completes
- **THEN** its output states that the installation is a single node without
  high availability

### Requirement: Teardown keeps data by default
The system SHALL provide a playbook that stops and removes the stack's
containers on a host and keeps volumes, secret files, and certificates,
unless the operator confirms a purge by name.

#### Scenario: Ordinary teardown
- **WHEN** the teardown playbook is run
- **THEN** containers are removed and a later deployment finds its earlier
  telemetry

#### Scenario: Purge without confirmation
- **WHEN** a purge is requested without the explicit confirmation
- **THEN** nothing is deleted
