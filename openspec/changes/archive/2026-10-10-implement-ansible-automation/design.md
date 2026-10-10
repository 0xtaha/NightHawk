# Design

## Context

See `proposal.md` for motivation and scope. The scope questions were settled
before drafting: Cilium is installed with k3s and the other add-ons wait for
phase 6; Docker hosts get the stack deployed, not only the runtime; Rocky
Linux 9 and AlmaLinux 9 join the Docker host matrix; and verification is
lint plus role tests in containers.

What exists today, from reading the repository:

- There is no `ansible/` directory and no Ansible tooling on this machine.
- `config/versions.yaml` pins k3s 1.37.1+k3s1, Cilium 1.20.2, and Alloy
  1.20.1, without checksums for the first and last. `os_support` lists
  Ubuntu 22.04/24.04 and Debian 12 for Docker hosts and Ubuntu for k3s
  nodes, with nothing recording why. `config.is_os_supported` exists and has
  no caller outside tests.
- `config/network.yaml` has 19 rules, all about the gateway, the backends,
  and the collector. It has no rule for SSH or for traffic between cluster
  nodes.
- `render-contracts` already renders non-secret outputs for the gateway,
  backends, Grafana, and Vault. `docs/01` records that Ansible inputs are not
  rendered yet.
- `quickstart.py` does four things in one call: check prerequisites, create
  missing secrets in Vault, render and materialize, and run Compose locally.
  Its `--bind-address` is refused unless it is loopback, and the Compose file
  publishes only the loopback-scoped entry point.
- `collector.py` has `vm` and `external-service` profiles that require a
  certificate-bound credential, and `alloy-configs/` holds their sources.
- `trust.issue_certificate` refuses to overwrite. The quickstart has private
  logic for "issue again if missing, expiring, or from another authority".
- Only the command-line tool talks to Vault. Hosts and containers receive
  files.

Constraints:

- No target machine exists, and none may be changed without separate
  permission. Rootless Podman is available for containers.
- The project's rules: no silent fallbacks, every port owned by the network
  contract, every version owned by the matrix, checksums for downloads.

## Goals / Non-Goals

**Goals:**

- A host can be taken from a bare supported system to a running single-node
  stack, or a set of hosts to a Ready Kubernetes cluster, by playbooks whose
  every input is rendered or pinned.
- Applying the automation cannot lock the administrator out.
- The logic that can be wrong in interesting ways (layout validation, rule
  derivation, rendering) lives in Python where it is unit-tested, not in
  templates.

**Non-Goals:**

- Installing MetalLB, Longhorn, Traefik, or cert-manager, and anything else
  that runs in the cluster (phase 6). Only their prerequisites are checked.
- Creating machines, or anything on AWS.
- High availability for the Docker deployment. It is one node.
- A publicly trusted server certificate for the Docker deployment. The
  gateway certificate comes from Vault's PKI, so clients must trust that
  authority.
- Upgrading k3s or Docker in place. A version mismatch stops the role; the
  upgrade procedure is phase 10 documentation.
- Kubernetes or cloud auth methods for Vault (phase 6).
- Running any of this against a real host as part of this change.

## Decisions

### D1. Layout

```
ansible/
  ansible.cfg
  requirements.yml            pinned collections
  requirements-dev.txt        ansible-core, ansible-lint, molecule (pinned)
  inventories/
    docker-host.example/      one host
    k3s-development.example/  one node
    k3s-production.example/   three servers, agents, storage nodes
  playbooks/
    docker-host.yml  docker-teardown.yml  k3s-cluster.yml  external-collector.yml
  roles/
    preflight  hardening  firewall  docker_engine  nighthawk_stack
    k3s_prerequisites  k3s_node  cilium  alloy_collector
```

Each playbook starts with `preflight` on every targeted host, so an
unsupported system stops the run before any host changes.

### D2. Ansible reads one rendered file, and nothing else from the repository

`render-contracts` gains an `ansible/` output: one variables file holding
the firewall rules per host role, the pins and checksums the roles install,
the supported operating systems per role, and the platform values roles need
(hostnames, entry point ports). Playbooks load that file by path. Roles
contain no port, version, or checksum literal, and a unit test scans the
role files for the values the rendered file provides.

*Alternative:* a custom Ansible lookup plugin that reads `config/*.yaml`.
Rejected: it would be a second, untested reader of the contracts.

### D3. Secrets are materialized by the command-line tool and copied

Ansible has no Vault access and the `community.hashi_vault` collection is
not used. Whatever a host needs (stack secrets, the collector's credential
and key, the k3s join token) is written on the control machine by existing
or new commands into an owner-only directory, and copied by `no_log` tasks.
This keeps the rule from the Vault change: one program holds the Vault
credential.

### D4. The network contract gains the flows a host firewall needs

New rules in `config/network.yaml`: SSH administration
(`restricted-external`), and between cluster nodes the API server, kubelet,
embedded etcd, and Cilium's overlay and health ports (`private`). The
gateway loader ignores rules it does not select, so existing consumers keep
working; the AWS parity test already filters on destination.

The rendered rule set per role:

| Role | Inbound from other machines |
| --- | --- |
| Docker host | SSH from the admin allowlist; the external entry point from the collector allowlist |
| Cluster server | SSH; API server, kubelet, etcd, Cilium ports from the other nodes |
| Cluster agent | SSH; kubelet and Cilium ports from the other nodes |
| External collector host | SSH only |

Node-to-node sources are the inventory's node addresses, not a subnet.

### D5. Firewall tool per family, with a self-cancelling undo

UFW on Debian and Ubuntu, firewalld on Rocky and Alma, from the same rendered
rules. Before any change the role fails if an allowlist is empty or covers
every address, or if the address of its own SSH connection is outside the
admin allowlist.

The change itself: record the current state, schedule a transient systemd
timer that restores it after a stated number of minutes, apply the rules
(SSH allow first, then default deny), open a fresh connection, and cancel
the timer only if that connection works. A lost connection therefore heals
itself.

Docker publishes container ports through its own chains and bypasses UFW and
firewalld by default. On Docker hosts the role restricts the published
external entry point in the `DOCKER-USER` chain, which Docker evaluates
first, and the loopback entry point is bound to loopback so nothing needs
filtering.

*Alternative:* write nftables directly on both families. Rejected: it
fights the tool each distribution ships and manages.

### D6. Docker Engine from the vendor repository, key pinned

The matrix records the engine version and the fingerprint of Docker's
repository signing key. The role downloads the key, compares fingerprints
before trusting it, adds the repository, installs the exact package version
for the distribution, and holds it. A different installed version is an
error unless `docker_engine_allow_version_change` is set.

### D7. Rendering is split from starting, and a deployment is rendered for a host

`quickstart.py` is refactored so that preflight, secret creation, and
rendering are callable without Compose. A new command,
`render-docker-deployment`, runs those for a remote host: it takes the
remote base directory, the remote account's UID and GID, and the external
bind address, and writes a rendered tree and a secret tree whose
`compose.env` names remote paths. `quickstart-docker` keeps its behaviour
and its loopback-only rule.

The command refuses a document that is not `profile: production` or does not
select the external entry point. The existing production rules then apply by
themselves: a TLS, non-loopback Vault and a non-root credential.

`nighthawk_stack` synchronizes both trees to the host (secrets `no_log`,
owner-only), copies the image build context that `.dockerignore` already
defines, builds the image there, and runs Compose with the new external
override. It reloads the auth service and collector when their files
changed, as the quickstart does.

*Alternative:* render on the host. Rejected: the host would need Vault
access and the Python environment.

### D8. External publication is a Compose override

`docker-compose.external.yaml` adds one port mapping to the gateway: the
external entry point's port on `NIGHTHAWK_EXTERNAL_BIND_ADDRESS`, required.
The base file is unchanged, so the local quickstart cannot publish it. An
end-to-end case starts the local stack with this override on a second
loopback address and checks that ingestion needs the client certificate.
That is the one part of remote deployment that can be observed here.

### D9. k3s from a verified binary, add-on-free, with Cilium through k3s itself

The role downloads the k3s binary for the node's architecture, verifies the
matrix checksum, and installs its own systemd unit and configuration file.
It does not run the upstream install script. Configuration: no Flannel, no
network policy controller, no Traefik, no ServiceLB, secrets encryption on.

Cilium is installed by placing a `HelmChart` manifest, with the pinned chart
version, in the server's manifests directory, where k3s's built-in Helm
controller applies it. No Helm binary or cluster credential is needed on the
control machine.

Servers join one at a time (`serial: 1`) with embedded etcd; agents join
after. The playbook waits for every node to be Ready.

*Alternative for Cilium:* the Cilium CLI, or Helm from the control machine.
Both need another pinned binary and the kubeconfig off the server.

### D10. The join token is a declared secret

The self-hosted platform document gains an optional
`cluster.join_token_secret_ref`. `generate-cluster-token` stores a generated
token in Vault with the existing store semantics, so it is created once and
never replaced. `materialize-secrets` writes it with the other secrets and
the role copies it.

### D11. Layout validation is Python; fact gathering is Ansible

`k3s_prerequisites` gathers per-node facts (kernel version, loaded and
available modules, packages, block devices and mounts, interface MTU,
addresses) and passes them, with the inventory's declared shape, ranges,
storage disks, and address pool, to a new command, `check-cluster-layout`,
on the control machine. The command returns every problem by node, or
nothing. All the rules in the `k3s-cluster-bootstrap` preflight requirement
live there and are unit-tested with plain data.

### D12. Alloy as a systemd service

The role installs the pinned Alloy archive (checksum from the matrix),
creates an `alloy` system account, and writes a unit with `NoNewPrivileges`,
`ProtectSystem=strict`, `ProtectHome`, and a single writable state
directory. The configuration comes from `render-collector` on the control
machine; the role runs Alloy's own validation on the host before replacing
the active configuration, and reloads rather than restarts.

### D13. `issue-certificate --if-needed`

The quickstart's private check moves into `trust.py` and is exposed as
`--if-needed --renew-before-days N`. When a replacement is needed the new
key and certificate are written beside the old ones and renamed into place
only after the returned certificate is verified, so a failed request leaves
a working pair.

### D14. Evidence in the OS matrix

Each `os_support` entry gains `evidence`: `container` (role tests ran in a
container of that system), `host` (a playbook ran on a real machine), or
`declared`. After this change every Docker host entry is `container`; the
k3s entries stay `declared`, because k3s is not started in tests.

### D15. Verification

- **Unit tests** for the rendered inputs, the layout checker, the deployment
  renderer, `--if-needed`, and the repository checks (no literals in roles,
  no secrets in inventories).
- **`ansible-lint`** on everything, and `--syntax-check` per playbook.
- **Molecule with the Podman driver**, on systemd-enabled images of the five
  supported systems, with converge, idempotence, and check-mode steps:

  | Role | In a container |
  | --- | --- |
  | `preflight` | Fully, including an unsupported image |
  | `hardening` | SSH configuration and validation; kernel settings where the container permits |
  | `docker_engine` | Key verification, repository, pinned package; the daemon is not started |
  | `alloy_collector` | Fully: the service runs and loads its configuration |
  | `firewall` | Rule generation and the refusals; enforcement is not observable |
  | `k3s_node`, `cilium` | Download, checksum, unit, configuration, manifest; k3s is not started |
  | `nighthawk_stack` | File synchronization and permissions; Compose is not run |

- **End-to-end**: the external override case in D8.

Nothing runs on a real host.

### D16. New and changed commands

| Command | Change |
| --- | --- |
| `render-contracts` | also writes `ansible/` inputs |
| `render-docker-deployment` | new: render a deployment for a remote host |
| `generate-cluster-token` | new |
| `check-cluster-layout` | new: validate gathered facts against the declared shape |
| `issue-certificate` | `--if-needed`, `--renew-before-days` |
| `quickstart-docker` | unchanged behaviour; shares code with the renderer |

## Risks / Trade-offs

- [Most of the automation cannot be run here] → Each role's testable part is
  tested in containers, the logic is in unit-tested Python, and
  `docs/08-ansible.md` lists what was never executed. The matrix's
  `evidence` field keeps the claim honest per system.
- [A firewall mistake cuts off a real host] → Three guards: allowlist
  validation, the own-address check, and the self-cancelling undo. None of
  the three has been exercised on a real host, which the document states.
- [Docker bypasses the host firewall] → The `DOCKER-USER` rule in D5; noted
  in the firewall section of the document.
- [The network contract grows rules unrelated to the gateway] → They carry
  their own source and destination identities, and the gateway loader only
  considers rules it selects. Unit tests cover that nothing else changed.
- [Remote build of the image needs registry access from the host] → Stated
  as a prerequisite; an air-gapped path is out of scope.
- [Molecule and its driver add a sizeable development dependency] → Kept in
  `requirements-dev.txt`, separate from the runtime requirements, and pinned
  in the matrix.
- [Systemd in rootless containers is fragile] → If an image cannot run
  systemd under Podman here, that system's `evidence` stays `declared` and
  the reason is recorded, not worked around.

## Migration Plan

Additive, except the network contract: rendered outputs gain rules, so a
re-render changes `ports.md` and `network.json`. Nothing deployed consumes
them. Rollback is reverting the change.

## Open Questions

- Whether production clusters should replace kube-proxy with Cilium. The
  bootstrap keeps kube-proxy; changing it later is a Cilium value, not a
  restructuring.
- The default for the firewall's undo delay. It is an inventory value with a
  documented starting point of five minutes.
