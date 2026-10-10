# NightHawk Grafana observability platform

## Objective and current state

> **Amendment:** This section and "Access and execution boundaries" describe
> the situation before any implementation: an empty repository, a Windows
> host, and no verified edit access. They are kept as the plan's starting
> point, not as the present state. Todos 1 to 5 are implemented. Todo 6 is
> partly implemented: the development profile of self-hosted Kubernetes runs
> on a local test cluster; its production profile and AWS/EKS are not built.
> Todos 7 to 10 are not started. Accepted behaviour is in `openspec/specs/`, the history
> in `openspec/changes/archive/`, and what was and was not verified in
> `docs/01-architecture.md`, `docs/07-docker-compose.md`, and
> `docs/08-ansible.md`, and `docs/09-kubernetes.md`. In short: the Docker Compose profile runs end to end
> on one machine under rootless Podman; the AWS infrastructure is validated
> as plans only and nothing has been applied; the Ansible automation is
> tested role by role in containers and has never run on a real host. The
> amendments below record, per section, where the implementation differs
> from the original text.

Build the standalone LGTM+ platform at the NightHawk repository root, implementing the attached brief across Docker Compose, self-hosted Kubernetes, and AWS EKS. This is an implementation plan only; no repository files have been changed.

The repository contains only a two-line README and has a clean working tree. There is no existing application or deployment to migrate. The branch has been renamed to `wp8fyqi-cariad-grafana-observability-platform`.

On the latest retry, the user explicitly enabled read-only worktree inspection and session-plan updates. Worktree commands and session-plan reads succeed. Repository editing is not yet verified: the previous implementation attempt could run `git status`, but its first `apply_patch` was denied. The repository still contains only the README and has no pending changes.

The Windows host has Python and WSL with an Ubuntu distribution. A read-only check inside Ubuntu confirmed Python 3 is available, but Docker, Terraform, Ansible, ansible-lint, Helm, Make, kubectl, SOPS, and age are unavailable. Use Ubuntu WSL for Linux-compatible validation with isolated, pinned tools after implementation approval. Cloud credentials, DNS ownership, target machines, and a container daemon have not been verified.

## Access and execution boundaries

- Current planning access: read worktree files, run read-only worktree/WSL commands, and read/update this session plan and its task checklist.
- After approval of this revised plan: the user authorizes repository edits, local validation, and installation of missing validation tools in an isolated project/session environment. Prefer a Python virtual environment and a local binary directory; do not modify global PATH or install system-wide packages.
- Recommended handoff: **interactive implementation**, not autopilot/fleet. Previous mode transitions were followed by permission denials; the root cause has not been established. Do not assume that a conversational approval changes the runtime's permission controls.
- Keep implementation paused during this plan retry. After explicit implementation approval, make the first repository patch small: create `docs/01-architecture.md`, then inspect the file and diff. This is useful implementation work, not a throwaway permission-probe file. Only after that edit succeeds should module skeletons, dependency setup, or parallel work begin.
- Install dependencies only after adding the relevant dependency manifest or encountering a missing-tool failure in the selected validation command. Pin versions and verify downloaded tool checksums or signatures.
- System-wide changes, Docker daemon setup, remote-host changes, AWS deployments, and billable resources require separate permission. Installing a Docker client is not evidence that a usable container runtime exists.
- Begin with architecture and contracts, then implement independent workstreams against frozen interfaces. Validate and integrate each increment before proceeding; do not produce unconnected component scaffolds and call the platform complete.
- If permissions are denied again, report the specific operation and request access before retrying. Do not bypass a denial through a different tool or child session.
- If the user is unavailable when access is requested, preserve the blocked state. Do not report implementation as complete or repeat a mode transition that has not restored the missing permission.
- At completion, distinguish verified local checks, CI definitions that have not run, and environment acceptance checks blocked on separately authorized infrastructure.

## Confirmed decisions

- Repository scope: standalone platform at the repository root.
- Self-hosted infrastructure: existing Linux nodes, bootstrapped with k3s, Cilium, MetalLB, and Longhorn. VM creation through Proxmox, vSphere, or another provider is out of scope.
- Authentication: TLS gateway with per-tenant credentials and mTLS for remote collectors; raw backend ingestion and query services remain private.
- Retention: customized by tenant and datastream, with explicit values for metrics, logs, traces, and profiles. There is no implicit production retention default.
- Isolation: each tenant/datastream pair maps to a unique backend tenant ID, allowing backend tenant-level retention to implement independent datastream policies.
- Production architecture: include Kafka-compatible infrastructure when required by supported backend versions. Local Compose remains monolithic and Kafka-free.

## Architecture and implementation conventions

### Deployment profiles

1. **Docker:** monolithic Mimir, Loki, Tempo, and Pyroscope; Alloy; Grafana; MinIO; and an authenticated TLS gateway. Named persistent volumes, private internal networks, readiness checks, restart policies, and automated bucket provisioning. The local quickstart uses generated credentials and a local CA; it never exposes unauthenticated backend ports. Localhost-only access is the default. Production single-node use requires explicit public/private endpoint configuration and is documented as non-HA.

   > **Amendment:** Read MinIO here as SeaweedFS, the approved local object
   > storage; see the Storage amendments section of `docs/01-architecture.md`.
   > Read "generated credentials and a local CA" as credentials generated
   > into Vault and certificates signed by Vault's PKI. The local quickstart
   > (`quickstart-docker`) publishes the gateway on loopback only and cannot
   > publish anything else. Production single-node use is implemented as a
   > deployment to another machine: `render-docker-deployment` renders it on
   > a control machine, the `docker-host.yml` playbook copies and starts it,
   > and `docker-compose/docker-compose.external.yaml` publishes the
   > certificate-requiring entry point on an address the operator must state.
   > It requires a `profile: production` document and is documented as
   > non-HA in `docs/08-ansible.md`.
2. **Self-hosted Kubernetes:** Ansible installs k3s with its bundled Flannel, Traefik, and ServiceLB disabled before installing Cilium, MetalLB, Longhorn, a pinned Traefik release, and cert-manager. Support a single-node development profile and a separately sized multi-node production profile. Production stateful replicas and storage must span distinct nodes; preflight rejects impossible replica/disk/IP-pool configurations.

   > **Amendment:** Todo 5 installs k3s with those three add-ons disabled and
   > installs Cilium, through a `HelmChart` manifest that k3s applies itself.
   > MetalLB, Longhorn, Traefik, and cert-manager are installed with the
   > workloads in todo 6. The preflight exists as `check-cluster-layout`: it
   > rejects impossible server counts, storage node counts, disks, MTUs,
   > overlapping ranges, and address pools before any node is changed. The
   > two shapes are `development` (exactly one node) and `production` (an odd
   > number of at least three servers, at least three storage nodes).
3. **AWS:** Terraform provisions a VPC across availability zones, EKS managed node groups, EBS CSI, S3, KMS/IAM integration, private service connectivity, and remote state prerequisites. Use on-demand capacity for stateful workloads and optional spot capacity for suitable stateless workloads. Traefik and cert-manager provide a consistent TLS ingress model, backed by an AWS load balancer and automated DNS. Use IRSA rather than static AWS credentials.

Use official Grafana charts, with a small platform chart for shared policies, gateways, provisioning resources, and integration fixtures. Use a version-pinned Strimzi-managed Kafka deployment on Kubernetes where selected backend architectures require it, with separate topics and access permissions per consumer backend. Kafka replication, disks, failure domains, monitoring, and recovery are part of the production profile, not an undocumented dependency.

### Shared configuration contracts

- Add `config/versions.yaml`, `config/platform.schema.json`, `config/tenants.example.yaml`, `config/network.yaml`, and environment examples.
- Use a small typed, tested configuration renderer/orchestrator to produce environment-specific backend settings, gateway routes, tenant overrides, Grafana provisioning, Ansible inputs, and Terraform inputs where needed. Deterministic output, strict validation, actionable errors, and no silent fallback values.

  > **Amendment:** The files that exist are `config/versions.yaml`,
  > `config/network.yaml`, and `config/platform.schema.json`, each of the
  > first two with its own schema, and two example platform documents:
  > `config/tenants.example.yaml` (Docker) and
  > `config/self-hosted.example.yaml` (self-hosted Kubernetes). There is no
  > AWS example document yet. The renderer is `python -m nighthawk`;
  > `render-contracts` writes the backend settings, gateway configuration,
  > tenant overrides, Grafana desired state, what the platform needs from
  > Vault, and `ansible/nighthawk.yml`, the one variables file every
  > playbook loads. Ansible reads nothing else: no role spells out a port,
  > version, or checksum, and a repository test enforces that.
- Keep non-secret templates and examples in Git. Generated decrypted configuration stays in explicitly ignored directories with restrictive permissions and cleanup.
- Each tenant/datastream entry defines a stable backend ID, credential references, enabled signals, explicit retention for each enabled signal, ingestion/query limits, and approved collection/redaction policy.
- Treat backend ID changes as migrations, not renames. Detect duplicate IDs, delimiter ambiguity, unsupported durations, missing secret references, and credential mappings spanning unauthorized tenants.
- The network contract owns ports, protocols, purpose, direction, and scope. Generate firewall inputs and the documentation port table from it; CI checks consumers against the contract.

  > **Amendment:** Since todo 5 the contract also holds SSH administration
  > and the node-to-node flows of a k3s cluster (API server, kubelet, etcd,
  > Cilium overlay and health), 27 rules in all. The firewall inputs are
  > generated: the rendered Ansible inputs carry the inbound rules per host
  > role, and the `firewall` role turns them into UFW or firewalld rules.
  > The checks against the contract are local tests; no CI runs them yet
  > (todo 9).
- Retain recognizable `terraform/`, `ansible/`, `helm/values/`, `docker-compose/`, `alloy-configs/`, `grafana/`, and `docs/` paths from the brief. Do not add fictitious Docker Helm values or empty Terraform modules solely to match an illustrative tree.

### Security and tenant boundaries

- Use SOPS + age as the baseline secret workflow for all environments, including automation for generation, encryption, decryption, rotation, and restricted runtime materialization. Production requires operator-provided age recipients and configured trust/DNS inputs; these are prerequisites, not click-ops.

  > **Amendment:** The SOPS + age decision above was superseded after phase 4.
  > Secrets are stored in HashiCorp Vault's key-value engine, and gateway,
  > storage, and collector certificates are signed by Vault's PKI engine, in
  > every environment. The Vault server is provided by the operator; the
  > platform renders the policy and PKI roles it needs and never deploys,
  > unseals, or holds a long-lived credential for it. `sops`, `age`, encrypted
  > files under `secrets/`, age recipients, and the locally generated
  > certificate authority are removed, and this also applies to the SOPS and
  > age pins listed under todo 1. Production prerequisites become a TLS Vault
  > address that is not loopback and a non-root credential. See
  > `docs/01-architecture.md`'s Secrets amendment for the rationale, the
  > licence note, and what is deferred to phase 6.

- Implement a shared gateway authentication policy that binds credentials and remote collector certificate identity to permitted backend tenant IDs. Reject unknown, missing, conflicting, or spoofed tenant headers rather than trusting `X-Scope-OrgID` from clients.
- Separate ingestion-only collector credentials from query/provisioning credentials. Validate both HTTP and gRPC gateway behavior; support OTLP HTTP as a documented collector transport.
- Provision a Grafana organization per customer tenant, with datastream-specific data sources and organization-scoped dashboards, alerts, and credentials. Disable anonymous access. Verify that non-admin users cannot query another customer's sources or change trusted tenant mappings.
- Use Kubernetes default-deny NetworkPolicies with explicit DNS, gateway, object storage, Kafka, ring/gossip, control-plane, and exporter allowances. Keep object stores, distributor ports, and query endpoints private.
- Use backend TLS/mTLS where the chosen versions support it. Document actual plaintext exceptions and their network restrictions rather than claiming universal mTLS.
- Separate minimally privileged Alloy workloads from optional privileged/eBPF profiling. Instrumented SDKs, kube-state-metrics, and device exporters remain necessary where Alloy cannot obtain the signal itself.
- Apply collection-time PII redaction to logs, OTLP attributes, resource attributes, and metric labels. Drop sensitive data by default; use only verified supported transformations, with documented limits of hashing and profiling payload sanitization.
- Implement vulnerability scanning, secret scanning, SBOM generation, and Cosign signing/verification. Verify upstream signatures against explicit trusted identities where available; document provenance review and attestations for unsigned upstream artifacts rather than silently skipping verification.

### Retention and storage correctness

- Generate runtime tenant overrides for every tenant/datastream pair and enable the appropriate compactor/deletion workers.
- Verify retention keys against the exact pinned versions. Mimir supports per-tenant rather than per-series retention; Loki supports tenant/stream policies; Tempo has tenant compaction overrides.
- Pyroscope storage generation is a compatibility gate: current reference documentation marks `compactor_blocks_retention_period` as v1-storage-only. Select a supported version/storage mode with demonstrable tenant retention; do not apply a v1 option to v2 or claim unsupported behavior. If no supported mode meets the requirement, pause for an explicit architecture decision before implementing a substitute.
- Test policy selection and actual deletion, accounting for compaction, block boundaries, delete delays, versioned objects, and backups. Do not equate a rendered retention value with verified GDPR deletion.
- Provision separate object storage buckets and least-privilege identities for Mimir, Loki, Tempo, and Pyroscope, plus additional rule/alert buckets required by selected versions.
- Backend retention is authoritative. Bucket lifecycle must not delete live data earlier or move active queryable blocks into inaccessible archival tiers. Glacier is for a separate archival/export workflow, not a transparent live-storage optimization.
- Account for S3 noncurrent object versions and backups in deletion policies; keep Terraform state lifecycle separate from telemetry lifecycle.
- AWS state uses a separately bootstrapped S3 backend and DynamoDB locking as requested, with compatible Terraform pins, encryption, protection, and a documented migration to native S3 locking. State resources are never destroyed by ordinary environment teardown.

## Implementation todos

### 1. Establish architecture, contracts, and compatibility

Start repository implementation with `docs/01-architecture.md` and Terraform module skeletons as requested, then complete them component by component.

Resolve and pin a tested compatibility matrix covering Terraform/providers, Ansible collections, OS distributions/architectures, Kubernetes/k3s, Helm charts, images/digests, Kafka/Strimzi, Cilium, Longhorn, MetalLB, Traefik, cert-manager, SOPS, and age. Add provider/chart lockfiles to the repository. Confirm image distribution/licensing and MinIO server/client availability, including a pinned source-build path if required; do not silently substitute another storage product.

> **Amendment:** "MinIO server/client availability" above was resolved by
> replacing MinIO with SeaweedFS, explicitly and not silently; see the Storage
> amendments section of `docs/01-architecture.md`. SOPS and age are no
> longer pinned; see the Vault amendment under "Security and tenant
> boundaries". The Ansible pins were added in todo 5: `ansible-core`, the
> collections, the lint and test tools, the Docker Engine version with its
> repository signing-key fingerprints, and SHA-256 checksums for the k3s
> binary and the Alloy archive. Each operating system entry records the
> evidence it rests on (`container`, `host`, or `declared`); all are
> `container` today. Longhorn, MetalLB, Traefik, cert-manager, and Strimzi
> are pinned in the matrix but nothing installs them yet. Chart lockfiles
> and image digests for Kubernetes are still open; see the recorded
> deferrals at the end of these todos.

Create validated platform/tenant/network schemas, examples, renderer, secret workflow, prerequisites checks, and shared test fixtures. Record supported combinations and migration constraints, including Pyroscope retention and Kafka requirements.

### 2. Implement Terraform infrastructure and state bootstrap

> **Amendment:** The `minio-backend` module named below was superseded before
> implementation. The approved local-storage decision is SeaweedFS on local
> disks/PVCs, owned by Compose initialization and Ansible/Helm orchestration,
> not a Terraform-managed MinIO module; see `docs/01-architecture.md`'s
> Storage amendments section for the full rationale (MinIO Community's
> upstream repository is unmaintained). `minio-backend` was intentionally
> not built as part of implementing this phase.

Add complete modules for `aws-vpc-network`, `aws-eks`, `aws-s3-backends`, and `minio-backend`, each with variables, outputs, validation, and concise explanations of non-obvious decisions.

Add a separate AWS state-bootstrap root plus `environments/aws`, `environments/self-hosted-k8s`, and `environments/docker`. Backend configuration belongs to each executable root; avoid an ineffective top-level-only `backend.tf`.

For Docker and self-hosted nodes, use Ansible for host-local resources. Terraform manages explicitly available outside-cluster resources; no invented provider-dependent firewall/VM resources or fake empty applies. Define MinIO ownership so Terraform and Ansible never compete to manage the same bucket or identity.

> **Amendment:** "MinIO ownership" above reads as SeaweedFS ownership: Compose
> initialization and Ansible/Helm own it, and Terraform does not manage it.

Implement per-component IRSA policies, S3 lifecycle safeguards, security groups derived from the shared network contract, EKS access configuration, EBS encryption, and necessary DNS/controller permissions. Keep infrastructure provisioning separate from Helm installation to avoid provider initialization races against a cluster that does not exist yet.

### 3. Implement collection, gateway, and tenant provisioning

Create Docker, Kubernetes node, Kubernetes cluster, remote-cluster, VM, and external-service Alloy configurations. Split node and cluster discovery to avoid duplicate metrics; include backend self-monitoring, retries/backpressure, bounded queues, relabeling, tenant routing, and OTLP receivers.

Implement and test the shared authenticated gateway policy, certificate lifecycle and credential rotation, runtime tenant overrides, Grafana organization provisioning, and data source correlations across metrics/logs/traces/profiles. Do not enable cross-tenant query federation by default.

### 4. Deliver Docker Compose end to end

Add all runtime configurations, the complete Compose file, S3 override, `.env.example`, readiness-aware startup, persistent volumes, per-component service credentials, and non-root execution where compatible.

Use functional probes that exist in each pinned image; do not assume a distroless image contains curl or a shell. One-shot initialization services report completion and fail visibly. Separate initialization from long-running services.

Add an instrumented sample workload and deterministic telemetry fixtures covering metrics, logs, traces, and profiles. Quickstart generates local secrets/trust and boots a runnable sample tenant/datastream whose explicit example retention values are documented as examples rather than production defaults.

### 5. Implement Ansible host and cluster automation

Implement the requested inventories, roles, and playbooks for Docker installation/deployment, k3s bootstrap/join, external Alloy systemd services, node hardening, and firewall configuration.

Support explicitly tested Ubuntu/Debian and RHEL-family versions for Docker hosts; declare a narrower tested OS matrix for k3s/Longhorn if required. Pin artifacts and verify checksums. Provide handlers, validation, check-mode behavior where supported, `no_log` for secret-bearing tasks, and idempotent secret generation.

Validate SSH/admin allowlists before firewall changes; avoid locking out automation. Model Cilium/MetalLB/Longhorn kernel, disk, MTU, address-pool, and network prerequisites explicitly. Keep inventory secrets out of example files.

> **Amendment: what todo 5 delivered.** The `ansible/` tree, documented in
> `docs/08-ansible.md`.
>
> - **Inventories:** examples for a Docker host, a single-node and a
>   production k3s cluster, and external collector hosts. They hold file
>   locations, never secrets; a repository test enforces that.
> - **Playbooks:** `docker-host.yml`, `docker-teardown.yml`,
>   `k3s-cluster.yml`, `external-collector.yml`.
> - **Roles:** `preflight`, `hardening`, `firewall`, `docker_engine`,
>   `nighthawk_stack`, `k3s_prerequisites`, `k3s_node`, `cilium`,
>   `alloy_collector`.
> - **Supported systems:** Docker and collector hosts on Ubuntu 22.04 and
>   24.04, Debian 12, Rocky Linux 9, and AlmaLinux 9; k3s nodes on Ubuntu
>   only. The preflight stops the whole run, before any change, if one host
>   is not on that list.
> - **Pinned and verified artifacts:** Docker Engine from a repository whose
>   signing key must match a pinned fingerprint; the k3s binary and the
>   Alloy archive by SHA-256. A mismatch installs nothing. k3s is installed
>   without running its upstream install script.
> - **Secrets:** "idempotent secret generation" is done by the command-line
>   tool, not by Ansible. A target host never talks to Vault; every secret
>   reaches it as a copied file in a `no_log` task. The cluster join token
>   is a declared secret, generated once by `generate-cluster-token` and
>   never replaced. The external collector's client certificate is issued
>   and renewed by `issue-certificate --if-needed`.
> - **Firewall:** the allowlists are validated first, including that the
>   playbook's own connection is covered. The change arms a timer that
>   restores the previous rules unless a fresh connection succeeds. On
>   Docker hosts the published entry point is also restricted in the
>   `DOCKER-USER` chain, because Docker bypasses UFW and firewalld.
> - **Cluster prerequisites:** kernel version and modules, storage
>   packages, disks, MTU, address ranges, and the load-balancer pool are
>   checked by `check-cluster-layout`. Of the cluster add-ons only Cilium is
>   installed here; MetalLB, Longhorn, Traefik, and cert-manager are
>   installed with the workloads in todo 6.
> - **Docker deployment:** the Compose stack is deployed to a remote host as
>   a production single node, not highly available. This was previously
>   recorded as deferred.
>
> **Verified:** `ansible-lint` (production profile) and syntax checks are
> clean; every role's scenario passes in systemd containers of each system
> it supports, including a dry run and a second run that changes nothing;
> the end-to-end suite publishes the external entry point locally and shows
> that ingestion there needs a client certificate. **Not verified:** nothing
> has run on a real host. Firewall enforcement, the automatic restore,
> loading kernel settings, starting the Docker daemon or the stack through
> the role, starting k3s, joining nodes, Cilium, and `arm64` were never
> exercised.

### 6. Deliver Kubernetes and EKS workloads

Add exact-version official Grafana chart values for self-hosted and AWS profiles, release orchestration, and the small supporting chart.

Wire MinIO/S3, TLS, gateway policies, tenant runtime configuration, service account roles, resource requests/limits, persistent storage, probes, disruption budgets, topology spread, autoscaling, and NetworkPolicies.

> **Amendment:** "MinIO/S3" above reads as SeaweedFS/S3.

Include Kafka where required, Cluster Autoscaler on AWS, metrics-server for HPA, and HPA only for verified horizontally scalable query/Grafana configurations. Multi-replica Grafana requires shared PostgreSQL and supported unified-alerting HA configuration; provision and document those dependencies rather than scaling SQLite-backed instances.

Document small/medium/large resource budgets including Kafka, object storage, shared database, replication, and disk throughput. Keep production and reduced-footprint development values visibly distinct.

> **Amendment: what todo 6 has delivered so far.** The development profile
> of self-hosted Kubernetes, documented in `docs/09-kubernetes.md`:
>
> - **Rendering:** `render-contracts` writes Helm values, an ordered release
>   list, and what Vault must allow for a cluster.
> - **Orchestration:** Ansible playbooks (`k8s-addons.yml`,
>   `k8s-platform.yml`, `k8s-teardown.yml`) run a pinned, checksum-verified
>   Helm from the control machine. Every upstream chart is locked by the
>   digest of its package, and every image the profile runs is pinned by
>   digest.
> - **Secrets:** the decision deferred from the Vault amendment is made.
>   Workloads get secrets through the Vault Secrets Operator and
>   certificates through cert-manager from Vault's PKI, each as its own
>   service account. No Vault credential is stored in the cluster. The
>   cluster therefore reads Vault, read-only and per workload; the
>   command-line tool remains the only writer.
> - **Workloads:** a small platform chart; SeaweedFS and monolithic Mimir
>   and Tempo from it with the configuration verified under Compose; Loki,
>   Pyroscope, Grafana, and Traefik from their upstream charts. The gateway
>   has a namespace of its own.
> - **Network:** default-deny NetworkPolicies in both platform namespaces,
>   every allowance generated from a rule of the network contract, which now
>   lists the in-cluster flows (62 rules).
> - **Verified:** an acceptance suite installs the profile on a local
>   single-node cluster and checks all four signals, tenant boundaries,
>   network isolation, secret delivery and rotation, and that data survives
>   a teardown.
>
> **Not delivered:** the production profile (distributed charts, Kafka
> through Strimzi, replicated SeaweedFS, shared PostgreSQL, Longhorn,
> disruption budgets, topology spread, autoscaling, resource budgets), and
> everything for AWS/EKS. Nothing has run on the k3s cluster of todo 5.

### 7. Provision dashboards and alerting

Ship cluster health, node health, Mimir/Loki/Tempo self-monitoring, and golden-signals dashboards, with supplemental Alloy, Pyroscope, Kafka, storage, and gateway health coverage.

Provision stable data source UIDs, variables, exemplars/correlations, alerting contact points, policies, recording rules, and multi-window burn-rate alerts backed by real sample workload metrics.

Use a configurable webhook contact point as the baseline. Local tests use a test receiver; production deployment requires a real endpoint and credentials. Include no-data/backend outage handling and an opt-in external dead-man endpoint to make shared-platform failure visibility explicit.

### 8. Complete external integrations and network examples

Ship deployable remote-cluster Alloy node/cluster manifests and VM registration automation, with tenant/datastream credentials and trust material generated through the same workflow.

Include static/file discovery and Consul/DNS discovery examples, supported basic-auth/OAuth2 collector syntax, and a push-versus-pull decision guide. Document NAT-friendly outbound OTLP/remote-write paths and restricted public versus VPN/peering/PrivateLink options.

Provide a small tested MQTT-to-metrics bridge example for constrained devices plus a direct OTLP HTTP example. Clarify that a central bridge performs tenant assignment and that constrained devices do not need deprecated Promtail.

Generate documented UFW/iptables rules, AWS SG/NACL examples, and Kubernetes policy expectations from the network contract, including stateful SG versus stateless NACL return traffic.

> **Amendment:** Two parts of this todo already exist from todo 5. A
> collector can be installed on a virtual machine or next to an external
> service with the `alloy_collector` role, using credentials and
> certificates from the same workflow; what remains is the remote-cluster
> manifests and any registration beyond running the playbook. Host firewall
> rules are generated from the contract and applied by the `firewall` role;
> what remains is the documented, standalone rule examples and the AWS and
> Kubernetes ones.

### 9. Add orchestration, CI, and acceptance coverage

Implement `make deploy-docker`, `make deploy-self-hosted`, `make deploy-aws`, corresponding destroy targets, `make lint`, and `make test`, backed by shared scripts rather than duplicated shell logic.

Fail fast for missing configuration, tools, credentials, unreachable endpoints, or unsupported versions. Preserve data by default on teardown; irreversible purge requires a separate explicit option and never removes state-bootstrap resources.

CI covers formatting/schema checks, Terraform init/validate and provider-mocked tests, Ansible lint/syntax and role tests, Helm lint/template/schema validation, Compose config validation, Alloy validation, dashboard/rule validation, security gates, and generated-artifact drift checks.

Bootstrap missing local validation tools into the authorized isolated environment. Run available static/unit checks through WSL without requiring a daemon. Container-dependent checks require an approved existing runtime or separately approved runtime setup; remote CI execution must not be represented as completed merely because workflow files exist.

Add Docker end-to-end tests, Kubernetes integration tests, and separately authorized self-hosted/AWS acceptance jobs. Lightweight cluster tests do not substitute for multi-node k3s/Longhorn or actual EKS/IRSA verification.

> **Amendment:** None of this todo is started: there is no Makefile and no
> CI workflow. What exists is run by hand: the Python unit suite, the
> provider-mocked Terraform tests, `ansible-lint` and the role scenarios
> (`ansible/molecule/run.sh`), the Compose configuration tests, and the
> Docker end-to-end suite (19 cases). Deployment and teardown are
> `quickstart-docker` and `teardown-docker` locally, and the playbooks for
> other machines; both teardowns keep data unless a purge is confirmed.

### 10. Complete documentation and release evidence

Finish README, `docs/00-quickstart.md`, and all ten numbered documents in the brief. Every guide has prerequisites, concrete commands, expected outcomes, troubleshooting, and rollback/teardown.

Explain maintenance, upgrades, certificate and secret rotation, Kafka recovery, backup/restore, retention/deletion semantics, cardinality/cost limits, monolithic-to-distributed migrations, and non-HA tradeoffs.

Verify commands against implemented targets. The Docker quickstart has an explicit prerequisites/download-speed/resource assumption and a measured cold/warm-start check against the brief's startup target; do not claim the target without measurement.

> **Amendment: recorded deferrals.** A comparison of todos 1 to 6 with the
> repository found items those todos name that are deliberately not built
> or not verified yet. Each is owned by a later todo:
>
> | Item | Named in | Owned by | Why later |
> | --- | --- | --- | --- |
> | Verification of the host automation on real machines | todo 5 | open | Todo 5 pinned the Ansible tooling and tested every role in containers of each supported system; no playbook has run on a real host. See `docs/08-ansible.md`, "Verification limits" |
> | The production profile of self-hosted Kubernetes: distributed backends, Kafka, replicated storage, shared PostgreSQL, Longhorn, availability settings | todo 6 | open | Todo 6 delivered the development profile only; see `docs/09-kubernetes.md` |
> | Image digests for the add-ons only production installs (Longhorn, Strimzi, CloudNativePG) | todo 1 | with the production profile | Every chart is locked by digest, and the images of everything the development profile runs are pinned |
> | IAM for the AWS load balancer controller | todo 2 | todo 6 | Its policy is published per controller release, and none is pinned yet |
> | Standalone firewall-format outputs: documented UFW or iptables rule examples, AWS SG/NACL examples | conventions | todo 8 | Todo 8 owns them. Host rules are generated and applied by the Ansible `firewall` role; outside it only the port table and a JSON copy of the contract are generated |
> | An AWS example platform document | conventions | todo 6 | Nothing consumes one until the EKS workloads exist |
> | Prerequisite checks beyond Vault and Terraform | todo 1 | todo 9 | They belong with the orchestration that needs the other tools |
> | Modelled backups, and an expiry for old state-bucket versions | storage conventions | todo 10 | They belong with backup and restore |
>
> Already recorded elsewhere and unchanged: retention deletion tests.
>
> Done since this list was first recorded: the Ansible tooling pins and the
> evidence behind each operating system entry (todo 5, in
> `config/versions.yaml`), and production single-node Docker Compose, which
> is the Docker host deployment in `docs/08-ansible.md`. What todo 5 could
> not verify without a target machine is the first row above.

## Dependencies and execution order

Establish contracts and compatibility first. Infrastructure, collection/gateway logic, and Ansible host roles can then progress independently against those contracts.

Compose depends on collection/gateway contracts. Kubernetes delivery depends on infrastructure, collection/gateway configuration, and host bootstrap interfaces. Dashboards and external integrations depend on the collection and tenant contract. Acceptance and final documentation depend on all deliverables.

Write architecture first and update documentation alongside each component, rather than postponing all documentation until the end.

## Acceptance criteria and safety boundaries

- All three environments render valid, internally consistent configurations with exact supported version pins and no committed plaintext secrets.
- Terraform validates; authenticated plans are clean for supplied environment inputs. After deployment, a second plan is empty and a second Ansible run reports no unintended changes. Helm release inputs and generated files are deterministic.

  > **Amendment, status after todo 5:** Terraform validates and its
  > provider-mocked tests pass; no authenticated plan has been made. A second
  > Ansible run changes nothing in every role's container test; that is not
  > yet shown on a real host. The criteria below that concern the Docker
  > profile (four signals, two customers with two datastreams each, spoofed
  > headers, revoked certificates, redaction, restarts, credential rotation)
  > are met by the end-to-end suite. Retention deletion, alerting, replica
  > failure, backup and restore, and everything on Kubernetes or AWS are
  > open.
- Smoke fixtures ingest and query all four signals, survive collector/backend restarts as designed, and exercise Grafana correlations.
- Two customer tenants with two datastreams each cannot ingest/query across unauthorized boundaries. Spoofed headers, bad/revoked certificates, and query attempts with write-only credentials fail.
- Different retention values are selected correctly for every signal and pair. Backend-specific deletion tests use supported durations and verify deletion after documented processing windows; long-duration tests are distinct from fast CI.
- PII fixtures are absent from received payloads and queried records; sensitive labels are not retained accidentally.
- Alerts reach the test receiver, real production destinations are configuration-gated, and malformed notification settings fail explicitly.
- Replica failures, storage persistence, allowed/denied network paths, HTTPS/OTLP gRPC routing, credential rotation, and backup/restore are exercised at the appropriate environment tier.
- Security checks fail closed with actionable findings and narrowly scoped, documented, expiring exceptions only.
- No AWS resources, DNS records, remote host changes, billable acceptance runs, or destructive purges occur merely to validate the plan. Implementation requests environment access/authorization when required.
- Report checks that actually ran separately from blocked cloud/host/runtime checks. Static validation is not evidence of a production-tested deployment.

## Research informing this plan

- Mimir retention and lack of per-series retention: https://grafana.com/docs/mimir/latest/configure/configure-metrics-storage-retention/
- Loki compactor retention and object-store lifecycle constraints: https://grafana.com/docs/loki/latest/operations/storage/retention/
- Tempo deployment modes, Kafka requirement in current distributed architecture, and tenant overrides: https://grafana.com/docs/tempo/latest/configuration/
- Pyroscope retention options and storage-generation restrictions: https://grafana.com/docs/pyroscope/latest/configure-server/reference-configuration-parameters/

These references establish architectural constraints, not a tested version matrix. Exact versions and storage-mode compatibility must be resolved in the first implementation milestone.
