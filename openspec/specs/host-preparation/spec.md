# host-preparation

## Purpose

Prepares a Linux machine for the platform: confirms it is a supported
system before anything is changed, and applies a small, explicit set of
hardening settings that do not break the workloads placed on it.

## Requirements

### Requirement: Operating system preflight
Every playbook SHALL verify, before its first change, that each target's
distribution, version, and architecture are in the supported set for the
role the host is given, and SHALL stop for the whole run when any host is
not.

#### Scenario: Supported host
- **WHEN** a playbook targets a host whose system is in the supported set
  for its role
- **THEN** the run proceeds

#### Scenario: Unsupported host
- **WHEN** any target's distribution, version, or architecture is not in the
  supported set for its role
- **THEN** the run stops before changing any host and names the host and
  what was found

#### Scenario: Supported for one role only
- **WHEN** a host's system is supported for Docker hosts but not for cluster
  nodes, and it is targeted as a cluster node
- **THEN** the run stops and says which role the system is not supported for

### Requirement: Explicit hardening settings
The hardening role SHALL apply a documented list of settings: SSH accepts
key authentication only and refuses direct root password login; documented
kernel network settings are set; time synchronization is enabled; and
automatic security updates are enabled only when the operator asks. It SHALL
NOT change a setting that is not on that list.

#### Scenario: Applying hardening
- **WHEN** the hardening role is applied to a supported host
- **THEN** each listed setting holds afterwards and the documentation lists
  exactly those settings

#### Scenario: Automatic updates not requested
- **WHEN** the operator does not ask for automatic security updates
- **THEN** the host's update configuration is unchanged

### Requirement: Hardening never removes the administrator's access
The hardening role SHALL verify that the connecting account can authenticate
with a key before it disables password authentication, and SHALL validate
the new SSH configuration before the service uses it.

#### Scenario: No working key
- **WHEN** the connecting account has no authorized key on the host
- **THEN** the role fails before changing the SSH configuration

#### Scenario: Invalid configuration
- **WHEN** the resulting SSH configuration fails the server's own validation
- **THEN** the previous configuration is kept and the service is not reloaded

### Requirement: Hardening leaves workload settings alone
Hardening SHALL NOT set a kernel network setting to a value that conflicts
with what the container runtime or the cluster network requires on that
host's role.

#### Scenario: Cluster node
- **WHEN** hardening is applied to a host that is also a cluster node
- **THEN** packet forwarding stays enabled and reverse-path filtering is not
  set to a mode the cluster network cannot work with
