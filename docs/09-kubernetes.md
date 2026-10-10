# Self-hosted Kubernetes

## Status

The **development profile** of the self-hosted deployment is implemented and
runs: one node, every backend as a single replica, no Kafka, not highly
available. It has been installed on a local single-node cluster and
exercised by an acceptance suite; see [Observed results](#observed-results).

The **production profile is not implemented.** Distributed backends, Kafka,
replicated object storage, the shared PostgreSQL, Longhorn, disruption
budgets, topology spread, and autoscaling are not rendered. A production
document renders everything except the Kubernetes workloads and says so.
The charts and operators it will need are already locked in the
compatibility matrix.

Nothing has run on the k3s cluster that [08-ansible.md](08-ansible.md)
builds. The test cluster is minikube's Kubernetes 1.34 with Calico under
rootless Podman, not the pinned k3s with Cilium. What was exercised is the
platform on Kubernetes, not that cluster. AWS/EKS is a separate, later change.

## How it fits together

```text
control machine                                      cluster
───────────────                                      ───────
platform.yaml ─► render-contracts ─► kubernetes/     namespace nighthawk
                                     vault/            backends, storage, auth service,
Vault ◄── doctor --cluster                             Grafana, collectors
  ▲                                                  namespace nighthawk-gateway
  │        ansible-playbook k8s-addons.yml ─────────►  gateway (Traefik)
  │        ansible-playbook k8s-platform.yml ───────► add-on namespaces
  │                                                    MetalLB, cert-manager,
  └──────────── read-only, per workload ◄───────────   Vault Secrets Operator
```

- The renderer turns the platform document into Helm values, an ordered
  release list, and what Vault must allow. It reads no secret and does not
  contact Vault.
- Two playbooks run Helm from the control machine against the cluster API.
  They take release names, charts, versions, and order from the rendered
  `releases.yaml`.
- Workloads get their secrets from Vault through the Vault Secrets Operator
  and their certificates from Vault's PKI through cert-manager. **No Vault
  credential is stored in the cluster.**

This changes one earlier rule. Under Compose only the command-line tool
talks to Vault. On Kubernetes the cluster reads Vault too: read-only, and
each workload only its own secrets. The tool remains the only writer.

## Prerequisites

- A cluster with a network plugin that enforces NetworkPolicy, and a
  kubeconfig for it on the control machine. For k3s, `k3s-cluster.yml` can
  fetch one.
- The tools: `python -m nighthawk fetch-tools` downloads the pinned Helm,
  kubectl, and kubeconform and keeps each only if its checksum matches the
  matrix. The Ansible environment is the one from
  [08-ansible.md](08-ansible.md#control-machine-setup); it now includes the
  `kubernetes.core` collection.
- The platform's own image in a registry the cluster can pull from, named
  by digest in the inventory (`nighthawk_image`). Build it from
  `docker-compose/nighthawk.Dockerfile`.
- A Vault the cluster can reach, with Kubernetes authentication; see below.

## The platform document

A self-hosted document differs from a Docker one in four places
(`config/self-hosted.example.yaml`, `tests/k8s/platform.yaml`):

| Setting | Why |
| --- | --- |
| `vault.kubernetes_auth.mount` | The Vault mount the cluster's service accounts log in at. Optional `cluster_address` when the cluster reaches Vault at another address than the tool does |
| `gateway.entry_points` includes `cluster-gateway` | The entry point collectors inside the cluster deliver through. It needs no client certificate; `remote-gateway` still does |
| `gateway.auth_service` and `gateway.upstreams` name `<service>.nighthawk.svc` | The gateway runs in a namespace of its own, so it calls the services by namespace. Rendering fails on any other host and names the expected one |
| One Vault path per secret | A Vault policy covers a whole path, so secrets read by different workloads cannot share one. Rendering fails and names the path and its readers |

`profile: development` selects the development profile. The playbook
refuses to deploy when it differs from the inventory's `k3s_cluster_shape`.

## Rendering

```sh
python -m nighthawk render-contracts --config platform.yaml --output deploy/contracts \
    --collector-tenant acme --collector-datastream web
```

`--collector-tenant` and `--collector-datastream` choose the datastream the
cluster's own collectors deliver to; without them it is the first.
`--collector-credential` and `--credential` choose among several ingestion
or query credentials, as elsewhere. Besides the other contracts this writes:

| Path | Content |
| --- | --- |
| `kubernetes/releases.yaml` | The profile, the add-ons and the releases in order, the locked source of every upstream chart, and the notice the deployment prints |
| `kubernetes/values/platform.yaml` | Values shared by every stage of the platform chart: workload identities and the secrets each reads, certificates, images by digest, and the non-secret files (gateway routes, overrides, collector configuration, backend configuration, Grafana desired state) |
| `kubernetes/values/<release>.yaml` | One per release |
| `vault/kubernetes-auth.json` | The roles and policies Vault must have |

Rendering is deterministic. Adding a datastream changes rendered output
only; no values file is edited by hand.

## What Vault must allow

`vault/kubernetes-auth.json` holds, for the mount the document names:

- one **role per workload identity**, bound to that workload's service
  account in the `nighthawk` namespace;
- one **read-only policy per workload**, covering exactly the paths of the
  secrets that workload reads;
- one role and policy for the certificate issuer, which may only have a
  request signed by the two PKI roles. It is bound to the `nighthawk` and
  `nighthawk-gateway` namespaces.

No policy can write a secret. Apply them to your Vault, enable Kubernetes
authentication at the mount, and point it at the cluster. Then check:

```sh
python -m nighthawk doctor --config platform.yaml --cluster deploy/contracts/vault/kubernetes-auth.json
```

It fails naming a missing mount, role, or policy, and a role or policy that
differs from the rendered one (it may allow more). For a disposable Vault:

```sh
python -m nighthawk bootstrap-dev-vault --config platform.yaml --confirm-disposable-vault \
    --ca-valid-days 365 --cluster-requirements deploy/contracts/vault/kubernetes-auth.json \
    --kubernetes-host https://kubernetes.default.svc
```

Two things to know:

- Vault validates a service account token by asking the cluster API, so
  **Vault must be able to reach the API server**. Add Vault's address to
  `firewall_admin_allowlist` on the servers. Vault's JWT authentication
  with the cluster's issuer keys needs no call back; it is not built here.
- The authority must outlive the certificates. They are issued for 90 days
  and renewed 30 days before expiry.

Create the secrets with `generate-credential`, `generate-storage-identity`,
and `store-secret`, as in [02-configuration.md](02-configuration.md).

## Add-ons

```sh
cd ansible
.venv/bin/ansible-playbook -i inventories/my-cluster playbooks/k8s-addons.yml
```

Inventory variables: `nighthawk_kubeconfig`, `nighthawk_k8s_rendered_dir`
(the rendered `kubernetes` directory), `nighthawk_helm_binary`,
`nighthawk_image`, `nighthawk_cluster_name`, and from phase 5
`k3s_cluster_shape` and `k3s_load_balancer_pool`.
`tests/k8s/inventory` is a working example.

For each add-on the role pulls the chart at its pinned version, compares
the package's SHA-256 with the matrix (`helm_charts`), and installs only
from a package that matches. It waits for each to be healthy and names the
one that is not.

| Add-on | Development | Production |
| --- | --- | --- |
| MetalLB (layer 2), with the inventory's address pool | installed | to be installed |
| cert-manager | installed | to be installed |
| Vault Secrets Operator | installed | to be installed |
| Longhorn, Strimzi, CloudNativePG | not installed: one node has no use for them | not implemented |

Every image of the three installed add-ons is set by digest
(`kubernetes_images` in the matrix).

## Deploying the platform

```sh
.venv/bin/ansible-playbook -i inventories/my-cluster playbooks/k8s-platform.yml
```

The releases, in order:

| Release | Chart | What |
| --- | --- | --- |
| `nighthawk-network`, `nighthawk-gateway-network` | platform | Default deny and the contract's allowances, before anything else exists |
| `nighthawk-foundation` | platform | Service accounts, secret synchronization, certificates, configuration |
| `nighthawk-storage`, `nighthawk-storage-init` | platform | SeaweedFS as one process, and the bucket job |
| `nighthawk-backends` | platform | Monolithic Mimir and Tempo |
| `loki`, `pyroscope` | upstream | One replica each, running the configuration this project renders |
| `nighthawk-authz` | platform | The auth service |
| `nighthawk-gateway-identity`, `traefik` | platform, upstream | The gateway's certificate and the gateway, in `nighthawk-gateway` |
| `grafana`, `grafana-provision` | upstream, platform | Grafana and the provisioning job |
| `nighthawk-collectors` | platform | The node and the cluster collector |

A release starts only when the one before it is healthy. A failure stops the
run and names the release and the pods that are not ready. If secrets or
certificates do not arrive, the failure lists them and points at
`doctor --cluster`. The last message states that the profile is a single
node without high availability and that retention values are examples.

Running the playbook again changes nothing: no release is upgraded, no pod
restarts, and the provisioning job reports `no changes`. After changing the
platform document, render again and run it again.

### Why some of it is not an upstream chart

Development Mimir and Tempo, and SeaweedFS, run from the platform chart with
the exact configuration and flags the Compose profile verified. The pinned
Mimir and Tempo charts are distributed-only. Loki, Pyroscope, and Grafana
use their upstream charts as packaging, with this project's configuration
passed in. So the development profile does not exercise the distributed
charts production will use.

### How a workload gets its secrets

Each workload has a service account and a `VaultAuth` bound to it. One
`VaultStaticSecret` per secret synchronizes it into a Secret named
`vault-<workload>-<secret>`. Two workloads that read the same secret each
get their own copy through their own identity. Secrets are read again every
60 seconds.

Three consumers need another shape than Vault stores:

- Backends read their S3 keys from the environment. The operator's
  transformation derives the two variables from the stored JSON.
- Grafana's chart wants a user and a password key; same mechanism.
- SeaweedFS needs one file listing every storage identity. An init
  container builds it with `render-storage-config` into a memory-backed
  volume; it is stored nowhere else.

The auth service derives its policy the same way, in an init container
running `render-gateway-policy`. The policy holds digests, never a
credential. Workloads that do not re-read their files are restarted by the
operator when one of their secrets changes.

### Certificates

cert-manager issues them from Vault's PKI `sign` endpoint. The private key
is generated in the cluster; Vault receives a signing request and never
holds a key. The gateway watches its certificate's Secret through Traefik's
own resources, so a renewed certificate is served without a restart. For
that the gateway may list Secrets in its namespace, which is why it has a
namespace that holds nothing but its certificate.

### Rotating a secret

Rotate it in Vault (`rotate-secret`). Within about a minute the operator
synchronizes it and restarts what needs restarting. For a query credential
also run the playbook with `-e k8s_platform_update_grafana_secrets=true`,
because Grafana cannot notice a changed data source password by itself.

## Network isolation

Both platform namespaces deny all pod traffic by default. Each component
gets one policy whose every entry is a rule of `config/network.yaml`; the
rule IDs are in the policy's `nighthawk.io/contract-rules` annotation. In
short:

| Component | Accepts from | May open |
| --- | --- | --- |
| Gateway | anywhere on the external entry point; collectors on the in-cluster one and the metrics port; Grafana | auth service, the four backends, Grafana, the cluster API |
| Auth service | gateway, collector | nothing |
| Mimir, Loki, Tempo, Pyroscope | gateway, collector, itself | object storage, itself (Pyroscope also the cluster API) |
| Object storage | the four backends, itself | itself |
| Grafana | gateway, provisioning job | the gateway |
| Collectors | any pod, on the OTLP and profile ports | the gateway, the scrape targets in the contract, the cluster API, kubelets |

Every component may resolve names. Nothing else leaves the cluster.

A flow that is not in the contract is refused. In particular the cluster
collector scrapes only the targets the contract lists; to scrape a
workload in another namespace, add a rule to `config/network.yaml`, render,
and deploy.

Standard NetworkPolicy resources are used, so any enforcing plugin applies
them. Two caveats for the k3s cluster of phase 5, neither verified:

- The cluster API and the kubelets are not pods, so the policies name them
  by address. Cilium does not match node addresses with an address block
  unless told to; set `policyCIDRMatchMode: nodes` in its values before
  relying on these policies.
- The playbook reads the API server's real addresses and port from the
  cluster instead of assuming the contract's port.

## Teardown

```sh
.venv/bin/ansible-playbook -i inventories/my-cluster playbooks/k8s-teardown.yml
```

This removes the releases and keeps every volume, so a later deployment
finds its earlier telemetry and Grafana's state. Network policies stay.
Secrets stay in Vault; nothing here touches Vault. To delete the volumes
too, confirm with the cluster's name:

```sh
.venv/bin/ansible-playbook -i inventories/my-cluster playbooks/k8s-teardown.yml \
    -e k8s_platform_purge=true -e k8s_platform_purge_confirm=<nighthawk_cluster_name>
```

A purge without the matching name deletes nothing.

## Tests

```sh
python -m unittest tests.test_kube tests.test_helm_charts        # offline
NIGHTHAWK_CHART_TESTS=1 python -m unittest tests.test_helm_charts  # pulls charts and schemas
NIGHTHAWK_K8S_E2E=1 python -m unittest tests.k8s.test_cluster      # creates a local cluster
```

The chart tests template every release of the development profile, check
each upstream chart against its locked digest, validate the platform
chart's manifests against the Kubernetes schema, and fail on any container
image that is not in the matrix by digest.

The acceptance suite creates a single-node minikube cluster under rootless
Podman:

```sh
minikube config set rootless true
minikube start -p nighthawk-e2e --driver=podman --container-runtime=containerd --cni=calico --cpus=4 --memory=9g
podman update --pids-limit -1 nighthawk-e2e
```

It runs a dev-mode Vault inside the cluster as a fixture
(`tests/k8s/vault-dev.yaml`), deploys with the two playbooks, and deletes
the cluster afterwards. `NIGHTHAWK_K8S_KEEP=1` keeps it;
`NIGHTHAWK_K8S_REUSE=1` uses one that is already running.

## Observed results

Run on 2026-10-10 on Fedora 43, rootless Podman 5.8.4, minikube 1.37.0
(Kubernetes 1.34.0, containerd, Calico 3.30.3), 4 CPUs, 16 GB RAM, against
`tests/k8s/platform.yaml` (two customers, two datastreams each).

All 16 cases of the acceptance suite passed, the first 15 in one run that
started from a new cluster. The whole cluster, platform and add-ons
together, used about 2.6 GB of memory when idle. The development profile
sets no resource requests or limits yet, so that figure is a measurement,
not a budget.

| Area | Observed |
| --- | --- |
| Deployment | Both playbooks installed everything in order. A second run of each changed nothing, restarted no pod, and the provisioning job printed `no changes` |
| Signals | Metrics, logs, traces, and profiles sent by a pod in another namespace to the cluster collector were returned by queries through the gateway. A Loki query through Grafana's provisioned data source returned 200 |
| Collectors | One series per node-scoped job, so the node and cluster collectors do not overlap. No workload is privileged |
| Tenant boundaries | Each datastream read only what was written to it. A spoofed tenant header, an ingestion credential used to query, a query credential used to write, and a missing or wrong credential were refused |
| Entry points | Ingestion on the external entry point was accepted with the credential's client certificate and refused without one; the in-cluster entry point accepted the certificate-free credential |
| Network isolation | A pod without a policy reached nothing. A pod labelled as the gateway reached the auth service and a backend, not object storage. A pod labelled as a backend reached object storage, not another backend, not Vault, not the internet. The only service with an external address was the gateway, on 443 |
| Secrets | No Secret or ConfigMap held a Vault token. A workload's identity was denied another workload's secret by Vault. A rotated ingestion credential was accepted and the old one refused without any operator action |
| Certificates | The gateway's certificate carried both host names, was signed by the authority in Vault, and matched a key that exists only in the cluster |
| Overrides | A changed limit was loaded by Mimir without any backend pod restarting |
| Data | Telemetry survived a deleted backend pod, and a teardown followed by a new deployment. A purge without the cluster's name deleted nothing |
| Locked charts | With one chart's digest altered in the rendered release list, the add-ons playbook stopped at that chart, said nothing was installed from it, and changed no release |

## What the test cluster taught

Three limits of the test environment shaped the implementation. Each is
also a limit some real hosts have.

- **inotify.** The host had no inotify instance left. The gateway's file
  watcher and the collector's credential watcher could not start. The
  gateway now reloads through a rollout when its routes change, and
  collectors poll the credential file every 30 seconds, under Compose too.
- **Processes.** Podman caps a container at 2048 processes, and the test
  cluster is one container. The storage readiness probe also left a defunct
  helper behind on every call; it is now a network probe.
- **Policy engine.** Calico under rootless Podman cannot program many
  policy rules in one transaction. Policies are applied one resource at a
  time, with a pause the inventory sets
  (`k8s_platform_policy_pause_seconds`, 0 by default).

## Verification limits

| Not verified | Why |
| --- | --- |
| The production profile | Not implemented |
| k3s, Cilium, Longhorn, MetalLB on real nodes | The test cluster is minikube with Calico and its own storage class. MetalLB ran, but its address was only reachable inside the test network |
| A Vault outside the cluster, with TLS | The fixture is a dev-mode Vault inside the cluster |
| Certificate renewal at expiry | Certificates are valid for 90 days; renewal was not waited for |
| High availability, node loss | One node |
| `arm64` | The test machine is `amd64` |
