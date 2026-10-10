# Proposal

## Why

Phase 5 leaves a k3s cluster with Cilium and nothing on it. The self-hosted
profile is the second of the three environments in the plan, and none of
the platform runs on Kubernetes yet: there are no chart values, no release
orchestration, no way for a workload to obtain a secret or a certificate,
and no network policy. Plan todo 6 covers this for self-hosted Kubernetes
and for AWS.

This change delivers the self-hosted half: the remaining cluster add-ons
and every platform workload, for the development and the production
profile. AWS/EKS follows in its own change and reuses what is built here.

## What Changes

**Cluster add-ons**

- Install MetalLB, Longhorn, Traefik, and cert-manager, each at the version
  the compatibility matrix pins, after the checks phase 5 already performs
  for them. Production also installs the Strimzi and PostgreSQL operators.
- Install the Vault Secrets Operator.

**Secrets and certificates inside the cluster**

- Resolve the decision phase 4 deferred: workloads receive secrets through
  the Vault Secrets Operator, which authenticates to the operator's Vault
  with the cluster's own service account identity. No Vault token is stored
  in the cluster, in a chart value, or in the inventory.
- Certificates are issued by cert-manager from Vault's PKI. Private keys
  are generated in the cluster and only a signing request reaches Vault, as
  on every other path.
- The platform renders what Vault must allow for this (an authentication
  role and read-only policies per workload) next to the policy and PKI
  roles it already renders. The operator applies them; the development
  bootstrap applies them to a dev-mode Vault.
- **BREAKING** for the stated rule that only the command-line tool talks to
  Vault: on Kubernetes the cluster reads Vault too, with read-only,
  per-workload access. The tool remains the only writer.

**Rendering**

- `render-contracts` produces, for a `self-hosted-k8s` document, the Helm
  values for every release and the NetworkPolicy manifests, for the
  development or the production profile. Output is deterministic and holds
  no secret.
- Backend configuration is rendered for Kubernetes (today it is rendered
  for Docker only).

**Workloads**

- A small platform chart holds what no official chart provides: the auth
  service, gateway routes, secret and certificate requests, NetworkPolicies,
  Grafana provisioning, and the monolithic Mimir and Tempo used in
  development.
- **Development profile** (one node, not highly available, no Kafka):
  Loki, Pyroscope, and Grafana from their official charts in single-binary
  mode; Mimir and Tempo monolithic from the platform chart with the
  configuration already verified under Compose; SeaweedFS on a local
  volume.
- **Production profile**: the official distributed charts for all four
  backends, Kafka through Strimzi where the backend versions need it,
  replicated SeaweedFS, a highly available PostgreSQL shared by Grafana and
  the SeaweedFS filer under separate databases and credentials, with
  replicas spread across nodes, disruption budgets, resource requests and
  limits, and autoscaling only where the component is verified to scale
  horizontally.
- Collectors: the node and cluster Alloy profiles that already render,
  deployed as a DaemonSet and a Deployment.
- Default-deny NetworkPolicies in every platform namespace, with allowances
  generated from the network contract. Backends and object storage are
  reachable only from inside the cluster; the gateway is the only entry.

**Orchestration**

- An Ansible playbook installs the add-ons and the releases in dependency
  order from the control machine, waits for each to be healthy, provisions
  Grafana, and is idempotent. A teardown playbook removes the releases and
  keeps volumes and secrets unless a purge is confirmed.
- Helm becomes a pinned, checksum-verified tool, fetched like Terraform.

**Verification**

- Lint, template, and schema-validate every release for both profiles, and
  test the rendered NetworkPolicies against the network contract.
- Install the development profile on a local single-node cluster and run
  the four-signal round trip and the tenant isolation checks against it.
- The production profile is rendered and validated but not installed:
  there is no multi-node cluster. Longhorn and replicated storage are not
  exercised. This is recorded per component in the matrix and documents.

**Not in this change**

- AWS/EKS values, IRSA wiring, the AWS load balancer controller, Cluster
  Autoscaler, and the AWS example document.
- Dashboards and alerting (todo 7), remote-cluster collector manifests
  (todo 8), Makefile and CI (todo 9), backup and restore (todo 10).

## Capabilities

### New Capabilities

- `kubernetes-cluster-addons`: the pinned add-ons and operators a
  self-hosted cluster needs before the platform, and their installation.
- `kubernetes-secret-delivery`: how workloads obtain secrets and
  certificates from Vault without a stored Vault credential, and how
  rotation reaches them.
- `kubernetes-workload-rendering`: rendered Helm values and manifests for
  the development and production profiles.
- `kubernetes-platform-deployment`: the workloads themselves, their
  ordering, health, idempotent re-deployment, the two profiles'
  availability requirements, and teardown.
- `kubernetes-network-isolation`: default-deny NetworkPolicies generated
  from the network contract, and private backends.

### Modified Capabilities

- `compatibility-matrix`: new requirement for pinned Kubernetes tooling,
  locked charts, and image digests for Kubernetes workloads.
- `secrets-workflow`: new requirement for the rendered Vault access a
  cluster needs; the development bootstrap applies it.
- `backend-static-configuration`: new requirement that backend
  configuration is rendered for the self-hosted Kubernetes deployment in
  both modes.

## Impact

- **New trees**: `helm/` (platform chart, values per release and profile,
  chart lockfiles) and Ansible roles and playbooks for add-ons and
  releases.
- **Python**: the renderer (`backends.py`, a new module for Kubernetes
  values and NetworkPolicies, `vault.py` for the in-cluster access
  requirements, `tools.py` for Helm), `config.py` and the platform schema
  (Vault Kubernetes authentication settings), `__main__.py`.
- **Config**: `config/versions.yaml` and its schema (Helm, the Vault
  Secrets Operator, the PostgreSQL operator, a manifest validator, the
  Ansible Kubernetes collection, chart digests, image digests),
  `config/network.yaml` (in-cluster flows not yet listed),
  `config/self-hosted.example.yaml`.
- **Ansible**: `ansible/requirements.yml` and `requirements-dev.txt` gain
  the Kubernetes collection and its Python client.
- **Tests**: unit tests for the renderers, chart tests, policy tests, and a
  local-cluster acceptance suite that is opt-in like the Compose one.
- **Documents**: a new `docs/09-kubernetes.md`; updates to `docs/01`,
  `docs/02`, `docs/03`, `docs/08`, `Plan.md`, and the README.
- **Dependencies**: Helm, a Kubernetes manifest validator, and a local
  cluster tool for the acceptance suite. Charts and images are pulled from
  their upstream registries during tests.
- **Vault**: an operator's Vault must enable Kubernetes authentication for
  the cluster and be reachable from it.
- **Existing behaviour**: the Docker profile and the phase 5 playbooks are
  unchanged. `k3s-cluster.yml` still ends at a Ready cluster with Cilium.
