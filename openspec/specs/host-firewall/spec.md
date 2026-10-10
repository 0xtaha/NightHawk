# host-firewall

## Purpose

Configures each host's firewall from the network contract and
operator-supplied allowlists, default-denying inbound traffic, without ever
cutting off the administrator who is applying it.

## Requirements

### Requirement: Rules come from the network contract
The firewall role SHALL open only the inbound ports the network contract
declares for the host's role, with the contract's protocol, and SHALL deny
every other inbound connection. A rule whose scope is loopback SHALL NOT be
opened on any other interface.

#### Scenario: Docker host
- **WHEN** the firewall is configured on a Docker host
- **THEN** the only inbound ports open to other machines are SSH and the
  gateway's external entry point

#### Scenario: Cluster node
- **WHEN** the firewall is configured on a cluster node
- **THEN** the node-to-node ports the contract declares are open to the other
  nodes only

#### Scenario: Port not in the contract
- **WHEN** an operator asks the role to open a port the contract does not
  declare
- **THEN** the role fails and names the port

### Requirement: Restricted sources need an allowlist
For a rule the contract scopes as restricted-external, and for SSH, the
operator SHALL supply the source ranges allowed to connect. The role SHALL
fail when the list is empty or contains a range covering every address.

#### Scenario: Allowlisted sources
- **WHEN** the operator supplies source ranges for the external entry point
- **THEN** that port accepts connections from those ranges only

#### Scenario: No allowlist
- **WHEN** no source range is supplied for a restricted-external rule or for
  SSH
- **THEN** the role fails before changing the firewall

#### Scenario: Open to every address
- **WHEN** a supplied range covers every address
- **THEN** the role fails before changing the firewall

### Requirement: The administrator is never locked out
Before changing the firewall, the role SHALL verify that the address its own
connection comes from is within the SSH allowlist. It SHALL allow SSH before
it enables default deny, and SHALL arrange for the firewall change to be
undone automatically unless a new connection confirms, within a stated time,
that access still works.

#### Scenario: Own address not allowlisted
- **WHEN** the connection applying the role comes from an address outside
  the SSH allowlist
- **THEN** the role fails before changing the firewall and names the address

#### Scenario: Access confirmed
- **WHEN** the firewall has been changed and a new connection succeeds
- **THEN** the automatic undo is cancelled and the rules are kept

#### Scenario: Access lost
- **WHEN** no new connection succeeds within the stated time
- **THEN** the host returns to its previous firewall state by itself

### Requirement: One role, two firewall tools
The role SHALL configure UFW on Debian and Ubuntu and firewalld on Rocky
Linux and AlmaLinux from the same rule set, and SHALL leave the system with
the same reachable ports on both.

#### Scenario: Same rule set on both families
- **WHEN** the same rules are applied to a Debian-family host and a
  RHEL-family host
- **THEN** the same ports are open to the same sources on both

#### Scenario: Container runtime traffic
- **WHEN** the firewall is configured on a host that runs containers
- **THEN** ports that containers publish are reachable only as the rules
  allow, and not opened by the container runtime around the firewall
