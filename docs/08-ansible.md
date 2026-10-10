# Host and cluster automation (Ansible)

## Status

The `ansible/` tree prepares machines other than the one the repository is
checked out on: it hardens a host, sets its firewall, installs a pinned
Docker Engine and deploys the Compose stack to it, bootstraps a k3s cluster
with Cilium for the self-hosted profile, and runs a collector as a host
service on machines outside the platform.

**No playbook has been run against a real machine.** Everything here is
linted, syntax-checked, and tested role by role in systemd containers under
rootless Podman. A container cannot show everything; what has and has not
been observed is listed under [Verification limits](#verification-limits),
and the same limits are recorded per operating system in
`config/versions.yaml` (`os_support.*.evidence`).

MetalLB, Longhorn, Traefik, and cert-manager are not installed by this
phase. Their prerequisites are checked here; they are installed with the
workloads in phase 6.

## How the pieces fit

```text
control machine                                         target host
───────────────                                         ───────────
config/*.yaml ──► python -m nighthawk render-contracts
Vault ──────────► python -m nighthawk render-docker-deployment
                  python -m nighthawk materialize-secrets
                  python -m nighthawk issue-certificate --if-needed
                          │ files
                          ▼
                  ansible-playbook ───── SSH ─────────► roles
```

Three rules hold for every role:

- **Ansible never reads `config/*.yaml` or Vault.** Ports, versions,
  checksums, signing-key fingerprints, kernel settings, and the supported
  operating systems come from one rendered file,
  `<output>/ansible/nighthawk.yml`. A repository test fails when a role
  spells out a value that file provides.
- **A target host never talks to Vault.** Every secret reaches it as a file
  copied from the control machine, in a task marked `no_log`.
- **An inventory holds no secret.** It holds the *location* of files on the
  control machine. A repository test fails on a secret-looking inventory
  variable that is not a location.

Every role supports check mode (`--check`) and is idempotent: a second run
reports no change.

## Control machine setup

The automation tools are separate from the platform's own Python
environment and are pinned in `config/versions.yaml`
(`automation_tools`). `python -m nighthawk check-pins` fails when the
requirement files and the matrix differ.

```sh
python3 -m venv ansible/.venv
ansible/.venv/bin/pip install -r ansible/requirements-dev.txt
cd ansible
.venv/bin/ansible-galaxy collection install -r requirements.yml -p .collections
```

Run every `ansible-playbook` command from the `ansible/` directory, where
`ansible.cfg` is. The control machine also needs the platform's own
environment (`requirements.txt`) for the `python -m nighthawk` commands, and
SSH access to the targets with an account that can become root.

### Rendering the inputs

```sh
python -m nighthawk render-contracts --config platform.yaml --output deploy/contracts
```

This writes `deploy/contracts/ansible/nighthawk.yml` among the other
contracts. It reads no secret and does not contact Vault. Render it again
after any change to the platform document, `config/network.yaml`, or
`config/versions.yaml`.

### Inventories

`ansible/inventories/` holds four examples. Copy one, replace the documented
addresses, and point `nighthawk_inputs_file` at the rendered file.

| Example | For |
| --- | --- |
| `docker-host.example` | One Docker host running the Compose stack |
| `k3s-development.example` | A single-node k3s cluster |
| `k3s-production.example` | Three servers and three agents that offer storage |
| `external-collector.example` | Machines outside the platform that run a collector |

## Supported systems and the preflight

| Host role | Supported |
| --- | --- |
| Docker host, collector host | Ubuntu 22.04 and 24.04, Debian 12, Rocky Linux 9, AlmaLinux 9 |
| k3s server or agent | Ubuntu 22.04 and 24.04 |

Architectures are `amd64` and `arm64`. The list is the `os_support` section
of the compatibility matrix; the roles read it from the rendered inputs.

Every playbook starts with the `preflight` role in a play of its own. It
compares each host's distribution, version, and architecture with that list
for the host's role (`nighthawk_host_role`), and if **any** host is not
supported it stops the run for **all** of them, naming each unsupported
host, before anything is changed.

## Hardening

The `hardening` role changes exactly the following, and nothing else.

**SSH** (`/etc/ssh/sshd_config.d/00-nighthawk.conf`; `sshd_config` gets an
`Include` line for that directory if it has none):

| Setting | Value |
| --- | --- |
| `PubkeyAuthentication` | `yes` |
| `PasswordAuthentication` | `no` |
| `KbdInteractiveAuthentication` | `no` |
| `PermitRootLogin` | `prohibit-password` |

**Kernel network settings** (`/etc/sysctl.d/90-nighthawk.conf`):

| Setting | Docker host | k3s node | Collector host |
| --- | --- | --- | --- |
| `net.ipv4.ip_forward` | 1 | 1 | 0 |
| `net.ipv4.conf.all.rp_filter` | 2 (loose) | 0 | 1 (strict) |
| `net.ipv4.conf.{all,default}.accept_redirects` | 0 | 0 | 0 |
| `net.ipv6.conf.{all,default}.accept_redirects` | 0 | 0 | 0 |
| `net.ipv4.conf.all.send_redirects` | 0 | 0 | 0 |
| `net.ipv4.conf.{all,default}.accept_source_route` | 0 | 0 | 0 |
| `net.ipv4.icmp_echo_ignore_broadcasts` | 1 | 1 | 1 |
| `net.ipv4.tcp_syncookies` | 1 | 1 | 1 |

Forwarding stays on wherever containers run, and a k3s node does not get a
strict reverse-path filter, because the cluster network routes packets
asymmetrically between nodes. The values per role are defined in
`nighthawk/ansible_inputs.py` and reach the role through the rendered
inputs.

**Time synchronization:** `systemd-timesyncd` on Debian and Ubuntu,
`chrony` on Rocky and Alma, installed and enabled.

**Automatic security updates:** off unless
`hardening_automatic_security_updates: true`. Then `unattended-upgrades`
(Debian family) or `dnf-automatic` limited to security fixes (RHEL family).

Two safeguards protect the administrator's access:

1. Password authentication is disabled only if the connecting account
   (`hardening_ssh_user`, by default the Ansible user) has at least one key
   in its `authorized_keys`. Otherwise the role fails and the SSH
   configuration is not touched.
2. After writing its settings the role runs `sshd -t`. If sshd refuses the
   configuration, the previous file is put back and sshd is not reloaded.

## Firewall

The `firewall` role uses the tool each distribution manages: UFW on Debian
and Ubuntu, firewalld on Rocky and Alma. Inbound traffic is denied by
default. The only ports opened are the network contract's rules for the
host's role, each from the addresses of one named group:

| Host role | Rule | Port | Allowed from |
| --- | --- | --- | --- |
| Docker host | `ssh-administration` | tcp 22 | `firewall_admin_allowlist` |
| | `remote-gateway` | tcp 443 | `firewall_collector_allowlist` |
| k3s server | `ssh-administration` | tcp 22 | `firewall_admin_allowlist` |
| | `cluster-api-administration` | tcp 6443 | `firewall_admin_allowlist` |
| | `cluster-api-server` | tcp 6443 | every cluster node |
| | `cluster-etcd-client`, `cluster-etcd-peer` | tcp 2379, 2380 | the servers |
| | `cluster-kubelet` | tcp 10250 | every cluster node |
| | `cluster-network-overlay` | udp 8472 | every cluster node |
| | `cluster-network-health` | tcp 4240 | every cluster node |
| k3s agent | `ssh-administration` | tcp 22 | `firewall_admin_allowlist` |
| | `cluster-kubelet`, `cluster-network-overlay`, `cluster-network-health` | as above | every cluster node |
| Collector host | `ssh-administration` | tcp 22 | `firewall_admin_allowlist` |

The table is `nighthawk.firewall` in the rendered inputs; the ports are
those of `config/network.yaml`. Cluster node addresses come from the
inventory's `k3s_servers` and `k3s_agents` groups.

### Refusals

Before anything is changed, the role fails if:

- a rule's group has no address (for example an empty
  `firewall_admin_allowlist`);
- an entry is not an address or CIDR range, or covers every address
  (`0.0.0.0/0`, `::/0`);
- `firewall_requested_ports` names a port that is not a contract rule for
  the host's role;
- the address the playbook's own SSH session comes from is not inside
  `firewall_admin_allowlist`.

### Protection against locking yourself out

When the rules differ from what is in force, the role:

1. saves the current firewall state;
2. starts a transient systemd timer (`nighthawk-firewall-rollback`) that
   restores that state after `firewall_rollback_minutes` (default 5);
3. applies the new rules, the SSH rule first;
4. drops its connection and opens a new one;
5. cancels the timer only if the new connection works, then makes the rules
   permanent.

If the new rules cut the administrator off, step 4 fails, the timer is not
cancelled, and the host restores its previous firewall by itself.

If everything else fails, use the machine's console: `ufw disable` (Debian
family) or `systemctl stop firewalld` (RHEL family) removes the filtering,
and `/usr/local/sbin/nighthawk-firewall-rollback --restore` puts back the
state saved before the last change.

`firewall_activate: false` writes the rule scripts and the plan under
`/etc/nighthawk/firewall` for review and activates nothing.

### Docker bypasses the host firewall

Docker publishes container ports through its own chains, which are
evaluated before UFW's and firewalld's. A UFW rule therefore does not
restrict a published port. On Docker hosts the role also installs
`nighthawk-firewall-docker.service`, which inserts rules into the
`DOCKER-USER` chain so that the published external entry point accepts
only `firewall_collector_allowlist`. The loopback entry point is bound to
`127.0.0.1` and needs no filtering.

## Docker host deployment

This is a **single node without high availability**. If the host is lost,
so is everything stored on it: metrics, logs, traces, profiles, and
Grafana's database. The secrets and the certificate authority are in
Vault and are not lost. There is no backup or restore in this phase.

### What you need

- A supported host, reachable over SSH.
- A platform document with `profile: production`. That profile already
  requires a TLS Vault address that is not loopback and a Vault credential
  without the root policy.
- The external entry point selected in the document:
  `gateway.entry_points` must include `remote-gateway`.
- The host address the external entry point is published on. There is no
  default.

### Render

On the control machine, with a Vault credential in `VAULT_TOKEN` or a token
file (see [configuration](02-configuration.md)):

```sh
python -m nighthawk render-contracts --config platform.yaml --output deploy/contracts
python -m nighthawk render-docker-deployment --config platform.yaml \
    --output deploy/host-1 --remote-dir /opt/nighthawk --uid 2001 --gid 2001 \
    --external-bind-address 192.0.2.10
```

`render-docker-deployment` does everything the local quickstart does
before starting containers: it checks Vault, creates missing secrets
there, issues certificates, and writes two trees, `deploy/host-1/rendered`
and `deploy/host-1/secrets`. The Compose environment file in the rendered
tree names the host's paths (`/opt/nighthawk/...`) and the UID and GID
given here. It starts nothing and does not touch this machine's container
runtime. Running it again with nothing changed creates no secret, issues
no certificate, and rewrites no file.

No file in either tree contains the Vault credential. The secret tree does
contain the platform's secrets and private keys; it is owner-only, and it
is what gets copied.

The command refuses a development-profile document, a document that does
not select `remote-gateway`, and a missing or malformed
`--external-bind-address`, before writing anything.

### Deploy

Set in the inventory (see `docker-host.example`): `nighthawk_inputs_file`,
`nighthawk_rendered_dir`, `nighthawk_secrets_dir`,
`nighthawk_external_bind_address`, `nighthawk_stack_dir` (the
`--remote-dir`), and the two firewall allowlists.

```sh
cd ansible
.venv/bin/ansible-playbook -i inventories/my-docker-host playbooks/docker-host.yml --check --diff
.venv/bin/ansible-playbook -i inventories/my-docker-host playbooks/docker-host.yml
```

The playbook runs `preflight`, `hardening`, `firewall`, `docker_engine`, and
`nighthawk_stack`.

**`docker_engine`** downloads Docker's repository signing key, compares its
fingerprint with the one pinned in the matrix, and only then trusts it. A
key with another fingerprint installs nothing. It installs the pinned
engine and Compose plugin (`host_artifacts.docker_engine`), holds the
version against unattended upgrades, and enables the service. If a
different engine version is already installed, the role fails and states
both versions; set `docker_engine_allow_version_change: true` to let it
change the version.

**`nighthawk_stack`** refuses a deployment that was rendered for another
directory or another external address. It creates the stack's account with
the rendered UID and GID, copies the rendered tree, copies the secret tree
(owner-only, for that account), and unpacks the image source. Only files
that differ are replaced, and files that are no longer rendered are
removed. It then builds the platform's own image on the host, starts the
stack with the external override, waits for every service to be healthy,
and provisions Grafana. If a service does not become healthy in time, the
playbook fails and names it. Its last message states that the installation
is a single node without high availability.

### Verify from another machine

From a machine inside `firewall_collector_allowlist`, with a client
certificate for a credential that declares a certificate identity (see
[gateway](05-gateway.md)):

```sh
curl --cacert gateway-ca.pem --cert client.crt.pem --key client.key.pem \
     --resolve gateway.nighthawk.internal:443:192.0.2.10 \
     --user "example-ingest:$(cat credential)" \
     --header 'Content-Type: application/json' \
     --data '{"streams":[{"stream":{"job":"check"},"values":[["'"$(date +%s%N)"'","hello"]]}]}' \
     https://gateway.nighthawk.internal:443/logs/loki/api/v1/push
```

It answers 204. Without `--cert` and `--key` the same request is refused
with 403: ingestion on the external entry point needs the client
certificate. The loopback entry point is published on the host's
`127.0.0.1` only and cannot be reached from another machine.

### Changing a running deployment

Change the platform document or a secret, run both render commands again,
and run the playbook again. Only changed files are copied. Services that
watch their files pick the change up by themselves; the auth service and
the collector do not, and are sent a reload when their files changed.
Stored telemetry is kept.

### Teardown

```sh
.venv/bin/ansible-playbook -i inventories/my-docker-host playbooks/docker-teardown.yml
```

This removes the containers and networks and keeps the volumes, the secret
files, and the certificates; a later deployment finds its earlier
telemetry. To delete those too, confirm with the host's inventory name:

```sh
.venv/bin/ansible-playbook -i inventories/my-docker-host playbooks/docker-teardown.yml \
    -e nighthawk_stack_purge=true -e nighthawk_stack_purge_confirm=nighthawk-docker-1
```

A purge without the matching name deletes nothing.

## k3s cluster (self-hosted profile)

### Shapes

| `k3s_cluster_shape` | Nodes | Use |
| --- | --- | --- |
| `development` | Exactly one, a server that also runs workloads | Reduced footprint, no high availability |
| `production` | An odd number of at least three servers, and at least three nodes that declare a storage disk | Embedded etcd keeps quorum when one server is lost |

### Inventory variables

| Variable | Meaning |
| --- | --- |
| `k3s_cluster_shape` | `development` or `production` |
| `k3s_node_network` | The network the nodes' own addresses are in |
| `k3s_pod_cidr`, `k3s_service_cidr` | Ranges for pods and services |
| `k3s_node_mtu` | MTU of the interface nodes reach each other on |
| `k3s_load_balancer_pool` | Addresses the load balancer will hand out in phase 6 |
| `k3s_storage_disks` | Per node: whole, unused block devices for the storage add-on |
| `k3s_join_token_file` | The materialized join token on the control machine |
| `k3s_fetch_kubeconfig_to` | Optional path on the control machine for the kubeconfig |

### The join token

The token nodes join with is a secret the platform document declares
(`cluster.join_token_secret_ref`; see `config/self-hosted.example.yaml`).
It is generated once, stored only in Vault, and never replaced, because
nodes already hold it:

```sh
python -m nighthawk generate-cluster-token --config platform.yaml
python -m nighthawk materialize-secrets --config platform.yaml --output-dir .materialized-secrets
```

Running `generate-cluster-token` again says the token exists and writes
nothing. The role copies the materialized file to each node, readable by
root only, in a `no_log` task; k3s reads it through `token-file`, so it
appears in no command line.

### Prerequisite check

Before any node is changed, `k3s_prerequisites` gathers facts from every
node and passes them to `python -m nighthawk check-cluster-layout` on the
control machine. Every rule lives in that command
(`nighthawk/cluster_layout.py`). All problems are reported together, each
starting with the node it concerns, and one problem on one node stops the
run for all of them. It checks:

- the shape: one server node for `development`; an odd number of at least
  three servers and at least three storage nodes for `production`;
- kernel 5.10 or newer on every node;
- the kernel modules `overlay`, `br_netfilter`, and `vxlan` loaded or
  loadable on every node, and `iscsi_tcp` and `dm_crypt` on nodes that
  declare a storage disk;
- the packages `open-iscsi`, `nfs-common`, and `cryptsetup` on nodes that
  declare a storage disk;
- that each declared storage disk exists and is not mounted, partitioned,
  or held by another device;
- one MTU across all nodes, equal to `k3s_node_mtu`;
- node addresses that are distinct and inside `k3s_node_network`;
- pod, service, and node ranges that do not overlap;
- a load-balancer pool that is not empty, lies inside the node network, and
  contains no node's address.

The check installs nothing. Install the storage packages yourself, or let
phase 6 do it.

### Bootstrap

```sh
cd ansible
.venv/bin/ansible-playbook -i inventories/my-cluster playbooks/k3s-cluster.yml
```

The playbook runs both checks on every node, hardens every node and sets
its firewall, bootstraps the first server, joins further servers one at a
time, joins the agents, and waits until every node is Ready. If a node is
not Ready in time, it fails and names it.

**`k3s_node`** downloads the k3s binary for the node's architecture and
keeps it only if its SHA-256 matches the matrix
(`host_artifacts.k3s.sha256`). It does not run the k3s project's install
script. It writes `/etc/rancher/k3s/config.yaml` and its own systemd unit.
Servers run with the bundled Flannel, network policy controller, Traefik,
and ServiceLB disabled and with secrets encryption on. A node that already
runs another k3s version is not changed: the role fails and states both
versions unless `k3s_node_allow_version_change: true`.

**`cilium`** places a `HelmChart` manifest with the pinned chart version,
the pod range, the MTU, and the overlay port in the servers' manifests
directory. k3s's built-in Helm controller applies it, so neither Helm nor a
cluster credential is needed on the control machine.

### Adding a node

Add the host to `k3s_servers` or `k3s_agents` and run the playbook again.
The existing token is used, Vault is not written to, and nodes whose
configuration did not change are not restarted.

### The kubeconfig

The administrative kubeconfig stays on the servers, readable by root only.
It leaves them only if `k3s_fetch_kubeconfig_to` is set; then it is written
on the control machine with owner-only permissions, addressed to the first
server.

### What comes next

[09-kubernetes.md](09-kubernetes.md): `k8s-addons.yml` installs MetalLB
(using the address pool checked here), cert-manager, and the Vault Secrets
Operator, and `k8s-platform.yml` deploys the platform. Only the development
profile is implemented; Longhorn and the production profile are not.

The `cilium` role sets `policyCIDRMatchMode: nodes`, which the platform's
NetworkPolicies need to reach the cluster API and the kubelets. Like the
rest of the cluster bootstrap, that has not run on a real cluster.

## External collectors

The `alloy_collector` role runs Grafana Alloy as a systemd service on a
virtual machine, or on a host next to an external service, delivering to
the gateway's external entry point.

### Prepare on the control machine

```sh
python -m nighthawk render-collector --config platform.yaml --tenant example \
    --datastream application --profile vm --entry-point remote-gateway --output deploy/collector-vm
python -m nighthawk materialize-secrets --config platform.yaml --output-dir .materialized-secrets
```

Set the paths in the inventory (see `external-collector.example`), with the
credential's ID, the certificate's validity, and the renewal period. The
ingestion credential must declare a `certificate_identity`: the external
entry point refuses ingestion without a client certificate.

### Run

```sh
cd ansible
.venv/bin/ansible-playbook -i inventories/my-collectors playbooks/external-collector.yml
```

The playbook first runs, on the control machine,

```sh
python -m nighthawk issue-certificate --config platform.yaml --credential example-ingest \
    --valid-days 90 --if-needed --renew-before-days 30 --output-dir deploy/collector-certificates
```

which issues a certificate only when none exists, when the existing one has
fewer than the renewal days left, or when it was signed by an authority
Vault no longer has. Otherwise it says there is nothing to do. It fails,
before any host is changed, when the credential declares no certificate
identity. The Vault credential comes from `VAULT_TOKEN` or a token file on
the control machine, never from the inventory.

On the host the role then:

- downloads the pinned Alloy archive and keeps it only if its SHA-256
  matches the matrix (`host_artifacts.alloy.sha256`);
- creates the `alloy` system account and the unit `nighthawk-alloy.service`,
  which restarts on failure and is confined: `NoNewPrivileges`,
  `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, no capabilities, and
  one writable directory, `/var/lib/nighthawk-alloy`. That is the set the
  role tests start on every supported system. Further `[Service]` lines,
  such as `PrivateDevices=true`, can be added with
  `alloy_collector_unit_extra_lines`; they are not tested here, because
  systemd needs a capability for them that an unprivileged test container
  does not have;
- copies the credential, the gateway's authority, and the client
  certificate and key to `/etc/nighthawk-alloy/secrets`, readable by the
  `alloy` account only;
- copies the rendered configuration as a candidate, runs
  `alloy validate` on it, and replaces the active configuration only if
  Alloy accepts it. A refused configuration fails the playbook and leaves
  the running one in place;
- reloads the service when only the configuration, the credential, or the
  certificate changed, so the write-ahead log is kept. It restarts only when
  the binary, the unit, or the account changed.

### Renewing the certificate

Run the playbook again, for example from a scheduler on the control
machine. While the certificate has more than `alloy_collector_renew_before_days`
left, nothing is requested and the service is not touched. Within that
period a new certificate is issued for the same identity and copied, and
the service reloads.

## Tests

```sh
ansible/molecule/images/build.sh     # once: one systemd test image per supported system
ansible/molecule/run.sh              # every role scenario, one after another
ansible/molecule/run.sh firewall     # one role
python -m unittest tests.test_ansible_lint tests.test_ansible_inputs tests.test_cluster_layout
```

`run.sh` renders the inputs from the example platform document, as an
operator would, and runs each scenario through Molecule's Podman driver:
a dry run against the untouched system, the real run, a second run that
must change nothing, and the scenario's own checks. Run one scenario at a
time; they share container names.

`tests.test_ansible_lint` runs `ansible-lint` with the `production` profile,
`--syntax-check` on every playbook, and parses every example inventory. It
is skipped when the tools are not installed.

## Verification limits

Nothing in this document has been run on a real machine. The role scenarios
run in unprivileged systemd containers, where the following cannot be
observed and were **not** exercised:

| Not exercised | Why | What was checked instead |
| --- | --- | --- |
| Firewall enforcement (UFW, firewalld, `DOCKER-USER`) | A rootless container has no packet filter of its own | The generated rules equal the rendered rule set; every refusal fails before any file is written |
| The self-cancelling undo | Needs a real firewall and a real SSH session | The scripts and the plan are generated; activation is switched off in the scenario |
| Loading kernel settings | A container cannot set host kernel parameters | The file is written with the values for the host's role |
| Starting the Docker daemon | No nested container runtime | The pinned packages are installed and held; the wrong-fingerprint case installs nothing |
| Building the image and starting the stack through `nighthawk_stack` | No container runtime in the test container | File synchronization, permissions, the refusals, reload detection, and teardown. The stack itself, with the external override, is exercised locally by the end-to-end suite |
| Starting k3s, joining nodes, Cilium, node readiness | Need a real kernel, cgroups, and several machines | The verified binary, configuration, unit, and manifest content |
| Kernel modules, disks, and MTU on real nodes | Container facts describe the test machine | Every layout rule is unit-tested with plain data; the fact gathering and the hand-over to the check run in a scenario |
| A deployment to a remote host, end to end | No target machine exists | Each part separately, as above |
| `arm64` | The test machine is `amd64` | Checksums for both architectures are pinned and validated for shape |

Each `os_support` entry in the matrix records which of these levels it
rests on: `container`, `host`, or `declared`.
