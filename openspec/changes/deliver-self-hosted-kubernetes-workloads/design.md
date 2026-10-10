# Design

## Context

See `proposal.md` for motivation and scope. What exists and shapes this
design:

- `k3s-cluster.yml` leaves a Ready k3s cluster with Cilium. Its preflight
  already checked the address pool, storage disks, kernel modules, and
  packages that MetalLB and Longhorn need.
- The compatibility matrix already pins MetalLB, Longhorn, Traefik,
  cert-manager, Strimzi, and the Grafana charts. Mimir's chart exists only
  as a weekly pre-release that packages a different Mimir build; the matrix
  records that as an exception.
- The renderer already produces, from the platform document: the Traefik
  static and dynamic configuration (file provider), the per-tenant runtime
  overrides, the Grafana desired state, the collector configurations for
  the `k8s-node` and `k8s-cluster` profiles, and monolithic backend
  configuration for Docker. All of it runs end to end under Compose.
- Secrets live in Vault. Materialized files use the layout
  `kv/<path>/<key>`. The auth service's policy is derived from those files
  and holds no recoverable secret.
- The Ansible conventions of phase 5: one rendered inputs file, no contract
  value spelled out in a role, no secret in an inventory, check mode,
  idempotence.
- Decisions taken with the user for this change: self-hosted only; secrets
  through the Vault Secrets Operator; releases driven by an Ansible
  playbook; development backends hybrid (official charts in single-binary
  mode for Loki, Pyroscope, and Grafana; monolithic Mimir and Tempo from
  the platform chart); verification by render checks plus a real
  installation of the development profile on a local cluster.
- This machine has 4 CPUs, 16 GB, rootless Podman, and minikube 1.37.0.
  There is no multi-node cluster.

## Goals / Non-Goals

**Goals:**

- Reuse what Compose already proved (gateway configuration, auth policy,
  overrides, Grafana provisioning, collector configuration, monolithic
  backend configuration) instead of writing Kubernetes-specific variants.
- One code path from the platform document to a running cluster, for both
  profiles, with the profile visible in every rendered file.
- A development profile that really starts here, and an honest record of
  what in the production profile was only rendered.

**Non-Goals:**

- AWS/EKS, dashboards, alerting, backup and restore, CI.
- Installing or operating Vault. The acceptance suite runs a dev-mode Vault
  as a test fixture only.
- Proving high availability. Without several nodes it cannot be observed.
- A GitOps controller. The playbook is the only orchestrator.

## Decisions

### D1. Helm runs on the control machine, driven by Ansible

Two roles, `k8s_addons` and `k8s_platform`, run on the control machine
against a kubeconfig with the `kubernetes.core` collection. The same roles
work for k3s, the local test cluster, and later EKS. The kubeconfig is the
one `k3s-cluster.yml` can already fetch.

*Alternative:* k3s `HelmChart` manifests, as the Cilium role uses. Rejected
for everything after the network plugin: it only works on k3s, gives no
ordered health gate across releases, and cannot verify a chart digest.

### D2. Charts are locked by content digest

The matrix records, per chart, its repository, version, and the SHA-256 of
the packaged chart. The role pulls the chart into a cache on the control
machine, compares the digest, and installs from the verified file.
`helm dependency` lockfiles are used only inside the platform chart, which
has no dependencies. Helm itself is fetched and checksum-verified by
`fetch-tools`, like Terraform.

### D3. Rendered layout

For a `self-hosted-k8s` document `render-contracts` adds:

```text
kubernetes/
  releases.yaml          ordered list: name, namespace, chart, version,
                         values file, health wait, profile
  values/<release>.yaml  one per release
  manifests/network-policies.yaml
  platform/              files the platform chart mounts: gateway
                         configuration, overrides, collector configuration,
                         Grafana desired state, monolithic backend
                         configuration (development)
vault/kubernetes-auth.json   the role and policies Vault must have
```

The profile is the platform document's `profile`. The role refuses to
deploy when it differs from the inventory's `k3s_cluster_shape`. Ansible
reads `releases.yaml` and never spells out a release, a chart version, or
a port, in line with the phase 5 repository check.

### D4. The gateway keeps its file-provider configuration

Traefik is installed from its official chart, but routes, middlewares, and
TLS options come from the configuration the renderer already produces,
mounted from a ConfigMap. Upstream addresses are the `gateway.upstreams` of
the platform document, which for Kubernetes are service DNS names.

*Alternative:* IngressRoute and Middleware custom resources. Rejected: it
would be a second, untested expression of the tenant routing that the
gateway specification and the end-to-end suite already cover.

### D5. A new in-cluster entry point

Collectors inside the cluster reach the gateway through its cluster
service. The network contract gains a `cluster-gateway` rule (scope
`private`), selected in the self-hosted document. The external entry point
keeps requiring client certificates; the in-cluster one authenticates by
credential, as the loopback entry point does under Compose. The auth
service already distinguishes entry points by scope.

### D6. Secrets: Vault Secrets Operator with Kubernetes authentication

- The platform document gains `vault.kubernetes_auth` with the mount name
  and, optionally, the address the cluster uses for Vault when it differs
  from the one the command-line tool uses.
- Each workload has its own service account, a `VaultAuth` bound to it, and
  `VaultStaticSecret` resources for the KV paths it reads. A synchronized
  Secret has one key per KV key, so mounting it at
  `/run/nighthawk/secrets/kv/<path>/` reproduces the materialized layout
  the existing commands read.
- `vault/kubernetes-auth.json` lists the role and one read-only policy per
  workload. `bootstrap-dev-vault` applies it; `doctor --cluster` compares
  a real Vault with it.
- Rotation: the operator re-reads on an interval that the values state,
  and its rollout-restart targets restart workloads that do not re-read
  files. The auth service re-derives its policy in an init step and on
  reload.

Vault's Kubernetes authentication validates service account tokens by
calling the cluster API, so Vault must reach the API server. The operator's
address for Vault is added to the administration allowlist of the firewall
role.

*Alternative:* JWT authentication with the cluster's issuer keys, which
needs no call back from Vault. Kept as a documented option, not built.
*Alternative:* the command-line tool writing Kubernetes Secrets. Not
chosen by the user; rotation would need a re-run.

### D7. Derived secrets are shaped by the operator, with a stated fallback

Three consumers need a shape that differs from what Vault stores: backend
S3 credentials as environment variables (stored as one JSON value), the
SeaweedFS identity file (built from all storage identities), and the
Grafana administrator password. The Vault Secrets Operator's
transformation templates produce these. If the pinned operator cannot
express one of them, that one Secret is produced by a Job in the platform
chart that runs the platform image with RBAC limited to writing that named
Secret. The first task of the secrets group establishes which applies, on
the local cluster, before anything depends on it.

### D8. Certificates: cert-manager with a Vault issuer

A namespaced `Issuer` per platform namespace uses Vault's PKI `sign`
endpoint with the same server and client roles the platform already
renders, authenticating with a service account. `Certificate` resources
cover the gateway (gateway and Grafana host names), object storage, and
client certificates for collectors whose credential declares an identity.
Keys are generated in the cluster. The authority's certificate comes from
the issued Secret's `ca.crt`.

### D9. The auth service derives its policy in the cluster

The auth service pod mounts the synchronized credential Secrets in the
materialized layout and the platform document from a ConfigMap. An init
container runs `render-gateway-policy`; the main container runs
`serve-authz`. Both use the platform image. A changed credential restarts
the pod through the operator; with two replicas in production, a rolling
restart keeps the gateway answering.

### D10. Backends per profile

| Backend | Development | Production |
| --- | --- | --- |
| Loki | official chart, single binary | official chart, distributed |
| Pyroscope | official chart, single binary | official chart, distributed |
| Grafana | official chart, one replica, local database | official chart, replicas with shared PostgreSQL |
| Mimir | platform chart, monolithic | official chart, distributed, Kafka |
| Tempo | platform chart, monolithic | official chart, distributed, Kafka |

Monolithic Mimir and Tempo use the configuration `backends.py` renders for
Docker, with only addresses and storage class differing. `backends.py`
gains a distributed mode for production and refuses a pinned version it was
not reviewed for.

Production Mimir uses the pinned weekly chart with the image set to the
pinned Mimir release by digest. That combination is rendered and
validated, not started; see Risks.

### D11. Object storage and database

Development: SeaweedFS from its chart as one master, one volume server,
one filer with an embedded store, and one S3 frontend, on a local volume.
Production: three masters, volume servers on the storage nodes with
Longhorn volumes, filers backed by PostgreSQL, and two S3 frontends.

Production PostgreSQL is one CloudNativePG cluster of three instances with
two databases and two roles, for Grafana and for the SeaweedFS filer, as
the architecture document already states. CloudNativePG is added to the
matrix.

Buckets and identities are created by the same initialization logic as
under Compose, run as a Job.

### D12. Kafka

Production only: one Strimzi-managed KRaft cluster of three brokers on
distinct nodes, with a topic and a `KafkaUser` per backend that needs an
ingest log, and ACLs limited to that topic. Credentials are Kubernetes
Secrets Strimzi creates; they do not pass through Vault, and the document
says so.

### D13. NetworkPolicies are generated from the contract

`config/network.yaml` gains the in-cluster flows it does not list yet
(name resolution, Vault, the cluster API for operators, Kafka, PostgreSQL,
ring and gossip ports per backend). A new renderer maps each in-cluster
rule to a standard `NetworkPolicy` using the contract's source and
destination names as workload labels, plus one default-deny policy per
namespace. Standard policies are used, not Cilium-specific ones, so the
local test cluster's plugin can enforce them too. A unit test checks the
mapping in both directions: every allowance has a rule, every in-cluster
rule has an allowance.

### D14. Images

Every image is set by digest in the rendered values. A test templates
every release of both profiles, collects the image references, and fails
on one that is not in the matrix with a digest. The platform's own image
is supplied by the operator as a repository and digest in the inventory;
the acceptance suite builds it and loads it into the test cluster.

### D15. Autoscaling and budgets

The matrix records which components are horizontally scalable. Production
enables a HorizontalPodAutoscaler only for those (queriers, query
frontends, distributors, gateway, Grafana with the shared database). k3s
ships a metrics server, which is left enabled. Resource budgets for a
small, medium, and large production installation are documented as
requests and limits per component; development has one stated budget that
must fit this machine.

### D16. Local acceptance cluster

An opt-in suite (`NIGHTHAWK_K8S_E2E=1`) creates a single-node minikube
profile on the Podman driver with a NetworkPolicy-enforcing plugin, runs a
dev-mode Vault inside the cluster as a fixture, bootstraps it, renders the
development profile, runs both playbooks, and then checks: four-signal
round trip, tenant isolation and spoofed headers, the certificate
requirement on the external entry point, denied connections between
workloads, a rotated credential reaching the gateway, a second playbook
run changing nothing, and teardown keeping data. It deletes the profile
afterwards.

Longhorn and MetalLB are exercised only as far as one node allows: MetalLB
with a pool in the test network; Longhorn rendered only.

If no local cluster can be started on this machine, implementation pauses
at that task for a decision instead of reporting the profile as verified.

### D17. Commands and playbooks

| Item | Purpose |
| --- | --- |
| `render-contracts` | also writes `kubernetes/` and `vault/kubernetes-auth.json` for a self-hosted document |
| `fetch-tools` | also fetches Helm and the manifest validator, by checksum |
| `bootstrap-dev-vault` | also applies the cluster authentication requirements |
| `doctor --cluster` | compares Vault with the rendered cluster requirements |
| `playbooks/k8s-addons.yml` | add-ons and operators |
| `playbooks/k8s-platform.yml` | platform releases, health gates, Grafana provisioning |
| `playbooks/k8s-teardown.yml` | removes releases; purge needs the cluster's name |

## Risks / Trade-offs

- [The local cluster may not start under rootless Podman] → D16 names the
  pause point. Nothing is claimed as started that was not.
- [The Vault Secrets Operator's templates may not cover a derived secret]
  → D7's fallback, decided by the first task that needs it.
- [Mimir's weekly chart with the pinned release image may not template or
  may pass flags the release does not know] → rendered and validated only,
  recorded as not started in the matrix, and named in the document as the
  first thing to check on a real cluster.
- [The production profile is never installed] → every production claim in
  the document is labelled rendered-only; the matrix keeps
  `runtime_verified: false`.
- [Pinning every add-on image by digest is a large, drift-prone list] →
  the template test fails on any unlisted image, so drift is found by a
  test, not in a cluster.
- [The cluster now reads Vault] → read-only policies per workload; no
  write path; the rule "only the tool writes" is kept and stated.
- [Vault must reach the cluster API for authentication] → documented
  prerequisite with the firewall allowlist entry; JWT authentication is
  the documented alternative.
- [16 GB may not fit the development profile next to the desktop] → the
  development budget is measured and stated; the suite reports the
  cluster's requested memory.
- [Development and production Mimir and Tempo run different deployment
  forms] → the user's choice; the document states that development does
  not exercise the distributed charts.

## Migration Plan

Nothing deployed changes. The Docker profile, the phase 5 playbooks, and
existing rendered output for Docker documents are unaffected. A cluster
built with `k3s-cluster.yml` is extended by running the two new playbooks.
Rollback is the teardown playbook, which keeps data.

## Open Questions

- Which NetworkPolicy plugin the local test cluster uses is decided by
  what starts under rootless Podman. It does not change the policies,
  which are standard resources.
- The interval at which rotated secrets are re-read is set during
  implementation from the operator's defaults and stated in the document.
