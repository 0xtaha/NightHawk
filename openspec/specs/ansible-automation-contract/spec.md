# ansible-automation-contract

## Purpose

Defines what every Ansible playbook and role in the repository has in common:
where its inputs come from, what its inventories may contain, and how it
behaves when it is run in check mode or run again.

## Requirements

### Requirement: Inputs are rendered, not read
The system SHALL render the non-secret inputs the Ansible automation needs
from the validated platform document, the network contract, and the
compatibility matrix, deterministically and without contacting Vault. Roles
and playbooks SHALL take those values from the rendered inputs and SHALL NOT
read the repository's configuration documents or declare their own copies of
a port, version, or checksum.

#### Scenario: Rendering the inputs
- **WHEN** contracts are rendered for a valid platform document
- **THEN** the output contains the firewall rules, version pins, checksums,
  and supported operating systems the automation uses, and rendering twice
  gives byte-identical output

#### Scenario: A pin changes
- **WHEN** a version in the compatibility matrix changes and the inputs are
  rendered again
- **THEN** the roles install the new version with no edit to any role

#### Scenario: A role hard-codes a contract value
- **WHEN** a role or playbook declares a literal port, version, or checksum
  that the rendered inputs provide
- **THEN** a repository check fails and names the file

### Requirement: Secrets reach hosts only as copied files
Ansible SHALL NOT contact Vault. A secret a host needs SHALL be materialized
on the control machine by the command-line tool and copied by a task that
does not log it, to a file readable only by the account that uses it.

#### Scenario: Copying a secret
- **WHEN** a role places a credential, key, or token on a host
- **THEN** the task is marked not to log, and the file's permissions exclude
  group and others

#### Scenario: Output of a run
- **WHEN** a playbook that handles secrets is run with verbose output
- **THEN** no secret value appears in the output

### Requirement: Inventories hold no secret
The example inventories SHALL contain no credential, token, key, or
password, and a repository check SHALL fail when an inventory or variable
file under version control contains one.

#### Scenario: Example inventory
- **WHEN** the example inventories are inspected
- **THEN** every value is a host, an address, a path, or a non-secret setting

#### Scenario: Secret added to an inventory
- **WHEN** a variable whose name marks it as a secret is given a literal
  value in a tracked inventory or variable file
- **THEN** the repository check fails and names the file and variable

### Requirement: Pinned automation tooling
The automation SHALL declare the exact versions of the Ansible collections
it uses and SHALL use no module from a collection it does not declare.

#### Scenario: Installing collections
- **WHEN** an operator installs the declared collections
- **THEN** each is installed at the version the compatibility matrix pins

#### Scenario: Undeclared collection
- **WHEN** a role uses a module from a collection that is not declared
- **THEN** the lint check fails

### Requirement: Check mode and repeated runs
Every role SHALL complete in check mode without changing the host, reporting
what it would change, except for steps that cannot predict their result,
which SHALL be skipped and named. A second run of any playbook against an
unchanged host SHALL report no change.

#### Scenario: Check mode
- **WHEN** a playbook is run in check mode against a host it has not
  configured
- **THEN** it finishes without error and the host is unchanged

#### Scenario: Second run
- **WHEN** a playbook is run twice against the same host with the same inputs
- **THEN** the second run reports zero changed tasks

#### Scenario: Service restarts
- **WHEN** a role changes a configuration file a service reads
- **THEN** the service is restarted or reloaded once, at the end, and not
  when nothing changed
