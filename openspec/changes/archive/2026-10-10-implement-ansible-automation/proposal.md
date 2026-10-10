# Proposal

## Why

Phases 1 to 4 run the platform on the machine where the repository is checked
out. Nothing yet prepares another machine: there is no way to install a
pinned container runtime on a host, deploy the stack to it, bootstrap a
Kubernetes cluster for the self-hosted profile, or run a collector as a host
service. Plan phase 5 is that automation, and phase 6 (the Kubernetes
workloads) cannot start without a cluster to deploy to.

Two items recorded as deferred to this phase are also closed here: the
Ansible tooling pins, and evidence for the supported OS matrix.

## What Changes

**Ansible foundation**

- A new `ansible/` tree: pinned collections, example inventories that hold no
  secret, playbooks, and roles. Every role supports check mode where its
  modules do, marks secret-bearing tasks `no_log`, and is idempotent.
- Ansible never reads `config/*.yaml` or Vault. `render-contracts` emits the
  non-secret inputs it needs (firewall rules from the network contract,
  version pins and checksums from the matrix, the supported OS list), and the
  command-line tool materializes any secret it must copy.
- The compatibility matrix gains pins for `ansible-core`, the collections,
  the lint and test tools, and checksums or signing-key fingerprints for
  every artifact a role installs: Docker Engine, k3s, and Alloy.

**Hosts**

- A preflight that refuses an operating system, version, or architecture the
  matrix does not list, before anything is changed.
- Rocky Linux 9 and AlmaLinux 9 are added as supported Docker hosts, next to
  Ubuntu 22.04/24.04 and Debian 12. k3s nodes stay Ubuntu only.
- Node hardening: SSH settings, kernel network settings, time
  synchronization, and opt-in automatic security updates.
- A host firewall (UFW on Debian and Ubuntu, firewalld on Rocky and Alma)
  whose rules come from the network contract plus operator-supplied
  allowlists. It verifies that the administrator's own connection is covered
  before changing anything and rolls itself back if the connection is lost.

**Docker hosts**

- Install a pinned Docker Engine from a repository whose signing key is
  verified against the matrix.
- **Deploy the Compose stack to a remote host.** This brings in the
  production single-node work that was recorded as deferred. The control
  machine renders the configuration and reads secrets from Vault; Ansible
  copies them and starts the stack. The host never talks to Vault.
- A Compose override publishes the certificate-requiring external entry
  point on an address the operator states. The loopback entry point stays on
  loopback. Documented as a single node without high availability.
- A teardown playbook that keeps data unless a purge is confirmed.

**Kubernetes cluster (self-hosted profile)**

- Bootstrap k3s from a checksum-verified binary with the bundled Flannel,
  Traefik, and ServiceLB disabled, join further servers and agents, and
  install Cilium so nodes become Ready.
- A single-node development shape and a multi-node production shape, with a
  preflight that rejects impossible layouts: too few servers or storage
  nodes, an address pool that overlaps node addresses, mismatched MTU,
  missing kernel modules or disks.
- The join token is generated once and stored in Vault.
- MetalLB, Longhorn, Traefik, and cert-manager are **not** installed here;
  their prerequisites are checked and their installation belongs to phase 6.

**External collectors**

- Run Alloy as a hardened systemd service on a virtual machine or next to an
  external service, from a checksum-verified binary, with its rendered
  configuration, credential, and a client certificate from Vault's PKI.
- `issue-certificate` gains a mode that issues only when a certificate is
  missing, close to expiry, or signed by an authority Vault no longer has, so
  a playbook can be re-run safely.

**Network contract**

- **BREAKING** for consumers that assume every rule concerns the gateway or
  a backend: `config/network.yaml` gains the cluster's node-to-node flows
  (API server, kubelet, etcd, Cilium overlay and health) and SSH
  administration, since the contract owns every port a firewall opens.

**Verification and documents**

- `ansible-lint` and syntax checks for everything; role tests in systemd
  containers under Podman for each supported operating system where a
  container can host the role; and an end-to-end case that publishes the
  external entry point locally and reaches it with a client certificate.
- A new `docs/08-ansible.md`, and updates to the plan's recorded deferrals.

No target machine exists. Starting k3s, Cilium, kernel prerequisites,
firewall enforcement, the lockout protection, and a deployment to a real
remote host are written and statically checked but not run, and are recorded
as such.

## Capabilities

### New Capabilities

- `ansible-automation-contract`: rendered non-secret inputs, pinned tooling,
  secret-free inventories, check mode, `no_log`, and idempotence across all
  roles.
- `host-preparation`: the operating system preflight and node hardening.
- `host-firewall`: contract-derived rules, allowlists, and protection against
  locking out the administrator.
- `docker-host-deployment`: pinned Docker Engine, remote deployment of the
  Compose stack, external publication, and teardown.
- `k3s-cluster-bootstrap`: k3s servers and agents, the join token, Cilium,
  cluster shapes, and the prerequisite preflight.
- `external-collector-service`: Alloy as a systemd service on hosts outside
  the platform.

### Modified Capabilities

- `compatibility-matrix`: the OS matrix adds the RHEL family and records the
  evidence behind each entry; new requirements for Ansible tooling pins and
  for checksums of host-installed artifacts.
- `docker-compose-stack`: a new requirement for publishing the external
  entry point through an override.
- `gateway-trust-lifecycle`: a new requirement for issuing a certificate only
  when it is needed.

## Impact

- **New tree**: `ansible/` (configuration, inventories, playbooks, roles,
  role tests).
- **Python**: `nighthawk/config.py`, `quickstart.py` (rendering split from
  starting), `trust.py`, `__main__.py`, and a new module for the Ansible
  inputs. New commands are listed in `design.md`.
- **Config**: `config/network.yaml`, `config/versions.yaml` and its schema,
  `config/platform.schema.json` (cluster join token reference), a new
  self-hosted example document.
- **Compose**: a new `docker-compose/docker-compose.external.yaml`.
- **Tests**: unit tests for the rendered inputs and new commands, role tests
  under `ansible/`, and one new end-to-end case.
- **Documents**: `docs/08-ansible.md`, `docs/01`, `docs/02`, `docs/07`,
  `Plan.md`, and the README.
- **Dependencies**: `ansible-core`, `ansible-lint`, and Molecule with its
  Podman driver, in a separate pinned requirements file that the platform's
  own runtime does not need. Container images of the five supported operating
  systems are pulled for tests.
- **Existing specs**: `docker-quickstart` is unchanged. The quickstart still
  publishes on loopback only; remote publication exists only through the new
  deployment path.
- **Verification limits**: stated above and repeated in `docs/08-ansible.md`.
